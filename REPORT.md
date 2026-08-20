# Design Write-Up

## 1. Architecture

A single synchronous Python process driving Playwright (Chromium, headless) against
ParaBank — a public, server-rendered banking demo with `<frameset>`/`<frame>` nesting,
table layout, and no test IDs. A real stand-in for "legacy web app," not a convenient one.

Central decision: **one locator module (`locator.py`) is the single source of truth
for what exists on a page, used identically by the LLM's action-checking during
discovery and by label-based re-matching during replay.** It extracts every
interactive control and every visible text run — including inside nested frames,
composited into top-page coordinates via Playwright's own frame geometry — and infers
each control's label purely from geometry (nearest text, direction-weighted: left/above
trusted more than right/below, matching how both legacy table forms and modern stacked
forms render). No DOM-tree signal (parent/child, id/class, `<label for>`) is used
anywhere: a legacy app can fail to have clean markup, but it can't fail to render a
control and its label at real pixel positions. That's the one signal a human operator
is guaranteed to have.

Discovery gives the LLM an unfiltered real screenshot and lets it click/type anywhere
a human could — never constrained to the deterministic candidate list. But every
action is checked against that list immediately after (`introspect.py` resolves the
raw coordinate/focus back to a candidate via `el.closest('[data-cua-id]')`). An
unmatched action is a genuine coverage gap, and discovery stops hard rather than
silently recording an unreplayable step. Replay never touches raw coordinates — it
re-locates every step by `(label, tag, type)` on a fresh candidate list.

Files: `locator.py` (extraction/labeling), `introspect.py` (raw-action → candidate,
discovery only), `matching.py` (label re-matching + shared utilities),
`browser_actions.py` (shared click+navigation-wait), `guardrails.py` (domain lock +
money gate), `escalation.py` (human-in-the-loop loop, shared), `discovery.py` (LLM
loop, artifact writer), `replay.py` (deterministic executor).

**Trade-off:** single process, no service boundaries, no queue. The assignment is
explicit that scaling infrastructure isn't rewarded prematurely, and the real
production split (discovery as a rare, human-supervised job; replay as a call the
agent-facing product invokes per request) is a natural evolution of this shape, not a
redesign.

One genuine simplification found by reconsidering, not assumed upfront: discovery
needs introspection to interpret a raw agent coordinate; replay clicks a known
candidate directly. But during escalation, a human always picks a candidate by its
known index, never a raw coordinate — so escalation shares one implementation for
both callers with no duplication.

## 2. Artifact schema

```json
{
  "name": "log_in_with_and_password", "version": 1,
  "goal": "Log in with username 'john' and password 'demo', ...",
  "final_status": {"success": true, "notes": "..."}, "failure": null,
  "steps": [
    {"step": 1, "comment": "Click \"Username\"", "action": "click",
     "value": null, "secret_ref": null,
     "target": {"label": "Username", "tag": "input", "type": "text"}},
    {"step": 4, "comment": "Insert (secret: SECRET_PASSWORD) into Password",
     "action": "type", "value": null, "secret_ref": "SECRET_PASSWORD",
     "target": {"label": "Password", "tag": "input", "type": "password"}},
    {"step": 8, "comment": "Read \"Balance:\" as balance", "action": "read",
     "output_key": "balance",
     "target": {"label": "Balance:", "tag": "text", "type": null}}
  ],
  "checkpoints": [{"after_step": 5, "expected_label": "Welcome",
                    "reason": "confirms login succeeded", "held": true}],
  "escalations": [], "outputs": {"balance": "-$2423.00"}
}
```

- **Targets are `(label, tag, type)`, never coordinates or DOM selectors.** Coordinates
  aren't stable across a fresh load; selectors/ids aren't reliable on markup a legacy
  vendor never designed for automation. A geometry-inferred label tolerates the drift
  enterprise UIs actually have while correctly refusing to guess when the label itself
  changes — that routes to escalation instead of a wrong click.
- **Secrets are never stored, not even redacted to a placeholder** (a placeholder can't
  be replayed — it would type the literal placeholder). A password step stores
  `secret_ref: "SECRET_PASSWORD"`, deterministically derived from the field's label;
  replay resolves the real value from `.env` only at replay time.
- **Outputs store what to read, not what was read.** A `read` step's target identifies
  *where* the value lives; replay re-reads the live value every time, so a balance
  that legitimately changed between runs isn't treated as drift.
- **`comment` is deterministic, not LLM-authored** — reviewability shouldn't depend on
  trusting model prose for what actually happened. `checkpoints[].reason` *is*
  LLM-authored (captured once, at record time), since that's the model's own
  justification for believing it reached the right state — worth preserving verbatim.
- **`name` is slugified from the goal**, stripping quoted literals first (`'john'`) so
  parameters don't leak into the capability's identity.
- **Full locator debug detail lives in a paired `_debug.json`**, not the main
  artifact — useful for debugging the heuristic, irrelevant to what the capability
  does. Keeps the main artifact the thing a human or agent should actually read.
- **Typed input parameters are scoped to secrets only**, deliberately (see Cuts).

## 3. Determinism & error handling

Replay makes zero LLM calls. Every step re-locates via `find_live_candidate(fresh,
label, tag, type)` — exact label match, falling back to a control's own rendered text
(covers a button/link whose own text is its label, if nearby text shifted the
inferred label without the control's own text changing). No fuzzy matching: a miss is
always a clean "not found," never a guess.

**Timing** is handled two ways, not blanket sleeps. Every click waits out a possible
navigation (bounded 3s, timing out harmlessly for non-navigating clicks). Separately,
a `wait` the LLM took during discovery is recorded as its own step and replayed
verbatim — found necessary empirically: ParaBank's account table loads after login
completes, the LLM noticed and waited, but that wait wasn't originally captured, so
replay raced content the LLM had already learned to wait for. Recording it turns a
one-time observation into a permanent fix, rather than guessing with a retry loop.

**Recoverable conditions route to a human, not automatic retry** — this is regulated
financial data behind a legacy UI; blindly retrying a failed step (e.g. resubmitting
after a validation error) risks compounding the mistake more than it risks a slow
recovery. The recorded `wait` is the one exception, since it isn't a guess — it's
exact reproduction of a decision already made once.

**Business outcome vs. hard failure — the honest current state:** an outcome
discovery *anticipated* (e.g. a goal like "confirm member 12345 doesn't exist")
already flows through correctly, since a checkpoint's success condition is whatever
state discovery recorded as correct — "not found" can be the checkpointed target just
as validly as "found." What isn't yet separately typed is an *unanticipated*
deviation: today any unexpected checkpoint/target miss becomes either a resolved
escalation or a hard failure, with no automatic "known alternate outcome" vs. "real
break" classification. Next step: a `business_outcome` status alongside
`success`/`failure`, checked against a small editable per-capability pattern list
(same shape as `MONEY_KEYWORDS`) before escalating, not instead of it.

Every failure carries the step, action, expected label/tag, and — via the escalation
record — what a human actually saw and did about it. Never a bare stack trace.

## 4. Heterogeneity & multi-tenant

**Surface abstraction.** The seam already exists and is exercised: everything above
`locator.py` operates only on the candidate shape it produces (`{tag, type, rect,
own_text, inferred_label, frame_index, local_candidate_id}`), never on a raw DOM API
directly — `score_candidate`/`infer_labels` are pure geometry over `(rect, text)`
pairs. Extending to desktop means a new extraction backend (an accessibility-tree
walker producing the same candidate shape from each control's role, rect, and own
text) behind the same `extract_all_frames(page) -> (elements, texts)` contract, plus
OS-automation click/type behind the same `click_and_wait` contract. Scoring, labeling,
matching, and replay need zero changes. Legacy web isn't a future extension at all —
frame-awareness, geometry-only labeling, and zero test-ID dependency are what this
system is already built around.

**Multi-tenant reuse.** Because targeting is `(label text, tag, type)` rather than a
tenant-specific selector or URL, an artifact recorded against one tenant's install of
a vendor product replays against another tenant's install of the same product as long
as field labels and control types match — which typically survives per-tenant
branding/config changes even when markup doesn't. The one genuinely per-tenant piece,
the target URL, is already factored out of the artifact and supplied at
replay-invocation time; the domain guardrail derives its allowlist from that
parameter, never from anything baked into the recording. For real divergence, the
design (not built — no second tenant target exists to demonstrate it) is an
`{artifact_name}.{tenant_id}.json` override that patches specific steps' `target.label`
or substitutes a value, merged over the base artifact before replay starts — the base
representation was chosen so this override needs no schema change.

**Drift detection**, similarly designed but not built: track, per replay, how often a
step needed the `own_text` fallback instead of an exact match, and how often a step
needed escalation. A rising rate for a given artifact is the drift signal — it would
gate that artifact from "approved" back to "needs review" rather than silently
degrading in production.

## 5. Escalation & handoff

**Detecting stuck** has three triggers: the discovery LLM calling
`report_goal_status(success=false)`, discovery exhausting its turn budget, and replay
raising a `ReplayError` (target not found, secret missing, checkpoint failed).

**Taking control** is literal: `run_escalation` operates on the exact same Playwright
`page` the automation was just driving — nothing torn down or reloaded — so the human
acts on the real, already-authenticated state.

**Who's in control is a structural guarantee, not a flag:** because everything is one
synchronous process, while a human is being prompted, the calling loop is simply
paused mid-call. Only one side can ever issue browser commands, enforced by call-stack
position rather than a lock that could be gotten wrong.

**Context on handoff:** a screenshot labeled with each clickable candidate's index
number drawn directly on the image, plus the full candidate list written to a paired
text file (kept out of the terminal, which would otherwise bury the actual prompt
under dozens of lines) — the same numbered list the agent itself reasons over, so
there's no separate operator vocabulary to learn.

**The human's interface is the terminal itself** — no separate operator console, per
the assignment's explicit allowance to mock that surface. Commands: `<index>` (click),
`<index> <value>` (click+type), `done`, `skip`. After live testing showed rapid
multi-line input was confusing (actions executing immediately per line, interleaving
with delayed output), this became queue-then-execute: nothing touches the page until
`done`; each line queues an action against the candidate list as it looked when
escalation started. Documented trade-off: queued indices can go stale if an earlier
queued action itself changes the page.

**Handing back:** `done` resumes the same calling loop — discovery gets a fresh
observation and continues; replay resumes at the *next* recorded step. Known
limitation, found empirically: if the failed step's own recorded follow-up (e.g. a
`wait`) would normally run next, a human's manual recovery skips it, occasionally
cascading into a second escalation. `skip` is a clean terminal failure instead.

Guardrails apply identically to human-queued actions — escalation can't bypass what
the agent itself is bound by. Every escalation is recorded into `escalations[]`:
reason, screenshot, exact human actions, outcome — a full audit trail.

## 6. Safety

**Domain allowlist:** derived from the target URL at invocation, not hardcoded —
checked after every click, agent- or human-driven, hard-stopping the instant the page
navigates outside it.

**Money-movement gate**, two layers: if the goal never authorized a money-flavored
action (checked against an editable `MONEY_KEYWORDS` list — `transfer`, `withdraw`,
`deposit`, `pay`, `loan` — against the goal text or the candidate's label), that
action is blocked outright, no prompt. If the goal did authorize it, that's necessary
but never sufficient — a live human still approves the specific action with `y`/`N`
every time it's about to execute, not once via the goal text upfront. Reversible
actions (clicks, reads, navigation) proceed autonomously; anything that looks
irreversible always requires live approval, regardless of what the goal said.

**Secrets:** never written to any artifact or log, not even redacted — a password
field's value becomes a deterministic environment-variable reference, resolved from
`.env` only at replay time. `.env` is gitignored and confirmed never committed.
Session identifiers (`jsessionid` in a captured URL) are stripped from the debug file
the same way, on the reasoning that a session token is a bearer credential exactly
like a password.

**What's not covered, honestly:** general PII redaction beyond secrets and session
tokens isn't built. A `read` value or typed field could in principle contain an SSN
or account number and would currently be written into the artifact/outputs as-is.
ParaBank's demo data never forced this; production would need field-level
pattern-classification ahead of writing anything to disk.

## 7. Cuts

- **Business-outcome vs. hard-failure as a distinct status.** Two-state today; the
  extension design is in Section 3. First thing I'd build, since the spec calls
  conflating these "the most common design mistake here" and there's already a sized
  plan for it.
- **General parameterization beyond secrets** (e.g. a typed member ID). Deferred
  because it requires the discovery LLM to distinguish "per-invocation parameter" from
  "fixed to this recording" — a real design problem, not a small addition.
- **JS-only clickable elements** (`<div onclick>` with no native semantics) are
  invisible to the extractor's element query. A real coverage gap for some legacy
  patterns, not encountered against ParaBank but plausible elsewhere.
- **CAPTCHA / 2FA:** no explicit handling. The honest expected behavior is that an
  unanticipated prompt fails a checkpoint or stalls the agent, both correctly routing
  to escalation — but never exercised against a real 2FA flow.
- **Multi-tenant override loading and desktop-surface extraction** are designed
  (Section 4) but not implemented — no second tenant/variant target exists, and no
  accessibility-tree backend was written.
- **Multi-run stability / confidence scoring / approval gating** (optional stretch
  goals): the natural consumer of the drift signal in Section 4, not built.
- **Escalation queue staleness**: documented in the prompt itself rather than solved.
- **Label collision on dense tabular data**: the geometry heuristic can grab a
  neighboring table row's value instead of the intended one — found empirically while
  building balance-reading, accepted as a real limitation of a strictly geometry-only,
  DOM-structure-blind approach.

**Next, in order:** the business-outcome/failure split; general parameterization (the
clearest limiter on artifact reusability); a second real tenant-variant target, to
demonstrate cross-tenant reuse rather than only design it.
