# Computer-Use Automation System

An LLM-driven agent that operates a legacy-style bank UI with no API, records a
successful run as a typed, replayable artifact, and replays it deterministically
— no LLM in the loop — with human escalation when either side gets stuck.

Target surface: [ParaBank](https://parabank.parasoft.com) (Parasoft's public banking
demo app) — chosen as a proxy for "legacy web app": server-rendered, classic
`<frameset>`/`<frame>` nesting, table-based layout, no test IDs. All credentials used
against it are ParaBank's own published demo credentials (`john`/`demo`), never real
PII.

See `/REPORT.md` for the full design write-up (architecture, artifact schema,
determinism/error handling, heterogeneity & multi-tenant design, escalation model,
safety guardrails, and cuts) and `/evidence/` for real discovery + replay run output,
including one replay that hits a genuine live server error.

## Setup

From the repo root:

```bash
cd src
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
```

Create `src/.env` (gitignored, never committed) with:

```
ANTHROPIC_API_KEY=sk-ant-...      # only needed to run discovery.py (the LLM step)
SECRET_PASSWORD=demo              # ParaBank's own public demo password
```

`SECRET_PASSWORD` is resolved from the environment only at replay time — it is never
written into any artifact. See REPORT.md's Artifact schema / Safety sections for why.

**Running without live services:** `replay.py` never calls an LLM or needs
`ANTHROPIC_API_KEY` at all — it only needs a real target site to drive (still a live
browser session, just no model in the loop) and, if the recorded flow types a secret,
that secret present in `.env`. The test suite's `--skip-llm` flag (below) exercises
everything else — locator, matching, guardrails, escalation parsing, introspection,
and a full replay — with zero API calls.

## Demo path

Run a genuine LLM-driven discovery run, then replay the artifact it produces,
deterministically:

```bash
cd src

# 1. Discovery: LLM logs in, opens the first account, and reads its balance.
.venv/bin/python core/discovery.py \
  https://parabank.parasoft.com/parabank/index.htm \
  "Log in with username 'john' and password 'demo', then open the first account listed to view its Account Details page, and read its balance from there"

# writes data/discovery_run.json (the artifact) + data/discovery_run_debug.json (locator debug detail)

# 2. Replay: same flow, no LLM, deterministic label-based re-matching.
.venv/bin/python core/replay.py \
  data/discovery_run.json \
  https://parabank.parasoft.com/parabank/index.htm

# writes data/<artifact_name>_replay.json; prints "outputs: balance = '...'" at the end
```

A simpler login-only goal works the same way with no arguments (both scripts default
to it): `.venv/bin/python core/discovery.py` then `.venv/bin/python core/replay.py`.

To see the human-escalation path fire for real: temporarily set `SECRET_PASSWORD` in
`.env` to a wrong value and re-run replay. It types the wrong password, the post-login
checkpoint fails, and an escalation prompt opens — a labeled screenshot, the full
candidate list, and a queued-command interface to fix it live. Restore the correct
value afterward. (`/evidence/replay_error_live_app_error/` shows this exact path
firing for a different, genuinely unplanned reason: ParaBank's own demo server
returning an internal error mid-run.)

## Test suite

```bash
.venv/bin/python verification/run_all_tests.py             # full suite, incl. one live LLM run
.venv/bin/python verification/run_all_tests.py --skip-llm  # fast/free subset, no API calls
```

Repeatable and self-cleaning: the live-LLM test writes and then deletes its own
artifact pair so it never leaves fake evidence behind.

## Layout

```
src/
  core/           the system itself (see file-level docstrings for each piece's role)
    locator.py       text+geometry candidate extraction/labeling (frame-aware)
    introspect.py     resolves a raw agent click/focus back to a candidate
    matching.py       label-based re-matching + small shared utilities
    browser_actions.py  shared click+navigation-wait
    guardrails.py     domain allowlist + money-movement gate (MONEY_KEYWORDS lives here)
    escalation.py     shared human-in-the-loop loop (discovery + replay)
    discovery.py      LLM agent loop; writes the artifact
    replay.py         deterministic executor; no LLM
  verification/    repeatable regression suite (run_all_tests.py)
  data/            gitignored scratch output: artifacts, debug files, escalation
                   screenshots from your own local runs -- regenerated automatically,
                   nothing here is meant to persist (see /evidence/ for what does)
evidence/         curated real discovery + replay run output, including a replay
                  that hits an error -- see evidence/README.md
```
