"""
Discovery agent: an LLM drives the real browser UI (unfiltered -- real
screenshots, real clicks, nothing hidden from it) to accomplish a goal. Every
action it takes is checked against the candidate list our deterministic
extractor already found (locator_prototype.py). If an action can't be matched
to a known candidate, that's a genuine coverage gap in the deterministic
side, and we stop immediately with a clear error rather than silently
recording a step we could never replay later.

This intentionally does NOT constrain what the agent can click -- it gets a
real screenshot and can act on anything a human could. The candidate list is
only used afterward, to check and record what it actually did.

Run (from prototype/, needs ANTHROPIC_API_KEY in .env):
    .venv/bin/python core/discovery.py
"""

import base64
import json
import sys
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
import anthropic

from locator_prototype import extract_all_frames, infer_labels
from introspect import resolve_click_target, resolve_typing_target
from matching import find_live_candidate

load_dotenv()

MODEL = "claude-sonnet-5"
COMPUTER_TOOL_TYPE = "computer_toolset_20260801"
COMPUTER_TOOL_BETA = "computer-use-2025-01-24"
VIEWPORT_WIDTH = 1280
VIEWPORT_HEIGHT = 900
MAX_TURNS = 20

DATA_DIR = Path(__file__).parent.parent / "data"


class CoverageGapError(Exception):
    """The agent acted on something our deterministic extractor never found.
    Hard stop: an artifact must never contain a step we can't describe for
    replay."""


class CheckpointError(Exception):
    """A declared checkpoint didn't hold. Hard stop, same reasoning as
    CoverageGapError."""


def describe_candidates(results):
    """Turns the extractor's output into a short, readable text block for
    the model -- what our deterministic side can currently see, in plain
    English, one line per candidate."""
    lines = []
    for i, r in enumerate(results):
        label = r["inferred_label"] or "(no label found)"
        lines.append(f"{i}. {r['tag']} ({r['type']}) -- \"{label}\"")
    return "Elements our deterministic detector currently sees on this page:\n" + "\n".join(lines)


def find_candidate(results, frame_index, local_candidate_id):
    for r in results:
        if r["frame_index"] == frame_index and r["local_candidate_id"] == local_candidate_id:
            return r
    return None


class DiscoverySession:
    """Holds all the state for one discovery run: the browser page, the
    running conversation with the model, and the list of recorded steps."""

    def __init__(self, page, goal):
        self.page = page
        self.goal = goal
        self.client = anthropic.Anthropic()
        self.recorded_steps = []
        self.checkpoints = []
        self.current_results = []  # latest extractor output, refreshed each turn

    def refresh_candidates(self):
        elements, texts = extract_all_frames(self.page)
        self.current_results = infer_labels(elements, texts)
        return self.current_results

    def take_screenshot_block(self):
        image_bytes = self.page.screenshot()
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": base64.b64encode(image_bytes).decode("utf-8"),
            },
        }

    def observation_blocks(self):
        """What the model sees after each action: a fresh screenshot, plus a
        fresh candidate-list description (the page may have changed)."""
        results = self.refresh_candidates()
        return [
            self.take_screenshot_block(),
            {"type": "text", "text": describe_candidates(results)},
        ]

    def record_step(self, action, frame_index, local_candidate_id, value=None):
        candidate = find_candidate(self.current_results, frame_index, local_candidate_id)
        self.recorded_steps.append({
            "action": action,
            "value": value,
            "matched_candidate": candidate,
        })
        print(f"  recorded: {action} on \"{candidate['inferred_label']}\""
              + (f" = {value!r}" if value else ""))

    def handle_left_click(self, x, y):
        match = resolve_click_target(self.page, x, y)
        if match is None:
            raise CoverageGapError(
                f"Agent clicked ({x}, {y}) but no candidate from our deterministic "
                f"extractor covers that point. This is a real detection gap, not a bug "
                f"in the agent -- stopping so it can be investigated."
            )
        frame_index, local_candidate_id = match

        # Waits out any navigation the click triggers (e.g. a form submit);
        # a plain click only waits for the event to dispatch. Most clicks
        # don't navigate at all, so timing out here is normal, not an error.
        try:
            with self.page.expect_navigation(timeout=3000):
                self.page.mouse.click(x, y)
        except PlaywrightTimeoutError:
            pass

        self.record_step("click", frame_index, local_candidate_id)

    def handle_type(self, text):
        self.page.keyboard.type(text)
        match = resolve_typing_target(self.page)
        if match is None:
            raise CoverageGapError(
                f"Agent typed {text!r} but the focused element isn't a candidate our "
                f"deterministic extractor found. This is a real detection gap -- "
                f"stopping so it can be investigated."
            )
        frame_index, local_candidate_id = match
        self.record_step("type", frame_index, local_candidate_id, value=text)

    def handle_key(self, key_text):
        # Key presses (Enter, Tab, etc.) aren't tied to a specific click point
        # or a text value the way clicks/typing are, so we don't require a
        # candidate match for them -- just execute and let the model observe
        # the result on the next screenshot.
        self.page.keyboard.press(key_text)
        print(f"  pressed key: {key_text}")

    def handle_checkpoint(self, expected_label, reason):
        """Checks a label the agent says it can currently see against the
        candidate list it was last shown (not re-refreshed -- the label
        should already be sitting in that same list)."""
        found = find_live_candidate(self.current_results, expected_label)
        self.checkpoints.append({
            "after_step_index": len(self.recorded_steps) - 1,
            "expected_label": expected_label,
            "reason": reason,
            "held": found is not None,
        })
        if found is None:
            raise CheckpointError(
                f"Agent named \"{expected_label}\" ({reason}) as something it could see, "
                f"but it isn't in the candidate list it was actually just shown. Stopping "
                f"so this can be investigated rather than continuing on a wrong assumption."
            )
        print(f"  checkpoint held: \"{expected_label}\" ({reason})")


def build_tools():
    return [
        {
            "type": COMPUTER_TOOL_TYPE,
        },
        {
            "name": "report_goal_status",
            "description": "Call this once the goal is complete, or if you are stuck and cannot proceed.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "success": {"type": "boolean"},
                    "notes": {"type": "string", "description": "What happened, in plain English."},
                },
                "required": ["success", "notes"],
            },
        },
        {
            "name": "declare_checkpoint",
            "description": (
                "Confirms you're in the expected state before relying on it for your next "
                "step. Only call this using a label copied EXACTLY from the candidate list "
                "you were just given in your last observation -- never a label you expect, "
                "assume, or remember from a similar page. If you can't find something in "
                "the current list that confirms you're where you intended to be, do not "
                "call this tool -- that itself is useful information."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "expected_label": {
                        "type": "string",
                        "description": "A label copied verbatim from the candidate list you were just shown.",
                    },
                    "reason": {"type": "string", "description": "Why this confirms your intended state, in plain English."},
                },
                "required": ["expected_label", "reason"],
            },
        },
    ]


def run_discovery(target_url, goal, out_name="discovery_run.json"):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT})
        page.goto(target_url, wait_until="networkidle")

        session = DiscoverySession(page, goal)
        system_prompt = (
            f"You are operating a real web browser to accomplish this goal: {goal}\n\n"
            "Use the computer tool to look at the screen and click/type as needed, "
            "exactly as a human would. Alongside each screenshot you will also be told "
            "what our own element detector currently sees on the page, for reference. "
            "After an action that should change the page (e.g. submitting a form), look "
            "at the candidate list you're given back and, if it confirms you reached the "
            "state you intended, call declare_checkpoint with a label copied exactly from "
            "that list -- this must describe what you actually observe, never what you "
            "expect or assume before looking. "
            "When the goal is complete, or if you get stuck, call report_goal_status."
        )

        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": f"Goal: {goal}"},
                *session.observation_blocks(),
            ],
        }]

        final_status = None

        for _turn in range(MAX_TURNS):
            response = session.client.beta.messages.create(
                model=MODEL,
                max_tokens=1024,
                system=system_prompt,
                tools=build_tools(),
                betas=[COMPUTER_TOOL_BETA],
                messages=messages,
            )
            messages.append({"role": "assistant", "content": response.content})

            tool_results = []
            done = False

            for block in response.content:
                if block.type == "text":
                    print(f"  agent: {block.text}")

                elif block.type == "tool_use" and block.name == "report_goal_status":
                    final_status = block.input
                    done = True
                    print(f"  goal status: success={final_status['success']} -- {final_status['notes']}")

                elif block.type == "tool_use" and block.name == "declare_checkpoint":
                    session.handle_checkpoint(block.input["expected_label"], block.input["reason"])
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": "Checkpoint recorded.",
                    })

                elif block.type == "tool_use" and getattr(block, "toolset_name", None) == "computer":
                    # The computer_toolset_20260801 tool type hands back a separate
                    # tool_use per action (block.name IS the action -- "left_click",
                    # "type", "key", "screenshot", ...), unlike the older single
                    # "computer" tool that used one shared name plus an "action" field.
                    action = block.name
                    print(f"  action: {action} {block.input}")

                    if action == "screenshot":
                        pass  # observation_blocks() below already refreshes the view
                    elif action == "left_click":
                        x, y = block.input["coordinate"]
                        session.handle_left_click(x, y)
                    elif action == "type":
                        session.handle_type(block.input["text"])
                    elif action == "key":
                        session.handle_key(block.input["text"])
                    elif action == "wait":
                        # Used after an action that triggers a page navigation
                        # (e.g. submitting a login form) so the next screenshot
                        # reflects the new page instead of a half-loaded one.
                        seconds = block.input.get("duration", 1)
                        page.wait_for_timeout(seconds * 1000)
                    else:
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "toolset_name": "computer",
                            "content": f"Action '{action}' isn't implemented yet in this prototype.",
                            "is_error": True,
                        })
                        continue

                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "toolset_name": "computer",
                        "content": session.observation_blocks(),
                    })

            if done:
                break
            if tool_results:
                messages.append({"role": "user", "content": tool_results})
            else:
                break  # model returned no tool calls at all -- nothing left to do

        DATA_DIR.mkdir(exist_ok=True)
        out_path = DATA_DIR / out_name
        with open(out_path, "w") as f:
            json.dump({
                "goal": goal,
                "final_status": final_status,
                "steps": session.recorded_steps,
                "checkpoints": session.checkpoints,
            }, f, indent=2)
        print(f"\nwrote {out_path}")

        browser.close()


if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else "https://parabank.parasoft.com/parabank/index.htm"
    goal_text = sys.argv[2] if len(sys.argv) > 2 else "Log in with username 'john' and password 'demo'"
    run_discovery(url, goal_text)
