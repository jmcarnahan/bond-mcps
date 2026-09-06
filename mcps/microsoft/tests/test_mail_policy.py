"""Tests for the external-sender mail policy module.

The pure parts are exercised directly; check_message/acheck_message go through
respx because their whole point is the one extra Graph request they make (and
the requests they must NOT make while the policy is off).
"""

from urllib.parse import quote

import httpx
import pytest
import respx
from ms_graph import mail_policy
from ms_graph.graph_client import GRAPH_BASE_URL, AsyncGraphClient, GraphClient, GraphError

from .conftest import (
    GRAPH_ERROR_404,
    SAMPLE_AWKWARD_MESSAGE_ID,
    SAMPLE_DELTA_TOMBSTONE,
    SAMPLE_EXTERNAL_MESSAGE,
    SAMPLE_MESSAGE,
    SAMPLE_ONBEHALF_MESSAGE,
    SAMPLE_SENDER_ONLY_EXTERNAL,
    SAMPLE_SENDER_ONLY_INTERNAL,
)

ENV = mail_policy.ENV_ALLOWED_SENDER_DOMAINS
ONE_DOMAIN = frozenset({"corp.com"})
ENCODED_AWKWARD_ID = quote(SAMPLE_AWKWARD_MESSAGE_ID, safe="")


def _on(monkeypatch, value="example.com"):
    monkeypatch.setenv(ENV, value)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class TestAllowedSenderDomains:
    """The env var is both the toggle and the allowlist."""

    def test_unset_is_off(self):
        assert mail_policy.allowed_sender_domains() == frozenset()
        assert mail_policy.enabled() is False

    @pytest.mark.parametrize("value", ["", "   ", "\n", ", ,\n", "@", ",,,"])
    def test_whitespace_only_value_is_off_not_deny_all(self, monkeypatch, value):
        # The dangerous misreading is "on, with an empty allowlist", which
        # would hide every message in the mailbox.
        monkeypatch.setenv(ENV, value)
        assert mail_policy.allowed_sender_domains() == frozenset()
        assert mail_policy.enabled() is False

    def test_entries_are_stripped_lowercased_and_deduped(self, monkeypatch):
        monkeypatch.setenv(ENV, " @Example.COM , example.com\n")
        assert mail_policy.allowed_sender_domains() == frozenset({"example.com"})
        assert mail_policy.enabled() is True

    def test_trailing_comma_is_not_an_error(self, monkeypatch):
        monkeypatch.setenv(ENV, "example.com,")
        assert mail_policy.allowed_sender_domains() == frozenset({"example.com"})

    def test_multiple_domains(self, monkeypatch):
        monkeypatch.setenv(ENV, "corp.com,corp.onmicrosoft.com,sub.corp.com")
        assert mail_policy.allowed_sender_domains() == frozenset(
            {"corp.com", "corp.onmicrosoft.com", "sub.corp.com"}
        )

    @pytest.mark.parametrize(
        "bad",
        ["example", "*", ".", "exa mple.com", "-bad.com", "a..b", "http://x.com", "bad-.com"],
    )
    def test_invalid_entry_raises_naming_only_that_entry(self, monkeypatch, bad):
        monkeypatch.setenv(ENV, f"good.example.com,{bad}")
        with pytest.raises(mail_policy.MailPolicyConfigError) as excinfo:
            mail_policy.allowed_sender_domains()
        message = str(excinfo.value)
        assert bad in message
        assert "good.example.com" not in message

    def test_enabled_propagates_the_config_error(self, monkeypatch):
        monkeypatch.setenv(ENV, "*")
        with pytest.raises(mail_policy.MailPolicyConfigError):
            mail_policy.enabled()

    def test_value_is_read_at_call_time_not_import_time(self, monkeypatch):
        assert mail_policy.enabled() is False
        monkeypatch.setenv(ENV, "example.com")
        assert mail_policy.enabled() is True
        monkeypatch.delenv(ENV)
        assert mail_policy.enabled() is False


# ---------------------------------------------------------------------------
# Address parsing
# ---------------------------------------------------------------------------


class TestSenderDomain:
    """The domain is whatever follows the LAST @, and nothing is massaged."""

    @pytest.mark.parametrize(
        ("address", "expected"),
        [
            ("A@B@Example.COM", "example.com"),
            ("user@corp.com", "corp.com"),
            (" x@d.com ", "d.com"),
            ('"a@evil.com"@corp.com', "corp.com"),
            ("user@CORP.com.", "corp.com."),
            ("noat", None),
            ("", None),
            (None, None),
            (5, None),
            ({"address": "a@b.com"}, None),
            ("x@", None),
            ("x@   ", None),
        ],
    )
    def test_sender_domain(self, address, expected):
        assert mail_policy.sender_domain(address) == expected


# ---------------------------------------------------------------------------
# The internal/external rule
# ---------------------------------------------------------------------------


class TestSenderAllowed:
    """Decision 3, pinned shape by shape. Anything unaccounted for is external."""

    @pytest.mark.parametrize(
        ("msg", "expected"),
        [
            ({}, False),
            ({"from": None}, False),
            ({"from": {}}, False),
            ({"from": {"emailAddress": None}}, False),
            ({"from": {"emailAddress": {}}}, False),
            ({"from": {"emailAddress": {"name": "Bob"}}}, False),
            ({"from": {"emailAddress": {"address": ""}}}, False),
            ({"from": {"emailAddress": {"address": None}}}, False),
            # A legacy X.500 DN has no domain at all.
            (
                {
                    "from": {
                        "emailAddress": {"address": "/O=EXCHANGELABS/OU=EX/CN=RECIPIENTS/CN=ab"}
                    }
                },
                False,
            ),
            ({"from": {"emailAddress": {"address": "USER@CORP.COM"}}}, True),
            ({"from": {"emailAddress": {"address": "user@corp.com."}}}, False),
            ({"from": {"emailAddress": {"address": "user@corp.com​"}}}, False),
            ({"from": {"emailAddress": {"address": "user@sub.corp.com"}}}, False),
            ({"from": {"emailAddress": {"address": "user@corp.com.evil.net"}}}, False),
            (None, False),
            ("junk", False),
            ([], False),
        ],
    )
    def test_from_shapes(self, msg, expected):
        assert mail_policy.sender_allowed(msg, ONE_DOMAIN) is expected

    @pytest.mark.parametrize(
        ("from_addr", "sender_addr", "expected"),
        [
            ("user@corp.com", "bot@evil.net", False),
            ("mallory@evil.net", "user@corp.com", False),
            ("user@corp.com", "svc@corp.com", True),
            ("user@corp.com", None, True),
            ("user@corp.com", "", False),
        ],
    )
    def test_sender_header_is_checked_too(self, from_addr, sender_addr, expected):
        msg = {"from": {"emailAddress": {"address": from_addr}}}
        if sender_addr is not None:
            msg["sender"] = {"emailAddress": {"address": sender_addr}}
        assert mail_policy.sender_allowed(msg, ONE_DOMAIN) is expected

    def test_sender_present_but_empty_object_is_ignored(self):
        msg = {"from": {"emailAddress": {"address": "user@corp.com"}}, "sender": {}}
        assert mail_policy.sender_allowed(msg, ONE_DOMAIN) is True

    def test_message_allowed_is_true_when_policy_is_off(self):
        assert mail_policy.message_allowed(SAMPLE_EXTERNAL_MESSAGE) is True

    def test_message_allowed_uses_the_configured_domains(self, monkeypatch):
        _on(monkeypatch)
        assert mail_policy.message_allowed(SAMPLE_MESSAGE) is True
        assert mail_policy.message_allowed(SAMPLE_EXTERNAL_MESSAGE) is False
        assert mail_policy.message_allowed(SAMPLE_ONBEHALF_MESSAGE) is False


# ---------------------------------------------------------------------------
# Listing filter
# ---------------------------------------------------------------------------


class TestFilterMessages:
    """Off is identity; on keeps internal messages and every tombstone."""

    def test_off_is_identity(self):
        messages = [SAMPLE_MESSAGE, SAMPLE_EXTERNAL_MESSAGE, SAMPLE_DELTA_TOMBSTONE]
        assert mail_policy.filter_messages(messages) is messages

    def test_on_drops_external_and_keeps_order(self, monkeypatch):
        _on(monkeypatch)
        second = {**SAMPLE_MESSAGE, "id": "second"}
        kept = mail_policy.filter_messages(
            [SAMPLE_MESSAGE, SAMPLE_EXTERNAL_MESSAGE, second, SAMPLE_ONBEHALF_MESSAGE]
        )
        assert [m["id"] for m in kept] == [SAMPLE_MESSAGE["id"], "second"]

    def test_tombstones_pass_through_unchanged(self, monkeypatch):
        _on(monkeypatch)
        kept = mail_policy.filter_messages([SAMPLE_EXTERNAL_MESSAGE, SAMPLE_DELTA_TOMBSTONE])
        assert len(kept) == 1
        # The same object, not a copy: the desktop deletes rows from it.
        assert kept[0] is SAMPLE_DELTA_TOMBSTONE

    def test_non_dict_entries_are_dropped(self, monkeypatch):
        _on(monkeypatch)
        assert mail_policy.filter_messages([None, "junk", 7, SAMPLE_MESSAGE]) == [SAMPLE_MESSAGE]

    def test_empty_list(self, monkeypatch):
        _on(monkeypatch)
        assert mail_policy.filter_messages([]) == []

    def test_hidden_count_is_logged_not_returned(self, monkeypatch, caplog):
        _on(monkeypatch)
        with caplog.at_level("INFO", logger="ms_graph.mail_policy"):
            kept = mail_policy.filter_messages([SAMPLE_MESSAGE, SAMPLE_EXTERNAL_MESSAGE])
        assert kept == [SAMPLE_MESSAGE]
        assert "hid 1 of 2" in caplog.text


# ---------------------------------------------------------------------------
# Forwarding inbox rules
# ---------------------------------------------------------------------------


class TestRuleForwards:
    """Rules that re-originate mail are the policy's self-service bypass."""

    @pytest.mark.parametrize("key", ["forwardTo", "forwardAsAttachmentTo", "redirectTo"])
    def test_forwarding_actions(self, key):
        assert mail_policy.rule_forwards({"actions": {key: ["a@b.com"]}}) is True

    @pytest.mark.parametrize(
        "actions",
        [{"moveToFolder": "id"}, {"markAsRead": True}, {"delete": True}, {}],
    )
    def test_non_forwarding_actions(self, actions):
        assert mail_policy.rule_forwards({"actions": actions}) is False

    @pytest.mark.parametrize("rule", [{}, {"actions": None}, {"actions": "junk"}, None, "junk"])
    def test_missing_or_junk_actions(self, rule):
        assert mail_policy.rule_forwards(rule) is False

    def test_forwarding_alongside_other_actions(self):
        rule = {"actions": {"markAsRead": True, "redirectTo": ["a@b.com"]}}
        assert mail_policy.rule_forwards(rule) is True


# ---------------------------------------------------------------------------
# The id-only check
# ---------------------------------------------------------------------------


class TestCheckMessageAsync:
    """acheck_message: one extra request while on, none while off."""

    @respx.mock
    async def test_off_makes_no_request(self):
        async with AsyncGraphClient("tok") as client:
            assert await mail_policy.acheck_message(client, SAMPLE_MESSAGE["id"], None) is True
        assert len(respx.calls) == 0

    @respx.mock
    async def test_on_asks_only_for_the_sender_fields(self, monkeypatch):
        _on(monkeypatch)
        route = respx.get(url__startswith=f"{GRAPH_BASE_URL}/me/messages/").mock(
            return_value=httpx.Response(200, json=SAMPLE_SENDER_ONLY_INTERNAL)
        )
        async with AsyncGraphClient("tok") as client:
            assert await mail_policy.acheck_message(client, SAMPLE_AWKWARD_MESSAGE_ID, None) is True

        url = str(route.calls[0].request.url)
        assert f"/me/messages/{ENCODED_AWKWARD_ID}" in url
        assert "%24select=id%2Cfrom%2Csender" in url or "$select=id,from,sender" in url

    @respx.mock
    async def test_external_sender_is_refused(self, monkeypatch):
        _on(monkeypatch)
        respx.get(url__startswith=f"{GRAPH_BASE_URL}/me/messages/").mock(
            return_value=httpx.Response(200, json=SAMPLE_SENDER_ONLY_EXTERNAL)
        )
        async with AsyncGraphClient("tok") as client:
            assert await mail_policy.acheck_message(client, SAMPLE_MESSAGE["id"], None) is False

    @respx.mock
    async def test_mailbox_is_threaded_into_the_path(self, monkeypatch):
        _on(monkeypatch)
        route = respx.get(url__startswith=f"{GRAPH_BASE_URL}/users/").mock(
            return_value=httpx.Response(200, json=SAMPLE_SENDER_ONLY_INTERNAL)
        )
        async with AsyncGraphClient("tok") as client:
            assert (
                await mail_policy.acheck_message(
                    client, SAMPLE_MESSAGE["id"], "support@example.com"
                )
                is True
            )

        path = route.calls[0].request.url.path
        assert path.startswith("/v1.0/users/support@example.com/messages/")

    @respx.mock
    async def test_graph_error_propagates(self, monkeypatch):
        _on(monkeypatch)
        respx.get(url__startswith=f"{GRAPH_BASE_URL}/me/messages/").mock(
            return_value=httpx.Response(404, json=GRAPH_ERROR_404)
        )
        async with AsyncGraphClient("tok") as client:
            with pytest.raises(GraphError):
                await mail_policy.acheck_message(client, SAMPLE_MESSAGE["id"], None)

    @respx.mock
    async def test_config_error_raises_before_any_request(self, monkeypatch):
        monkeypatch.setenv(ENV, "*")
        async with AsyncGraphClient("tok") as client:
            with pytest.raises(mail_policy.MailPolicyConfigError):
                await mail_policy.acheck_message(client, SAMPLE_MESSAGE["id"], None)
        assert len(respx.calls) == 0


class TestCheckMessageSync:
    """check_message: the synchronous twin the CLI and forward-spec path use."""

    @respx.mock
    def test_off_makes_no_request(self):
        with GraphClient("tok") as client:
            assert mail_policy.check_message(client, SAMPLE_MESSAGE["id"], None) is True
        assert len(respx.calls) == 0

    @respx.mock
    def test_on_encodes_the_id_and_selects_the_sender_fields(self, monkeypatch):
        _on(monkeypatch)
        route = respx.get(url__startswith=f"{GRAPH_BASE_URL}/me/messages/").mock(
            return_value=httpx.Response(200, json=SAMPLE_SENDER_ONLY_INTERNAL)
        )
        with GraphClient("tok") as client:
            assert mail_policy.check_message(client, SAMPLE_AWKWARD_MESSAGE_ID, None) is True

        url = str(route.calls[0].request.url)
        assert f"/me/messages/{ENCODED_AWKWARD_ID}" in url
        assert "%24select=id%2Cfrom%2Csender" in url or "$select=id,from,sender" in url

    @respx.mock
    def test_external_sender_is_refused(self, monkeypatch):
        _on(monkeypatch)
        respx.get(url__startswith=f"{GRAPH_BASE_URL}/me/messages/").mock(
            return_value=httpx.Response(200, json=SAMPLE_SENDER_ONLY_EXTERNAL)
        )
        with GraphClient("tok") as client:
            assert mail_policy.check_message(client, SAMPLE_MESSAGE["id"], None) is False

    @respx.mock
    def test_mailbox_is_threaded_into_the_path(self, monkeypatch):
        _on(monkeypatch)
        route = respx.get(url__startswith=f"{GRAPH_BASE_URL}/users/").mock(
            return_value=httpx.Response(200, json=SAMPLE_SENDER_ONLY_INTERNAL)
        )
        with GraphClient("tok") as client:
            assert (
                mail_policy.check_message(client, SAMPLE_MESSAGE["id"], "support@example.com")
                is True
            )

        assert route.calls[0].request.url.path.startswith(
            "/v1.0/users/support@example.com/messages/"
        )

    @respx.mock
    def test_graph_error_propagates(self, monkeypatch):
        _on(monkeypatch)
        respx.get(url__startswith=f"{GRAPH_BASE_URL}/me/messages/").mock(
            return_value=httpx.Response(404, json=GRAPH_ERROR_404)
        )
        with GraphClient("tok") as client:
            with pytest.raises(GraphError):
                mail_policy.check_message(client, SAMPLE_MESSAGE["id"], None)
