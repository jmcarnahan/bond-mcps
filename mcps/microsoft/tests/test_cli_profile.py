"""CLI parity for ``whoami``, including another user's profile and photo.

The CLI is its own caller boundary: it decides whether to read ``/me`` or the
directory, validates ``--photo-size`` before a token is ever fetched, and
writes the photo to disk itself. These tests call ``cmd_whoami`` directly with
an argparse.Namespace and assert on stdout, the request trail and the file on
disk. The load-bearing case is the missing directory scope: a deployment
without ``User.ReadBasic.All`` must print one line and nothing else, so every
Graph read happens before the first print. A regression there would show up as
half a profile followed by an error, which no other assertion would catch.
"""

import argparse
from pathlib import Path
from unittest.mock import patch

import httpx
import ms_graph_cli
import pytest
import respx
from ms_graph.graph_client import GRAPH_BASE_URL

from .conftest import (
    GRAPH_ERROR_403,
    GRAPH_ERROR_404,
    SAMPLE_DIRECTORY_USER,
    SAMPLE_MAILBOX_SETTINGS,
    SAMPLE_USER_PROFILE,
)

ME_URL = f"{GRAPH_BASE_URL}/me"
MAILBOX_SETTINGS_URL = f"{GRAPH_BASE_URL}/me/mailboxSettings"
USER_ID = SAMPLE_DIRECTORY_USER["id"]
USER_URL = f"{GRAPH_BASE_URL}/users/{USER_ID}"

# Photo routes are registered with EXACT urls: '/me/photo' is a prefix of
# '/me/photos/240x240', so a url__startswith route would swallow the wrong one.
ME_PHOTO_240_URL = f"{GRAPH_BASE_URL}/me/photos/240x240/$value"
ME_PHOTO_96_URL = f"{GRAPH_BASE_URL}/me/photos/96x96/$value"
ME_PHOTO_ORIGINAL_URL = f"{GRAPH_BASE_URL}/me/photo/$value"
USER_PHOTO_240_URL = f"{GRAPH_BASE_URL}/users/{USER_ID}/photos/240x240/$value"

GRAPH_ERROR_400 = {"error": {"code": "Request_BadRequest", "message": "Invalid object identifier"}}

PNG_BYTES = b"\x89PNG\r\n\x1a\n-fake-png"

SCOPE_MISSING_LINE = (
    "Could not retrieve the profile: this connection lacks the User.ReadBasic.All permission.\n"
)

ME_PROFILE_OUT = (
    "Display Name:       Test User\n"
    "Mail:               user@example.com\n"
    "User Principal:     user@example.com\n"
    "Mailbox Address:    mailbox@example.com\n"
    "ID:                 user-id-001\n"
)


@pytest.fixture(autouse=True)
def local_token():
    """The CLI reads its token from the local auth proxy; short-circuit it."""
    with patch("ms_graph_cli.get_local_token", return_value="tok") as mock:
        yield mock


def _ns(**kw) -> argparse.Namespace:
    return argparse.Namespace(**{"user": "", "photo": "", "photo_size": "", **kw})


def _trail() -> list[tuple[str, str]]:
    """(method, path) for every request the command made, in order."""
    return [(call.request.method, call.request.url.path) for call in respx.calls]


def _mock_me():
    """Serve the two requests mail.get_profile makes."""
    respx.get(ME_URL).mock(return_value=httpx.Response(200, json=SAMPLE_USER_PROFILE))
    respx.get(MAILBOX_SETTINGS_URL).mock(
        return_value=httpx.Response(200, json=SAMPLE_MAILBOX_SETTINGS)
    )


def _mock_user(response: httpx.Response | None = None):
    """Serve the directory lookup (the url carries a $select query)."""
    return respx.get(url__startswith=f"{USER_URL}?").mock(
        return_value=response or httpx.Response(200, json=SAMPLE_DIRECTORY_USER)
    )


class TestCliWhoami:
    @respx.mock
    def test_plain_whoami_output_unchanged(self, capsys):
        _mock_me()

        ms_graph_cli.cmd_whoami(_ns())

        assert capsys.readouterr().out == ME_PROFILE_OUT
        assert _trail() == [("GET", "/v1.0/me"), ("GET", "/v1.0/me/mailboxSettings")]
        assert not any("photo" in path for _, path in _trail())

    @respx.mock
    def test_user_prints_directory_profile_without_mailbox_settings(self, capsys):
        _mock_user()

        ms_graph_cli.cmd_whoami(_ns(user=USER_ID))

        out = capsys.readouterr().out
        assert "Display Name:       Ada Lovelace" in out
        assert "Job Title:          Engineer" in out
        assert "ID:                 user-id-002" in out
        assert "Mailbox Address" not in out
        assert _trail() == [("GET", f"/v1.0/users/{USER_ID}")]
        assert not any("photo" in path for _, path in _trail())

    @respx.mock
    def test_photo_writes_bytes_and_prints_saved_line(self, capsys, tmp_path):
        _mock_me()
        respx.get(ME_PHOTO_240_URL).mock(
            return_value=httpx.Response(
                200, content=PNG_BYTES, headers={"Content-Type": "image/png"}
            )
        )
        out_path = tmp_path / "x.jpg"

        ms_graph_cli.cmd_whoami(_ns(photo=str(out_path)))

        assert out_path.read_bytes() == PNG_BYTES
        lines = capsys.readouterr().out.rstrip("\n").split("\n")
        assert lines[-1] == f"Saved {len(PNG_BYTES)} bytes to {out_path} (image/png, 240x240)"
        trail = _trail()
        assert trail[-1] == ("GET", "/v1.0/me/photos/240x240/$value")
        # The metadata endpoint is never touched: bytes answer "no photo" too.
        assert ("GET", "/v1.0/me/photo") not in trail

    @respx.mock
    def test_photo_size_original_uses_photo_value_endpoint(self, capsys, tmp_path):
        _mock_me()
        respx.get(ME_PHOTO_ORIGINAL_URL).mock(
            return_value=httpx.Response(
                200, content=PNG_BYTES, headers={"Content-Type": "image/png"}
            )
        )
        out_path = tmp_path / "x.jpg"

        ms_graph_cli.cmd_whoami(_ns(photo=str(out_path), photo_size="original"))

        assert out_path.read_bytes() == PNG_BYTES
        assert capsys.readouterr().out.rstrip("\n").endswith("(image/png, original)")
        assert _trail()[-1] == ("GET", "/v1.0/me/photo/$value")

    @respx.mock
    def test_photo_size_case_insensitive(self, capsys, tmp_path):
        _mock_me()
        respx.get(ME_PHOTO_96_URL).mock(
            return_value=httpx.Response(
                200, content=PNG_BYTES, headers={"Content-Type": "image/png"}
            )
        )
        out_path = tmp_path / "x.jpg"

        ms_graph_cli.cmd_whoami(_ns(photo=str(out_path), photo_size="96X96"))

        assert capsys.readouterr().out.rstrip("\n").endswith("(image/png, 96x96)")
        assert _trail()[-1] == ("GET", "/v1.0/me/photos/96x96/$value")

    @respx.mock
    def test_photo_404_prints_no_photo_and_exits_without_file(self, capsys, tmp_path):
        _mock_me()
        respx.get(ME_PHOTO_240_URL).mock(return_value=httpx.Response(404, json=GRAPH_ERROR_404))
        out_path = tmp_path / "x.jpg"

        with pytest.raises(SystemExit) as exc:
            ms_graph_cli.cmd_whoami(_ns(photo=str(out_path)))

        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert out == ME_PROFILE_OUT + "No profile photo set.\n"
        assert not out_path.exists()

    @respx.mock
    def test_bad_photo_size_exits_before_any_request(self, capsys, local_token):
        with pytest.raises(SystemExit) as exc:
            ms_graph_cli.cmd_whoami(_ns(photo="x.jpg", photo_size="100x100"))

        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "photo_size must be one of" in out
        assert "100x100" in out
        assert len(respx.calls) == 0
        assert local_token.called is False

    @respx.mock
    def test_photo_size_without_photo_exits_before_any_request(self, capsys, local_token):
        with pytest.raises(SystemExit) as exc:
            ms_graph_cli.cmd_whoami(_ns(photo_size="96x96"))

        assert exc.value.code == 1
        assert capsys.readouterr().out == "--photo-size only applies with --photo\n"
        assert len(respx.calls) == 0
        assert local_token.called is False

    @respx.mock
    def test_user_403_prints_scope_message_only(self, capsys):
        _mock_user(httpx.Response(403, json=GRAPH_ERROR_403))

        with pytest.raises(SystemExit) as exc:
            ms_graph_cli.cmd_whoami(_ns(user=USER_ID))

        assert exc.value.code == 1
        assert capsys.readouterr().out == SCOPE_MISSING_LINE

    @respx.mock
    def test_user_photo_403_prints_scope_message_only_and_writes_nothing(self, capsys, tmp_path):
        """The lookup succeeds and the photo 403s: still one line, no profile."""
        _mock_user()
        respx.get(USER_PHOTO_240_URL).mock(return_value=httpx.Response(403, json=GRAPH_ERROR_403))
        out_path = tmp_path / "x.jpg"

        with pytest.raises(SystemExit) as exc:
            ms_graph_cli.cmd_whoami(_ns(user=USER_ID, photo=str(out_path)))

        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert out == SCOPE_MISSING_LINE
        assert "Display Name" not in out
        assert not out_path.exists()
        assert _trail() == [
            ("GET", f"/v1.0/users/{USER_ID}"),
            ("GET", f"/v1.0/users/{USER_ID}/photos/240x240/$value"),
        ]

    @respx.mock
    def test_me_photo_403_prints_scope_message_only(self, capsys, tmp_path):
        """A tenant policy can 403 the signed-in user's own photo."""
        _mock_me()
        respx.get(ME_PHOTO_240_URL).mock(return_value=httpx.Response(403, json=GRAPH_ERROR_403))
        out_path = tmp_path / "x.jpg"

        with pytest.raises(SystemExit) as exc:
            ms_graph_cli.cmd_whoami(_ns(photo=str(out_path)))

        assert exc.value.code == 1
        assert capsys.readouterr().out == SCOPE_MISSING_LINE
        assert not out_path.exists()

    @respx.mock
    def test_user_404_prints_user_not_found(self, capsys, tmp_path):
        _mock_user(httpx.Response(404, json=GRAPH_ERROR_404))
        out_path = tmp_path / "x.jpg"

        with pytest.raises(SystemExit) as exc:
            ms_graph_cli.cmd_whoami(_ns(user=USER_ID, photo=str(out_path)))

        assert exc.value.code == 1
        assert capsys.readouterr().out == f"User not found: {USER_ID}\n"
        assert _trail() == [("GET", f"/v1.0/users/{USER_ID}")]
        assert not out_path.exists()

    @respx.mock
    def test_user_400_prints_invalid_user(self, capsys):
        _mock_user(httpx.Response(400, json=GRAPH_ERROR_400))

        with pytest.raises(SystemExit) as exc:
            ms_graph_cli.cmd_whoami(_ns(user=USER_ID))

        assert exc.value.code == 1
        assert capsys.readouterr().out.startswith("Invalid user:")

    @respx.mock
    def test_photo_content_type_header_missing_prints_unknown_type(self, capsys, tmp_path):
        """httpx adds no Content-Type of its own, so the header can be absent."""
        _mock_me()
        respx.get(ME_PHOTO_240_URL).mock(return_value=httpx.Response(200, content=PNG_BYTES))
        out_path = tmp_path / "x.jpg"

        ms_graph_cli.cmd_whoami(_ns(photo=str(out_path)))

        assert capsys.readouterr().out.rstrip("\n").endswith("(unknown type, 240x240)")
        assert Path(out_path).read_bytes() == PNG_BYTES
