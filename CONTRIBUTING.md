# Contributing to bond-mcps

## Workflow: branch + PR, always

No direct commits to `main`. Every change — code, docs, CI, even one-line typo fixes — goes through:

1. **Branch off main**: `git checkout -b <kebab-case-name>` (e.g. `fix-confluence-scope`, `add-ecs-deployment`, `docs-cli-cleanup`).
2. **Commit on the branch** with focused, well-described commits. Reference the relevant suggestion or issue in the commit body when applicable.
3. **Push and open a PR**: `git push -u origin <branch>` then `gh pr create` (or open in the GitHub UI).
4. **Wait for CI** ([`.github/workflows/test.yml`](.github/workflows/test.yml)) to go green. The workflow runs the full test suite across all 4 packages on Python 3.10, 3.11, and 3.12.
5. **Merge after review.** Squash or rebase merge — your call.

Branch protection should be configured in GitHub repo settings to enforce this (require PRs, require status checks). If it isn't yet, treat the rule as enforced by convention regardless.

## Repository layout

```
bond-mcps/
├── auth/                       # Python package `auth` — OAuth proxy + token store
│   └── tests/                  # http.server + token_store tests
├── mcps/
│   ├── microsoft/              # Microsoft Graph MCP — port 18001, CLI: ms-graph-cli
│   ├── github/                 # GitHub MCP — port 18002, CLI: github-cli
│   └── atlassian/              # Atlassian MCP — port 18003, CLI: atlassian-cli
└── deployment/                 # (planned) shared cluster infra
```

Each MCP is an independent Poetry project that depends on the local `auth` package via path dep (`bond-auth = {path = "../../auth", develop = true}`).

## Local development

Prereqs: Python ≥ 3.10, Poetry. macOS or Linux (the Makefile uses `lsof`, `kill`, `ps`, `curl`).

The recommended path is the repo-root `Makefile`, which orchestrates the auth proxy and all 3 MCPs together:

```bash
make install            # poetry install in auth/ + each MCP, then `bond-mcps migrate-db`
# Populate .env per MCP first — see README.md "Quick start" step 0
# Then mint an encryption key:
cd auth && poetry run bond-mcps generate-key
export BOND_MCPS_ENCRYPTION_KEY=<paste>
make doctor             # validate config, DB schema, encryption setup
make dev                # boots all 4 services in tmp/logs/
make login              # primes OAuth tokens via the CLIs (opens browser per provider)
make claude-add         # registers the 3 MCPs with Claude Code (user scope)
make status             # show [up]/[down] per service
make stop               # shut everything down
```

The full target list is in the [Makefile](Makefile) itself. See [`README.md`](README.md) for env-var requirements and the per-MCP READMEs for OAuth app registration.

For single-MCP debugging (bypassing the Makefile):

```bash
cd auth && poetry install && poetry run python -m auth    # auth proxy (separate terminal)
cd mcps/<name> && poetry install && poetry run pytest -q
poetry run <cli-name> --help
poetry run fastmcp run <name>_mcp.py --transport streamable-http --port <port>
```

## Running tests

```bash
# Single package
cd <package> && poetry run pytest -q

# All packages
for p in auth mcps/microsoft mcps/github mcps/atlassian; do
  (cd $p && poetry run pytest -q) || break
done
```

All tests are hermetic — no real credentials needed. MCP tests use `respx` to mock httpx; `auth/` tests use stdlib `http.server` to spin up a real proxy on `127.0.0.1:0` and `unittest.mock` for client-side patching.

The atlassian package has 16 integration tests gated by `--integration`; they hit real Atlassian APIs and are skipped by default.

## Code conventions

- **Python**: 3.10+ features OK (PEP 604 union syntax, `dict[str, X]` builtins, structural pattern matching where it clarifies). Type hints on public functions.
- **Auth package**: SQLAlchemy, Alembic, `cryptography`. Lazy-import the DB stack inside functions where possible so non-DB consumers (the auth proxy) don't pay the import cost. Tests use pytest + stdlib mock; the conftest fixtures set up an ephemeral SQLite DB at `tmp_path/tokens.db` and a fresh AES key per test.
- **MCPs**: httpx for HTTP (sync + async), fastmcp for the server, python-dotenv for `.env` loading, respx for test mocking.
- **No comments that just restate the code.** Comment the *why* (non-obvious constraints, workarounds, invariants), not the *what*.

## Adding a new MCP

1. Create `mcps/<name>/` with its own `pyproject.toml` declaring `bond-auth = {path = "../../auth", develop = true}`.
2. Write the MCP server using FastMCP (template: `mcps/microsoft/ms_graph_mcp.py`).
3. Add a CLI script entry: `[tool.poetry.scripts]` → `<name>-cli = "<name>_cli:main"`.
4. Write `respx`-mocked tests in `tests/`.
5. Add the MCP to the matrix in [`.github/workflows/test.yml`](.github/workflows/test.yml).
6. Update tables in the [top-level README](README.md) (MCP list + env vars + token caches).

## Secrets and `.env`

- `.env` files are gitignored. Don't commit real credentials.
- Each MCP has a `.env.example` template — copy and fill in locally.
- The auth proxy reads no secrets; the MCPs do.

## Reporting issues

Open a GitHub issue with: reproduction steps, expected vs actual behavior, Python version, and the MCP affected.
