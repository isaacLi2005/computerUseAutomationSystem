"""
Matches a stored label against a fresh candidate list, so replay and
checkpointing can re-locate an element without an LLM in the loop. Matches
on inferred label text (falling back to own_text), exact only -- no fuzzy
matching or confidence scoring, so a miss is always a clean "not found"
rather than a guess.
"""


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
