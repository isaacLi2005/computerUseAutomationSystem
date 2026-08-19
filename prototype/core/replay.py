"""
Replay engine: re-runs a recorded discovery_run.json against a fresh page
load, with no LLM involved. Each step's target is re-located by label via
matching.find_live_candidate rather than reusing discovery-time coordinates,
since a fresh page load won't be pixel-identical. Anything that can't be
re-located is a clean, reported failure -- never a guess.

Run (from prototype/):
    .venv/bin/python core/replay.py [artifact_path] [target_url]
"""

import json
import sys

from playwright.sync_api import sync_playwright

from locator_prototype import extract_all_frames, infer_labels, DATA_DIR, VIEWPORT_WIDTH, VIEWPORT_HEIGHT
from matching import find_live_candidate
from browser_actions import click_and_wait


class ReplayError(Exception):
    """A step or checkpoint couldn't be re-located on replay."""


def get_fresh_candidates(page):
    elements, texts = extract_all_frames(page)
    return infer_labels(elements, texts)


def click_candidate(page, candidate):
    rect = candidate["rect"]
    x = rect["x"] + rect["width"] / 2
    y = rect["y"] + rect["height"] / 2
    click_and_wait(page, x, y)


def replay_step(page, step):
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

    if step["action"] == "click":
        click_candidate(page, live)
    elif step["action"] == "type":
        click_candidate(page, live)  # focus it first, don't assume it already has focus
        page.keyboard.type(step["value"])
    else:
        raise ReplayError(f"step {step['step']}: unknown action {step['action']!r}")

    print(f"  replayed: {step['comment']}")


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

    result = {
        "goal": artifact["goal"],
        "status": "success",
        "steps_completed": 0,
        "total_steps": len(steps),
        "failure": None,
    }

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT})
        page.goto(target_url, wait_until="networkidle")

        try:
            for step in steps:
                replay_step(page, step)
                result["steps_completed"] = step["step"]

                checkpoint = checkpoints_by_step.get(step["step"])
                if checkpoint is not None:
                    verify_checkpoint(page, checkpoint)

        except ReplayError as e:
            result["status"] = "failure"
            result["failure"] = str(e)
            print(f"  REPLAY FAILED: {e}")

        browser.close()

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
