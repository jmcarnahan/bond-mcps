"""Dynamic discovery of the MCP servers available in this project.

bond-ai (and other consumers) should not be hard-configured with the list of
MCP servers and their endpoints. Instead they call a single unauthenticated
REST endpoint (``GET /connections/discovery`` on the auth proxy) that returns
the MCPs available here. Everything beyond the endpoint URL — tools, auth,
capabilities — the consumer learns via the MCP protocol's ``initialize`` call.

Discovery is filesystem-based and dynamic: each MCP self-describes with a tiny
``mcp.json`` in its own ``mcps/<name>/`` directory. Dropping in a new MCP
directory (with an ``mcp.json``) makes it appear; removing the directory makes
it disappear. No central registry to edit.

``mcp.json`` schema::

    {
      "name": "ms-graph",         # required — stable identifier
      "display_name": "Microsoft", # optional — human label (defaults to name)
      "port": 18001,               # required — local HTTP port
      "path": "/mcp"               # optional — mount path (defaults to /mcp)
    }

The presence of ``mcp.json`` is the opt-in marker: directories without it are
skipped, so non-MCP directories need no special-casing.

Scope: this is a local / dev-checkout feature. It needs the ``mcps/`` directory
present on disk, which a deployed/installed copy of the ``auth`` package does
not have — in that case discovery returns an empty list rather than scanning
somewhere meaningless.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

MANIFEST_NAME = "mcp.json"
DEFAULT_PATH = "/mcp"
DEFAULT_BASE_HOST = "http://localhost"

# Env override pointing directly at the directory that contains the per-MCP
# subdirectories. Primarily a testing seam, but also lets a non-standard
# checkout layout work without code changes.
ENV_MCPS_DIR = "BOND_MCPS_MCPS_DIR"

# Markers that identify a real dev checkout at the resolved repo root. Mirrors
# the same idea in ``auth/auth/db/session.py`` so an installed wheel (where
# ``mcps/`` does not exist next to the package) degrades gracefully instead of
# scanning into site-packages.
_DEV_CHECKOUT_MARKERS = ("Makefile", "pyproject.toml")


def _mcps_dir() -> Path | None:
    """Resolve the ``mcps/`` directory, or ``None`` if it can't be found.

    Resolution order:
      1. ``BOND_MCPS_MCPS_DIR`` env var, if set and a directory.
      2. ``<repo-root>/mcps`` where repo root is two parents up from this file
         (``<repo>/auth/auth/discovery.py`` → ``<repo>``), but only when the
         repo root looks like a real dev checkout.
    """
    override = os.environ.get(ENV_MCPS_DIR, "").strip()
    if override:
        path = Path(override)
        return path if path.is_dir() else None

    repo_root = Path(__file__).resolve().parents[2]
    if not any((repo_root / marker).exists() for marker in _DEV_CHECKOUT_MARKERS):
        return None
    candidate = repo_root / "mcps"
    return candidate if candidate.is_dir() else None


def _load_manifest(manifest_path: Path, base_host: str) -> dict | None:
    """Parse one ``mcp.json`` into a discovery entry, or ``None`` if invalid.

    Invalid manifests are logged and skipped so a single bad file never breaks
    discovery for every other MCP.
    """
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("Skipping unreadable MCP manifest %s: %s", manifest_path, exc)
        return None

    if not isinstance(data, dict):
        logger.warning("Skipping MCP manifest %s: not a JSON object", manifest_path)
        return None

    name = data.get("name")
    port = data.get("port")
    if not isinstance(name, str) or not name.strip():
        logger.warning("Skipping MCP manifest %s: missing/invalid 'name'", manifest_path)
        return None
    if not isinstance(port, int) or isinstance(port, bool):
        logger.warning("Skipping MCP manifest %s: missing/invalid 'port'", manifest_path)
        return None

    name = name.strip()
    path = data.get("path") or DEFAULT_PATH
    if not isinstance(path, str) or not path.startswith("/"):
        logger.warning("Skipping MCP manifest %s: invalid 'path'", manifest_path)
        return None

    display_name = data.get("display_name")
    if not isinstance(display_name, str) or not display_name.strip():
        display_name = name

    return {
        "name": name,
        "display_name": display_name,
        "url": f"{base_host}:{port}{path}",
    }


def discover_mcps(mcps_dir: Path | None = None, base_host: str | None = None) -> list[dict]:
    """Return the MCP servers available in this project.

    Args:
        mcps_dir: Directory containing per-MCP subdirectories. Defaults to the
            resolved ``mcps/`` directory (see :func:`_mcps_dir`).
        base_host: Scheme+host the endpoint URLs are built from. Defaults to
            ``http://localhost`` (local dev).

    Returns:
        A list of ``{"name", "display_name", "url"}`` dicts, sorted by name.
        Empty if the ``mcps/`` directory can't be found or contains no valid
        manifests.
    """
    base_host = (base_host or DEFAULT_BASE_HOST).rstrip("/")
    directory = mcps_dir if mcps_dir is not None else _mcps_dir()
    if directory is None or not directory.is_dir():
        return []

    entries: list[dict] = []
    for child in directory.iterdir():
        if not child.is_dir() or child.name.startswith("."):
            continue
        manifest_path = child / MANIFEST_NAME
        if not manifest_path.is_file():
            continue
        entry = _load_manifest(manifest_path, base_host)
        if entry is not None:
            entries.append(entry)

    entries.sort(key=lambda e: e["name"])
    return entries
