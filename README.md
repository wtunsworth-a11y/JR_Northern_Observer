# Northern Observer — digital edition

An article-by-article reader for the *Northern Observer*, the monthly newsletter of the
Oro Provincial Government. It is a static web page: no server code and no build step
for the page itself.

- **Issues**: an index of every issue, each with a contents page grouped by section
  (Law and Order, Health, Governance, Infrastructure…) in print order.
- **Articles**: headline, standfirst, body text, captions and photos taken from the print
  PDF, with previous/next links through the issue.
- **Places and Topics**: every article is tagged with the places it covers and its topics,
  and both indexes build up across issues.
- **Search** across all issues, plus a **Credits** page.
- Issues still in review show an **editor's review checklist** on their contents page.

## Layout

```
index.html               the app (HTML, CSS and JS in one file)
data/issues.json         list of issues (generated)
data/issue-NN.json       one issue's articles (generated)
data/credits.json        credits page content (edit by hand)
img/issue-NN/            photos, cover and masthead (generated)
source/                  the print PDFs
tools/build.py           PDF → data + photos
tools/issues/issue_NN.py one config per issue: where each article sits on the page
```

## Adding an issue

1. Put the print PDF in `source/` as `northern-observer-issue-NN.pdf`.
2. Copy `tools/issues/issue_02.py` to `issue_NN.py` and list the articles. For each one,
   give the headline, section, text boxes, photo boxes, places and topics. Boxes are
   `(x0, top, x1, bottom)` in PDF points from the top-left corner. Columns inside a text
   box are found automatically.
3. Run `python3 tools/build.py` (needs `pdfplumber`, `pypdfium2` and `pillow`).
4. Open `index.html` through any static web server, e.g. `python3 -m http.server`.

Set `"status": "review"` while an issue is in final review and list open items in
`review_notes`; switch to `"published"` once it is printed.

Useful per-photo options: `"raw": True` uses the original photo when a headline is printed
over it; `"caption": "…"` overrides the caption found under the photo, and `None` drops it.
`"fixes"` on an article corrects text after extraction.
