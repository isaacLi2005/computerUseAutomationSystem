"""
Resolves what the agent actually interacted with, and checks it against the
candidate list our deterministic extractor already found.

Two entry points:
  - resolve_click_target(page, x, y): "what candidate is at this screen point?"
  - resolve_typing_target(page): "what candidate currently has keyboard focus?"

Both return (frame_index, local_candidate_id) on a match, or None if the
agent touched something our extractor never found -- a genuine coverage gap
in the deterministic side. Matching a raw hit against the candidate list uses
the browser's own `.closest('[data-cua-id]')` -- the standard DOM
"walk up to the nearest matching ancestor" call -- rather than any custom
tree-walking code of ours, since locator.py already tags every
candidate element with a data-cua-id attribute at extraction time.

Frame handling: a click point is given in top-page (screenshot) coordinates.
If it lands on a nested frame's own <iframe>/<frame> element, we step into
that frame (via Playwright's ElementHandle.content_frame()) and repeat the
lookup inside it, converting the point into that frame's local coordinates
along the way. This mirrors the same frame_offset() logic used during
extraction, so a click resolves correctly no matter how deeply nested the
target frame is.
"""

from locator import frame_offset


CLOSEST_CANDIDATE_JS = """
el => {
    const candidate = el.closest('[data-cua-id]');
    return candidate ? candidate.getAttribute('data-cua-id') : null;
}
"""


def _frame_index(page, frame):
    return page.frames.index(frame)


def resolve_click_target(page, global_x, global_y):
    """Finds the candidate at top-page point (global_x, global_y), descending
    into nested frames as needed. Returns (frame_index, local_candidate_id),
    or None if the point hit something outside every known candidate."""
    frame = page.main_frame

    while True:
        offset_x, offset_y = frame_offset(frame)
        local_x = global_x - offset_x
        local_y = global_y - offset_y

        element = frame.evaluate_handle(
            "([x, y]) => document.elementFromPoint(x, y)", [local_x, local_y]
        ).as_element()

        if element is None:
            return None  # nothing rendered at this point at all

        child_frame = element.content_frame()
        if child_frame is not None:
            # The point landed on a nested frame's own element. Step into
            # that frame and repeat the same lookup inside it. The point
            # stays expressed in top-page coordinates -- frame_offset()
            # already gives each frame's position relative to the top page
            # directly, however deep it's nested, so no extra math is needed.
            frame = child_frame
            continue

        candidate_id = element.evaluate(CLOSEST_CANDIDATE_JS)
        if candidate_id is None:
            return None  # hit something real, but not one of our candidates

        return _frame_index(page, frame), int(candidate_id)


def resolve_typing_target(page):
    """Finds the candidate that currently has keyboard focus. Only one frame
    on the whole page can hold focus at a time, so we check each frame's own
    document.activeElement until we find one that isn't just <body>."""
    for frame in page.frames:
        element = frame.evaluate_handle("document.activeElement").as_element()
        if element is None:
            continue

        tag_name = element.evaluate("el => el.tagName")
        if tag_name == "BODY":
            continue  # nothing focused in this particular frame

        candidate_id = element.evaluate(CLOSEST_CANDIDATE_JS)
        if candidate_id is None:
            return None  # something is focused, but it isn't a candidate

        return _frame_index(page, frame), int(candidate_id)

    return None  # no frame has anything focused at all
