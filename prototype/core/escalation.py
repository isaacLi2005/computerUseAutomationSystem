"""
Human-in-the-loop escalation: when discovery.py's agent reports it's stuck
(or runs out of turns), or replay.py hits a condition it can't recover from,
we pause on the SAME live session -- nothing torn down or reloaded -- and
let a human act on it directly, using the same numbered candidate list the
agent already sees.

The full loop lives here, shared by both callers -- not just the stateless
pieces. It looked at first like discovery's and replay's action execution
differ too much to share (discovery's normal handlers do live introspection
to figure out what a raw agent coordinate landed on; replay just clicks a
known candidate's center directly). But during escalation specifically, the
human always picks a candidate by its known index -- no introspection is
ever needed -- so the execution here is the same direct-click model replay
already uses, in both contexts. Nothing left to duplicate.
"""

from locator_prototype import DATA_DIR
from matching import describe_candidates
from guardrails import check_money_guardrail, check_domain
from browser_actions import click_and_wait

_next_screenshot_id = 0


def capture_context(page, results, reason):
    """Screenshots the live page and formats the candidate list + reason
    into readable text. Returns (screenshot_path, description_text)."""
    global _next_screenshot_id
    DATA_DIR.mkdir(exist_ok=True)
    _next_screenshot_id += 1
    screenshot_path = DATA_DIR / f"escalation_{_next_screenshot_id}.png"
    page.screenshot(path=str(screenshot_path))

    description = f"Stopped: {reason}\n\n{describe_candidates(results)}"
    return screenshot_path, description


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
    screenshot_path, description = capture_context(page, results, reason)
    print(f"\n  === ESCALATION: {reason} ===")
    print(f"  screenshot: {screenshot_path}")
    print(f"  {description}")

    human_actions = []
    outcome = "done"

    while True:
        answer = input('\n  > human action ("5" to click, "9 demo" to type, "done", or "skip"): ')
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
        candidate = results[index]
        label = candidate["inferred_label"]
        check_money_guardrail(label, money_actions_authorized)

        rect = candidate["rect"]
        x, y = rect["x"] + rect["width"] / 2, rect["y"] + rect["height"] / 2
        click_and_wait(page, x, y)
        if value is not None:
            page.keyboard.type(value)
        check_domain(page, allowed_domain)

        human_actions.append({"action": "type" if value is not None else "click", "label": label, "value": value})
        print(f"  human {'typed into' if value is not None else 'clicked'} \"{label}\"")

        results = refresh_candidates()
        print(f"  {describe_candidates(results)}")

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
