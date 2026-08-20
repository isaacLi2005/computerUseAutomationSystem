"""
Discovery agent: an LLM drives the real browser UI (unfiltered -- real
screenshots, real clicks, nothing hidden from it) to accomplish a goal. Every
action it takes is checked against the candidate list our deterministic
extractor already found (locator.py). If an action can't be matched
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
import re
import sys
from urllib.parse import urlparse

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright
import anthropic

from locator import extract_all_frames, infer_labels, DATA_DIR, VIEWPORT_WIDTH, VIEWPORT_HEIGHT
from introspect import resolve_click_target, resolve_typing_target
from matching import find_live_candidate, describe_candidates, secret_ref_for_label
from browser_actions import click_and_wait
from guardrails import GuardrailError, check_money_guardrail, check_domain, mentions_money_movement
from escalation import run_escalation

load_dotenv()

MODEL = "claude-sonnet-4-5-20250929"
COMPUTER_TOOL_TYPE = "computer_20250124"
COMPUTER_TOOL_BETA = "computer-use-2025-01-24"
MAX_TURNS = 20


class CoverageGapError(Exception):
    """The agent acted on something our deterministic extractor never found.
    Hard stop: an artifact must never contain a step we can't describe for
    replay."""


class CheckpointError(Exception):
    """A declared checkpoint didn't hold. Hard stop, same reasoning as
    CoverageGapError."""


def find_candidate_by_id(results, frame_index, local_candidate_id):
    for r in results:
        if r["frame_index"] == frame_index and r["local_candidate_id"] == local_candidate_id:
            return r
    return None


def slugify_goal(goal):
    """Deterministic capability name from the goal text. Strips quoted
    literals first (e.g. "'john'") so parameters mentioned in the goal
    don't leak into the name, then lowercases and underscore-joins what's
    left."""
    without_quotes = re.sub(r"'[^']*'|\"[^\"]*\"", "", goal)
    words = re.findall(r"[a-zA-Z0-9]+", without_quotes.lower())
    return "_".join(words) or "unnamed_capability"


def describe_step(action, value, label, secret_ref=None):
    """Deterministic, human-readable one-liner for a recorded click/type step
    (see handle_key for why key presses aren't recorded; handle_read and
    handle_wait build their own comments directly, since neither has a
    value/secret_ref in the click/type sense)."""
    if secret_ref:
        return f"Insert (secret: {secret_ref}) into {label}"
    if action == "type":
        return f'Insert "{value}" into {label}'
    return f'Click "{label}"'


def redact_candidate(candidate):
    """A copy of a candidate dict with the session id stripped out of any
    URL field. A session id is a bearer token for an authenticated session --
    leaking it into a debug file is the same class of problem as leaking a
    password."""
    redacted = dict(candidate)
    for field in ("href", "frame_url"):
        if redacted.get(field):
            redacted[field] = re.sub(r";jsessionid=[^?#]*", "", redacted[field])
    return redacted


class DiscoverySession:
    """Holds all the state for one discovery run: the browser page, the
    running conversation with the model, and the list of recorded steps."""

    def __init__(self, page, goal, allowed_domain):
        self.page = page
        self.allowed_domain = allowed_domain
        self.money_actions_authorized = mentions_money_movement(goal)
        self.client = anthropic.Anthropic()
        self.messages = []
        self.recorded_steps = []
        self.debug_steps = []  # full candidate detail, kept out of the main artifact
        self.checkpoints = []
        self.escalations = []
        self.outputs = {}  # output_key -> value, from read_value calls
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

    def _append_step(self, action, comment, target=None, value=None, secret_ref=None, matched_candidate=None, **extra):
        """Common to every recorded step: assigns the next step number,
        appends the step dict (same shape every time, regardless of action,
        so replay never has to guess which keys are present), and -- when a
        real candidate was matched -- stashes its full detail in the debug
        file (see run_discovery). Returns the step number."""
        step_number = len(self.recorded_steps) + 1
        self.recorded_steps.append({
            "step": step_number,
            "comment": comment,
            "action": action,
            "value": value,
            "secret_ref": secret_ref,
            "target": target,
            **extra,
        })
        if matched_candidate is not None:
            self.debug_steps.append({"step": step_number, "matched_candidate": redact_candidate(matched_candidate)})
        return step_number

    def record_step(self, action, frame_index, local_candidate_id, value=None):
        candidate = find_candidate_by_id(self.current_results, frame_index, local_candidate_id)
        label = candidate["inferred_label"]

        # Never persist a password value -- not even redacted to a placeholder,
        # since a placeholder can't be replayed either. Instead store a
        # reference to where the real value lives (an environment variable);
        # replay looks the real value up from there, at replay time, never
        # from the artifact. A click on a password field has no value to
        # protect in the first place (value is None).
        is_secret = value is not None and candidate["type"] == "password"
        secret_ref = secret_ref_for_label(label) if is_secret else None
        stored_value = None if is_secret else value

        self._append_step(
            action, describe_step(action, stored_value, label, secret_ref),
            target={"label": label, "tag": candidate["tag"], "type": candidate["type"]},
            value=stored_value, secret_ref=secret_ref, matched_candidate=candidate,
        )

        detail = f"[secret: {secret_ref}]" if secret_ref else (f" = {stored_value!r}" if stored_value else "")
        print(f"  recorded: {action} on \"{label}\" {detail}".rstrip())

    def handle_read(self, expected_label, output_key, reason):
        """Reads the current value of a piece of page content (tag == "text"),
        the same grounded-observation discipline declare_checkpoint uses: the
        label must be copied exactly from the candidate list just shown, never
        assumed or read visually. Unlike a checkpoint, this also captures the
        value -- into self.outputs (this run's observed value, kept as
        evidence of what the capability actually returned) and into a
        recorded "read" step that stores what to read, not what it read, so
        replay re-reads whatever the live value is then, not this one."""
        live = find_live_candidate(self.current_results, expected_label, tag="text")
        if live is None:
            raise CoverageGapError(
                f"Agent tried to read \"{expected_label}\" ({reason}) but no text "
                f"candidate with that label is in the list it was just shown."
            )
        value = live["own_text"]
        self.outputs[output_key] = value

        self._append_step(
            "read", f'Read "{expected_label}" as {output_key}',
            target={"label": expected_label, "tag": "text", "type": None},
            matched_candidate=live, output_key=output_key,
        )

        print(f'  read: "{expected_label}" = {value!r} (output_key={output_key})')
        return value

    def handle_wait(self, seconds):
        """Records a deliberate wait the agent took (e.g. after submitting a
        form, to let async content finish loading before the next
        screenshot). Recorded the same way any other action is -- so replay
        reproduces the exact same wait, instead of racing content that only
        the LLM noticed wasn't ready yet."""
        self.page.wait_for_timeout(seconds * 1000)
        self._append_step("wait", f"Wait {seconds}s for the page to settle", seconds=seconds)
        print(f"  recorded: wait {seconds}s")

    def resolve_candidate(self, match, error_message):
        """Common to every action: turn a resolve_*_target() match into
        (frame_index, local_candidate_id, candidate), raising CoverageGapError
        with a caller-specific message if there was no match, and checking
        the money guardrail before the caller acts on the candidate."""
        if match is None:
            raise CoverageGapError(error_message)
        frame_index, local_candidate_id = match
        candidate = find_candidate_by_id(self.current_results, frame_index, local_candidate_id)
        check_money_guardrail(candidate["inferred_label"], self.money_actions_authorized)
        return frame_index, local_candidate_id, candidate

    def handle_left_click(self, x, y):
        frame_index, local_candidate_id, _ = self.resolve_candidate(
            resolve_click_target(self.page, x, y),
            f"Agent clicked ({x}, {y}) but no candidate from our deterministic "
            f"extractor covers that point. This is a real detection gap, not a bug "
            f"in the agent -- stopping so it can be investigated.",
        )
        click_and_wait(self.page, x, y)
        check_domain(self.page, self.allowed_domain)
        self.record_step("click", frame_index, local_candidate_id)

    def handle_type(self, text):
        frame_index, local_candidate_id, _ = self.resolve_candidate(
            resolve_typing_target(self.page),
            f"Agent typed {text!r} but the focused element isn't a candidate our "
            f"deterministic extractor found. This is a real detection gap -- "
            f"stopping so it can be investigated.",
        )
        self.page.keyboard.type(text)
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
            "after_step": len(self.recorded_steps),  # 1-indexed, matches steps[i]["step"]
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


def escalate(session, reason):
    """Runs the shared human-escalation loop (escalation.run_escalation) on
    this session's live page, records what happened, and returns the
    outcome ("done" or "skip")."""
    outcome, record = run_escalation(
        session.page, session.refresh_candidates, reason,
        session.money_actions_authorized, session.allowed_domain,
    )
    session.escalations.append(record)
    return outcome


TOOLS = [
    {
        "type": COMPUTER_TOOL_TYPE,
        "name": "computer",
        "display_width_px": VIEWPORT_WIDTH,
        "display_height_px": VIEWPORT_HEIGHT,
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
                    "description": (
                        "ONLY the quoted text from a candidate list line, not the whole "
                        "line. E.g. if the list shows: 5. a (None) -- \"Log Out\", pass "
                        "exactly Log Out -- not \"5. a (None) -- \\\"Log Out\\\"\"."
                    ),
                },
                "reason": {"type": "string", "description": "Why this confirms your intended state, in plain English."},
            },
            "required": ["expected_label", "reason"],
        },
    },
    {
        "name": "read_value",
        "description": (
            "Reads and reports the current value of a piece of page content -- e.g. an "
            "account balance -- so it can be returned to whoever asked for it. Only use "
            "a label copied EXACTLY from a candidate list line whose tag is \"text\" -- "
            "never a value you read visually or remember. E.g. if the list shows: "
            "14. text -- \"Balance\": '$1,234.56', pass exactly Balance as expected_label "
            "-- the actual value is looked up deterministically, not typed in by you."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "expected_label": {
                    "type": "string",
                    "description": "The quoted label from a text-tagged candidate list line, not the whole line.",
                },
                "output_key": {"type": "string", "description": "A short name for this value, e.g. \"balance\"."},
                "reason": {"type": "string", "description": "Why this is the value the goal asked for, in plain English."},
            },
            "required": ["expected_label", "output_key", "reason"],
        },
    },
]


def run_discovery(target_url, goal, out_name="discovery_run.json"):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT})
        page.goto(target_url, wait_until="networkidle")

        allowed_domain = urlparse(target_url).netloc
        session = DiscoverySession(page, goal, allowed_domain)
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
            "If the goal asks you to find out or report a value (e.g. a balance), once "
            "you can see it as a \"text\"-tagged line in the candidate list, call "
            "read_value with that label and a short output_key naming it, and mention "
            "the value in your final report_goal_status notes too. "
            "When the goal is complete, or if you get stuck, call report_goal_status."
        )

        session.messages.append({
            "role": "user",
            "content": [
                {"type": "text", "text": f"Goal: {goal}"},
                *session.observation_blocks(),
            ],
        })

        final_status = None
        failure = None

        try:
            final_status = run_turns(session, system_prompt)
        except (CoverageGapError, CheckpointError, GuardrailError) as e:
            failure = str(e)
            print(f"  DISCOVERY FAILED: {e}")

        if session.outputs:
            print("\n  outputs:")
            for key, value in session.outputs.items():
                print(f"    {key} = {value!r}")

        DATA_DIR.mkdir(exist_ok=True)

        out_path = DATA_DIR / out_name
        with open(out_path, "w") as f:
            json.dump({
                "name": slugify_goal(goal),
                "version": 1,
                "goal": goal,
                "final_status": final_status,
                "failure": failure,
                "steps": session.recorded_steps,
                "checkpoints": session.checkpoints,
                "escalations": session.escalations,
                "outputs": session.outputs,
            }, f, indent=2)
        print(f"\nwrote {out_path}")

        # Locator-heuristic debug detail (see record_step) lives in its own
        # file, paired by name, rather than inside the main artifact.
        debug_path = DATA_DIR / out_name.replace(".json", "_debug.json")
        with open(debug_path, "w") as f:
            json.dump({"steps": session.debug_steps}, f, indent=2)
        print(f"wrote {debug_path}")

        browser.close()


def run_turns(session, system_prompt):
    """Runs the model/tool-execution loop until report_goal_status is called
    or the model stops issuing tool calls. Returns final_status (the
    report_goal_status input), or None if the model never called it.
    CoverageGapError/CheckpointError/GuardrailError propagate up to the caller."""
    final_status = None
    turns_exhausted = True

    for _turn in range(MAX_TURNS):
        response = session.client.beta.messages.create(
            model=MODEL,
            max_tokens=1024,
            system=system_prompt,
            tools=TOOLS,
            betas=[COMPUTER_TOOL_BETA],
            messages=session.messages,
        )
        session.messages.append({"role": "assistant", "content": response.content})

        tool_results = []
        done = False

        for block in response.content:
            if block.type == "text":
                print(f"  agent: {block.text}")

            elif block.type == "tool_use" and block.name == "report_goal_status":
                final_status = block.input
                print(f"  goal status: success={final_status['success']} -- {final_status['notes']}")

                if final_status["success"]:
                    done = True
                else:
                    # The agent says it's stuck -- pause and let a human take
                    # over the same live session rather than just giving up.
                    outcome = escalate(session, final_status["notes"])
                    if outcome == "skip":
                        done = True  # the human couldn't unstick it either
                    else:
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": [
                                {"type": "text", "text": "A human intervened. Here is the "
                                                          "current state -- continue toward the goal."},
                                *session.observation_blocks(),
                            ],
                        })

            elif block.type == "tool_use" and block.name == "read_value":
                value = session.handle_read(block.input["expected_label"], block.input["output_key"], block.input["reason"])
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": f"Recorded. Current value of \"{block.input['expected_label']}\" is {value!r}.",
                })

            elif block.type == "tool_use" and block.name == "declare_checkpoint":
                session.handle_checkpoint(block.input["expected_label"], block.input["reason"])
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": "Checkpoint recorded.",
                })

            elif block.type == "tool_use" and block.name == "computer":
                action = block.input.get("action")
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
                    # Recorded (see handle_wait) so replay waits too.
                    session.handle_wait(block.input.get("duration", 1))
                else:
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": f"Action '{action}' isn't implemented yet in this prototype.",
                        "is_error": True,
                    })
                    continue

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": session.observation_blocks(),
                })

        if done:
            turns_exhausted = False
            break
        if tool_results:
            session.messages.append({"role": "user", "content": tool_results})
        else:
            turns_exhausted = False
            break  # model returned no tool calls at all -- nothing left to do

    if turns_exhausted:
        escalate(session, f"used all {MAX_TURNS} turns without finishing")

    return final_status


if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else "https://parabank.parasoft.com/parabank/index.htm"
    goal_text = sys.argv[2] if len(sys.argv) > 2 else "Log in with username 'john' and password 'demo'"
    run_discovery(url, goal_text)
