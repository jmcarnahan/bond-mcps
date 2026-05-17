# Contributing to bond-mcps

## Workflow: branch + PR, always

No direct commits to `main`. Every change — code, docs, CI, even one-line typo fixes — goes through:

1. **Branch off main**: `git checkout -b <kebab-case-name>` (e.g. `fix-confluence-scope`, `add-ecs-deployment`, `docs-cli-cleanup`).
2. **Commit on the branch** with focused, well-described commits. Reference the relevant suggestion or issue in the commit body when applicable.
3. **Push and open a PR**: `git push -u origin <branch>` then `gh pr create` (or open in the GitHub UI).
4. **Wait for CI** ([`.github/workflows/test.yml`](.github/workflows/test.yml)) to go green. The workflow runs the full test suite (~752 tests) across all 4 packages on Python 3.10, 3.11, and 3.12.
5. **Merge after review.** Squash or rebase merge — your call.

Branch protection should be configured in GitHub repo settings to enforce this (require PRs, require status checks). If it isn't yet, treat the rule as enforced by convention regardless.

## Repository layout

```
bond-mcps/
├── auth/                       # Python package `auth` — OAuth proxy + token store
│   └── tests/                  # 53 tests
├── mcps/
│   ├── microsoft/              # Microsoft Graph MCP — port 5557, CLI: ms-graph-cli
│   ├── github/                 # GitHub MCP — port 5558, CLI: github-cli
│   └── atlassian/              # Atlassian MCP — port 9001, CLI: atlassian-cli
└── deployment/                 # (planned) shared cluster infra
```

Each MCP is an independent Poetry project that depends on the local `auth` package via path dep (`bond-auth = {path = "../../auth", develop = true}`).

## Local development

Prereqs: Python ≥ 3.10, Poetry.

```bash
# 1. Install the shared auth library
cd auth && poetry install

# 2. Start the OAuth callback proxy (separate terminal)
cd auth && poetry run python -m auth

# 3. Install + run an MCP
cd mcps/<name> && poetry install
poetry run pytest -q
poetry run <cli-name> --help
poetry run fastmcp run <name>_mcp.py --transport streamable-http --port <port>
```

Per-MCP env var requirements are documented in each MCP's README and in the [top-level README](README.md).

## Running tests

```bash
# Single package
cd <package> && poetry run pytest -q

# All packages
for p in auth mcps/microsoft mcps/github mcps/atlassian; do
  (cd $p && poetry run pytest -q) || break
done
```

All tests are unit tests using `respx` to mock HTTP — no real credentials needed.

The atlassian package has 16 integration tests gated by `--integration`; they hit real Atlassian APIs and are skipped by default.

## Code conventions

- **Python**: 3.10+ features OK (PEP 604 union syntax, `dict[str, X]` builtins, structural pattern matching where it clarifies). Type hints on public functions.
- **Auth package**: stdlib only — no external runtime deps. Tests use pytest + the standard library mock.
- **MCPs**: httpx for HTTP (sync + async), fastmcp for the server, respx for test mocking.
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
