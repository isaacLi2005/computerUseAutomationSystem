# Evidence

Real runs against the live ParaBank demo site, no simulation. Four pieces, each with
its own console log:

## `discovery_run/`
A complete, successful, LLM-driven discovery run: log in, open the first account,
navigate to its Account Details page, read the balance. `artifact.json` is the exact
artifact this run produced; `artifact_debug.json` is its paired locator-debug detail
(candidate geometry/scoring, kept separate from the main artifact — see REPORT.md's
Artifact schema section for why); `console_log.txt` is the full agent transcript,
turn by turn, including the model's own reasoning.

## `replay_success/`
A clean deterministic replay of that same shape of artifact — no LLM involved. See
that folder's own note for why this one reuses a verbatim transcript from earlier in
development rather than a freshly re-captured run (short version: ParaBank's own
demo server was intermittently erroring at evidence-collection time — see below).

## `replay_error_async_race/`
A **reproducible** replay failure: `discovery_run/artifact.json`'s discovery run
happened to complete without ever needing to record a `wait` step (the account table
loaded fast enough within the LLM's own turn-taking latency). Replay has no such
latency — it moves as fast as the browser allows — so it consistently outruns the
same asynchronous table load discovery never had to wait for, and step 6 (clicking
the first account) fails to find its target. `result.json` is the complete structured
failure result (`status: "failure"`, the exact step/expected-label detail);
`console_log.txt` shows detection, the escalation prompt (screenshot + candidate list
+ instructions), and the terminal outcome. This is the "recoverable timing condition"
category from REPORT.md's Determinism & error handling section, and a concrete
illustration of a real limitation: a recorded `wait` only protects replay when
*discovery itself* happened to need one.

## `replay_error_live_app_error/`
A **genuine, unplanned, live server error** — not something we constructed. Mid
evidence-collection, ParaBank's own demo server started intermittently returning its
own "Error! An internal error has occurred and has been logged." page after login.
`escalation_screenshot.png` is the actual labeled escalation screenshot showing that
exact error page; `escalation_candidates.txt` is the paired candidate list;
`console_log.txt` shows our checkpoint (`expecting to see "John Smith"`) correctly
failing and escalating with full context, exactly as designed, when the underlying
app itself broke.
`independent_sanity_check_screenshot.png` is a minimal, separate script (bypassing
locator/matching/escalation entirely — just raw Playwright selectors) run at the same
time, confirming this really is ParaBank's own error page and not a bug anywhere in
our system: the page title alone reads `"ParaBank | Error"`.

This is exactly the "outright app errors" / "transient slowness" failure category
Section 1 of the assignment calls out as the real-world condition a record-once /
replay-many system has to survive — we didn't have to simulate it; it happened live
while collecting evidence, and the system's error-handling and escalation path
correctly caught it and surfaced full debugging context rather than crashing or
silently proceeding.
