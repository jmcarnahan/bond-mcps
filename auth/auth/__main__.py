"""Entry point: python -m auth [--port 8000] | python -m auth status

Port resolution: --port flag > BOND_AUTH_PROXY_PORT env var > 8000. Matches
auth.proxy_client.OAuthProxyClient so the server and clients agree on the port
when an override is set via the env var (e.g. via the repo-root Makefile).
"""

import argparse
import os
import sys

from auth.proxy_server import run_proxy

# Computed at module-import time. Correct for production (`python -m auth` is
# a fresh process) but tests that monkeypatch the env var must
# `importlib.reload(auth.__main__)` to pick up the new value.
DEFAULT_PORT = int(os.environ.get("BOND_AUTH_PROXY_PORT", "8000"))


def main():
    parser = argparse.ArgumentParser(description="Bond MCPs OAuth callback proxy")
    sub = parser.add_subparsers(dest="command")

    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--host", default="127.0.0.1")

    status_parser = sub.add_parser("status", help="Check if the proxy is running")
    status_parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    status_parser.add_argument("--host", default="127.0.0.1")

    args = parser.parse_args()

    if args.command == "status":
        from auth.proxy_client import OAuthProxyClient

        client = OAuthProxyClient(port=args.port, host=args.host)
        if client._health_check():
            print(f"Auth proxy is running on {args.host}:{args.port}")
        else:
            print(f"Auth proxy is NOT running on {args.host}:{args.port}")
            sys.exit(1)
    else:
        run_proxy(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
