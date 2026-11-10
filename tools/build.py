#!/usr/bin/env python3
"""
Northern Observer — issue builder.

Turns a print PDF of the Northern Observer into the article-by-article data the
web app reads. Each issue has a small config file in tools/issues/ that says
where each article sits on the page (text boxes and photo boxes, in PDF points,
origin top-left) plus the article's headline, section, places and topics.

    python3 tools/build.py              # build every issue in tools/issues/
    python3 tools/build.py issue_02     # build one issue

Outputs (all relative to the repo root):
    data/issues.json                    index of all issues (newest first)
    data/issue-NN.json                  one issue: articles, blocks, photos
    img/issue-NN/*.jpg                  cropped photos, cover thumbnail

Requires: pdfplumber, pypdfium2, pillow.
"""
import importlib.util
import json
import re
import statistics
import sys
from pathlib import Path

import pdfplumber
import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "tools" / "issues"

RENDER_SCALE = 2.5          # 180 dpi page renders for photo crops
MAX_IMG_WIDTH = 1400
JPEG_QUALITY = 80

BODY_MIN, BODY_MAX = 9.2, 12.5    # body copy is 10pt
CAPTION_MIN, CAPTION_MAX = 7.5, 9.1


# --------------------------------------------------------------------------- #
# Text extraction
# --------------------------------------------------------------------------- #
def font(c):
    return c["fontname"].split("+")[-1]


def is_bold(c):
    return "Bold" in font(c)


def is_dropcap(c):
    return "Harrington" in font(c) or c["size"] > 20


def is_bullet(c):
    f = font(c)
    return ("Wingdings" in f and "3" not in f) or c["text"] in "●•■"


def keep_body(c):
    if c["text"].isspace():
        return BODY_MIN <= c["size"] <= BODY_MAX
    f = font(c)
    if "Wingdings3" in f:          # caption arrow glyph
        return False
    if "Harrington" in f:          # decorative drop cap
        return True
    if is_bullet(c) and c["size"] >= 8:
        return True
    return BODY_MIN <= c["size"] <= BODY_MAX


def in_box(c, box):
    x0, top, x1, bottom = box
    cx = (c["x0"] + c["x1"]) / 2
    cy = (c["top"] + c["bottom"]) / 2
    return x0 <= cx <= x1 and top <= cy <= bottom


def split_columns(chars, box, min_gap=5.0):
    """Find column gutters: vertical strips with no body text at all."""
    x0, _, x1, _ = box
    res = 0.5
    n = int((x1 - x0) / res) + 2
    cover = [False] * n
    for c in chars:
        if is_dropcap(c) or c["text"].isspace():
            continue
        a = max(0, int((c["x0"] - x0) / res))
        b = min(n - 1, int((c["x1"] - x0) / res))
        for i in range(a, b + 1):
            cover[i] = True
    cols, start, gap = [], None, 0
    for i, v in enumerate(cover):
        if v:
            if start is None:
                start = i
            gap = 0
        elif start is not None:
            gap += 1
            if gap * res >= min_gap:
                cols.append((x0 + start * res, x0 + (i - gap + 1) * res))
                start, gap = None, 0
    if start is not None:
        cols.append((x0 + start * res, x0 + (n - gap) * res))
    return cols


def group_lines(chars):
    """Group characters into lines (by baseline), left-to-right."""
    lines = []
    for c in sorted(chars, key=lambda c: (round(c["bottom"]), c["x0"])):
        if is_dropcap(c):
            continue
        for ln in lines:
            if abs(ln["bottom"] - c["bottom"]) < 2.0:
                ln["chars"].append(c)
                break
        else:
            lines.append({"bottom": c["bottom"], "chars": [c]})
    out = []
    for ln in lines:
        cs = sorted(ln["chars"], key=lambda c: c["x0"])
        while cs and cs[0]["text"].isspace():
            cs.pop(0)
        while cs and cs[-1]["text"].isspace():
            cs.pop()
        if not cs:
            continue
        out.append({
            "chars": cs,
            "x0": cs[0]["x0"],
            "x1": cs[-1]["x1"],
            "top": min(c["top"] for c in cs),
            "bottom": ln["bottom"],
        })
    out.sort(key=lambda l: l["top"])
    return out


def line_runs(chars):
    """Return [(text, bold)] runs for a line, inserting spaces at gaps."""
    runs = []
    prev = None
    pending_space = False
    for c in chars:
        if c["text"].isspace():
            pending_space = True
            continue
        t = c["text"]
        if prev is not None:
            gap = c["x0"] - prev["x1"]
            if pending_space or gap > c["size"] * 0.15:
                t = " " + t
        pending_space = False
        b = is_bold(c) and not is_bullet(c)
        if runs and runs[-1][1] == b:
            runs[-1][0] += t
        else:
            runs.append([t, b])
        prev = c
    return runs


BULLET_RE = re.compile(r"^(\d{1,3}\.)\s+")
SKIP_RE = re.compile(r"(CONTINUED ON PAGE|FROM PAGE)\s*\d*", re.I)


def extract_text(pdf, regions):
    """Extract paragraphs from one or more (page, box) regions, in order."""
    paras = []          # {"runs": [[text,bold]...], "kind": str}
    cur = None

    def flush():
        nonlocal cur
        if cur and "".join(r[0] for r in cur["runs"]).strip():
            paras.append(cur)
        cur = None

    for page_no, box in regions:
        page = pdf.pages[page_no - 1]
        all_chars = [c for c in page.chars if in_box(c, box) and keep_body(c)]
        dropcaps = [c for c in all_chars if is_dropcap(c) and not c["text"].isspace()]
        for x0, x1 in split_columns(all_chars, box):
            col = [c for c in all_chars if x0 - 1 <= (c["x0"] + c["x1"]) / 2 <= x1 + 1]
            lines = group_lines(col)
            if not lines:
                continue
            base = statistics.mode(round(l["x0"]) for l in lines)
            col_dcs = [d for d in dropcaps if x0 - 20 <= d["x0"] <= x0 + 6]
            prev = None
            dc_until = -1
            for ln in lines:
                runs = line_runs(ln["chars"])
                text = "".join(r[0] for r in runs).strip()
                if not text:
                    continue
                bare = re.sub(r"^(n\s+|[^\w“]+)", "", text) if is_bullet(ln["chars"][0]) else text
                if SKIP_RE.match(bare) and len(bare) < 24:
                    continue
                first = ln["chars"][0]
                dc = next((d for d in col_dcs if abs(d["bottom"] - ln["bottom"]) < 26 and d["top"] < ln["top"] + 2), None)
                in_dc = ln["top"] < dc_until
                if dc:
                    dc_until = dc["bottom"] + 1
                    col_dcs.remove(dc)
                    runs[0][0] = dc["text"] + runs[0][0].lstrip()
                bullet = is_bullet(first)
                numbered = BULLET_RE.match(text)
                all_bold = all(r[1] for r in runs if r[0].strip())
                indent = ln["x0"] - base
                new = False
                if cur is None or dc or bullet or numbered:
                    new = True
                elif prev is not None and ln["top"] - prev["top"] > 17:
                    new = True               # blank line
                elif in_dc:
                    new = False              # lines beside the drop cap
                elif cur["kind"] in ("li", "ol") and abs(ln["x0"] - cur["hang"]) < 1.2:
                    new = False              # hanging indent of a list item
                elif indent > 4:
                    new = True               # first-line indent
                elif cur["bold_open"] and not runs[0][1]:
                    new = cur["all_bold"]    # end of a bold standfirst/subhead
                elif runs[0][1] and not cur["runs"][-1][1]:
                    new = True               # run-in bold subhead
                if new:
                    flush()
                    kind = "li" if bullet else ("ol" if numbered else "p")
                    cur = {"runs": [], "kind": kind, "all_bold": True, "bold_open": False, "hang": -99}
                    if bullet or numbered:
                        cs = [c for c in ln["chars"] if not c["text"].isspace()]
                        if bullet:
                            cur["hang"] = cs[1]["x0"] if len(cs) > 1 else ln["x0"]
                        else:
                            k = next((i for i, c in enumerate(cs) if c["text"] == "."), 0)
                            cur["hang"] = cs[k + 1]["x0"] if k + 1 < len(cs) else ln["x0"]
                    if bullet:
                        runs = [[r[0], r[1]] for r in runs]
                        runs[0][0] = runs[0][0].lstrip()[1:].lstrip()
                        if not runs[0][0]:
                            runs = runs[1:]
                # join with previous line
                if cur["runs"]:
                    last = cur["runs"][-1]
                    if last[0].endswith("-") and not last[0].endswith(" -"):
                        pass
                    else:
                        runs[0][0] = " " + runs[0][0].lstrip()
                for r in runs:
                    if cur["runs"] and cur["runs"][-1][1] == r[1]:
                        cur["runs"][-1][0] += r[0]
                    else:
                        cur["runs"].append([r[0], r[1]])
                cur["all_bold"] = cur["all_bold"] and all_bold
                cur["bold_open"] = runs[-1][1]
                prev = ln
    flush()
    return paras


def to_blocks(paras):
    blocks = []
    for i, p in enumerate(paras):
        runs = [[re.sub(r"\s+", " ", t), b] for t, b in p["runs"]]
        plain = "".join(t for t, _ in runs).strip()
        plain = plain.replace("“ ", "“").replace(" ”", "”")
        if p["all_bold"] and i == 0 and len(plain) > 60:
            blocks.append({"type": "lede", "text": plain})
        elif p["all_bold"] and len(plain) < 70 and not plain.endswith((".", ",")) and p["kind"] == "p":
            blocks.append({"type": "h", "text": plain.rstrip(":")})
        else:
            md = ""
            for t, b in runs:
                if b and t.strip():
                    lead = t[: len(t) - len(t.lstrip())]
                    trail = t[len(t.rstrip()):]
                    md += f"{lead}**{t.strip()}**{trail}"
                else:
                    md += t
            md = re.sub(r"\s+", " ", md).strip()
            md = md.replace("“ ", "“").replace(" ”", "”")
            kind = {"li": "li", "ol": "ol"}.get(p["kind"], "p")
            if kind == "ol":
                md = BULLET_RE.sub("", md)
            blocks.append({"type": kind, "text": md})
    return blocks


# --------------------------------------------------------------------------- #
# Captions
# --------------------------------------------------------------------------- #
def find_caption(page, box, band=48):
    x0, _, x1, bottom = box
    small = [c for c in page.chars if CAPTION_MIN <= c["size"] <= CAPTION_MAX or "Wingdings3" in font(c)]
    arrows = [c for c in small if "Wingdings3" in font(c) and bottom - 4 <= c["top"] <= bottom + 22]
    mine = [a for a in arrows if x0 - 4 <= a["x0"] <= x1 - 10]
    if not mine:
        return None
    a = min(mine, key=lambda c: c["top"])
    all_arrows = [c for c in small if "Wingdings3" in font(c)]
    others = [o["x0"] for o in all_arrows if o["x0"] > a["x0"] + 5 and abs(o["top"] - a["top"]) < 30]
    right = min(others) - 2 if others else 10_000
    cand = [c for c in small if a["x0"] - 1 <= c["x0"] < right and a["top"] - 2 <= c["top"] <= a["top"] + band
            and "Wingdings3" not in font(c)]
    lines = group_lines(cand)
    text, prev = [], None
    for ln in lines:
        if prev is not None and ln["top"] - prev["top"] > 14:
            break
        text.append("".join(r[0] for r in line_runs(ln["chars"])).strip())
        prev = ln
    cap = re.sub(r"\s+", " ", " ".join(text)).strip()
    if not cap or re.fullmatch(r"(caption\s*)+", cap, re.I):
        return None
    return cap


def find_label(page, box, band=60):
    """Name-card style captions (no arrow): small text directly under a photo."""
    x0, _, x1, bottom = box
    cand = [c for c in page.chars if CAPTION_MIN <= c["size"] <= CAPTION_MAX
            and x0 - 2 <= c["x0"] <= x1 + 22 and bottom - 2 <= c["top"] <= bottom + band]
    lines = group_lines(cand)
    return [ "".join(r[0] for r in line_runs(l["chars"])).strip() for l in lines ]


# --------------------------------------------------------------------------- #
# Images
# --------------------------------------------------------------------------- #
class Renderer:
    def __init__(self, path):
        self.doc = pdfium.PdfDocument(str(path))
        self.cache = {}

    def page_image(self, n, scale=RENDER_SCALE):
        key = (n, scale)
        if key not in self.cache:
            self.cache = {k: v for k, v in self.cache.items() if k[0] == n}
            self.cache[key] = self.doc[n - 1].render(scale=scale).to_pil().convert("RGB")
        return self.cache[key]

    def crop(self, n, box, scale=RENDER_SCALE):
        im = self.page_image(n, scale)
        x0, top, x1, bottom = box
        return im.crop((int(x0 * scale), int(top * scale), int(x1 * scale), int(bottom * scale)))

    def raw(self, n, box):
        """The embedded photo itself (no overprinted headline), best match by position."""
        page = self.doc[n - 1]
        h = page.get_height()
        best, best_d = None, 1e9
        for obj in page.get_objects(filter=[pdfium_c.FPDF_PAGEOBJ_IMAGE], max_depth=5):
            l, b, r, t = obj.get_bounds() if hasattr(obj, "get_bounds") else obj.get_pos()
            d = abs(l - box[0]) + abs((h - t) - box[1]) + abs(r - box[2]) + abs((h - b) - box[3])
            if d < best_d:
                best, best_d = obj, d
        if best is None or best_d > 120:
            return self.crop(n, box)
        try:
            return best.get_bitmap(render=False).to_pil().convert("RGB")
        except Exception:
            return self.crop(n, box)


def save_jpeg(im, path, max_w=MAX_IMG_WIDTH):
    if im.width > max_w:
        im = im.resize((max_w, int(im.height * max_w / im.width)))
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path, "JPEG", quality=JPEG_QUALITY, optimize=True, progressive=True)
    return im.width, im.height


# --------------------------------------------------------------------------- #
# Build
# --------------------------------------------------------------------------- #
def load_config(name):
    path = CONFIG_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.ISSUE


def build_issue(cfg):
    num = cfg["number"]
    slug = f"issue-{num:02d}"
    src = ROOT / cfg["pdf"]
    pdf = pdfplumber.open(str(src))
    rnd = Renderer(src)
    img_dir = ROOT / "img" / slug
    for old in img_dir.glob("*.jpg"):
        old.unlink()

    def photo(art_id, idx, spec):
        spec = dict(spec) if isinstance(spec, dict) else {"box": spec}
        page_no = spec.get("page", spec.get("_page"))
        box = spec["box"]
        im = rnd.raw(page_no, box) if spec.get("raw") else rnd.crop(page_no, box, spec.get("scale", RENDER_SCALE))
        name = f"{art_id}-{idx}.jpg"
        w, h = save_jpeg(im, img_dir / name, spec.get("max_w", MAX_IMG_WIDTH))
        cap = spec.get("caption", "auto")
        if cap == "auto":
            cap = find_caption(pdf.pages[page_no - 1], box)
        elif cap == "label":
            cap = " · ".join(find_label(pdf.pages[page_no - 1], box)) or None
        out = {"src": f"img/{slug}/{name}", "w": w, "h": h}
        if cap:
            out["caption"] = cap
        return out

    articles = []
    for order, a in enumerate(cfg["articles"]):
        aid = a["id"]
        regions = [(r[0], r[1:]) for r in a.get("text", [])]
        blocks = to_blocks(extract_text(pdf, regions)) if regions else []
        for fix_from, fix_to in a.get("fixes", []):
            for b in blocks:
                b["text"] = b["text"].replace(fix_from, fix_to)
        split = []
        for b in blocks:
            if "¶" in b.get("text", ""):
                for seg in b["text"].split("¶"):
                    seg = seg.strip()
                    if seg.startswith("#"):
                        split.append({"type": "h", "text": seg.lstrip("# ")})
                    elif seg:
                        split.append({**b, "text": seg})
            else:
                split.append(b)
        blocks = split
        for i, b in enumerate(blocks):
            if b["type"] == "h" and b["text"].startswith("Hon.") and i >= len(blocks) - 3:
                rest = [x["text"] for x in blocks[i + 1:]]
                blocks = blocks[:i] + [{"type": "sign", "text": b["text"], "role": " ".join(rest)}]
                break
        if a.get("intro"):
            blocks = [{"type": "p", "text": t} for t in a["intro"]] + blocks
        images = []
        k = 0
        for spec in a.get("images", []):
            k += 1
            images.append(photo(aid, k, spec))
        for g in a.get("galleries", []):
            pics = []
            for spec in g["images"]:
                k += 1
                pics.append(photo(aid, k, spec))
            blocks.append({"type": "gallery", "title": g.get("title"), "images": pics,
                           **({"text": g["text"]} if g.get("text") else {})})
        lede = next((b["text"] for b in blocks if b["type"] == "lede"), None)
        if not lede:
            lede = next((re.sub(r"\*\*", "", b["text"]) for b in blocks if b["type"] == "p"), "")
        words = sum(len(b.get("text", "").split()) for b in blocks if b["type"] != "gallery")
        art = {
            "id": aid,
            "order": order,
            "title": a["title"],
            "subtitle": a.get("subtitle"),
            "section": a["section"],
            "pages": sorted({r[0] for r in regions} | {s.get("page") for s in a.get("images", []) if isinstance(s, dict)} - {None}) or a.get("pages", []),
            "byline": a.get("byline"),
            "kind": a.get("kind", "story"),
            "featured": a.get("featured", False),
            "summary": (lede[:220].rsplit(" ", 1)[0] + "…") if len(lede) > 220 else lede,
            "minutes": max(1, round(words / 200)),
            "locations": a.get("locations", []),
            "topics": a.get("topics", []),
            "images": images,
            "blocks": blocks,
        }
        articles.append({k: v for k, v in art.items() if v not in (None, [], "")} | {"featured": art["featured"]})

    # cover thumbnail
    cover = rnd.crop(1, (0, 0, pdf.pages[0].width, pdf.pages[0].height), 1.0)
    trim = cfg.get("trim")
    if trim:
        cover = rnd.crop(1, trim, 1.0)
    save_jpeg(cover, img_dir / "cover.jpg", 600)
    if cfg.get("masthead"):
        save_jpeg(rnd.crop(1, cfg["masthead"], 3), img_dir / "masthead.jpg", 1200)


    issue = {
        "number": num,
        "slug": slug,
        "title": cfg["title"],
        "date": cfg["date"],
        "status": cfg.get("status", "published"),
        "theme": cfg.get("theme", {}),
        "sections": list(dict.fromkeys(a["section"] for a in articles)),
        "cover": f"img/{slug}/cover.jpg",
        "reviewNotes": cfg.get("review_notes", []),
        "credits": cfg.get("credits", {}),
        "articles": articles,
    }
    (ROOT / "data").mkdir(exist_ok=True)
    (ROOT / "data" / f"{slug}.json").write_text(json.dumps(issue, ensure_ascii=False, indent=1))
    print(f"{slug}: {len(articles)} articles, {sum(len(a.get('images', [])) for a in articles)} lead photos")
    return issue


def write_index():
    issues = []
    for f in sorted((ROOT / "data").glob("issue-*.json")):
        d = json.loads(f.read_text())
        lead = next((a for a in d["articles"] if a.get("featured")), d["articles"][0])
        issues.append({
            "number": d["number"], "slug": d["slug"], "title": d["title"], "date": d["date"], "status": d.get("status", "published"),
            "cover": d["cover"], "theme": d.get("theme", {}),
            "lead": {"id": lead["id"], "title": lead["title"]},
            "articleCount": len(d["articles"]),
        })
    issues.sort(key=lambda i: i["number"], reverse=True)
    (ROOT / "data" / "issues.json").write_text(json.dumps({"issues": issues}, ensure_ascii=False, indent=1))


def main(argv):
    names = argv or sorted(p.stem for p in CONFIG_DIR.glob("issue_*.py"))
    for n in names:
        build_issue(load_config(n))
    write_index()


if __name__ == "__main__":
    main(sys.argv[1:])
