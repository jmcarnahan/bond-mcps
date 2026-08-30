"""Tests for mail operations (sync and async)."""

import json

import httpx
import pytest
import respx
from ms_graph import mail
from ms_graph.graph_client import GRAPH_BASE_URL, AsyncGraphClient, GraphClient, GraphError
from ms_graph.mail import _detect_body_type

from .conftest import (
    GRAPH_ERROR_400,
    GRAPH_ERROR_403,
    GRAPH_ERROR_410,
    SAMPLE_AWKWARD_MESSAGE_ID,
    SAMPLE_DELTA_LINK,
    SAMPLE_DELTA_NEXT_LINK,
    SAMPLE_DELTA_PAGE_FINAL,
    SAMPLE_DELTA_PAGE_NEXT,
    SAMPLE_MAILBOX_SETTINGS,
    SAMPLE_MESSAGE,
    SAMPLE_MESSAGE_DETAIL,
    SAMPLE_MESSAGES_RESPONSE,
    SAMPLE_REPLY_DRAFT,
    SAMPLE_USER_PROFILE,
)


class TestProfileSync:
    """Synchronous profile operation tests."""

    @respx.mock
    def test_get_profile_with_mailbox_settings(self):
        respx.get(f"{GRAPH_BASE_URL}/me").mock(
            return_value=httpx.Response(200, json=SAMPLE_USER_PROFILE)
        )
        respx.get(f"{GRAPH_BASE_URL}/me/mailboxSettings").mock(
            return_value=httpx.Response(200, json=SAMPLE_MAILBOX_SETTINGS)
        )
        with GraphClient("tok") as client:
            profile = mail.get_profile(client)

        assert profile["displayName"] == "Test User"
        assert profile["mail"] == "user@example.com"
        assert profile["mailboxAddress"] == "mailbox@example.com"

    @respx.mock
    def test_get_profile_without_mailbox_settings_scope(self):
        """When MailboxSettings.Read scope is not granted, mailboxAddress is absent."""
        respx.get(f"{GRAPH_BASE_URL}/me").mock(
            return_value=httpx.Response(200, json=SAMPLE_USER_PROFILE)
        )
        respx.get(f"{GRAPH_BASE_URL}/me/mailboxSettings").mock(
            return_value=httpx.Response(
                403, json={"error": {"code": "ErrorAccessDenied", "message": "Access denied"}}
            )
        )
        with GraphClient("tok") as client:
            profile = mail.get_profile(client)

        assert profile["displayName"] == "Test User"
        assert "mailboxAddress" not in profile


class TestProfileAsync:
    """Async profile operation tests."""

    @respx.mock
    async def test_aget_profile_with_mailbox_settings(self):
        respx.get(f"{GRAPH_BASE_URL}/me").mock(
            return_value=httpx.Response(200, json=SAMPLE_USER_PROFILE)
        )
        respx.get(f"{GRAPH_BASE_URL}/me/mailboxSettings").mock(
            return_value=httpx.Response(200, json=SAMPLE_MAILBOX_SETTINGS)
        )
        async with AsyncGraphClient("tok") as client:
            profile = await mail.aget_profile(client)

        assert profile["displayName"] == "Test User"
        assert profile["mailboxAddress"] == "mailbox@example.com"

    @respx.mock
    async def test_aget_profile_without_mailbox_settings_scope(self):
        """When MailboxSettings.Read scope is not granted, mailboxAddress is absent."""
        respx.get(f"{GRAPH_BASE_URL}/me").mock(
            return_value=httpx.Response(200, json=SAMPLE_USER_PROFILE)
        )
        respx.get(f"{GRAPH_BASE_URL}/me/mailboxSettings").mock(
            return_value=httpx.Response(
                403, json={"error": {"code": "ErrorAccessDenied", "message": "Access denied"}}
            )
        )
        async with AsyncGraphClient("tok") as client:
            profile = await mail.aget_profile(client)

        assert profile["displayName"] == "Test User"
        assert "mailboxAddress" not in profile


class TestMailSync:
    """Synchronous mail operation tests."""

    @respx.mock
    def test_list_messages(self):
        respx.get(f"{GRAPH_BASE_URL}/me/mailFolders/inbox/messages").mock(
            return_value=httpx.Response(200, json=SAMPLE_MESSAGES_RESPONSE)
        )
        with GraphClient("tok") as client:
            messages = mail.list_messages(client)

        assert len(messages) == 2
        assert messages[0]["subject"] == "Weekly Report"

    @respx.mock
    def test_list_messages_custom_folder(self):
        route = respx.get(f"{GRAPH_BASE_URL}/me/mailFolders/sentitems/messages").mock(
            return_value=httpx.Response(200, json={"value": []})
        )
        with GraphClient("tok") as client:
            messages = mail.list_messages(client, folder="sentitems", top=5)

        assert messages == []
        assert "top=5" in str(route.calls[0].request.url)

    @respx.mock
    def test_get_message(self):
        msg_id = "AAMkAGI2TG93AAA="
        respx.get(f"{GRAPH_BASE_URL}/me/messages/{msg_id}").mock(
            return_value=httpx.Response(200, json=SAMPLE_MESSAGE)
        )
        with GraphClient("tok") as client:
            msg = mail.get_message(client, msg_id)

        assert msg["subject"] == "Weekly Report"
        assert msg["body"]["content"].startswith("Here is")

    @respx.mock
    def test_send_message(self):
        route = respx.post(f"{GRAPH_BASE_URL}/me/sendMail").mock(return_value=httpx.Response(202))
        with GraphClient("tok") as client:
            mail.send_message(
                client,
                to=["alice@example.com"],
                subject="Hello",
                body="Hi Alice!",
            )

        assert route.called
        payload = json.loads(route.calls[0].request.content)
        assert payload["message"]["subject"] == "Hello"
        assert len(payload["message"]["toRecipients"]) == 1
        assert (
            payload["message"]["toRecipients"][0]["emailAddress"]["address"] == "alice@example.com"
        )

    @respx.mock
    def test_send_message_with_cc(self):
        route = respx.post(f"{GRAPH_BASE_URL}/me/sendMail").mock(return_value=httpx.Response(202))
        with GraphClient("tok") as client:
            mail.send_message(
                client,
                to=["alice@example.com"],
                subject="Hello",
                body="Hi!",
                cc=["bob@example.com"],
            )

        payload = json.loads(route.calls[0].request.content)
        assert len(payload["message"]["ccRecipients"]) == 1
        assert payload["message"]["ccRecipients"][0]["emailAddress"]["address"] == "bob@example.com"

    @respx.mock
    def test_send_message_with_from_address(self):
        route = respx.post(f"{GRAPH_BASE_URL}/me/sendMail").mock(return_value=httpx.Response(202))
        with GraphClient("tok") as client:
            mail.send_message(
                client,
                to=["alice@example.com"],
                subject="Hello",
                body="Hi!",
                from_address="mailbox@example.com",
            )

        payload = json.loads(route.calls[0].request.content)
        assert payload["message"]["from"]["emailAddress"]["address"] == "mailbox@example.com"

    @respx.mock
    def test_send_message_without_from_address(self):
        route = respx.post(f"{GRAPH_BASE_URL}/me/sendMail").mock(return_value=httpx.Response(202))
        with GraphClient("tok") as client:
            mail.send_message(
                client,
                to=["alice@example.com"],
                subject="Hello",
                body="Hi!",
            )

        payload = json.loads(route.calls[0].request.content)
        assert "from" not in payload["message"]

    @respx.mock
    def test_search_messages(self):
        respx.get(f"{GRAPH_BASE_URL}/me/messages").mock(
            return_value=httpx.Response(200, json=SAMPLE_MESSAGES_RESPONSE)
        )
        with GraphClient("tok") as client:
            results = mail.search_messages(client, "weekly report")

        assert len(results) == 2


class TestMailAsync:
    """Async mail operation tests."""

    @respx.mock
    async def test_alist_messages(self):
        respx.get(f"{GRAPH_BASE_URL}/me/mailFolders/inbox/messages").mock(
            return_value=httpx.Response(200, json=SAMPLE_MESSAGES_RESPONSE)
        )
        async with AsyncGraphClient("tok") as client:
            messages = await mail.alist_messages(client)

        assert len(messages) == 2

    @respx.mock
    async def test_aget_message(self):
        msg_id = "AAMkAGI2TG93AAA="
        respx.get(f"{GRAPH_BASE_URL}/me/messages/{msg_id}").mock(
            return_value=httpx.Response(200, json=SAMPLE_MESSAGE)
        )
        async with AsyncGraphClient("tok") as client:
            msg = await mail.aget_message(client, msg_id)

        assert msg["subject"] == "Weekly Report"

    @respx.mock
    async def test_asend_message(self):
        route = respx.post(f"{GRAPH_BASE_URL}/me/sendMail").mock(return_value=httpx.Response(202))
        async with AsyncGraphClient("tok") as client:
            await mail.asend_message(
                client,
                to=["alice@example.com"],
                subject="Async Hello",
                body="Async body",
            )

        assert route.called

    @respx.mock
    async def test_asend_message_with_from_address(self):
        route = respx.post(f"{GRAPH_BASE_URL}/me/sendMail").mock(return_value=httpx.Response(202))
        async with AsyncGraphClient("tok") as client:
            await mail.asend_message(
                client,
                to=["alice@example.com"],
                subject="Async Hello",
                body="Async body",
                from_address="mailbox@example.com",
            )

        payload = json.loads(route.calls[0].request.content)
        assert payload["message"]["from"]["emailAddress"]["address"] == "mailbox@example.com"

    @respx.mock
    async def test_asearch_messages(self):
        respx.get(f"{GRAPH_BASE_URL}/me/messages").mock(
            return_value=httpx.Response(200, json={"value": [SAMPLE_MESSAGE]})
        )
        async with AsyncGraphClient("tok") as client:
            results = await mail.asearch_messages(client, "report")

        assert len(results) == 1


# ---------------------------------------------------------------------------
# _detect_body_type — comprehensive parametrized coverage
# ---------------------------------------------------------------------------

# (body, expected_result, description)
_DETECT_CASES = [
    # --- Full HTML documents ---
    ("<html><body><h1>Title</h1><p>Content</p></body></html>", "HTML", "full HTML document"),
    ("<!DOCTYPE html><html><body><p>x</p></body></html>", "HTML", "doctype + html doc"),
    # --- Common inline fragments (the primary use case) ---
    ("<p>Hello <strong>world</strong>!</p>", "HTML", "p + strong fragment"),
    ("<strong>bold text</strong>", "HTML", "strong tag only"),
    ("<em>italic</em>", "HTML", "em tag"),
    ("<b>bold</b> and <i>italic</i>", "HTML", "b and i tags"),
    ('<a href="https://example.com">Click here</a>', "HTML", "anchor with href"),
    ('<a href="https://example.com">link</a> for info', "HTML", "anchor in sentence"),
    ("Line 1<br>Line 2", "HTML", "br tag inline"),
    ("Line 1<br/>Line 2", "HTML", "br self-closing"),
    ("Line 1<br />Line 2", "HTML", "br with space"),
    ("<p>Para 1</p><p>Para 2</p>", "HTML", "multiple paragraphs"),
    ("<ul><li>Item 1</li><li>Item 2</li></ul>", "HTML", "unordered list"),
    ("<ol><li>First</li><li>Second</li></ol>", "HTML", "ordered list"),
    ("<div>section</div>", "HTML", "div block"),
    ('<div class="foo">styled</div>', "HTML", "div with class attr"),
    ('<img src="pic.png" alt="photo">', "HTML", "img tag"),
    ("<table><tr><td>cell</td></tr></table>", "HTML", "table"),
    ("<h1>Heading</h1><p>Body text.</p>", "HTML", "h1 + p"),
    ("<h2>Sub</h2><p>text</p>", "HTML", "h2"),
    ("Run <code>git pull</code> now", "HTML", "code inline"),
    ("<pre>def foo():\n    pass</pre>", "HTML", "pre block"),
    ("<blockquote>quoted text</blockquote>", "HTML", "blockquote"),
    ("<hr>", "HTML", "hr divider"),
    ("<span>inline</span>", "HTML", "span"),
    ("<del>removed</del> <ins>added</ins>", "HTML", "del + ins"),
    ("<mark>highlighted</mark>", "HTML", "mark"),
    ("<sup>1</sup> footnote", "HTML", "sup"),
    ("<sub>2</sub>", "HTML", "sub"),
    ("<small>fine print</small>", "HTML", "small"),
    # --- Case insensitivity ---
    ("<P>uppercase tag</P>", "HTML", "uppercase P tag"),
    ("<STRONG>BOLD</STRONG>", "HTML", "uppercase STRONG"),
    ("<Div>Mixed case</Div>", "HTML", "mixed case Div"),
    ("<A HREF='x'>link</A>", "HTML", "uppercase A HREF"),
    ("<BR>", "HTML", "uppercase BR"),
    # --- Mixed content (HTML embedded in prose) ---
    ("Please see <strong>Section 3</strong> for details.", "HTML", "strong in sentence"),
    ("Visit <a href='x'>our site</a> for more.", "HTML", "anchor in prose"),
    ("Hi,\n\nSee <p>this paragraph</p>\n\nThanks", "HTML", "p in multiline body"),
    # --- Unicode content with HTML ---
    ("<p>Héllo Wörld</p>", "HTML", "unicode chars with p tag"),
    ("<strong>日本語</strong>", "HTML", "CJK with strong"),
    # --- Plain text: should never trigger ---
    ("Hello world, plain text.", "Text", "simple plain text"),
    ("", "Text", "empty string"),
    ("   \n  \t  ", "Text", "whitespace only"),
    ("No markup here at all.", "Text", "prose, no markup"),
    ("Line 1\nLine 2\nLine 3", "Text", "multiline plain"),
    # --- False-positive traps: angle brackets that are NOT HTML ---
    ("Dear <FirstName>,", "Text", "angle-bracket placeholder name"),
    ("Hello <Alice>!", "Text", "angle-bracket name in greeting"),
    ("Hi <username>, your token is <token>", "Text", "template-style placeholders"),
    ("if x<y and z>w return true", "Text", "math comparison operators"),
    ("if (x<y) { return; }", "Text", "code: x<y comparison"),
    ("price < $100 and discount > 10%", "Text", "less-than/greater-than in prose"),
    ("5 < 10", "Text", "numeric comparison"),
    ("Result should be <expected", "Text", "single unclosed angle"),
    ("<foo>bar</foo>", "Text", "made-up XML tag not in whitelist"),
    ("<note>reminder</note>", "Text", "XML-style note tag"),
    ("<CustomTag>value</CustomTag>", "Text", "non-HTML custom element"),
    ("<Vector<int>>", "Text", "C++ template syntax"),
    # --- Markdown (must stay Text) ---
    ("**bold** and *italic*", "Text", "markdown bold/italic"),
    ("# Heading\n\nParagraph text.", "Text", "markdown heading"),
    ("[click here](https://example.com)", "Text", "markdown link"),
    ("Visit https://example.com for more.", "Text", "plain URL"),
    ("> quoted block", "Text", "markdown blockquote"),
    ("- item one\n- item two", "Text", "markdown list"),
    # --- Large bodies ---
    ("x" * 100_000, "Text", "100k char plain text"),
    ("<p>" + "x" * 100_000 + "</p>", "HTML", "100k char wrapped in p"),
]


@pytest.mark.parametrize(
    "body,expected,description", _DETECT_CASES, ids=[c[2] for c in _DETECT_CASES]
)
def test_detect_body_type(body, expected, description):
    assert _detect_body_type(body) == expected


# ---------------------------------------------------------------------------
# body_type parameter — sync send_message
# ---------------------------------------------------------------------------


class TestBodyTypeParameterSync:
    """Tests for body_type parameter in sync send_message."""

    @respx.mock
    def test_auto_detects_html_fragment(self):
        route = respx.post(f"{GRAPH_BASE_URL}/me/sendMail").mock(return_value=httpx.Response(202))
        with GraphClient("tok") as client:
            mail.send_message(
                client,
                to=["alice@example.com"],
                subject="HTML email",
                body="<p>Hello <strong>Alice</strong>!</p>",
            )

        payload = json.loads(route.calls[0].request.content)
        assert payload["message"]["body"]["contentType"] == "HTML"

    @respx.mock
    def test_auto_detects_anchor_link(self):
        """Regression: anchor tags must be detected so links render as hyperlinks."""
        route = respx.post(f"{GRAPH_BASE_URL}/me/sendMail").mock(return_value=httpx.Response(202))
        with GraphClient("tok") as client:
            mail.send_message(
                client,
                to=["alice@example.com"],
                subject="Link email",
                body='Click <a href="https://example.com">here</a>.',
            )

        payload = json.loads(route.calls[0].request.content)
        assert payload["message"]["body"]["contentType"] == "HTML"

    @respx.mock
    def test_auto_detects_plain_text(self):
        route = respx.post(f"{GRAPH_BASE_URL}/me/sendMail").mock(return_value=httpx.Response(202))
        with GraphClient("tok") as client:
            mail.send_message(
                client,
                to=["alice@example.com"],
                subject="Plain email",
                body="Hello Alice, this is plain text.",
            )

        payload = json.loads(route.calls[0].request.content)
        assert payload["message"]["body"]["contentType"] == "Text"

    @respx.mock
    def test_auto_placeholder_not_mistaken_for_html(self):
        """'Dear <FirstName>,' must stay Text — not be mis-sent as HTML which would strip the name."""
        route = respx.post(f"{GRAPH_BASE_URL}/me/sendMail").mock(return_value=httpx.Response(202))
        with GraphClient("tok") as client:
            mail.send_message(
                client,
                to=["alice@example.com"],
                subject="Template email",
                body="Dear <FirstName>, thanks for reaching out.",
            )

        payload = json.loads(route.calls[0].request.content)
        assert payload["message"]["body"]["contentType"] == "Text"

    @respx.mock
    def test_auto_math_comparison_stays_text(self):
        """Bodies with < > as comparison operators must not be mis-classified as HTML."""
        route = respx.post(f"{GRAPH_BASE_URL}/me/sendMail").mock(return_value=httpx.Response(202))
        with GraphClient("tok") as client:
            mail.send_message(
                client,
                to=["alice@example.com"],
                subject="Analysis",
                body="Revenue grew 12%, since x<y and z>w we can conclude...",
            )

        payload = json.loads(route.calls[0].request.content)
        assert payload["message"]["body"]["contentType"] == "Text"

    @respx.mock
    def test_explicit_html_overrides_plain_body(self):
        route = respx.post(f"{GRAPH_BASE_URL}/me/sendMail").mock(return_value=httpx.Response(202))
        with GraphClient("tok") as client:
            mail.send_message(
                client,
                to=["alice@example.com"],
                subject="Force HTML",
                body="plain text body",
                body_type="HTML",
            )

        payload = json.loads(route.calls[0].request.content)
        assert payload["message"]["body"]["contentType"] == "HTML"

    @respx.mock
    def test_explicit_text_overrides_html_body(self):
        route = respx.post(f"{GRAPH_BASE_URL}/me/sendMail").mock(return_value=httpx.Response(202))
        with GraphClient("tok") as client:
            mail.send_message(
                client,
                to=["alice@example.com"],
                subject="Force Text",
                body="<p>HTML content</p>",
                body_type="Text",
            )

        payload = json.loads(route.calls[0].request.content)
        assert payload["message"]["body"]["contentType"] == "Text"

    def test_invalid_body_type_raises(self):
        with pytest.raises(ValueError, match="body_type"):
            with GraphClient("tok") as client:
                mail.send_message(
                    client,
                    to=["alice@example.com"],
                    subject="Test",
                    body="body",
                    body_type="html",  # lowercase — invalid
                )

    def test_invalid_body_type_xml_raises(self):
        with pytest.raises(ValueError, match="body_type"):
            with GraphClient("tok") as client:
                mail.send_message(
                    client,
                    to=["alice@example.com"],
                    subject="Test",
                    body="body",
                    body_type="XML",
                )


# ---------------------------------------------------------------------------
# body_type parameter — async asend_message
# ---------------------------------------------------------------------------


class TestBodyTypeParameterAsync:
    """Tests for body_type parameter in async asend_message."""

    @respx.mock
    async def test_auto_detects_html_fragment(self):
        route = respx.post(f"{GRAPH_BASE_URL}/me/sendMail").mock(return_value=httpx.Response(202))
        async with AsyncGraphClient("tok") as client:
            await mail.asend_message(
                client,
                to=["alice@example.com"],
                subject="HTML email",
                body="<p>Hello <strong>Alice</strong>!</p>",
            )

        payload = json.loads(route.calls[0].request.content)
        assert payload["message"]["body"]["contentType"] == "HTML"

    @respx.mock
    async def test_auto_detects_anchor_link(self):
        """Regression: anchor tags must be detected so links render as hyperlinks."""
        route = respx.post(f"{GRAPH_BASE_URL}/me/sendMail").mock(return_value=httpx.Response(202))
        async with AsyncGraphClient("tok") as client:
            await mail.asend_message(
                client,
                to=["alice@example.com"],
                subject="Link email",
                body='Click <a href="https://example.com">here</a>.',
            )

        payload = json.loads(route.calls[0].request.content)
        assert payload["message"]["body"]["contentType"] == "HTML"

    @respx.mock
    async def test_auto_detects_plain_text(self):
        route = respx.post(f"{GRAPH_BASE_URL}/me/sendMail").mock(return_value=httpx.Response(202))
        async with AsyncGraphClient("tok") as client:
            await mail.asend_message(
                client,
                to=["alice@example.com"],
                subject="Plain email",
                body="Hello Alice, this is plain text.",
            )

        payload = json.loads(route.calls[0].request.content)
        assert payload["message"]["body"]["contentType"] == "Text"

    @respx.mock
    async def test_auto_placeholder_not_mistaken_for_html(self):
        """'Dear <FirstName>,' must stay Text."""
        route = respx.post(f"{GRAPH_BASE_URL}/me/sendMail").mock(return_value=httpx.Response(202))
        async with AsyncGraphClient("tok") as client:
            await mail.asend_message(
                client,
                to=["alice@example.com"],
                subject="Template email",
                body="Dear <FirstName>, thanks for reaching out.",
            )

        payload = json.loads(route.calls[0].request.content)
        assert payload["message"]["body"]["contentType"] == "Text"

    @respx.mock
    async def test_explicit_html_overrides_plain_body(self):
        route = respx.post(f"{GRAPH_BASE_URL}/me/sendMail").mock(return_value=httpx.Response(202))
        async with AsyncGraphClient("tok") as client:
            await mail.asend_message(
                client,
                to=["alice@example.com"],
                subject="Force HTML",
                body="plain text body",
                body_type="HTML",
            )

        payload = json.loads(route.calls[0].request.content)
        assert payload["message"]["body"]["contentType"] == "HTML"

    @respx.mock
    async def test_explicit_text_overrides_html_body(self):
        route = respx.post(f"{GRAPH_BASE_URL}/me/sendMail").mock(return_value=httpx.Response(202))
        async with AsyncGraphClient("tok") as client:
            await mail.asend_message(
                client,
                to=["alice@example.com"],
                subject="Force Text",
                body="<p>HTML content</p>",
                body_type="Text",
            )

        payload = json.loads(route.calls[0].request.content)
        assert payload["message"]["body"]["contentType"] == "Text"

    async def test_invalid_body_type_raises(self):
        with pytest.raises(ValueError, match="body_type"):
            async with AsyncGraphClient("tok") as client:
                await mail.asend_message(
                    client,
                    to=["alice@example.com"],
                    subject="Test",
                    body="body",
                    body_type="text",  # lowercase — invalid
                )


# ---------------------------------------------------------------------------
# Desktop JSON operations
# ---------------------------------------------------------------------------

# The exact query string the fresh-start delta must produce. Spelled out
# literally rather than derived from mail.DELTA_SELECT so that a change to the
# select list has to be made deliberately in two places.
EXPECTED_DELTA_SELECT = (
    "id%2CinternetMessageId%2CconversationId%2Csubject%2Cfrom%2CtoRecipients"
    "%2CreceivedDateTime%2CisRead%2CisDraft%2CbodyPreview"
)

DELTA_URL = f"{GRAPH_BASE_URL}/me/mailFolders/inbox/messages/delta"


class TestDeltaPageSync:
    """Synchronous mail delta tests."""

    @respx.mock
    def test_fresh_start_sends_exact_select(self):
        route = respx.get(url__startswith=DELTA_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_DELTA_PAGE_NEXT)
        )
        with GraphClient("tok") as client:
            data = mail.delta_page(client, folder="inbox")

        assert data == SAMPLE_DELTA_PAGE_NEXT
        url = str(route.calls[0].request.url)
        assert f"$select={EXPECTED_DELTA_SELECT}" in url
        assert "$filter" not in url

    @respx.mock
    def test_min_received_filter_uses_percent_20_not_plus(self):
        """Graph's OData parser rejects '+' as a space in $filter."""
        route = respx.get(url__startswith=DELTA_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_DELTA_PAGE_NEXT)
        )
        with GraphClient("tok") as client:
            mail.delta_page(client, folder="inbox", min_received="2026-01-01T00:00:00Z")

        url = str(route.calls[0].request.url)
        assert "$filter=receivedDateTime%20ge%202026-01-01T00%3A00%3A00Z" in url
        assert "+" not in url

    @respx.mock
    def test_folder_is_url_encoded(self):
        route = respx.get(url__startswith=f"{GRAPH_BASE_URL}/me/mailFolders/").mock(
            return_value=httpx.Response(200, json=SAMPLE_DELTA_PAGE_FINAL)
        )
        with GraphClient("tok") as client:
            mail.delta_page(client, folder="AQMkA/DE+F=")

        assert "/me/mailFolders/AQMkA%2FDE%2BF%3D/messages/delta" in str(route.calls[0].request.url)

    @respx.mock
    def test_cursor_is_fetched_verbatim(self):
        """nextLink/deltaLink are absolute URLs with opaque tokens — never rebuild them."""
        route = respx.get(SAMPLE_DELTA_NEXT_LINK).mock(
            return_value=httpx.Response(200, json=SAMPLE_DELTA_PAGE_FINAL)
        )
        with GraphClient("tok") as client:
            data = mail.delta_page(client, folder="inbox", cursor=SAMPLE_DELTA_NEXT_LINK)

        assert data == SAMPLE_DELTA_PAGE_FINAL
        assert str(route.calls[0].request.url) == SAMPLE_DELTA_NEXT_LINK

    @respx.mock
    def test_cursor_wins_over_folder_and_min_received(self):
        route = respx.get(SAMPLE_DELTA_NEXT_LINK).mock(
            return_value=httpx.Response(200, json=SAMPLE_DELTA_PAGE_FINAL)
        )
        with GraphClient("tok") as client:
            mail.delta_page(
                client,
                folder="archive",
                cursor=SAMPLE_DELTA_NEXT_LINK,
                min_received="2026-01-01T00:00:00Z",
            )

        assert route.call_count == 1
        assert str(route.calls[0].request.url) == SAMPLE_DELTA_NEXT_LINK

    @respx.mock
    def test_fetches_one_page_only(self):
        """A nextLink in the response must NOT be followed by the op itself."""
        route = respx.get(url__startswith=DELTA_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_DELTA_PAGE_NEXT)
        )
        with GraphClient("tok") as client:
            mail.delta_page(client, folder="inbox")

        assert route.call_count == 1


class TestDeltaPageAsync:
    """Async mail delta tests."""

    @respx.mock
    async def test_fresh_start_sends_exact_select(self):
        route = respx.get(url__startswith=DELTA_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_DELTA_PAGE_NEXT)
        )
        async with AsyncGraphClient("tok") as client:
            data = await mail.adelta_page(client, folder="inbox")

        assert data == SAMPLE_DELTA_PAGE_NEXT
        assert f"$select={EXPECTED_DELTA_SELECT}" in str(route.calls[0].request.url)

    @respx.mock
    async def test_min_received_filter_uses_percent_20_not_plus(self):
        route = respx.get(url__startswith=DELTA_URL).mock(
            return_value=httpx.Response(200, json=SAMPLE_DELTA_PAGE_NEXT)
        )
        async with AsyncGraphClient("tok") as client:
            await mail.adelta_page(client, folder="inbox", min_received="2026-01-01T00:00:00Z")

        url = str(route.calls[0].request.url)
        assert "%20ge%20" in url
        assert "+" not in url

    @respx.mock
    async def test_cursor_is_fetched_verbatim(self):
        route = respx.get(SAMPLE_DELTA_LINK).mock(
            return_value=httpx.Response(200, json=SAMPLE_DELTA_PAGE_FINAL)
        )
        async with AsyncGraphClient("tok") as client:
            data = await mail.adelta_page(client, cursor=SAMPLE_DELTA_LINK)

        assert data == SAMPLE_DELTA_PAGE_FINAL
        assert str(route.calls[0].request.url) == SAMPLE_DELTA_LINK

    @respx.mock
    async def test_410_propagates_to_caller(self):
        """The op does not swallow a stale cursor — the tool layer maps it to resync."""
        respx.get(SAMPLE_DELTA_LINK).mock(return_value=httpx.Response(410, json=GRAPH_ERROR_410))
        async with AsyncGraphClient("tok") as client:
            with pytest.raises(GraphError) as exc:
                await mail.adelta_page(client, cursor=SAMPLE_DELTA_LINK)

        assert exc.value.status_code == 410


class TestGetMessageDetailSync:
    """Synchronous message detail tests."""

    @respx.mock
    def test_sends_prefer_text_body_header(self):
        route = respx.get(url__startswith=f"{GRAPH_BASE_URL}/me/messages/").mock(
            return_value=httpx.Response(200, json=SAMPLE_MESSAGE_DETAIL)
        )
        with GraphClient("tok") as client:
            data = mail.get_message_detail(client, SAMPLE_MESSAGE["id"])

        assert data == SAMPLE_MESSAGE_DETAIL
        req = route.calls[0].request
        assert req.headers["prefer"] == 'outlook.body-content-type="text"'
        assert "$select=id%2CuniqueBody%2CinternetMessageHeaders%2ChasAttachments" in str(req.url)

    @respx.mock
    def test_message_id_is_url_encoded(self):
        route = respx.get(url__startswith=f"{GRAPH_BASE_URL}/me/messages/").mock(
            return_value=httpx.Response(200, json=SAMPLE_MESSAGE_DETAIL)
        )
        with GraphClient("tok") as client:
            mail.get_message_detail(client, SAMPLE_AWKWARD_MESSAGE_ID)

        assert "/me/messages/AAMkA%2FGI2%2BTG93AAA%3D?" in str(route.calls[0].request.url)


class TestGetMessageDetailAsync:
    """Async message detail tests."""

    @respx.mock
    async def test_sends_prefer_text_body_header(self):
        route = respx.get(url__startswith=f"{GRAPH_BASE_URL}/me/messages/").mock(
            return_value=httpx.Response(200, json=SAMPLE_MESSAGE_DETAIL)
        )
        async with AsyncGraphClient("tok") as client:
            data = await mail.aget_message_detail(client, SAMPLE_MESSAGE["id"])

        assert data == SAMPLE_MESSAGE_DETAIL
        assert route.calls[0].request.headers["prefer"] == 'outlook.body-content-type="text"'


class TestCreateReplyDraftSync:
    """Synchronous reply-draft tests."""

    @respx.mock
    def test_no_timezone_sends_no_prefer_header(self):
        route = respx.post(url__startswith=f"{GRAPH_BASE_URL}/me/messages/").mock(
            return_value=httpx.Response(201, json=SAMPLE_REPLY_DRAFT)
        )
        with GraphClient("tok") as client:
            draft = mail.create_reply_draft(client, SAMPLE_MESSAGE["id"])

        assert draft == SAMPLE_REPLY_DRAFT
        assert route.call_count == 1
        assert "prefer" not in route.calls[0].request.headers

    @respx.mock
    def test_timezone_sends_prefer_header(self):
        route = respx.post(url__startswith=f"{GRAPH_BASE_URL}/me/messages/").mock(
            return_value=httpx.Response(201, json=SAMPLE_REPLY_DRAFT)
        )
        with GraphClient("tok") as client:
            mail.create_reply_draft(client, SAMPLE_MESSAGE["id"], timezone="America/New_York")

        assert route.call_count == 1
        assert route.calls[0].request.headers["prefer"] == 'outlook.timezone="America/New_York"'

    @respx.mock
    def test_400_retries_once_without_prefer_header(self):
        route = respx.post(url__startswith=f"{GRAPH_BASE_URL}/me/messages/").mock(
            side_effect=[
                httpx.Response(400, json=GRAPH_ERROR_400),
                httpx.Response(201, json=SAMPLE_REPLY_DRAFT),
            ]
        )
        with GraphClient("tok") as client:
            draft = mail.create_reply_draft(client, SAMPLE_MESSAGE["id"], timezone="Bad/Zone")

        assert draft == SAMPLE_REPLY_DRAFT
        assert route.call_count == 2
        assert route.calls[0].request.headers["prefer"] == 'outlook.timezone="Bad/Zone"'
        assert "prefer" not in route.calls[1].request.headers

    @respx.mock
    def test_400_without_timezone_propagates(self):
        """No Prefer header was sent, so there is nothing to retry without."""
        route = respx.post(url__startswith=f"{GRAPH_BASE_URL}/me/messages/").mock(
            return_value=httpx.Response(400, json=GRAPH_ERROR_400)
        )
        with GraphClient("tok") as client:
            with pytest.raises(GraphError) as exc:
                mail.create_reply_draft(client, SAMPLE_MESSAGE["id"])

        assert exc.value.status_code == 400
        assert route.call_count == 1

    @respx.mock
    def test_non_400_error_is_not_retried(self):
        route = respx.post(url__startswith=f"{GRAPH_BASE_URL}/me/messages/").mock(
            return_value=httpx.Response(403, json=GRAPH_ERROR_403)
        )
        with GraphClient("tok") as client:
            with pytest.raises(GraphError) as exc:
                mail.create_reply_draft(client, SAMPLE_MESSAGE["id"], timezone="UTC")

        assert exc.value.status_code == 403
        assert route.call_count == 1


class TestCreateReplyDraftAsync:
    """Async reply-draft tests."""

    @respx.mock
    async def test_timezone_sends_prefer_header(self):
        route = respx.post(url__startswith=f"{GRAPH_BASE_URL}/me/messages/").mock(
            return_value=httpx.Response(201, json=SAMPLE_REPLY_DRAFT)
        )
        async with AsyncGraphClient("tok") as client:
            draft = await mail.acreate_reply_draft(
                client, SAMPLE_MESSAGE["id"], timezone="Europe/London"
            )

        assert draft == SAMPLE_REPLY_DRAFT
        assert route.calls[0].request.headers["prefer"] == 'outlook.timezone="Europe/London"'

    @respx.mock
    async def test_400_retries_once_without_prefer_header(self):
        route = respx.post(url__startswith=f"{GRAPH_BASE_URL}/me/messages/").mock(
            side_effect=[
                httpx.Response(400, json=GRAPH_ERROR_400),
                httpx.Response(201, json=SAMPLE_REPLY_DRAFT),
            ]
        )
        async with AsyncGraphClient("tok") as client:
            draft = await mail.acreate_reply_draft(
                client, SAMPLE_MESSAGE["id"], timezone="Bad/Zone"
            )

        assert draft == SAMPLE_REPLY_DRAFT
        assert route.call_count == 2
        assert "prefer" not in route.calls[1].request.headers

    @respx.mock
    async def test_message_id_is_url_encoded(self):
        route = respx.post(url__startswith=f"{GRAPH_BASE_URL}/me/messages/").mock(
            return_value=httpx.Response(201, json=SAMPLE_REPLY_DRAFT)
        )
        async with AsyncGraphClient("tok") as client:
            await mail.acreate_reply_draft(client, SAMPLE_AWKWARD_MESSAGE_ID)

        assert "/me/messages/AAMkA%2FGI2%2BTG93AAA%3D/createReply" in str(
            route.calls[0].request.url
        )


class TestDraftBodyAndSend:
    """Draft update and send tests (sync and async)."""

    @respx.mock
    def test_update_draft_body_patch_shape(self):
        route = respx.patch(f"{GRAPH_BASE_URL}/me/messages/AAMkAGI2draft001%3D").mock(
            return_value=httpx.Response(200, json=SAMPLE_REPLY_DRAFT)
        )
        with GraphClient("tok") as client:
            mail.update_draft_body(client, "AAMkAGI2draft001=", "Thanks, will review.")

        payload = json.loads(route.calls[0].request.content)
        assert payload == {"body": {"contentType": "text", "content": "Thanks, will review."}}

    @respx.mock
    async def test_aupdate_draft_body_patch_shape(self):
        route = respx.patch(url__startswith=f"{GRAPH_BASE_URL}/me/messages/").mock(
            return_value=httpx.Response(200, json=SAMPLE_REPLY_DRAFT)
        )
        async with AsyncGraphClient("tok") as client:
            await mail.aupdate_draft_body(client, SAMPLE_AWKWARD_MESSAGE_ID, "Reply text")

        assert str(route.calls[0].request.url).endswith("/me/messages/AAMkA%2FGI2%2BTG93AAA%3D")
        payload = json.loads(route.calls[0].request.content)
        assert payload["body"]["contentType"] == "text"
        assert payload["body"]["content"] == "Reply text"

    @respx.mock
    def test_send_draft_accepts_202_with_no_body(self):
        route = respx.post(f"{GRAPH_BASE_URL}/me/messages/AAMkAGI2draft001%3D/send").mock(
            return_value=httpx.Response(202)
        )
        with GraphClient("tok") as client:
            assert mail.send_draft(client, "AAMkAGI2draft001=") is None

        assert route.call_count == 1

    @respx.mock
    async def test_asend_draft_accepts_202_with_no_body(self):
        route = respx.post(url__startswith=f"{GRAPH_BASE_URL}/me/messages/").mock(
            return_value=httpx.Response(202)
        )
        async with AsyncGraphClient("tok") as client:
            assert await mail.asend_draft(client, SAMPLE_AWKWARD_MESSAGE_ID) is None

        assert str(route.calls[0].request.url).endswith(
            "/me/messages/AAMkA%2FGI2%2BTG93AAA%3D/send"
        )
