# Business outcome vs. failure — a tested demonstration

A real answer to the assignment's "no such member is a legitimate answer, not a
crash" challenge, built and empirically tested against ParaBank's loan feature
(`Request Loan`), which gives a clean `"Status:" -> "Approved"/"Denied"` result --
two outcomes, both legitimate, neither a bug.

**`artifact.json`** / **`artifact_debug.json`** / **`discovery_console_log.txt`** --
a real discovery run: log in, apply for a loan, `read_value` the `Status:` field.
The recorded step's target is `{"label": "Status:", "tag": "text"}` -- the STABLE
label, not the value ("Denied") it happened to observe.

**`label_robustness_proof.txt`** -- direct, live proof (via
`matching.find_live_candidate`, the exact function `replay.py` uses) that this
recording generalizes correctly to the *other* outcome (Approved), while a
naively-recorded artifact that used the observed value as its label would not --
it would raise `ReplayError` and escalate a perfectly legitimate "Approved"
answer as if something had broken.

**The actual finding, and it's a real one, not staged:** the first attempt at
this demo caught the LLM doing exactly the wrong thing -- calling `read_value`
with `expected_label="Denied"` (the value) instead of `"Status:"` (the label).
It worked anyway, by luck (`find_live_candidate`'s `own_text` fallback matched
the value to itself), which is precisely how this class of bug hides until the
outcome changes. `discovery.py`'s `read_value` tool description was strengthened
in response (see its `IMPORTANT: pick the STABLE label...` clause), and the
second attempt picked the correct label on its own.

**What this shows:** the existing `read` mechanism (no new schema field, no new
step type) already solves the business-outcome-vs-failure conflation problem for
the whole class of "one stable field, several legitimate values" scenarios --
*when* the recording anchors to the right thing. It does not solve the harder
case where the alternate outcome has no stable field to read at all (e.g.
ParaBank's transaction search: "not found" is just an empty results table, no
distinctive label) -- see `evidence/replay_error_async_race/` and REPORT.md's
Cuts section for that still-open case.
