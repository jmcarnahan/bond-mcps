#!/usr/bin/env python3
"""
GitHub CLI -- interact with GitHub using device code flow.

Usage:
    export GH_CLIENT_ID=<your-github-oauth-app-client-id>

    python github_cli.py repos list [--type all] [--sort updated] [--per-page 30]
    python github_cli.py repos get <owner> <repo>
    python github_cli.py repos search <query>

    python github_cli.py issues list <owner> <repo> [--state open]
    python github_cli.py issues get <owner> <repo> <number>
    python github_cli.py issues create <owner> <repo> <title> [--body "..."]

    python github_cli.py pulls list <owner> <repo> [--state open]
    python github_cli.py pulls get <owner> <repo> <number>

    python github_cli.py code get <owner> <repo> <path>
    python github_cli.py code search <query>

    python github_cli.py user
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

import httpx

from github.github_client import GitHubClient, GitHubError
from github import repos, issues, pulls, code

TOKEN_CACHE_PATH = Path.home() / ".github_mcp_tokens.json"

# GitHub OAuth scopes
SCOPES = "repo user read:org"


def _get_token() -> str:
    """Authenticate using GitHub device code flow with token caching."""
    # Try cached token first
    if TOKEN_CACHE_PATH.exists():
        try:
            cache = json.loads(TOKEN_CACHE_PATH.read_text())
            token = cache.get("access_token")
            if token:
                # Verify the token still works
                with GitHubClient(token) as client:
                    try:
                        client.get("/user")
                        return token
                    except (httpx.HTTPError, GitHubError):
                        pass  # Token expired or revoked, re-authenticate
        except (json.JSONDecodeError, OSError):
            pass  # Corrupt or unreadable cache file, re-authenticate

    client_id = os.environ.get("GH_CLIENT_ID")
    if not client_id:
        print("Error: GH_CLIENT_ID environment variable is required.", file=sys.stderr)
        sys.exit(1)

    # Step 1: Request device code
    with httpx.Client(timeout=30.0) as http:
        resp = http.post(
            "https://github.com/login/device/code",
            data={"client_id": client_id, "scope": SCOPES},
            headers={"Accept": "application/json"},
        )
        resp.raise_for_status()
        device_data = resp.json()

    user_code = device_data.get("user_code")
    verification_uri = device_data.get("verification_uri")
    device_code = device_data.get("device_code")
    interval = device_data.get("interval", 5)
    expires_in = device_data.get("expires_in", 900)

    print(f"\nTo authenticate, visit: {verification_uri}")
    print(f"Enter code: {user_code}\n")
    print(f"Waiting for authorization (expires in {expires_in}s)...")

    # Step 2: Poll for access token
    deadline = time.time() + expires_in
    with httpx.Client(timeout=30.0) as http:
        while time.time() < deadline:
            time.sleep(interval)
            resp = http.post(
                "https://github.com/login/oauth/access_token",
                data={
                    "client_id": client_id,
                    "device_code": device_code,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                },
                headers={"Accept": "application/json"},
            )
            resp.raise_for_status()
            token_data = resp.json()

            error = token_data.get("error")
            if error == "authorization_pending":
                continue
            elif error == "slow_down":
                interval = token_data.get("interval", interval + 5)
                continue
            elif error:
                print(f"Authentication failed: {token_data.get('error_description', error)}", file=sys.stderr)
                sys.exit(1)

            access_token = token_data.get("access_token")
            if access_token:
                # Cache the token
                TOKEN_CACHE_PATH.write_text(json.dumps({"access_token": access_token}))
                TOKEN_CACHE_PATH.chmod(0o600)
                print("Authenticated successfully!\n")
                return access_token

    print("Authentication timed out.", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Repository commands
# ---------------------------------------------------------------------------

def cmd_repos_list(args: argparse.Namespace) -> None:
    token = _get_token()
    with GitHubClient(token) as client:
        repo_list = repos.list_repos(client, type=args.type, sort=args.sort, per_page=args.per_page)

    if not repo_list:
        print("No repositories found.")
        return

    print(f"Repositories ({len(repo_list)}):\n")
    for r in repo_list:
        vis = "private" if r.get("private") else "public"
        lang = r.get("language") or "—"
        print(f"  {r.get('full_name', '?')}  ({vis}, {lang}, {r.get('stargazers_count', 0)} stars)")


def cmd_repos_get(args: argparse.Namespace) -> None:
    token = _get_token()
    with GitHubClient(token) as client:
        r = repos.get_repo(client, args.owner, args.repo)

    print(f"Name: {r.get('full_name', '?')}")
    print(f"Description: {r.get('description') or 'None'}")
    print(f"Language: {r.get('language') or '—'}")
    print(f"Stars: {r.get('stargazers_count', 0)} | Forks: {r.get('forks_count', 0)}")
    print(f"Default Branch: {r.get('default_branch', '?')}")
    print(f"URL: {r.get('html_url', '?')}")


def cmd_repos_search(args: argparse.Namespace) -> None:
    token = _get_token()
    with GitHubClient(token) as client:
        results = repos.search_repos(client, query=args.query, per_page=args.per_page)

    if not results:
        print("No repositories found.")
        return

    print(f"Search results ({len(results)}):\n")
    for r in results:
        lang = r.get("language") or "—"
        print(f"  {r.get('full_name', '?')}  ({lang}, {r.get('stargazers_count', 0)} stars)")
        desc = r.get("description") or ""
        if desc:
            print(f"    {desc[:80]}")


# ---------------------------------------------------------------------------
# Issue commands
# ---------------------------------------------------------------------------

def cmd_issues_list(args: argparse.Namespace) -> None:
    token = _get_token()
    with GitHubClient(token) as client:
        issue_list = issues.list_issues(client, args.owner, args.repo, state=args.state, per_page=args.per_page)

    # Filter out PRs
    issue_list = [i for i in issue_list if "pull_request" not in i]

    if not issue_list:
        print("No issues found.")
        return

    print(f"Issues ({len(issue_list)}):\n")
    for i in issue_list:
        print(f"  #{i.get('number', '?')} {i.get('title', '?')} [{i.get('state', '?')}]")


def cmd_issues_get(args: argparse.Namespace) -> None:
    token = _get_token()
    with GitHubClient(token) as client:
        issue = issues.get_issue(client, args.owner, args.repo, args.number)

    print(f"#{issue.get('number', '?')} {issue.get('title', '?')}")
    print(f"State: {issue.get('state', '?')}")
    print(f"Author: @{issue.get('user', {}).get('login', '?')}")
    print(f"Created: {issue.get('created_at', '?')}")
    print(f"URL: {issue.get('html_url', '?')}")
    print()
    print(issue.get("body") or "(no description)")


def cmd_issues_create(args: argparse.Namespace) -> None:
    token = _get_token()
    with GitHubClient(token) as client:
        issue = issues.create_issue(client, args.owner, args.repo, title=args.title, body=args.body or "")

    print(f"Created issue #{issue.get('number', '?')}: {issue.get('title', '?')}")
    print(f"URL: {issue.get('html_url', '?')}")


# ---------------------------------------------------------------------------
# Pull request commands
# ---------------------------------------------------------------------------

def cmd_pulls_list(args: argparse.Namespace) -> None:
    token = _get_token()
    with GitHubClient(token) as client:
        pr_list = pulls.list_pulls(client, args.owner, args.repo, state=args.state, per_page=args.per_page)

    if not pr_list:
        print("No pull requests found.")
        return

    print(f"Pull Requests ({len(pr_list)}):\n")
    for pr in pr_list:
        draft = " [DRAFT]" if pr.get("draft") else ""
        print(f"  #{pr.get('number', '?')} {pr.get('title', '?')}{draft}")


def cmd_pulls_get(args: argparse.Namespace) -> None:
    token = _get_token()
    with GitHubClient(token) as client:
        pr = pulls.get_pull(client, args.owner, args.repo, args.number)

    print(f"#{pr.get('number', '?')} {pr.get('title', '?')}")
    print(f"State: {pr.get('state', '?')}")
    print(f"Author: @{pr.get('user', {}).get('login', '?')}")
    print(f"Branch: {pr.get('head', {}).get('ref', '?')} -> {pr.get('base', {}).get('ref', '?')}")
    print(f"Changed Files: {pr.get('changed_files', '?')} | +{pr.get('additions', '?')} -{pr.get('deletions', '?')}")
    print(f"URL: {pr.get('html_url', '?')}")
    print()
    print(pr.get("body") or "(no description)")


# ---------------------------------------------------------------------------
# Code commands
# ---------------------------------------------------------------------------

def cmd_code_get(args: argparse.Namespace) -> None:
    token = _get_token()
    with GitHubClient(token) as client:
        data = code.get_file_content(client, args.owner, args.repo, args.path, ref=args.ref or "")

    if isinstance(data, list):
        print(f"Directory ({len(data)} items):\n")
        for item in data:
            print(f"  [{item.get('type', '?')}] {item.get('name', '?')}")
        return

    content = data.get("decoded_content", "")
    if content:
        print(f"--- {data.get('name', '?')} ({data.get('size', 0)} bytes) ---\n")
        print(content)
    else:
        print(f"{data.get('name', '?')}: binary file, cannot display.")
        print(f"Download: {data.get('download_url', '?')}")


def cmd_code_search(args: argparse.Namespace) -> None:
    token = _get_token()
    with GitHubClient(token) as client:
        results = code.search_code(client, query=args.query, per_page=args.per_page)

    if not results:
        print("No code found.")
        return

    print(f"Code search results ({len(results)}):\n")
    for r in results:
        repo_name = r.get("repository", {}).get("full_name", "?")
        print(f"  {r.get('name', '?')} in {repo_name}")
        print(f"    Path: {r.get('path', '?')}")
        print(f"    URL: {r.get('html_url', '?')}")
        print()


# ---------------------------------------------------------------------------
# User command
# ---------------------------------------------------------------------------

def cmd_user(args: argparse.Namespace) -> None:
    token = _get_token()
    with GitHubClient(token) as client:
        user = code.get_authenticated_user(client)

    print(f"Login: @{user.get('login', '?')}")
    print(f"Name: {user.get('name') or '—'}")
    print(f"Email: {user.get('email') or '—'}")
    print(f"Company: {user.get('company') or '—'}")
    print(f"Public Repos: {user.get('public_repos', 0)}")
    print(f"Followers: {user.get('followers', 0)} | Following: {user.get('following', 0)}")
    print(f"URL: {user.get('html_url', '?')}")


def main() -> None:
    parser = argparse.ArgumentParser(description="GitHub CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    # repos
    p_repos = sub.add_parser("repos", help="Repository operations")
    repos_sub = p_repos.add_subparsers(dest="repos_command", required=True)

    p_rlist = repos_sub.add_parser("list", help="List your repositories")
    p_rlist.add_argument("--type", default="all", choices=["all", "owner", "public", "private", "member"])
    p_rlist.add_argument("--sort", default="updated", choices=["created", "updated", "pushed", "full_name"])
    p_rlist.add_argument("--per-page", type=int, default=30)
    p_rlist.set_defaults(func=cmd_repos_list)

    p_rget = repos_sub.add_parser("get", help="Get repository details")
    p_rget.add_argument("owner")
    p_rget.add_argument("repo")
    p_rget.set_defaults(func=cmd_repos_get)

    p_rsearch = repos_sub.add_parser("search", help="Search repositories")
    p_rsearch.add_argument("query")
    p_rsearch.add_argument("--per-page", type=int, default=10)
    p_rsearch.set_defaults(func=cmd_repos_search)

    # issues
    p_issues = sub.add_parser("issues", help="Issue operations")
    issues_sub = p_issues.add_subparsers(dest="issues_command", required=True)

    p_ilist = issues_sub.add_parser("list", help="List issues")
    p_ilist.add_argument("owner")
    p_ilist.add_argument("repo")
    p_ilist.add_argument("--state", default="open", choices=["open", "closed", "all"])
    p_ilist.add_argument("--per-page", type=int, default=30)
    p_ilist.set_defaults(func=cmd_issues_list)

    p_iget = issues_sub.add_parser("get", help="Get issue details")
    p_iget.add_argument("owner")
    p_iget.add_argument("repo")
    p_iget.add_argument("number", type=int)
    p_iget.set_defaults(func=cmd_issues_get)

    p_icreate = issues_sub.add_parser("create", help="Create an issue")
    p_icreate.add_argument("owner")
    p_icreate.add_argument("repo")
    p_icreate.add_argument("title")
    p_icreate.add_argument("--body", default="")
    p_icreate.set_defaults(func=cmd_issues_create)

    # pulls
    p_pulls = sub.add_parser("pulls", help="Pull request operations")
    pulls_sub = p_pulls.add_subparsers(dest="pulls_command", required=True)

    p_plist = pulls_sub.add_parser("list", help="List pull requests")
    p_plist.add_argument("owner")
    p_plist.add_argument("repo")
    p_plist.add_argument("--state", default="open", choices=["open", "closed", "all"])
    p_plist.add_argument("--per-page", type=int, default=30)
    p_plist.set_defaults(func=cmd_pulls_list)

    p_pget = pulls_sub.add_parser("get", help="Get pull request details")
    p_pget.add_argument("owner")
    p_pget.add_argument("repo")
    p_pget.add_argument("number", type=int)
    p_pget.set_defaults(func=cmd_pulls_get)

    # code
    p_code = sub.add_parser("code", help="Code operations")
    code_sub = p_code.add_subparsers(dest="code_command", required=True)

    p_cget = code_sub.add_parser("get", help="Get file content")
    p_cget.add_argument("owner")
    p_cget.add_argument("repo")
    p_cget.add_argument("path")
    p_cget.add_argument("--ref", default="")
    p_cget.set_defaults(func=cmd_code_get)

    p_csearch = code_sub.add_parser("search", help="Search code")
    p_csearch.add_argument("query")
    p_csearch.add_argument("--per-page", type=int, default=10)
    p_csearch.set_defaults(func=cmd_code_search)

    # user
    p_user = sub.add_parser("user", help="Get authenticated user info")
    p_user.set_defaults(func=cmd_user)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
