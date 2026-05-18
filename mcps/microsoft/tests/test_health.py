"""Smoke test for the /healthz route registered on the MCP's HTTP app.

The handler is intentionally trivial — no DB, no auth-proxy poll. This
test exercises the function itself; the live HTTP route is verified by
the chart's helm-test pod and by k8s probes.
"""

import asyncio
import json

from ms_graph_mcp import healthz


def test_healthz_returns_ok_json():
    response = asyncio.run(healthz(None))
    assert response.status_code == 200
    body = json.loads(response.body)
    assert body == {"status": "ok"}
