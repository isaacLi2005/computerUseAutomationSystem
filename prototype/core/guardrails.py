"""
Safety guardrails shared by discovery.py and replay.py: a domain lock (stay
on the target site) and a money-movement gate (block actions that look like
they move money to/from an account unless the goal said to). Free functions,
not tied to DiscoverySession, so replay's human-escalation path can apply
the same checks to a human's actions, not just the agent's.

MONEY_KEYWORDS is the one thing to edit to adapt this to a different app or
domain -- add whatever words describe a money-moving action for that app.
"""

from urllib.parse import urlparse

MONEY_KEYWORDS = ["transfer", "withdraw", "deposit", "pay", "loan"]


class GuardrailError(Exception):
    """Whoever is acting (the agent, or a human during escalation) tried to
    leave the allowed site, or move money without the goal authorizing it.
    Hard stop."""


def mentions_money_movement(text):
    text = (text or "").lower()
    return any(keyword in text for keyword in MONEY_KEYWORDS)


def check_money_guardrail(label, money_actions_authorized):
    """Raises GuardrailError if `label` looks like a money-movement action
    the goal never authorized. If the goal DID authorize it, still doesn't
    silently allow it -- a live human has to approve it, every time."""
    if not mentions_money_movement(label):
        return
    if not money_actions_authorized:
        raise GuardrailError(
            f"Tried to act on \"{label}\", which looks like it moves money to "
            f"or from an account, but the goal never mentioned doing that. Stopping."
        )
    answer = input(f'\n  >>> about to act on "{label}", which moves money. Allow? [y/N] ')
    if answer.strip().lower() != "y":
        raise GuardrailError(f'Human did not approve "{label}". Stopping.')
    print(f'  human approved: "{label}"')


def check_domain(page, allowed_domain):
    current_domain = urlparse(page.url).netloc
    if current_domain != allowed_domain:
        raise GuardrailError(
            f"Navigated to {page.url}, outside the allowed site "
            f"({allowed_domain}). Stopping."
        )
