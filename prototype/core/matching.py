"""
Matches a stored label against a fresh candidate list, so replay and
checkpointing can re-locate an element without an LLM in the loop. Matches
on inferred label text (falling back to own_text), exact only -- no fuzzy
matching or confidence scoring, so a miss is always a clean "not found"
rather than a guess.

Also holds two small candidate-list utilities shared by discovery.py and
escalation.py: describe_candidates (render a candidate list as text -- for
tag == "text" candidates, this also shows the current value, since those
exist to be read, not clicked/typed into) and secret_ref_for_label (name the
environment variable a secret's real value should be supplied under -- see
discovery.py's record_step and replay.py's replay_step).
"""

import re


def describe_candidates(results):
    """A short, readable text block, one line per candidate -- what the
    deterministic side currently sees, shown to both the LLM (as part of its
    observation) and a human (during escalation)."""
    lines = []
    for i, r in enumerate(results):
        label = r["inferred_label"] or "(no label found)"
        if r["tag"] == "text":
            lines.append(f"{i}. text -- \"{label}\": {r['own_text']!r}")
        else:
            lines.append(f"{i}. {r['tag']} ({r['type']}) -- \"{label}\"")
    return "Elements the deterministic detector currently sees on this page:\n" + "\n".join(lines)


def secret_ref_for_label(label):
    """Deterministic environment-variable name for a secret field's label,
    e.g. "Password" -> "SECRET_PASSWORD". The real value is never stored in
    an artifact -- only this reference is; replay looks the real value up
    from the environment (.env) at replay time."""
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", label).strip("_").upper()
    return f"SECRET_{slug}"


def find_live_candidate(fresh_candidates, label, tag=None, type_=None):
    """Returns the fresh candidate dict whose label matches `label`, or None
    if nothing matches. `tag`/`type_` narrow the search when given (e.g. so
    we don't match a button to an input that happens to share wording)."""

    def matches_tag_and_type(candidate):
        if tag is not None and candidate["tag"] != tag:
            return False
        if type_ is not None and candidate["type"] != type_:
            return False
        return True

    for candidate in fresh_candidates:
        if candidate["inferred_label"] == label and matches_tag_and_type(candidate):
            return candidate

    # Fall back to own_text -- covers buttons/links whose own rendered text
    # IS effectively their label (e.g. "Log In"), in case some nearby text
    # shifted and a different label now scores higher, even though the
    # control's own text is unchanged.
    for candidate in fresh_candidates:
        if candidate["own_text"] == label and matches_tag_and_type(candidate):
            return candidate

    return None
