"""Dynamic discovery of the MCP servers available in this project.

bond-ai (and other consumers) should not be hard-configured with the list of
MCP servers and their endpoints. Instead they call a single unauthenticated
REST endpoint (``GET /connections/discovery``) that returns the MCPs available
here. Everything beyond the endpoint URL — tools, auth, capabilities — the
consumer learns via the MCP protocol's ``initialize`` call.

Two data sources, resolved in priority order:

1. **Deployment** — ``BOND_MCPS_DISCOVERY_FILE`` points at a JSON manifest
   authored at deploy time (rendered by Terraform/Helm, mounted via ConfigMap).
   Its entries carry absolute ``url``s. This is "hard-coded at deploy time, but
   dynamic": the operator/infra controls the file, no code change to add/remove
   an MCP. Served by the always-on Authorization Server in deployment.

2. **Local dev** — scan ``mcps/*/mcp.json``. Each MCP self-describes with a tiny
   manifest in its own directory; dropping a directory in makes it appear,
   removing it makes it disappear. No central registry to edit.

Manifest entry schema (both sources share it)::

    {
      "name": "ms-graph",          # required — stable identifier
      "display_name": "Microsoft",  # optional — human label (defaults to name)
      "url": "https://ms-graph.x/mcp",  # absolute URL (deployment); OR:
      "port": 18001,                # local HTTP port (-> http://localhost:<port>)
      "path": "/mcp"                # optional — mount path for the port form
    }

An entry provides EITHER ``url`` (absolute, used verbatim) OR ``port`` (+ optional
``path``, built against ``base_host`` which defaults to ``http://localhost``).
In the filesystem source, the presence of ``mcp.json`` is the opt-in marker, so
non-MCP directories are skipped. When no source resolves, discovery returns ``[]``.
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

# Deployment source: a JSON manifest file authored at deploy time. Takes
# precedence over the filesystem scan when set.
ENV_DISCOVERY_FILE = "BOND_MCPS_DISCOVERY_FILE"

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


def _entry_from_dict(data: dict, base_host: str, *, source: str) -> dict | None:
    """Build a ``{name, display_name, url}`` entry from a manifest dict.

    Accepts either an absolute ``url`` (used verbatim — deployment) or a
    ``port`` (+ optional ``path``) built against ``base_host`` (local dev).
    Invalid entries are logged and skipped so one bad entry never breaks the
    rest.
    """
    if not isinstance(data, dict):
        logger.warning("Skipping MCP entry from %s: not a JSON object", source)
        return None

    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        logger.warning("Skipping MCP entry from %s: missing/invalid 'name'", source)
        return None
    name = name.strip()

    url = data.get("url")
    if isinstance(url, str) and url.strip():
        resolved_url = url.strip()
    else:
        port = data.get("port")
        if not isinstance(port, int) or isinstance(port, bool):
            logger.warning("Skipping MCP entry %r from %s: needs 'url' or 'port'", name, source)
            return None
        path = data.get("path") or DEFAULT_PATH
        if not isinstance(path, str) or not path.startswith("/"):
            logger.warning("Skipping MCP entry %r from %s: invalid 'path'", name, source)
            return None
        resolved_url = f"{base_host}:{port}{path}"

    display_name = data.get("display_name")
    if not isinstance(display_name, str) or not display_name.strip():
        display_name = name

    return {"name": name, "display_name": display_name, "url": resolved_url}


def _discover_from_file(path: Path, base_host: str) -> list[dict]:
    """Load discovery entries from a deploy-time JSON manifest file.

    Accepts either ``{"mcps": [...]}`` or a bare list. A missing/unreadable/
    malformed file yields ``[]`` (fail soft — discovery should never 500 the
    whole endpoint over a bad file).
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("Discovery file %s unreadable/invalid: %s", path, exc)
        return []

    items = raw.get("mcps") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        logger.warning("Discovery file %s: expected a list or {'mcps': [...]}", path)
        return []

    entries: list[dict] = []
    for item in items:
        entry = _entry_from_dict(item, base_host, source=str(path))
        if entry is not None:
            entries.append(entry)
    return entries


def _discover_from_dir(directory: Path, base_host: str) -> list[dict]:
    """Scan ``<directory>/*/mcp.json`` for per-MCP manifests (local dev)."""
    entries: list[dict] = []
    for child in directory.iterdir():
        if not child.is_dir() or child.name.startswith("."):
            continue
        manifest_path = child / MANIFEST_NAME
        if not manifest_path.is_file():
            continue
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("Skipping unreadable MCP manifest %s: %s", manifest_path, exc)
            continue
        entry = _entry_from_dict(data, base_host, source=str(manifest_path))
        if entry is not None:
            entries.append(entry)
    return entries


def discover_mcps(mcps_dir: Path | None = None, base_host: str | None = None) -> list[dict]:
    """Return the MCP servers available in this project.

    Source precedence:
      1. An explicit ``mcps_dir`` argument (test/override seam) — scanned directly.
      2. ``BOND_MCPS_DISCOVERY_FILE`` (deploy-time JSON manifest), if set.
      3. The resolved ``mcps/`` directory scan (local dev).
      4. ``[]``.

    Args:
        mcps_dir: Scan this directory directly, bypassing the file source. When
            ``None``, the file source then the resolved ``mcps/`` dir are tried.
        base_host: Scheme+host that ``port``-form entries are built against.
            Defaults to ``http://localhost``. Absolute-``url`` entries ignore it.

    Returns:
        A list of ``{"name", "display_name", "url"}`` dicts, sorted by name.
    """
    base_host = (base_host or DEFAULT_BASE_HOST).rstrip("/")

    if mcps_dir is not None:
        entries = _discover_from_dir(mcps_dir, base_host) if mcps_dir.is_dir() else []
    else:
        discovery_file = os.environ.get(ENV_DISCOVERY_FILE, "").strip()
        if discovery_file:
            entries = _discover_from_file(Path(discovery_file), base_host)
        else:
            directory = _mcps_dir()
            entries = (
                _discover_from_dir(directory, base_host)
                if directory is not None and directory.is_dir()
                else []
            )

    entries.sort(key=lambda e: e["name"])
    return entries
