"""
Human-in-the-loop escalation: when discovery.py's agent reports it's stuck
(or runs out of turns), or replay.py hits a condition it can't recover from,
we pause on the SAME live session -- nothing torn down or reloaded -- and
let a human act on it directly, using the same numbered candidate list the
agent already sees. The escalation screenshot itself is labeled with those
same index numbers (see _label_clickable_candidates), so a human can match
"5" to the actual button/link/input by eye instead of cross-referencing the
printed list.

The full loop lives here, shared by both callers -- not just the stateless
pieces. Discovery's normal handlers need live introspection
(resolve_click_target) to figure out what a raw agent coordinate landed on;
replay clicks a known candidate's center directly, since it already has the
candidate. Escalation only ever needs the latter: a human always picks a
candidate by its known index, never a raw coordinate, so both callers share
this same direct-click execution model.
"""

import uuid

from PIL import Image, ImageDraw, ImageFont

from locator import DATA_DIR, rect_center
from matching import describe_candidates
from guardrails import check_money_guardrail, check_domain
from browser_actions import click_and_wait

# A short id generated once per process, not per escalation -- multiple
# escalations within the same run still count 1, 2, 3... (readable), but two
# separate discovery/replay runs never collide and overwrite each other's
# screenshots, which a plain per-process counter starting back at 0 would do.
_run_id = uuid.uuid4().hex[:8]
_next_screenshot_id = 0


def _label_clickable_candidates(screenshot_path, results):
    """Draws each candidate's index number right next to where it actually
    is on the page, so a human doesn't have to cross-reference the printed
    candidate list against the image by eye. Only for tag != "text" --
    those are read-only values (see read_value/handle_read), and escalation's
    index-based commands ("5" to click, "9 demo" to type) don't do anything
    meaningful with them, so labeling them here would be misleading."""
    image = Image.open(screenshot_path)
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=16)

    for i, r in enumerate(results):
        if r["tag"] == "text":
            continue
        rect = r["rect"]
        text = str(i)
        # Sit just above the element, like a callout, so the number doesn't
        # cover whatever's actually inside it; clamp to the image if the
        # element is already flush with the top.
        label_y = rect["y"] - 18 if rect["y"] - 18 >= 0 else rect["y"]
        bbox = draw.textbbox((rect["x"], label_y), text, font=font)
        draw.rectangle(bbox, fill="red")
        draw.text((rect["x"], label_y), text, fill="white", font=font)

    image.save(screenshot_path)


def capture_context(page, results):
    """Screenshots the live page (labeled with clickable candidates' index
    numbers -- see _label_clickable_candidates) and writes the full
    candidate list out to its own paired text file, rather than dumping
    dozens of lines straight to the terminal where they'd bury the actual
    prompt. Returns (screenshot_path, candidates_path)."""
    global _next_screenshot_id
    DATA_DIR.mkdir(exist_ok=True)
    _next_screenshot_id += 1
    screenshot_path = DATA_DIR / f"escalation_{_run_id}_{_next_screenshot_id}.png"
    candidates_path = DATA_DIR / f"escalation_{_run_id}_{_next_screenshot_id}_candidates.txt"

    page.screenshot(path=str(screenshot_path))
    _label_clickable_candidates(screenshot_path, results)
    candidates_path.write_text(describe_candidates(results))

    return screenshot_path, candidates_path


def run_escalation(page, refresh_candidates, reason, money_actions_authorized, allowed_domain):
    """Pauses on the live page and lets a human act on it directly, using
    the same numbered candidate list the agent sees, until they type "done"
    (resolved, caller should resume) or "skip" (couldn't fix it either,
    caller should treat this as a hard failure). `refresh_candidates` is a
    zero-argument callable returning a fresh candidate list -- both callers
    already have one (DiscoverySession.refresh_candidates / replay.py's
    get_fresh_candidates), so escalation doesn't need its own copy of that
    extraction logic.

    Returns (outcome, record) where outcome is "done" or "skip" and record
    is a dict to append to the artifact's escalations list."""
    results = refresh_candidates()
    screenshot_path, candidates_path = capture_context(page, results)
    print(f"\n  === ESCALATION: {reason} ===")
    print(f"  {len(results)} candidates currently on the page -- full list: {candidates_path}")
    print(
        "\n  How to respond -- one command per line. Nothing touches the page until\n"
        "  you type \"done\": each line just queues an action against the candidate\n"
        "  list as it looks right now, so you can type a whole multi-step fix (e.g.\n"
        "  username, then password, then submit) without anything executing between\n"
        "  lines. Caveat: if an early queued action would itself change the page,\n"
        "  later indices in the same queue may no longer point at what you expect --\n"
        "  queue across a page change at your own risk.\n"
        f"  A screenshot (numbered to match the candidate list) is at {screenshot_path}.\n"
        "    <index>            queue a click, e.g. \"5\"\n"
        "    <index> <value>    queue a click, then type that value, e.g. \"9 demo\"\n"
        "    done               run everything queued so far, then resume\n"
        "    skip               give up without running anything -- fail this run"
    )

    queued = []  # [(index, value), ...] -- nothing executes until "done"
    outcome = "done"

    while True:
        answer = input(f'\n  > queue action #{len(queued) + 1} ("5" to click, "9 demo" to type, '
                        f'"done" to run the queue, or "skip"): ')
        parsed = parse_human_action(answer)
        if parsed is None:
            print("  didn't understand that -- try again")
            continue

        kind, payload = parsed
        if kind in ("done", "skip"):
            outcome = kind
            break

        index, value = payload
        if index >= len(results):
            print(f"  no candidate {index} -- there are only {len(results)}")
            continue
        queued.append((index, value))
        label = results[index]["inferred_label"]
        print(f"  queued: {'type into' if value is not None else 'click'} \"{label}\" "
              f"({len(queued)} queued so far, nothing run yet)")

    human_actions = []
    if outcome == "done":
        for index, value in queued:
            candidate = results[index]
            label = candidate["inferred_label"]
            check_money_guardrail(label, money_actions_authorized)

            x, y = rect_center(candidate["rect"])
            click_and_wait(page, x, y)
            if value is not None:
                page.keyboard.type(value)
            check_domain(page, allowed_domain)

            human_actions.append({"action": "type" if value is not None else "click", "label": label, "value": value})
            print(f"  human {'typed into' if value is not None else 'clicked'} \"{label}\"")

        if queued:
            fresh = refresh_candidates()
            candidates_path.write_text(describe_candidates(fresh))
            print(f"  {len(fresh)} candidates now on the page -- updated list: {candidates_path}")

    record = {
        "reason": reason,
        "screenshot": str(screenshot_path),
        "human_actions": human_actions,
        "outcome": outcome,
    }
    return outcome, record


def parse_human_action(answer):
    """Parses one line the human typed at the escalation prompt.
    Returns:
      ("done", None) -- the human is finished, resume normally.
      ("skip", None) -- the human couldn't resolve it either, give up.
      ("act", (index, value)) -- act on candidate `index`; `value` is the
          text to type, or None for a plain click.
      None -- didn't parse as any of the above; ask again.
    """
    answer = answer.strip()
    if answer.lower() == "done":
        return "done", None
    if answer.lower() in ("skip", "give up", "fail"):
        return "skip", None

    parts = answer.split(maxsplit=1)
    if not parts or not parts[0].isdigit():
        return None
    index = int(parts[0])
    value = parts[1] if len(parts) > 1 else None
    return "act", (index, value)
