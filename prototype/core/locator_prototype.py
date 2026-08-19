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

Frame-aware: a page's visible content can be split across several separate
documents (classic <frameset>/<frame>, or <iframe>) with no single DOM tree
spanning all of it. We run the same extraction inside every attached frame and
translate each frame's local rects into top-page coordinates using that frame's
own bounding box (Playwright/CDP composites this correctly across nesting
depth, so no manual offset math is needed). Matching (score_candidate) is
entirely unaware that frames exist -- it just sees already-consistent global
rects.

Run (from prototype/): .venv/bin/python core/locator_prototype.py [URL] [output_filename]
"""

import json
import sys
from dataclasses import dataclass
from pathlib import Path

from playwright.sync_api import sync_playwright

DEFAULT_TARGET_URL = "https://parabank.parasoft.com/parabank/index.htm"
DATA_DIR = Path(__file__).parent.parent / "data"

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
  const controlNodes = document.querySelectorAll('input, textarea, select, button, a[href]');
  for (const el of controlNodes) {
    if (el.type === 'hidden') continue;
    if (!isVisible(el)) continue;
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;

    // Tag the live element with its position in this list. This lets later code
    // ask the browser "which candidate (if any) does this DOM node belong to" with
    // a single built-in call -- el.closest('[data-cua-id]') -- instead of having to
    // compare element identity across separate evaluate() calls, which Playwright
    // has no simple way to do.
    const localId = elements.length;
    el.setAttribute('data-cua-id', String(localId));

    elements.push({
      tag: el.tagName.toLowerCase(),
      type: el.type || null,
      id: el.id || null,
      name: el.name || null,
      href: el.tagName === 'A' ? el.getAttribute('href') : null,
      own_text: (el.innerText || el.value || '').trim().slice(0, 80),
      local_candidate_id: localId,
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
            "frame_index": el["frame_index"],
            "local_candidate_id": el["local_candidate_id"],
            "rect": el["rect"],
            "href": el.get("href"),
            "frame_url": el.get("frame_url"),
            "own_text": el["own_text"],
            "inferred_label": best["text"] if best else None,
            "label_score": best["score"] if best else None,
            "label_direction": best["direction"] if best else None,
            "top_candidates": top,
        })

    return results


def frame_offset(frame):
    """Top-page-relative (x, y) origin of `frame`'s content, or None if the
    frame is detached/not currently rendered (caller should skip it)."""
    if frame.parent_frame is None:
        return 0.0, 0.0
    element = frame.frame_element()
    box = element.bounding_box()
    if box is None:
        return None
    return box["x"], box["y"]


def extract_all_frames(page):
    """Runs EXTRACT_JS in every attached frame (main + nested) and merges the
    results into one global element/text list, each rect translated into
    top-page coordinates. A frame that fails (detached, no document yet,
    navigated away mid-extraction) is skipped, not fatal."""
    all_elements, all_texts = [], []
    for frame_index, frame in enumerate(page.frames):
        offset = frame_offset(frame)
        if offset is None:
            print(f"  (skipped detached frame: {frame.url})")
            continue
        ox, oy = offset
        try:
            data = frame.evaluate(EXTRACT_JS)
        except Exception as e:
            print(f"  (skipped frame {frame.url!r}: {e})")
            continue
        for el in data["elements"]:
            el["rect"]["x"] += ox
            el["rect"]["y"] += oy
            el["frame_url"] = frame.url
            # frame_index is this element's position in page.frames -- lets later
            # code jump straight back to the exact frame it came from.
            el["frame_index"] = frame_index
            all_elements.append(el)
        for t in data["texts"]:
            t["rect"]["x"] += ox
            t["rect"]["y"] += oy
            all_texts.append(t)
    return all_elements, all_texts


def main():
    target_url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TARGET_URL
    out_name = sys.argv[2] if len(sys.argv) > 2 else "output.json"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto(target_url, wait_until="networkidle")

        elements, texts = extract_all_frames(page)
        print(f"extracted {len(elements)} interactive elements, "
              f"{len(texts)} text runs across {len(page.frames)} frame(s)", flush=True)

        results = infer_labels(elements, texts)

        DATA_DIR.mkdir(exist_ok=True)
        out_path = DATA_DIR / out_name
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
