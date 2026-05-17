"""Entry point: python -m auth [--port 8000] | python -m auth status"""

import argparse
import sys

from auth.proxy_server import run_proxy


def main():
    parser = argparse.ArgumentParser(description="Bond AI OAuth callback proxy")
    sub = parser.add_subparsers(dest="command")

    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1")

    status_parser = sub.add_parser("status", help="Check if the proxy is running")
    status_parser.add_argument("--port", type=int, default=8000)
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
