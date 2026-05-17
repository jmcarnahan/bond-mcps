#!/usr/bin/env python3
"""
Temporary test script to probe Microsoft Graph Teams API capabilities.

Tests:
  1. List joined teams
  2. List channels in a team
  3. Read messages from a channel
  4. Send a message to a channel
  5. List chats (1:1, group, meeting)
  6. Read messages from a chat
  7. Send a message to a chat (1:1, group, or meeting)

Usage:
    export MS_CLIENT_ID=<your-client-id>
    export MS_CLIENT_SECRET=<your-client-secret>
    export MS_TENANT_ID=<your-tenant-id>

    cd mcps/microsoft
    poetry run python test_teams_api.py
"""

import json
import sys
import os

from dotenv import load_dotenv
load_dotenv()

from ms_graph.graph_client import GraphClient, GraphError
from ms_graph.local_auth import get_local_token


def header(title: str) -> None:
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}\n")


def api_call(client: GraphClient, method: str, path: str, **kwargs):
    """Make a Graph API call, print the result or error, and return it."""
    print(f"  >> {method} {path}")
    try:
        if method == "GET":
            result = client.get(path, **kwargs)
        elif method == "POST":
            result = client.post(path, **kwargs)
        else:
            raise ValueError(f"Unknown method: {method}")
        return result
    except GraphError as e:
        print(f"  !! ERROR: {e}")
        print(f"     Status: {e.status_code}, Code: {e.error_code}")
        return None


def test_list_teams(client: GraphClient) -> list:
    header("TEST 1: List Joined Teams")
    data = api_call(client, "GET", "/me/joinedTeams")
    if not data:
        return []

    teams = data.get("value", [])
    if not teams:
        print("  No teams found.")
        return []

    print(f"  Found {len(teams)} team(s):\n")
    for i, t in enumerate(teams):
        print(f"  [{i}] {t.get('displayName', '?')}")
        print(f"      ID: {t.get('id', '?')}")
        print(f"      Description: {t.get('description', '(none)')[:80]}")
    return teams


def test_list_channels(client: GraphClient, team_id: str, team_name: str) -> list:
    header(f"TEST 2: List Channels in '{team_name}'")
    data = api_call(client, "GET", f"/teams/{team_id}/channels")
    if not data:
        return []

    channels = data.get("value", [])
    if not channels:
        print("  No channels found.")
        return []

    print(f"  Found {len(channels)} channel(s):\n")
    for i, ch in enumerate(channels):
        membership = ch.get("membershipType", "?")
        print(f"  [{i}] {ch.get('displayName', '?')} (membership: {membership})")
        print(f"      ID: {ch.get('id', '?')}")
    return channels


def test_read_channel_messages(client: GraphClient, team_id: str, channel_id: str, channel_name: str):
    header(f"TEST 3: Read Messages from Channel '{channel_name}'")

    # This requires ChannelMessage.Read.All scope
    data = api_call(
        client, "GET",
        f"/teams/{team_id}/channels/{channel_id}/messages",
        params={"$top": "5"},
    )
    if not data:
        print("\n  NOTE: Reading channel messages requires 'ChannelMessage.Read.All' scope.")
        print("  If you got a 403, this scope needs to be added to your app registration")
        print("  and re-consented.")
        return

    messages = data.get("value", [])
    if not messages:
        print("  No messages in this channel.")
        return

    print(f"  Found {len(messages)} message(s):\n")
    for i, msg in enumerate(messages):
        sender = msg.get("from") or {}
        user = sender.get("user") or {}
        app = sender.get("application") or {}
        display_name = user.get("displayName") or app.get("displayName") or "(system)"
        msg_type = msg.get("messageType", "?")
        body = msg.get("body") or {}
        content = body.get("content", "")
        if body.get("contentType") == "html" and content:
            import re
            content = re.sub(r"<[^>]+>", "", content).strip()
        content_preview = content[:200] if content else "(no body text)"

        # Check for attachments (adaptive cards, files, etc.)
        attachments = msg.get("attachments") or []
        hosted_contents = msg.get("hostedContents") or []

        print(f"  [{i}] Type: {msg_type}")
        print(f"      From: {display_name}")
        print(f"      Date: {msg.get('createdDateTime', '?')}")
        print(f"      Body type: {body.get('contentType', '?')}")
        print(f"      Content: {content_preview}")
        if attachments:
            print(f"      Attachments ({len(attachments)}):")
            for j, att in enumerate(attachments):
                att_type = att.get("contentType", "?")
                att_name = att.get("name", "(unnamed)")
                att_content = att.get("content", "")
                att_preview = att_content[:200] if att_content else "(empty)"
                print(f"        [{j}] type={att_type}, name={att_name}")
                print(f"            content: {att_preview}")
        if hosted_contents:
            print(f"      Hosted contents: {len(hosted_contents)}")
        print(f"      ID: {msg.get('id', '?')}")

        # Also dump raw keys for debugging
        extra_keys = [k for k in msg.keys() if k not in (
            "id", "replyToId", "etag", "messageType", "createdDateTime",
            "lastModifiedDateTime", "lastEditedDateTime", "deletedDateTime",
            "subject", "summary", "chatId", "importance", "locale",
            "webUrl", "policyViolation", "eventDetail", "from", "body",
            "attachments", "mentions", "reactions", "hostedContents",
            "channelIdentity", "onBehalfOf",
        )]
        if extra_keys:
            print(f"      Extra fields: {extra_keys}")
        print()


def test_send_channel_message(client: GraphClient, team_id: str, channel_id: str, channel_name: str):
    header(f"TEST 4: Send Messages to Channel '{channel_name}'")

    print("  This test sends 2 messages: plain text and HTML formatted.\n")
    confirm = input("  Send test messages to this channel? (y/N): ").strip().lower()
    if confirm != "y":
        print("  Skipped.")
        return

    # 4a: Plain text message
    print("\n  --- 4a: Plain text message ---")
    result = api_call(
        client, "POST",
        f"/teams/{team_id}/channels/{channel_id}/messages",
        json_data={"body": {"contentType": "text", "content": "[Test] Plain text message from bond-ai test script"}},
    )
    if result:
        print(f"  SUCCESS - Message ID: {result.get('id', '?')}")

    # 4b: HTML formatted message (bold, lists, links)
    print("\n  --- 4b: HTML formatted message ---")
    html_body = (
        "<h3>Bond AI Test Message</h3>"
        "<p>This is an <b>HTML formatted</b> message with:</p>"
        "<ul>"
        "<li>Bold text</li>"
        "<li>A bullet list</li>"
        "<li>A <a href='https://learn.microsoft.com/en-us/graph/api/overview'>link</a></li>"
        "</ul>"
        "<p><em>Sent by test_teams_api.py</em></p>"
    )
    result = api_call(
        client, "POST",
        f"/teams/{team_id}/channels/{channel_id}/messages",
        json_data={"body": {"contentType": "html", "content": html_body}},
    )
    if result:
        print(f"  SUCCESS - Message ID: {result.get('id', '?')}")

    # Now re-read to see how our messages appear
    print("\n  --- Re-reading channel to verify messages ---")
    data = api_call(
        client, "GET",
        f"/teams/{team_id}/channels/{channel_id}/messages",
        params={"$top": "3"},
    )
    if data:
        for msg in data.get("value", [])[:3]:
            sender = msg.get("from") or {}
            user_info = (sender.get("user") or {})
            body = msg.get("body") or {}
            content = body.get("content", "")[:150]
            print(f"    - From: {user_info.get('displayName', '?')}, "
                  f"Type: {body.get('contentType', '?')}, "
                  f"Content: {content}")


def test_list_chats(client: GraphClient) -> list:
    header("TEST 5: List Chats (1:1, Group, Meeting)")

    # This requires Chat.Read or Chat.ReadWrite scope
    data = api_call(
        client, "GET",
        "/me/chats",
        params={"$top": "15", "$expand": "members", "$orderby": "lastMessagePreview/createdDateTime desc"},
    )
    if not data:
        print("\n  NOTE: Listing chats requires 'Chat.Read' or 'Chat.ReadWrite' scope.")
        print("  If you got a 403, add the scope to your app registration and re-consent.")
        return []

    chats = data.get("value", [])
    if not chats:
        print("  No chats found.")
        return []

    print(f"  Found {len(chats)} chat(s):\n")
    for i, chat in enumerate(chats):
        chat_type = chat.get("chatType", "?")
        topic = chat.get("topic", "(no topic)")
        chat_id = chat.get("id", "?")
        last_updated = chat.get("lastUpdatedDateTime", "?")

        # Build member list from expanded members
        members = chat.get("members", [])
        member_names = [m.get("displayName", "?") for m in members if m.get("displayName")]
        members_str = ", ".join(member_names[:5])
        if len(member_names) > 5:
            members_str += f" (+{len(member_names) - 5} more)"

        print(f"  [{i}] Type: {chat_type}")
        print(f"      Topic: {topic}")
        print(f"      Members: {members_str or '(unknown)'}")
        print(f"      Last Updated: {last_updated}")
        print(f"      ID: {chat_id[:40]}...")
        print()

    return chats


def test_read_chat_messages(client: GraphClient, chat_id: str, chat_label: str):
    header(f"TEST 6: Read Messages from Chat '{chat_label}'")

    data = api_call(
        client, "GET",
        f"/me/chats/{chat_id}/messages",
        params={"$top": "5"},
    )
    if not data:
        print("\n  NOTE: Reading chat messages requires 'Chat.Read' or 'Chat.ReadWrite' scope.")
        return

    messages = data.get("value", [])
    if not messages:
        print("  No messages in this chat.")
        return

    print(f"  Found {len(messages)} message(s):\n")
    for i, msg in enumerate(messages):
        sender = msg.get("from") or {}
        user = sender.get("user") or {}
        app = sender.get("application") or {}
        display_name = user.get("displayName") or app.get("displayName") or "(system)"
        msg_type = msg.get("messageType", "?")
        body = msg.get("body") or {}
        content = body.get("content", "")
        if body.get("contentType") == "html" and content:
            import re
            content = re.sub(r"<[^>]+>", "", content).strip()
        content_preview = content[:120] if content else "(empty)"

        print(f"  [{i}] Type: {msg_type}")
        print(f"      From: {display_name}")
        print(f"      Date: {msg.get('createdDateTime', '?')}")
        print(f"      Content: {content_preview}")
        print()


def test_send_chat_message(client: GraphClient, chat_id: str, chat_label: str):
    header(f"TEST 7: Send Message to Chat '{chat_label}'")

    confirm = input("  Send a test message to this chat? (y/N): ").strip().lower()
    if confirm != "y":
        print("  Skipped.")
        return

    test_msg = "[Test] Automated Teams API test from bond-ai test script"
    result = api_call(
        client, "POST",
        f"/me/chats/{chat_id}/messages",
        json_data={"body": {"content": test_msg}},
    )
    if result:
        print(f"  SUCCESS - Message ID: {result.get('id', '?')}")
    else:
        print("\n  NOTE: Sending chat messages requires 'Chat.ReadWrite' or 'ChatMessage.Send' scope.")


def pick_index(items: list, label: str) -> int | None:
    """Prompt user to pick an index, or None to skip."""
    if not items:
        return None
    choice = input(f"  Pick a {label} index (or Enter to skip): ").strip()
    if not choice:
        return None
    try:
        idx = int(choice)
        if 0 <= idx < len(items):
            return idx
        print(f"  Invalid index (0-{len(items)-1})")
        return None
    except ValueError:
        print("  Invalid input.")
        return None


def main():
    # Check env
    if not os.environ.get("MS_CLIENT_ID"):
        print("ERROR: Set MS_CLIENT_ID environment variable.")
        print("  export MS_CLIENT_ID=<your-client-id>")
        print("  export MS_CLIENT_SECRET=<your-client-secret>")
        print("  export MS_TENANT_ID=<your-tenant-id>")
        sys.exit(1)

    header("Microsoft Graph Teams API Test Script")
    print("  Authenticating...")
    token = get_local_token()
    client = GraphClient(token)
    print("  Authenticated successfully.\n")

    # Print current scopes info
    print("  Current token scopes determine what works.")
    print("  If tests fail with 403, the scope is missing from the token.\n")
    print("  Scopes needed for all tests:")
    print("    - Team.ReadBasic.All        (list teams)")
    print("    - Channel.ReadBasic.All     (list channels)")
    print("    - ChannelMessage.Read.All   (read channel messages) *NEW*")
    print("    - ChannelMessage.Send       (send channel messages)")
    print("    - Chat.Read                 (list & read chats)     *NEW*")
    print("    - Chat.ReadWrite            (send chat messages)    *NEW*")
    print("    - ChatMessage.Send          (send chat messages, alternative)")

    # ---------------------------------------------------------------
    # Test 1: List Teams
    # ---------------------------------------------------------------
    teams = test_list_teams(client)
    team_idx = pick_index(teams, "team")

    if team_idx is not None:
        team = teams[team_idx]
        team_id = team["id"]
        team_name = team.get("displayName", "?")

        # Test 2: List Channels
        channels = test_list_channels(client, team_id, team_name)
        ch_idx = pick_index(channels, "channel")

        if ch_idx is not None:
            channel = channels[ch_idx]
            channel_id = channel["id"]
            channel_name = channel.get("displayName", "?")

            # Test 3: Read Channel Messages
            test_read_channel_messages(client, team_id, channel_id, channel_name)

            # Test 4: Send Channel Message
            test_send_channel_message(client, team_id, channel_id, channel_name)

    # ---------------------------------------------------------------
    # Test 5-7: Chats (1:1, Group, Meeting)
    # ---------------------------------------------------------------
    chats = test_list_chats(client)
    chat_idx = pick_index(chats, "chat")

    if chat_idx is not None:
        chat = chats[chat_idx]
        chat_id = chat["id"]
        chat_type = chat.get("chatType", "?")
        chat_topic = chat.get("topic", "(no topic)")
        chat_label = f"{chat_type}: {chat_topic}"

        # Test 6: Read Chat Messages
        test_read_chat_messages(client, chat_id, chat_label)

        # Test 7: Send Chat Message
        test_send_chat_message(client, chat_id, chat_label)

    # ---------------------------------------------------------------
    # Summary
    # ---------------------------------------------------------------
    header("DONE")
    print("  Review the output above to see which API calls succeeded/failed.")
    print("  Any 403 errors indicate missing OAuth scopes that need to be")
    print("  added to the Azure app registration and re-consented.\n")
    print("  To re-consent with new scopes, delete the token cache:")
    print(f"    rm ~/.ms_graph_tokens.json\n")

    client.close()


if __name__ == "__main__":
    main()
