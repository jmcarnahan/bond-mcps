"""CLI parity for the external-sender mail policy.

The CLI is a separate caller boundary from the MCP server: it builds its own
GraphClient and formats its own output, so the policy has to be wired into it
independently. These tests call the ``cmd_email_*`` functions directly with an
argparse.Namespace and assert on both stdout and the request trail — a refusal
that still fetched the body would leak into a log or a saved file.
"""

import argparse
from unittest.mock import patch
from urllib.parse import parse_qs

import httpx
import ms_graph_cli
import pytest
import respx
from ms_graph import mail_policy
from ms_graph.graph_client import GRAPH_BASE_URL

from .conftest import (
    EXTERNAL_SENDER_ADDRESS,
    SAMPLE_EXTERNAL_ITEM_ATTACHMENT,
    SAMPLE_EXTERNAL_ITEM_ATTACHMENT_META,
    SAMPLE_EXTERNAL_MESSAGE,
    SAMPLE_FILE_ATTACHMENT,
    SAMPLE_MESSAGE,
    SAMPLE_ONBEHALF_MESSAGE,
    SAMPLE_SENDER_ONLY_EXTERNAL,
    SAMPLE_SENDER_ONLY_INTERNAL,
)

MSG_ID = "MSG1"
ATT_ID = "ATT1"
MESSAGE_URL = f"{GRAPH_BASE_URL}/me/messages/{MSG_ID}"
ATTACHMENT_URL = f"{MESSAGE_URL}/attachments/{ATT_ID}"
INBOX_URL = f"{GRAPH_BASE_URL}/me/mailFolders/inbox/messages"
SEARCH_URL = f"{GRAPH_BASE_URL}/me/messages"


@pytest.fixture(autouse=True)
def _local_token():
    """The CLI reads its token from the local auth proxy; short-circuit it."""
    with patch("ms_graph_cli.get_local_token", return_value="tok"):
        yield


def _policy_on(monkeypatch) -> None:
    monkeypatch.setenv("MS_MAIL_ALLOWED_SENDER_DOMAINS", "example.com")


def _trail() -> list[tuple[str, str]]:
    """(method, path) for every request the command made, in order."""
    return [(call.request.method, call.request.url.path) for call in respx.calls]


def _assert_no_canary(text: str) -> None:
    """No field of a hidden message may appear in what the CLI printed."""
    for marker in ("CANARY-", "mallory", "Mallory"):
        assert marker not in text, f"{marker!r} leaked into CLI output: {text[:400]}"


def _list_args(query=None, folder="inbox", top=10) -> argparse.Namespace:
    return argparse.Namespace(query=query, folder=folder, top=top)


class TestCliEmailList:
    @respx.mock
    def test_list_hides_external_and_appends_notice(self, monkeypatch, capsys):
        _policy_on(monkeypatch)
        respx.get(INBOX_URL).mock(
            return_value=httpx.Response(
                200, json={"value": [SAMPLE_MESSAGE, SAMPLE_EXTERNAL_MESSAGE]}
            )
        )

        ms_graph_cli.cmd_email_list(_list_args())

        out = capsys.readouterr().out
        _assert_no_canary(out)
        assert "alice@example.com" in out
        assert "(1)" in out
        assert [line for line in out.splitlines() if line.strip()][-1] == mail_policy.POLICY_NOTICE

    @respx.mock
    def test_list_notice_after_no_messages_found(self, monkeypatch, capsys):
        _policy_on(monkeypatch)
        respx.get(SEARCH_URL).mock(
            return_value=httpx.Response(200, json={"value": [SAMPLE_EXTERNAL_MESSAGE]})
        )

        ms_graph_cli.cmd_email_list(_list_args(query="CANARY"))

        out = capsys.readouterr().out
        _assert_no_canary(out)
        assert out == f"No messages found.\n{mail_policy.POLICY_NOTICE}\n"

    @respx.mock
    def test_list_notice_identical_for_one_and_three_hidden(self, monkeypatch, capsys):
        """The output must not be an oracle for how much was hidden."""
        _policy_on(monkeypatch)
        respx.get(SEARCH_URL).mock(
            return_value=httpx.Response(200, json={"value": [SAMPLE_EXTERNAL_MESSAGE]})
        )
        ms_graph_cli.cmd_email_list(_list_args(query="CANARY"))
        one = capsys.readouterr().out

        respx.get(SEARCH_URL).mock(
            return_value=httpx.Response(200, json={"value": [SAMPLE_EXTERNAL_MESSAGE] * 3})
        )
        ms_graph_cli.cmd_email_list(_list_args(query="CANARY"))
        three = capsys.readouterr().out

        assert one == three

    @respx.mock
    def test_list_unchanged_when_policy_off(self, capsys):
        respx.get(INBOX_URL).mock(
            return_value=httpx.Response(
                200, json={"value": [SAMPLE_MESSAGE, SAMPLE_EXTERNAL_MESSAGE]}
            )
        )

        ms_graph_cli.cmd_email_list(_list_args())

        out = capsys.readouterr().out
        assert "alice@example.com" in out
        assert EXTERNAL_SENDER_ADDRESS in out
        assert "CANARY-SUBJECT" in out
        assert mail_policy.POLICY_NOTICE not in out


class TestCliEmailRead:
    @respx.mock
    def test_read_refuses_external_before_listing_attachments(self, monkeypatch, capsys):
        _policy_on(monkeypatch)
        respx.get(MESSAGE_URL).mock(return_value=httpx.Response(200, json=SAMPLE_EXTERNAL_MESSAGE))

        ms_graph_cli.cmd_email_read(argparse.Namespace(message_id=MSG_ID))

        out = capsys.readouterr().out
        _assert_no_canary(out)
        assert out.strip() == mail_policy.EXTERNAL_SENDER_TEXT
        assert not [path for _, path in _trail() if "/attachments" in path]

    @respx.mock
    def test_read_refuses_on_behalf_sender(self, monkeypatch, capsys):
        _policy_on(monkeypatch)
        respx.get(MESSAGE_URL).mock(return_value=httpx.Response(200, json=SAMPLE_ONBEHALF_MESSAGE))

        ms_graph_cli.cmd_email_read(argparse.Namespace(message_id=MSG_ID))

        out = capsys.readouterr().out
        _assert_no_canary(out)
        assert out.strip() == mail_policy.EXTERNAL_SENDER_TEXT

    @respx.mock
    def test_read_internal_unchanged_when_policy_on(self, monkeypatch, capsys):
        _policy_on(monkeypatch)
        respx.get(MESSAGE_URL).mock(return_value=httpx.Response(200, json=SAMPLE_MESSAGE))

        ms_graph_cli.cmd_email_read(argparse.Namespace(message_id=MSG_ID))

        out = capsys.readouterr().out
        assert "From:    Alice Smith <alice@example.com>" in out
        assert "Here is the weekly report." in out
        assert mail_policy.EXTERNAL_SENDER_TEXT not in out

    @respx.mock
    def test_read_unchanged_when_policy_off(self, capsys):
        respx.get(MESSAGE_URL).mock(return_value=httpx.Response(200, json=SAMPLE_EXTERNAL_MESSAGE))
        respx.get(f"{MESSAGE_URL}/attachments").mock(
            return_value=httpx.Response(200, json={"value": []})
        )

        ms_graph_cli.cmd_email_read(argparse.Namespace(message_id=MSG_ID))

        out = capsys.readouterr().out
        assert "CANARY-BODY" in out
        assert "CANARY-SUBJECT" in out
        assert mail_policy.EXTERNAL_SENDER_TEXT not in out


class TestCliEmailAttachment:
    @staticmethod
    def _args(tmp_path) -> argparse.Namespace:
        return argparse.Namespace(
            message_id=MSG_ID,
            attachment_id=ATT_ID,
            out=str(tmp_path / "saved.bin"),
        )

    @respx.mock
    def test_attachment_refuses_external_with_one_request(self, monkeypatch, capsys, tmp_path):
        _policy_on(monkeypatch)
        respx.get(MESSAGE_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_SENDER_ONLY_EXTERNAL)
        )

        ms_graph_cli.cmd_email_attachment(self._args(tmp_path))

        out = capsys.readouterr().out
        _assert_no_canary(out)
        assert out.strip() == mail_policy.EXTERNAL_SENDER_TEXT
        assert len(respx.calls) == 1
        request = respx.calls[0].request
        assert request.url.path == f"/v1.0/me/messages/{MSG_ID}"
        assert parse_qs(request.url.query.decode())["$select"] == [mail_policy.SENDER_SELECT]
        assert not list(tmp_path.iterdir())

    @respx.mock
    def test_attachment_refuses_external_item_attachment(self, monkeypatch, capsys, tmp_path):
        _policy_on(monkeypatch)
        respx.get(MESSAGE_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_SENDER_ONLY_INTERNAL)
        )
        respx.get(f"{ATTACHMENT_URL}/$value").mock(
            return_value=httpx.Response(200, content=b"nope")
        )

        def _attachment(request):
            # One path serves both reads; the $expand query is what tells the
            # metadata read apart from the item fetch.
            if "expand" in str(request.url):
                return httpx.Response(200, json=SAMPLE_EXTERNAL_ITEM_ATTACHMENT)
            return httpx.Response(200, json=SAMPLE_EXTERNAL_ITEM_ATTACHMENT_META)

        respx.get(url__startswith=ATTACHMENT_URL).mock(side_effect=_attachment)

        ms_graph_cli.cmd_email_attachment(self._args(tmp_path))

        out = capsys.readouterr().out
        _assert_no_canary(out)
        assert out.strip() == mail_policy.EXTERNAL_SENDER_TEXT
        assert not [path for _, path in _trail() if path.endswith("/$value")]
        assert not list(tmp_path.iterdir())

    @respx.mock
    def test_attachment_internal_file_saved_when_policy_on(self, monkeypatch, capsys, tmp_path):
        _policy_on(monkeypatch)
        respx.get(MESSAGE_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_SENDER_ONLY_INTERNAL)
        )
        respx.get(f"{ATTACHMENT_URL}/$value").mock(
            return_value=httpx.Response(
                200, content=b"hello", headers={"Content-Type": "text/plain"}
            )
        )
        respx.get(url__startswith=ATTACHMENT_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_FILE_ATTACHMENT)
        )

        args = self._args(tmp_path)
        ms_graph_cli.cmd_email_attachment(args)

        assert "Saved 5 bytes" in capsys.readouterr().out
        assert (tmp_path / "saved.bin").read_bytes() == b"hello"
        assert _trail() == [
            ("GET", f"/v1.0/me/messages/{MSG_ID}"),
            ("GET", f"/v1.0/me/messages/{MSG_ID}/attachments/{ATT_ID}"),
            ("GET", f"/v1.0/me/messages/{MSG_ID}/attachments/{ATT_ID}/$value"),
        ]

    @respx.mock
    def test_attachment_policy_off_issues_no_extra_request(self, capsys, tmp_path):
        respx.get(f"{ATTACHMENT_URL}/$value").mock(
            return_value=httpx.Response(
                200, content=b"hello", headers={"Content-Type": "text/plain"}
            )
        )
        respx.get(url__startswith=ATTACHMENT_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_FILE_ATTACHMENT)
        )

        ms_graph_cli.cmd_email_attachment(self._args(tmp_path))

        assert "Saved 5 bytes" in capsys.readouterr().out
        assert (tmp_path / "saved.bin").exists()
        assert len(respx.calls) == 2
        assert not [c for c in respx.calls if c.request.url.path == f"/v1.0/me/messages/{MSG_ID}"]
        assert not [c for c in respx.calls if "sender" in str(c.request.url)]
