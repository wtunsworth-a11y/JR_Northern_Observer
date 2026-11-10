#!/usr/bin/env python3
"""
Package the whole Northern Observer site into ONE self-contained HTML file:
app, issue data and every photo embedded, so it can be emailed, shared or
hosted anywhere without the repo's web address.

    python3 tools/package.py [output.html]      (default: dist/northern-observer.html)

Photos are re-encoded smaller (max 1000px wide) to keep the file light.
"""
import base64
import io
import json
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
MAX_W, QUALITY = 1000, 72


def data_uri(path, max_w=MAX_W):
    im = Image.open(ROOT / path).convert("RGB")
    if im.width > max_w:
        im = im.resize((max_w, round(im.height * max_w / im.width)), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=QUALITY, optimize=True, progressive=True)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def main(out):
    data, assets, cache = {}, {}, {}
    files = ["data/issues.json", "data/credits.json"] + [
        f"data/{i['slug']}.json" for i in json.loads((ROOT / "data/issues.json").read_text())["issues"]
    ]
    for f in files:
        d = json.loads((ROOT / f).read_text())
        data[f] = d

    def embed(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k in ("src", "cover") and isinstance(v, str) and v.startswith("img/"):
                    if v not in cache:
                        cache[v] = data_uri(v, 1200 if "notice" in v else MAX_W)
                    obj[k] = cache[v]
                else:
                    embed(v)
        elif isinstance(obj, list):
            for v in obj:
                embed(v)

    for d in data.values():
        embed(d)
    for f in sorted((ROOT / "img").glob("*/masthead.jpg")):
        rel = f.relative_to(ROOT).as_posix()
        assets[rel] = data_uri(rel, 1200)

    bundle = json.dumps({"data": data, "assets": assets}, ensure_ascii=False, separators=(",", ":"))
    bundle = bundle.replace("</", "<\\/")
    html = (ROOT / "index.html").read_text()
    head = "<!doctype html>\n<html lang=\"en\">\n<head>\n"
    html = head + html.replace("<header>", "</head>\n<body>\n<header>", 1) + "\n</body>\n</html>\n"
    html = html.replace("<script>\n(() => {", f"<script>window.__NO_BUNDLE = {bundle};</script>\n<script>\n(() => {{", 1)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html)
    print(f"{out}: {out.stat().st_size / 1e6:.1f} MB, {len(cache) + len(assets)} images")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else ROOT / "dist" / "northern-observer.html")
