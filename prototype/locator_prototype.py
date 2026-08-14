"""
Prototype 1: text + geometry locator/labeling checker.

Feasibility test for the deterministic-replay locator strategy, decoupled from the
LLM agent loop. Deliberately avoids any DOM-tree relationship signal (parent/child/
sibling, id/class matching, <label for>) — the only inputs are:
  - each interactive control's own rendered bounding box + own type,
  - every selectable text run's rendered bounding box,
  - the geometric relationship between the two.

Rationale: a UI usable by a human must render controls and nearby explanatory text
at real pixel positions (Gestalt law of proximity) — that's the only signal a human
operator is guaranteed to have, regardless of markup quality. DOM structure is an
implementation choice a legacy app may not have made cleanly; geometry isn't.

Run: .venv/bin/python locator_prototype.py
"""

import json
from dataclasses import dataclass, asdict

from playwright.sync_api import sync_playwright

TARGET_URL = "https://parabank.parasoft.com/parabank/index.htm"

# Direction weights: lower = more trusted. Left-of and above are the conventional
# label positions in both table-based legacy forms (label | input) and stacked
# modern forms (label above input); right-of and below are rarer but real
# (e.g. a checkbox's label sitting to its right); pure diagonal/no-alignment is
# the least trustworthy and heavily penalized.
DIRECTION_WEIGHT = {
    "left": 1.0,
    "above": 1.2,
    "right": 2.5,
    "below": 3.0,
    "diagonal": 5.0,
}

MAX_SCORE = 800  # beyond this, treat as "no label found" rather than force a bad match

EXTRACT_JS = """
() => {
  function isVisible(el) {
    const style = getComputedStyle(el);
    return style.display !== 'none' && style.visibility !== 'hidden' && style.opacity !== '0';
  }

  const elements = [];
  const controlNodes = document.querySelectorAll('input, textarea, select, button');
  for (const el of controlNodes) {
    if (el.type === 'hidden') continue;
    if (!isVisible(el)) continue;
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    elements.push({
      tag: el.tagName.toLowerCase(),
      type: el.type || null,
      id: el.id || null,
      name: el.name || null,
      own_text: (el.innerText || el.value || '').trim().slice(0, 80),
      rect: { x: r.x, y: r.y, width: r.width, height: r.height },
    });
  }

  const texts = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null);
  let node;
  while ((node = walker.nextNode())) {
    const value = node.nodeValue.trim();
    if (!value) continue;
    const parent = node.parentElement;
    if (!parent) continue;
    const tag = parent.tagName;
    if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'NOSCRIPT' || tag === 'TITLE') continue;
    if (!isVisible(parent)) continue;
    const range = document.createRange();
    range.selectNodeContents(node);
    const r = range.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    texts.push({
      text: value.slice(0, 80),
      rect: { x: r.x, y: r.y, width: r.width, height: r.height },
    });
  }

  return { elements, texts };
}
"""


@dataclass
class Rect:
    x: float
    y: float
    width: float
    height: float

    @property
    def left(self):
        return self.x

    @property
    def right(self):
        return self.x + self.width

    @property
    def top(self):
        return self.y

    @property
    def bottom(self):
        return self.y + self.height

    @property
    def center(self):
        return (self.x + self.width / 2, self.y + self.height / 2)


def overlap_y(a: Rect, b: Rect) -> float:
    return min(a.bottom, b.bottom) - max(a.top, b.top)


def overlap_x(a: Rect, b: Rect) -> float:
    return min(a.right, b.right) - max(a.left, b.left)


def score_candidate(elem: Rect, text: Rect):
    """Returns (score, direction) for how well `text` labels `elem`, geometry-only."""
    dx_left = elem.left - text.right     # >0 if text sits to the left of elem
    dx_right = text.left - elem.right    # >0 if text sits to the right of elem
    dy_above = elem.top - text.bottom    # >0 if text sits above elem
    dy_below = text.top - elem.bottom    # >0 if text sits below elem

    if overlap_y(elem, text) > 0 and dx_left >= 0:
        return dx_left * DIRECTION_WEIGHT["left"], "left"
    if overlap_y(elem, text) > 0 and dx_right >= 0:
        return dx_right * DIRECTION_WEIGHT["right"], "right"
    if overlap_x(elem, text) > 0 and dy_above >= 0:
        return dy_above * DIRECTION_WEIGHT["above"], "above"
    if overlap_x(elem, text) > 0 and dy_below >= 0:
        return dy_below * DIRECTION_WEIGHT["below"], "below"

    ex, ey = elem.center
    tx, ty = text.center
    euclidean = ((ex - tx) ** 2 + (ey - ty) ** 2) ** 0.5
    return euclidean * DIRECTION_WEIGHT["diagonal"], "diagonal"


def infer_labels(elements, texts):
    text_rects = [(t["text"], Rect(**t["rect"])) for t in texts]
    results = []

    for el in elements:
        elem_rect = Rect(**el["rect"])
        scored = []

        # Native form controls (input[type=submit/button], <button>) render their
        # own value/innerText through widget chrome, not as a DOM text node, so
        # the TreeWalker scan below can never see it. When present, it fully
        # overlaps the control itself and is the strongest possible signal --
        # stronger than any external text -- so seed it at distance 0.
        if el["own_text"]:
            scored.append({"text": el["own_text"], "score": 0.0, "direction": "own_text"})

        for text_value, text_rect in text_rects:
            score, direction = score_candidate(elem_rect, text_rect)
            scored.append({
                "text": text_value,
                "score": round(score, 1),
                "direction": direction,
            })
        scored.sort(key=lambda c: c["score"])
        top = scored[:3]

        best = top[0] if top and top[0]["score"] <= MAX_SCORE else None

        results.append({
            "tag": el["tag"],
            "type": el["type"],
            "id": el["id"],
            "name": el["name"],
            "own_text": el["own_text"],
            "inferred_label": best["text"] if best else None,
            "label_score": best["score"] if best else None,
            "label_direction": best["direction"] if best else None,
            "top_candidates": top,
        })

    return results


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto(TARGET_URL, wait_until="networkidle")

        data = page.evaluate(EXTRACT_JS)
        print(f"extracted {len(data['elements'])} interactive elements, "
              f"{len(data['texts'])} text runs", flush=True)

        results = infer_labels(data["elements"], data["texts"])

        out_path = "output.json"
        with open(out_path, "w") as f:
            json.dump(results, f, indent=2)

        print(f"wrote {out_path}\n")
        for r in results:
            label = r["inferred_label"] or "<NO LABEL FOUND>"
            print(f"  [{r['tag']:8s} type={str(r['type']):10s}] "
                  f"id={str(r['id']):20s} -> \"{label}\" "
                  f"(score={r['label_score']}, dir={r['label_direction']})")

        browser.close()


if __name__ == "__main__":
    main()
