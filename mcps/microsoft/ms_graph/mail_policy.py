"""
External-sender mail policy: hide mail that arrived from outside the org.

Configuration is one environment variable, ``MS_MAIL_ALLOWED_SENDER_DOMAINS``,
which is both the toggle and the allowlist. Unset, empty, or whitespace-only
means the policy is OFF and every mail surface behaves exactly as it did
before. A non-empty comma-separated list of domains turns it ON. A boolean
plus a list would be two knobs that can disagree; one knob cannot. The list is
explicit and never derived from the signed-in user: the UPN domain is only one
of a tenant's verified domains, guests carry ``#EXT#`` UPNs, and deriving would
cost a Graph call per request. Operators list every domain the org legitimately
sends *from* — primary domain, aliases, and ``<tenant>.onmicrosoft.com`` for
Exchange system senders such as postmaster and NDRs — because anything not
listed is hidden.

**What counts as internal.** ``from.emailAddress.address`` must exist and its
domain (the text after the last ``@``, stripped and lowercased) must be in the
allowlist; when ``sender.emailAddress.address`` is present its domain must be
in the allowlist too. Everything else — a missing or null ``from``, an address
without an ``@`` (legacy X.500 DNs), a trailing dot, an unknown domain — is
external. Fail closed, no special cases, exact match only (subdomains are
listed explicitly). ``sender`` is consulted because Exchange does not
authenticate the Sender header the way DMARC covers From, so "sent on behalf
of an internal user by an external service" is external-originated content.
``replyTo`` is not consulted. The one exception is a draft: Graph returns no
``from`` on a draft at all (verified live 2026-09-06 on the create response,
a single GET, the folder listing, and the delta feed), so a message Exchange
marks ``isDraft`` is the user's own composition and is allowed. A sender
cannot set ``isDraft`` on mail they deliver; only Exchange does, and only on
messages composed in the mailbox.

**Where enforcement lives.** At the caller boundary, in this module only —
never inside the ~18 sync/async fetch functions in ``mail.py`` and
``attachments.py``, which would force ``$select`` rewriting everywhere and
re-check the same parent message once per attachment call. The invariant:
*every surface that lists messages filters the list; every surface that takes a
message id verifies the sender before any other Graph call for that message,
using the same ``mailbox`` the read will use; an attached message (item
attachment) is judged by the same rule as a message.* Surfaces that already
hold the message dict check it for free; id-only surfaces pay one extra
``GET {base}/messages/{id}?$select=id,from,sender`` and only while the policy
is on.

**Deliberately not gated.** ``mark_mail_read_json`` and ``list_emails``'
``mark_as_read`` are writes that return counts only, on ids the caller must
already hold — and the gated surfaces never hand out an external id.
``update_draft_body``, ``send_draft``, and ``add_draft_attachment_json`` take
draft ids, which Exchange rejects on non-drafts. ``manage_mail_folders``
returns folder metadata, not mail. Teams, calendar, files, and Power BI are out
of scope; calendar invites from external organisers are the recommended next
follow-up.

**Scope of the control.** It keys on Exchange's ``from``/``sender``, so it is a
control against agents reading mail that *arrived* from outside. It does not
defeat a forged internal From that already passed the tenant's anti-spoofing
(that is the mail gateway's job — DMARC reject/quarantine), and it does not
cover content re-originated internally: a colleague's inline forward, an
existing reply draft, a ticketing relay whose ``internetMessageHeaders`` still
name the original sender, an attachment previously saved to OneDrive, or a
``.eml`` posted in Teams.
"""

import logging
import os
import re
from typing import Any

from . import mail as mail_ops
from .graph_client import AsyncGraphClient, GraphClient

logger = logging.getLogger(__name__)

ENV_ALLOWED_SENDER_DOMAINS = "MS_MAIL_ALLOWED_SENDER_DOMAINS"

# The only fields the id-only check fetches: enough to judge the sender and
# nothing that could leak the message's content into a refusal path.
SENDER_SELECT = "id,from,sender,isDraft"

EXTERNAL_SENDER_TEXT = (
    "This message is from a sender outside the allowed domains and is hidden by the mail policy."
)

# Desktop JSON `error` value — a permanent error, never retried.
EXTERNAL_SENDER_ERROR = "external_sender"

# Appended to list_emails whenever the policy is on, independent of what was
# hidden: a hidden *count* on a $search query would be a content oracle.
POLICY_NOTICE = "Messages from senders outside the allowed domains are hidden by the mail policy."

# A rule carrying any of these re-delivers every external message as an
# internal one (from = the user), which is a durable self-service bypass.
FORWARDING_ACTIONS = frozenset({"forwardTo", "forwardAsAttachmentTo", "redirectTo"})
FORWARDING_RULE_TEXT = (
    "Inbox rules that forward or redirect mail cannot be created while the mail policy is on."
)

# A dotted hostname, lowercased: labels of 1-63 chars that neither start nor
# end with '-', at least two of them, 253 chars overall. Anything else in the
# configuration ('*', a bare TLD, a URL) is a misconfiguration, not a domain.
_DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$"
)


class MailPolicyConfigError(RuntimeError):
    """The allowlist is set but malformed — fail closed rather than unfiltered."""


def allowed_sender_domains() -> frozenset[str]:
    """Parse the allowlist from the environment. Empty frozenset = policy off.

    Read at call time, never captured at import: ``.env`` and the chart's
    ConfigMap can land after this module is imported. Blank entries (a trailing
    comma, a stray newline from a ConfigMap block) are skipped rather than
    rejected, because they carry no intent.
    """
    raw = os.environ.get(ENV_ALLOWED_SENDER_DOMAINS) or ""
    domains: set[str] = set()
    for entry in raw.split(","):
        cleaned = entry.strip().lower().removeprefix("@")
        if not cleaned:
            continue
        if not _DOMAIN_RE.match(cleaned):
            raise MailPolicyConfigError(
                f"{ENV_ALLOWED_SENDER_DOMAINS} contains an invalid domain: {cleaned!r}"
            )
        domains.add(cleaned)
    return frozenset(domains)


def enabled() -> bool:
    """True when the allowlist is non-empty. Raises on a malformed value."""
    return bool(allowed_sender_domains())


def sender_domain(address: Any) -> str | None:
    """The domain of an SMTP address, or None when there is no usable one.

    Splits on the LAST ``@`` because a quoted local part may contain one
    (``"a@evil.com"@corp.com`` is corp.com's). No trailing-dot stripping and no
    other normalisation: an address the parser cannot fully account for must
    fail the allowlist rather than be massaged into it.
    """
    if not isinstance(address, str):
        return None
    _, sep, domain = address.strip().rpartition("@")
    if not sep:
        return None
    domain = domain.strip().lower()
    return domain or None


def _address(node: Any) -> Any:
    """The ``emailAddress.address`` under a Graph recipient node, if any.

    Delta ``value`` entries are raw Graph objects, so every level here can be
    missing, null, or the wrong type.
    """
    if not isinstance(node, dict):
        return None
    email = node.get("emailAddress")
    if not isinstance(email, dict):
        return None
    return email.get("address")


def sender_allowed(msg: Any, domains: frozenset[str]) -> bool:
    """True when a message originated inside ``domains``. Pure, fail closed.

    A draft is allowed outright: Graph returns no ``from`` on a draft, and
    ``isDraft`` is set by Exchange on messages composed in the mailbox, never
    by a sender. The check is for the boolean ``True`` only, so a string or
    any other truthy junk in a malformed payload does not open the gate.
    """
    if not isinstance(msg, dict):
        return False
    if msg.get("isDraft") is True:
        return True
    if sender_domain(_address(msg.get("from"))) not in domains:
        return False
    on_behalf = _address(msg.get("sender"))
    return on_behalf is None or sender_domain(on_behalf) in domains


def message_allowed(msg: Any) -> bool:
    """True when the policy is off, else whether this message is internal."""
    domains = allowed_sender_domains()
    return not domains or sender_allowed(msg, domains)


def filter_messages(messages: list[Any]) -> list[Any]:
    """Drop external messages from a listing. Identity when the policy is off.

    ``@removed`` tombstones are kept unchanged: they carry no content and the
    desktop client needs them to delete rows. The hidden count goes to the
    server log only — returning it to the caller would turn a ``$search`` query
    into a content oracle.
    """
    domains = allowed_sender_domains()
    if not domains:
        return messages
    kept = [
        msg
        for msg in messages
        if isinstance(msg, dict) and ("@removed" in msg or sender_allowed(msg, domains))
    ]
    hidden = len(messages) - len(kept)
    if hidden > 0:
        logger.info("Mail policy hid %d of %d message(s)", hidden, len(messages))
    return kept


def rule_forwards(rule: Any) -> bool:
    """True when an inbox-rule definition would forward or redirect mail."""
    actions = rule.get("actions") if isinstance(rule, dict) else None
    if not isinstance(actions, dict):
        return False
    return bool(FORWARDING_ACTIONS & actions.keys())


def check_message(client: GraphClient, message_id: str, mailbox: str | None) -> bool:
    """Verify a message's sender by id, before anything else is read from it.

    Costs no request while the policy is off. ``mailbox`` is positional and
    required so that no call site can forget it: checking ``/me`` before a
    ``/users/{mailbox}`` read is the wrong check even when it happens to 404.
    """
    domains = allowed_sender_domains()
    if not domains:
        return True
    msg = client.get(
        f"{mail_ops._base(mailbox)}/messages/{mail_ops._safe_id(message_id)}",
        params={"$select": SENDER_SELECT},
    )
    return sender_allowed(msg, domains)


async def acheck_message(client: AsyncGraphClient, message_id: str, mailbox: str | None) -> bool:
    """Verify a message's sender by id (async). See :func:`check_message`."""
    domains = allowed_sender_domains()
    if not domains:
        return True
    msg = await client.get(
        f"{mail_ops._base(mailbox)}/messages/{mail_ops._safe_id(message_id)}",
        params={"$select": SENDER_SELECT},
    )
    return sender_allowed(msg, domains)
