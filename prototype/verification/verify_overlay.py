"""Independent visual verification: screenshot the live page and draw the
element boxes + inferred label boxes from output.json on top, so the
geometric claim can be eyeballed against reality rather than trusted blind."""

import json
from pathlib import Path

from playwright.sync_api import sync_playwright
from PIL import Image, ImageDraw

TARGET_URL = "https://parabank.parasoft.com/parabank/index.htm"
HERE = Path(__file__).parent
DATA_DIR = HERE.parent / "data"

with open(DATA_DIR / "output.json") as f:
    results = json.load(f)

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    page.goto(TARGET_URL, wait_until="networkidle")
    page.screenshot(path=str(HERE / "verify_raw.png"), full_page=True)

    # re-extract to get exact rects tied to this same run (same selector as locator.py)
    data = page.evaluate("""
    () => {
      const out = [];
      document.querySelectorAll('input, textarea, select, button, a[href]').forEach(el => {
        if (el.type === 'hidden') return;
        const r = el.getBoundingClientRect();
        if (r.width === 0 || r.height === 0) return;
        out.push({tag: el.tagName.toLowerCase(), type: el.type,
                   rect: {x:r.x,y:r.y,width:r.width,height:r.height}});
      });
      return out;
    }
    """)
    browser.close()

img = Image.open(HERE / "verify_raw.png")
draw = ImageDraw.Draw(img)

LINK_COLOR = "dodgerblue"
CONTROL_COLOR = "red"

items = []
for el, r in zip(results, data):
    rect = r["rect"]
    box = (rect["x"], rect["y"], rect["x"] + rect["width"], rect["y"] + rect["height"])
    items.append({"el": el, "box": box, "is_link": el["tag"] == "a"})

# Draw boxes + labels for everything first.
for item in items:
    box, el, is_link = item["box"], item["el"], item["is_link"]
    color = LINK_COLOR if is_link else CONTROL_COLOR
    draw.rectangle(box, outline=color, width=2)
    label = el["inferred_label"] or "NONE"
    draw.text((box[0], box[1] - 12), label, fill=color)

# Draw hrefs below the box, but only where there's vertical room before the
# next element's row starts -- avoids smearing overlapping text across the
# page's dense two-column nav clusters. Sort by top y to find that gap.
by_y = sorted(items, key=lambda it: it["box"][1])
n_href_drawn = 0
for i, item in enumerate(by_y):
    href = item["el"].get("href") if item["is_link"] else None
    if not href:
        continue
    box = item["box"]
    next_top = by_y[i + 1]["box"][1] if i + 1 < len(by_y) else float("inf")
    room = (next_top - 12) - box[3]
    if room >= 12:
        draw.text((box[0], box[3] + 1), href, fill="gray")
        n_href_drawn += 1

n_links = sum(1 for it in items if it["is_link"])
img.save(HERE / "verify_overlay.png")
print(f"saved verify_overlay.png ({len(items)} elements: {n_links} links, "
      f"{len(items) - n_links} form controls; href text drawn for "
      f"{n_href_drawn}/{n_links} links -- the rest were skipped to avoid "
      f"overlapping a denser cluster)")
