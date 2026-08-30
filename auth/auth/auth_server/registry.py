"""Registry endpoint — returns terraform-computed MCP config as JSON."""

import json
import logging
import os

from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

_EMPTY = {"mcpServers": {}}


async def registry_endpoint(request: Request) -> JSONResponse:
    raw = os.environ.get("BOND_MCPS_REGISTRY_CONFIG", "").strip()
    if not raw:
        return JSONResponse(_EMPTY)
    try:
        return JSONResponse(json.loads(raw))
    except (json.JSONDecodeError, ValueError) as exc:
        logger.error("BOND_MCPS_REGISTRY_CONFIG contains invalid JSON: %s", exc)
        return JSONResponse(_EMPTY)
