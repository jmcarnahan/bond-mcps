"""``python -m auth.auth_server`` — run the AS standalone via uvicorn."""

from __future__ import annotations

import argparse
import os
from pathlib import Path


def _load_dotenv_if_present() -> None:
    """Mirror the MCPs' behaviour: load ``auth/.env`` at startup so operators
    don't have to maintain a parallel set of inline ``Makefile`` exports."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    # auth/auth/auth_server/__main__.py  →  auth/.env
    env_file = Path(__file__).resolve().parents[2] / ".env"
    if env_file.exists():
        load_dotenv(env_file)


def main() -> None:
    _load_dotenv_if_present()

    from auth.auth_server import run

    parser = argparse.ArgumentParser(description="bond-mcps OAuth 2.1 Authorization Server")
    parser.add_argument(
        "--host",
        default=os.environ.get("BOND_MCPS_AS_HOST", "127.0.0.1"),
        help="Bind host (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("BOND_MCPS_AS_PORT", "8001")),
        help="Bind port (default: 8001)",
    )
    args = parser.parse_args()
    run(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
