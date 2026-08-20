"""
Replay engine: re-runs a recorded discovery_run.json against a fresh page
load, with no LLM involved. Each step's target is re-located by label via
matching.find_live_candidate rather than reusing discovery-time coordinates,
since a fresh page load won't be pixel-identical. Anything that can't be
re-located pauses for human escalation (same live session, same mechanism
discovery.py uses) rather than failing outright -- only becomes a real
failure if the human can't fix it either. "wait" steps are the one exception:
they have no target to re-locate, and just reproduce a deliberate pause the
agent took during discovery (e.g. letting async content finish loading).

Run (from prototype/):
    .venv/bin/python core/replay.py [artifact_path] [target_url]
"""

import json
import os
import sys
from urllib.parse import urlparse

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

from locator import extract_all_frames, infer_labels, DATA_DIR, VIEWPORT_WIDTH, VIEWPORT_HEIGHT
from matching import find_live_candidate
from browser_actions import click_and_wait
from guardrails import mentions_money_movement
from escalation import run_escalation

load_dotenv()


class ReplayError(Exception):
    """A step or checkpoint couldn't be re-located on replay, and escalating
    to a human didn't resolve it either."""


def get_fresh_candidates(page):
    elements, texts = extract_all_frames(page)
    return infer_labels(elements, texts)


def click_candidate(page, candidate):
    rect = candidate["rect"]
    x = rect["x"] + rect["width"] / 2
    y = rect["y"] + rect["height"] / 2
    click_and_wait(page, x, y)


def resolve_secret_value(secret_ref):
    value = os.environ.get(secret_ref)
    if value is None:
        raise ReplayError(
            f"needs secret {secret_ref}, but it isn't set in the environment. "
            f"Add it to .env (e.g. {secret_ref}=your_value_here) and try again."
        )
    return value


def replay_step(page, step):
    if step["action"] == "wait":
        # No target to re-locate -- just reproduce the same deliberate wait
        # the agent took during discovery (see handle_wait), so replay
        # doesn't race async content the same way it needed to be waited for.
        page.wait_for_timeout(step["seconds"] * 1000)
        print(f"  replayed: {step['comment']}")
        return None

    label = step["target"]["label"]
    tag = step["target"]["tag"]
    type_ = step["target"]["type"]

    candidates = get_fresh_candidates(page)
    live = find_live_candidate(candidates, label, tag=tag, type_=type_)
    if live is None:
        raise ReplayError(
            f"step {step['step']} ({step['action']} on \"{label}\"): not found on "
            f"replay -- expected a {tag} labeled \"{label}\"."
        )

    read_value = None
    if step["action"] == "click":
        click_candidate(page, live)
    elif step["action"] == "type":
        value = resolve_secret_value(step["secret_ref"]) if step.get("secret_ref") else step["value"]
        click_candidate(page, live)  # focus it first, don't assume it already has focus
        page.keyboard.type(value)
    elif step["action"] == "read":
        # Re-read the live value now, not whatever discovery saw -- that's
        # the point of a read step (e.g. a balance is expected to change
        # between runs; only the label identifying it should stay stable).
        read_value = live["own_text"]
        print(f"  read: \"{label}\" = {read_value!r}")
    else:
        raise ReplayError(f"step {step['step']}: unknown action {step['action']!r}")

    print(f"  replayed: {step['comment']}")
    return read_value


def verify_checkpoint(page, checkpoint):
    candidates = get_fresh_candidates(page)
    live = find_live_candidate(candidates, checkpoint["expected_label"])
    if live is None:
        raise ReplayError(
            f"checkpoint failed: expected to still see \"{checkpoint['expected_label']}\" "
            f"({checkpoint['reason']}) but it's not there."
        )
    print(f"  checkpoint verified: \"{checkpoint['expected_label']}\"")


def replay(artifact_path, target_url):
    with open(artifact_path) as f:
        artifact = json.load(f)

    checkpoints_by_step = {c["after_step"]: c for c in artifact["checkpoints"]}
    steps = artifact["steps"]
    money_actions_authorized = mentions_money_movement(artifact["goal"])
    allowed_domain = urlparse(target_url).netloc

    result = {
        "goal": artifact["goal"],
        "status": "success",
        "steps_completed": 0,
        "total_steps": len(steps),
        "failure": None,
        "escalations": [],
        "outputs": {},
    }

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT})
        page.goto(target_url, wait_until="networkidle")

        step_index = 0
        while step_index < len(steps):
            step = steps[step_index]
            try:
                value = replay_step(page, step)
                if step["action"] == "read":
                    result["outputs"][step["output_key"]] = value
                result["steps_completed"] = step["step"]

                checkpoint = checkpoints_by_step.get(step["step"])
                if checkpoint is not None:
                    verify_checkpoint(page, checkpoint)

                step_index += 1

            except ReplayError as e:
                outcome, record = run_escalation(
                    page, lambda: get_fresh_candidates(page), str(e),
                    money_actions_authorized, allowed_domain,
                )
                result["escalations"].append(record)
                if outcome == "skip":
                    result["status"] = "failure"
                    result["failure"] = str(e)
                    print(f"  REPLAY FAILED: {e}")
                    break
                step_index += 1  # human resolved it -- move past the failed step

        browser.close()

    if result["outputs"]:
        print("\n  outputs:")
        for key, value in result["outputs"].items():
            print(f"    {key} = {value!r}")

    DATA_DIR.mkdir(exist_ok=True)
    out_path = DATA_DIR / "replay_run.json"
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2)
    print(f"\nwrote {out_path}")
    return result


if __name__ == "__main__":
    artifact_file = sys.argv[1] if len(sys.argv) > 1 else str(DATA_DIR / "discovery_run.json")
    url = sys.argv[2] if len(sys.argv) > 2 else "https://parabank.parasoft.com/parabank/index.htm"
    replay(artifact_file, url)
