# MCP discovery endpoint

A single, unauthenticated REST endpoint that lists the MCP servers available in
this project and their HTTP endpoints — and nothing else. Consumers (e.g.
bond-ai) call it once to learn *where* the MCPs are, then use the MCP protocol
itself (the `initialize` call) for everything beyond the URL: tools, prompts,
auth requirements, capabilities. The goal is that applications no longer need to
be hard-configured with the list of MCPs.

## Endpoint

```
GET /connections/discovery        (no auth)
```

Hosted on the **auth proxy** (`auth/auth/proxy_server.py`) — the one server
that's always running in `make dev` and `make dev-combined`. It lives under
`/connections/` so that in combined mode bond-ai reaches it through the existing
nginx `/connections/*` route with no nginx changes:

| Mode | URL |
|------|-----|
| split (`make dev`) | `http://localhost:8000/connections/discovery` |
| combined (`make dev-combined`) | `http://localhost:8000/connections/discovery` (nginx :8000 → proxy :18000) |
| combined, direct to proxy | `http://localhost:18000/connections/discovery` |

## Response

```json
{
  "mcps": [
    { "name": "atlassian",  "display_name": "Atlassian",  "url": "http://localhost:18003/mcp" },
    { "name": "databricks", "display_name": "Databricks", "url": "http://localhost:18004/mcp" },
    { "name": "github",     "display_name": "GitHub",     "url": "http://localhost:18002/mcp" },
    { "name": "ms-graph",   "display_name": "Microsoft",  "url": "http://localhost:18001/mcp" }
  ]
}
```

Entries are sorted by `name`. Each is just `name` + `display_name` + `url` — no
tool or MCP-specific metadata by design. On an unexpected internal error the
endpoint returns `500 {"error": "discovery_failed"}` rather than a partial list.

## How discovery works

Discovery is filesystem-based and dynamic. Each MCP self-describes with a small
`mcp.json` in its own directory:

```
mcps/<name>/mcp.json
```

```json
{
  "name": "ms-graph",
  "display_name": "Microsoft",
  "port": 18001,
  "path": "/mcp"
}
```

| field | required | notes |
|-------|----------|-------|
| `name` | yes | stable identifier returned to consumers |
| `port` | yes | local HTTP port (integer; `true`/`false` rejected) |
| `display_name` | no | human label; defaults to `name` |
| `path` | no | mount path; defaults to `/mcp`; must start with `/` |

At request time `auth.discovery.discover_mcps()` scans `mcps/*/`:

- The **presence of `mcp.json` is the opt-in marker** — directories without it
  (and dot-directories like `.venv`) are skipped. Non-MCP directories need no
  special-casing.
- A malformed manifest, or one missing a required field, is logged and skipped —
  one bad file never breaks discovery for the others.
- The endpoint URL is built as `http://localhost:<port><path>`.

This makes the set of MCPs **dynamic with no central registry to edit**: drop in
`mcps/foo/mcp.json` and `foo` appears on the next request; remove the directory
and it disappears. No code or Makefile change required.

## Resolving the `mcps/` directory

`discover_mcps()` finds the directory in this order:

1. `BOND_MCPS_MCPS_DIR` — explicit override (used by tests; also covers
   non-standard layouts).
2. `<repo-root>/mcps`, where repo root is resolved relative to the `auth`
   package and only used when the root looks like a real dev checkout
   (`Makefile`/`pyproject.toml` present).

If neither resolves to a real directory, discovery returns `[]`.

## Scope and limitations

- **Local / dev-checkout only.** Discovery scans the on-disk `mcps/` directory,
  which exists in a checkout but not in a deployed/installed copy of the `auth`
  package. In deployment the endpoint returns `[]`. Serving discovery in
  production would need a different data source (e.g. the per-service config the
  Terraform deployment already knows) and is intentionally out of scope here.
- **URLs are local (`http://localhost:<port>`).** `discover_mcps(base_host=...)`
  accepts an override, but environment-aware/public URLs are not wired into the
  endpoint yet — same future-deployment concern as above.
- **Ports come from `mcp.json`, not the running process.** Discovery reports the
  declared port. With the default Makefile ports this matches what's running; if
  you launch an MCP on an env-overridden port, discovery still reports the
  manifest value.

## Tests

`auth/tests/test_discovery.py` covers the function (missing/empty dir, valid and
malformed manifests, hidden dirs, sorting, default/custom path, `display_name`
fallback, env override, dynamic add/remove) and the live route (200 + shape,
unauthenticated, 404 on unknown paths), plus a contract test against the real
`mcps/` directory.

```
cd auth && poetry run pytest tests/test_discovery.py -v
```
