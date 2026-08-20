"""
Repeatable regression suite for the whole prototype -- locator, frame
handling, matching, guardrails, escalation, introspection, and replay. Run
any time to confirm nothing has broken.

Run (from prototype/):
    .venv/bin/python verification/run_all_tests.py

Everything here is fast and free (no API key, no LLM calls) except the one
clearly-marked live discovery test, which only runs if ANTHROPIC_API_KEY is
set (and costs a small amount of real API usage) -- pass --skip-llm to skip
it even if a key is present.

Exits 0 if everything passed, 1 if anything failed.
"""

import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "core"))

from playwright.sync_api import sync_playwright

import locator as lp
import matching
import guardrails
import escalation
import introspect
import replay as replay_module

PARABANK_URL = "https://parabank.parasoft.com/parabank/index.htm"
NESTED_FRAMES_URL = "https://the-internet.herokuapp.com/nested_frames"
DATA_DIR = Path(__file__).parent.parent / "data"

results = []  # (name, passed, detail)


def check(name, passed, detail=""):
    results.append((name, passed, detail))
    status = "PASS" if passed else "FAIL"
    print(f"  [{status}] {name}" + (f" -- {detail}" if detail and not passed else ""))


# ---------------------------------------------------------------- locator --

def test_locator_parabank(page):
    print("\nlocator: ParaBank login page")
    page.goto(PARABANK_URL, wait_until="networkidle")
    elements, texts = lp.extract_all_frames(page)
    found = lp.infer_labels(elements, texts)
    check("finds a reasonable number of elements", len(found) >= 30, f"got {len(found)}")
    labels = {r["inferred_label"] for r in found}
    expected = {"Username", "Password", "Log In"}
    check("finds Username/Password/Log In", expected <= labels, f"missing {expected - labels}")


def test_locator_text_candidates(page):
    print("\nlocator: text (read) candidates")
    page.goto(PARABANK_URL, wait_until="networkidle")
    elements, texts = lp.extract_all_frames(page)
    found = lp.infer_labels(elements, texts)
    text_candidates = [r for r in found if r["tag"] == "text"]
    check("finds a reasonable number of text candidates", len(text_candidates) >= 10, f"got {len(text_candidates)}")
    check(
        "some text candidates get an inferred label",
        any(r["inferred_label"] for r in text_candidates),
    )


def test_locator_nested_frames(page):
    print("\nlocator: nested_frames (frame-aware extraction)")
    page.goto(NESTED_FRAMES_URL, wait_until="networkidle")
    elements, texts = lp.extract_all_frames(page)
    check("sees all 6 frames", len(page.frames) == 6, f"got {len(page.frames)}")
    text_values = {t["text"] for t in texts}
    expected = {"LEFT", "MIDDLE", "RIGHT", "BOTTOM"}
    check("finds text from every frame", expected <= text_values, f"missing {expected - text_values}")
    check("finds no interactive elements (page has none)", len(elements) == 0, f"got {len(elements)}")


# --------------------------------------------------------------- matching --

def test_matching():
    print("\nmatching")
    fresh = [
        {"inferred_label": "Log In", "own_text": "Log In", "tag": "input", "type": "submit"},
        {"inferred_label": "Username", "own_text": "", "tag": "input", "type": "text"},
        {"inferred_label": "Balance", "own_text": "$1,234.56", "tag": "text", "type": None},
    ]
    check("exact label match found", matching.find_live_candidate(fresh, "Username") is not None)
    check(
        "text-tagged (read) candidate matches by label",
        matching.find_live_candidate(fresh, "Balance", tag="text") is not None,
    )
    check("no match returns None", matching.find_live_candidate(fresh, "Nonexistent") is None)
    check(
        "tag/type narrowing rejects a wrong-type match",
        matching.find_live_candidate(fresh, "Username", tag="input", type_="password") is None,
    )
    check(
        "secret_ref_for_label is deterministic",
        matching.secret_ref_for_label("Password") == "SECRET_PASSWORD",
    )


# -------------------------------------------------------------- guardrails --

def test_guardrails():
    print("\nguardrails")

    with patch("builtins.input", side_effect=AssertionError("must not prompt")):
        try:
            guardrails.check_money_guardrail("Transfer Funds", False)
            check("unauthorized money action is blocked", False)
        except guardrails.GuardrailError:
            check("unauthorized money action is blocked", True)

    with patch("builtins.input", return_value="y"):
        try:
            guardrails.check_money_guardrail("Transfer Funds", True)
            check("authorized + human approves -> allowed", True)
        except guardrails.GuardrailError:
            check("authorized + human approves -> allowed", False)

    with patch("builtins.input", return_value="n"):
        try:
            guardrails.check_money_guardrail("Transfer Funds", True)
            check("authorized + human declines -> blocked", False)
        except guardrails.GuardrailError:
            check("authorized + human declines -> blocked", True)

    with patch("builtins.input", side_effect=AssertionError("must not prompt")):
        try:
            guardrails.check_money_guardrail("Log Out", False)
            check("non-money label never prompts", True)
        except guardrails.GuardrailError:
            check("non-money label never prompts", False)


def test_domain_guardrail(page):
    print("\ndomain guardrail")
    page.goto(PARABANK_URL, wait_until="networkidle")
    try:
        guardrails.check_domain(page, "parabank.parasoft.com")
        check("on-domain page passes", True)
    except guardrails.GuardrailError:
        check("on-domain page passes", False)

    page.goto("https://www.parasoft.com/", wait_until="networkidle")
    try:
        guardrails.check_domain(page, "parabank.parasoft.com")
        check("off-domain page is blocked", False)
    except guardrails.GuardrailError:
        check("off-domain page is blocked", True)


# -------------------------------------------------------------- escalation --

def test_escalation_parsing():
    print("\nescalation: human-input parsing")
    check("parses 'done'", escalation.parse_human_action("done") == ("done", None))
    check("parses 'skip'", escalation.parse_human_action("skip") == ("skip", None))
    check("parses a plain index (click)", escalation.parse_human_action("5") == ("act", (5, None)))
    check("parses index + value (type)", escalation.parse_human_action("9 demo") == ("act", (9, "demo")))
    check("rejects unparseable input", escalation.parse_human_action("hello") is None)


# -------------------------------------------------------------- introspect --

def test_introspect(page):
    print("\nintrospect: click/focus resolution")
    page.goto(PARABANK_URL, wait_until="networkidle")
    elements, texts = lp.extract_all_frames(page)
    found = lp.infer_labels(elements, texts)
    username = next(r for r in found if r["inferred_label"] == "Username")
    rect = username["rect"]
    cx, cy = rect["x"] + rect["width"] / 2, rect["y"] + rect["height"] / 2

    match = introspect.resolve_click_target(page, cx, cy)
    expected = (username["frame_index"], username["local_candidate_id"])
    check("click on a real candidate resolves correctly", match == expected, f"got {match}, expected {expected}")

    empty_match = introspect.resolve_click_target(page, 640, 700)
    check("click on empty space resolves to None", empty_match is None)


# ------------------------------------------------------------------ replay --

def test_replay_secret_resolution():
    print("\nreplay: secret resolution")
    try:
        replay_module.resolve_secret_value("SECRET_DOES_NOT_EXIST_12345")
        check("missing secret raises ReplayError", False)
    except replay_module.ReplayError:
        check("missing secret raises ReplayError", True)

    os.environ["SECRET_TEST_PROBE"] = "probe_value"
    try:
        value = replay_module.resolve_secret_value("SECRET_TEST_PROBE")
        check("present secret resolves correctly", value == "probe_value", f"got {value!r}")
    finally:
        del os.environ["SECRET_TEST_PROBE"]


def test_replay_against_saved_artifact():
    print("\nreplay: full run against data/discovery_run.json")
    artifact_path = DATA_DIR / "discovery_run.json"
    if not artifact_path.exists():
        check("replay artifact exists", False, "run discovery.py at least once first")
        return

    result = replay_module.replay(str(artifact_path), PARABANK_URL)
    check(
        "replay completes (success, or a clean reported failure)",
        result["status"] in ("success", "failure"),
        f"got status={result['status']!r}",
    )
    if result["status"] == "failure":
        print(f"    (replay reported a failure -- this is fine if SECRET_PASSWORD "
              f"isn't set in .env: {result['failure']})")


# --------------------------------------------------------------------- llm --

def test_live_discovery_and_replay():
    print("\nlive discovery + replay (uses the real API)")
    import discovery

    out_name = "test_suite_discovery_run.json"
    discovery.run_discovery(PARABANK_URL, "Log in with username 'john' and password 'demo'", out_name=out_name)

    import json
    with open(DATA_DIR / out_name) as f:
        artifact = json.load(f)
    check(
        "live run reports success",
        artifact["final_status"] is not None and artifact["final_status"]["success"],
        f"final_status={artifact['final_status']}",
    )
    check("live run hit no coverage gaps or guardrail violations", artifact["failure"] is None, f"failure={artifact['failure']}")

    result = replay_module.replay(str(DATA_DIR / out_name), PARABANK_URL)
    check("replay of the fresh run succeeds", result["status"] == "success", f"status={result['status']}")

    # Clean up the test-only artifact pair so it doesn't linger as fake evidence.
    (DATA_DIR / out_name).unlink(missing_ok=True)
    (DATA_DIR / out_name.replace(".json", "_debug.json")).unlink(missing_ok=True)


# -------------------------------------------------------------------- main --

def main():
    skip_llm = "--skip-llm" in sys.argv

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": lp.VIEWPORT_WIDTH, "height": lp.VIEWPORT_HEIGHT})

        test_locator_parabank(page)
        test_locator_text_candidates(page)
        test_locator_nested_frames(page)
        test_matching()
        test_guardrails()
        test_domain_guardrail(page)
        test_escalation_parsing()
        test_introspect(page)

        browser.close()

    test_replay_secret_resolution()
    test_replay_against_saved_artifact()

    if skip_llm:
        print("\nlive discovery test skipped (--skip-llm)")
    elif not os.environ.get("ANTHROPIC_API_KEY"):
        print("\nlive discovery test skipped (no ANTHROPIC_API_KEY set)")
    else:
        test_live_discovery_and_replay()

    passed = sum(1 for _, ok, _ in results if ok)
    failed = [name for name, ok, detail in results if not ok]
    print(f"\n{passed}/{len(results)} checks passed")
    if failed:
        print("FAILED:")
        for name in failed:
            print(f"  - {name}")
        sys.exit(1)
    print("ALL PASSED")


if __name__ == "__main__":
    main()
