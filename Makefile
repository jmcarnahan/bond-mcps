# Dev orchestration for the five local services (auth proxy + 4 MCPs).
#
# Requires (macOS or Linux):
#   - poetry          : install in each package, run servers
#   - lsof, ps, kill  : port + process probing (check-ports, status, stop)
#   - curl            : auth-proxy health probe (_check-proxy)
#   - claude          : MCP registration (claude-add / claude-remove); optional
#
# All MCP ports are env-overridable (defaults: 18001/2/3/4 for MCPs, 8000 for
# auth proxy). Auth proxy port flows via BOND_AUTH_PROXY_PORT which both the
# proxy server and the MCP-side OAuthProxyClient read.

.PHONY: install dev dev-combined dev-multitenant stop logs status check-ports check-ports-mt \
        claude-add claude-remove \
        login login-microsoft login-github login-atlassian login-databricks \
        logout logout-microsoft logout-github logout-atlassian logout-databricks \
        migrate-db import-tokens doctor migrate-tokens \
        _check-proxy _ensure-as-keypair _ensure-as-shared-secret

# Login flows open the browser one at a time — never parallelize.
.NOTPARALLEL:

LOG_DIR := tmp/logs
TOKEN_DIR := $(HOME)/.bond_mcps

AUTH_PORT       ?= 8000
AS_PORT         ?= 8001
MS_GRAPH_PORT   ?= 18001
GITHUB_PORT     ?= 18002
ATLASSIAN_PORT  ?= 18003
DATABRICKS_PORT ?= 18004


# Multi-tenant local dev defaults — used by dev-multitenant.
AS_BASE_URL     ?= http://localhost:$(AS_PORT)
AS_KEY_FILE     ?= $(HOME)/.bond_mcps/jwt_signing_key.pem
JWT_SHARED_SECRET_FILE ?= $(HOME)/.bond_mcps/jwt_shared_secret

install:
	cd auth && poetry install
	cd mcps/microsoft && poetry install
	cd mcps/github && poetry install
	cd mcps/atlassian && poetry install
	cd mcps/databricks && poetry install
	@$(MAKE) --no-print-directory migrate-db

# ----- Code-quality / security hooks -------------------------------------
# Install pre-commit + pre-push hooks into .git/hooks. One-time per clone.
# Requires `cd auth && poetry install` first (pre-commit lives in auth's dev deps).
hooks-install:
	cd auth && poetry run pre-commit install --install-hooks
	cd auth && poetry run pre-commit install --hook-type pre-push

# Run every hook against every file. Useful after a `git pull` introduced new
# files, or on first install to catch existing drift.
hooks-run:
	cd auth && poetry run pre-commit run --all-files

# Read-only Python lint. CI runs the same via pre-commit.
lint:
	cd auth && poetry run ruff check ../auth ../mcps

# Auto-fix Python lint + reformat. Run before committing big changes.
format:
	cd auth && poetry run ruff check --fix ../auth ../mcps
	cd auth && poetry run ruff format ../auth ../mcps

# Terraform formatting. Recursive over the whole module.
tf-fmt:
	terraform -chdir=deployment/terraform-existing-vpc fmt -recursive

# Bring the token DB schema up to head. Idempotent — safe to run repeatedly.
# Uses sqlite:///<repo>/tokens.db unless BOND_MCPS_DB_URL is set.
migrate-db:
	@cd auth && poetry run bond-mcps migrate-db

# One-shot health check: validate config, DB, schema, and encryption setup.
doctor:
	@cd auth && poetry run bond-mcps doctor

# One-shot import: pull legacy ~/.bond_mcps/*.json tokens into the encrypted DB.
# Idempotent; imported files are moved to ~/.bond_mcps/legacy_imported/.
import-tokens:
	@cd auth && poetry run bond-mcps import-files

check-ports:
	@busy=0; for p in $(AUTH_PORT) $(MS_GRAPH_PORT) $(GITHUB_PORT) $(ATLASSIAN_PORT) $(DATABRICKS_PORT); do \
	  if lsof -nP -iTCP:$$p -sTCP:LISTEN >/dev/null 2>&1; then \
	    owner=$$(lsof -nP -iTCP:$$p -sTCP:LISTEN -t | head -1 | xargs -I{} ps -p {} -o comm= 2>/dev/null | tail -1); \
	    echo "  port $$p in use by $$owner"; busy=1; \
	  fi; \
	done; \
	if [ $$busy -ne 0 ]; then \
	  echo "Free the ports above or override via AUTH_PORT / MS_GRAPH_PORT / GITHUB_PORT / ATLASSIAN_PORT / DATABRICKS_PORT." >&2; \
	  exit 1; \
	fi

dev: check-ports
	@mkdir -p $(LOG_DIR)
	@echo "Starting auth proxy on :$(AUTH_PORT)..."
	@( cd auth && BOND_AUTH_PROXY_PORT=$(AUTH_PORT) nohup poetry run python -m auth ) > $(CURDIR)/$(LOG_DIR)/auth.log 2>&1 &
	@sleep 1
	@echo "Starting Microsoft MCP on :$(MS_GRAPH_PORT)..."
	@( cd mcps/microsoft && BOND_AUTH_PROXY_PORT=$(AUTH_PORT) nohup poetry run fastmcp run ms_graph_mcp.py --transport streamable-http --port $(MS_GRAPH_PORT) ) > $(CURDIR)/$(LOG_DIR)/microsoft.log 2>&1 &
	@echo "Starting GitHub MCP on :$(GITHUB_PORT)..."
	@( cd mcps/github && BOND_AUTH_PROXY_PORT=$(AUTH_PORT) nohup poetry run fastmcp run github_mcp.py --transport streamable-http --port $(GITHUB_PORT) ) > $(CURDIR)/$(LOG_DIR)/github.log 2>&1 &
	@echo "Starting Atlassian MCP on :$(ATLASSIAN_PORT)..."
	@( cd mcps/atlassian && BOND_AUTH_PROXY_PORT=$(AUTH_PORT) nohup poetry run fastmcp run atlassian_mcp.py --transport streamable-http --port $(ATLASSIAN_PORT) ) > $(CURDIR)/$(LOG_DIR)/atlassian.log 2>&1 &
	@echo "Starting Databricks MCP on :$(DATABRICKS_PORT)..."
	@( cd mcps/databricks && BOND_AUTH_PROXY_PORT=$(AUTH_PORT) nohup poetry run fastmcp run databricks_mcp.py --transport streamable-http --port $(DATABRICKS_PORT) ) > $(CURDIR)/$(LOG_DIR)/databricks.log 2>&1 &
	@# Heuristic — FastMCP normally binds within ~1s. Bump if `make status`
	@# shows [down] services immediately after `make dev` on a slow system.
	@sleep 3
	@$(MAKE) --no-print-directory status

# Alias for `dev` — kept for symmetry with bond-ai's `make dev-combined`. In
# the current combined-mode design, OAuth callbacks stay at :8000 (the
# bond-mcps auth proxy's default), so no env-var change is needed. The auth
# proxy's startup log will show "Public redirect base: http://localhost:8000"
# which is exactly what's already registered with OAuth providers.
dev-combined: dev

# Snapshot-then-kill: list PIDs first (so we can report all 4 services even
# when the first pgid-sweep kills the rest), then loop kills. The window
# between snapshot and kill is tiny; signals to already-dead PIDs are
# silenced. Don't "fix" this into a per-port find+kill — that pattern only
# reports the first service stopped, since subsequent iterations find nothing.
stop:
	@pids_to_kill=""; \
	for entry in "auth:$(AUTH_PORT)" "as:$(AS_PORT)" "microsoft:$(MS_GRAPH_PORT)" "github:$(GITHUB_PORT)" "atlassian:$(ATLASSIAN_PORT)" "databricks:$(DATABRICKS_PORT)"; do \
	  name=$${entry%%:*}; port=$${entry##*:}; \
	  pid=$$(lsof -nP -iTCP:$$port -sTCP:LISTEN -t 2>/dev/null | head -1); \
	  if [ -n "$$pid" ]; then \
	    echo "Stopping $$name :$$port (pid $$pid)"; \
	    pids_to_kill="$$pids_to_kill $$pid"; \
	  fi; \
	done; \
	for pid in $$pids_to_kill; do \
	  pgid=$$(ps -o pgid= -p $$pid 2>/dev/null | tr -d ' '); \
	  if [ -n "$$pgid" ]; then kill -- -$$pgid 2>/dev/null; fi; \
	  kill $$pid 2>/dev/null || true; \
	done

status:
	@for entry in "auth:$(AUTH_PORT)" "microsoft:$(MS_GRAPH_PORT)" "github:$(GITHUB_PORT)" "atlassian:$(ATLASSIAN_PORT)" "databricks:$(DATABRICKS_PORT)"; do \
	  name=$${entry%%:*}; port=$${entry##*:}; \
	  pid=$$(lsof -nP -iTCP:$$port -sTCP:LISTEN -t 2>/dev/null | head -1); \
	  if [ -n "$$pid" ]; then echo "  [up]   $$name :$$port (pid $$pid)"; \
	  else echo "  [down] $$name :$$port"; fi; \
	done

logs:
	tail -F $(LOG_DIR)/*.log


# ---------------------------------------------------------------------------
# Multi-tenant local dev (JWT mode + Authorization Server)
# ---------------------------------------------------------------------------
#
# Starts the laptop stack PLUS the bond-mcps Authorization Server with JWT
# verification turned on across all four MCPs. The signing key is auto-
# generated on first run at AS_KEY_FILE; the upstream Cognito/Okta config
# is read from the environment when set, otherwise the AS will still start
# but /oauth/authorize returns 500 until BOND_MCPS_UPSTREAM_* is provided.
# For round-trip testing without an upstream, hand-craft a JWT signed with
# the AS_KEY_FILE private key (sub=<your-user-id>, aud="bond-mcps", iss=
# $(AS_BASE_URL)) and pass it as `Authorization: Bearer <jwt>`.

_ensure-as-keypair:
	@mkdir -p $(dir $(AS_KEY_FILE))
	@if [ ! -f $(AS_KEY_FILE) ]; then \
	  echo "Generating AS signing keypair at $(AS_KEY_FILE)..."; \
	  cd auth && poetry run python -c \
	    "from auth.auth_server.keys import load_signing_key; load_signing_key()" \
	    > /dev/null; \
	fi

check-ports-mt:
	@busy=0; for p in $(AUTH_PORT) $(AS_PORT) $(MS_GRAPH_PORT) $(GITHUB_PORT) $(ATLASSIAN_PORT) $(DATABRICKS_PORT); do \
	  if lsof -nP -iTCP:$$p -sTCP:LISTEN >/dev/null 2>&1; then \
	    owner=$$(lsof -nP -iTCP:$$p -sTCP:LISTEN -t | head -1 | xargs -I{} ps -p {} -o comm= 2>/dev/null | tail -1); \
	    echo "  port $$p in use by $$owner"; busy=1; \
	  fi; \
	done; \
	if [ $$busy -ne 0 ]; then \
	  echo "Free the ports above or override via AUTH_PORT / AS_PORT / MS_GRAPH_PORT / GITHUB_PORT / ATLASSIAN_PORT / DATABRICKS_PORT." >&2; \
	  exit 1; \
	fi

dev-multitenant: check-ports-mt _ensure-as-keypair
	@mkdir -p $(LOG_DIR)
	@echo "Starting auth proxy on :$(AUTH_PORT)..."
	@( cd auth && BOND_AUTH_PROXY_PORT=$(AUTH_PORT) nohup poetry run python -m auth ) > $(CURDIR)/$(LOG_DIR)/auth.log 2>&1 &
	@sleep 1
	@echo "Starting Authorization Server on :$(AS_PORT)..."
	@( cd auth && BOND_MCPS_AS_ENABLED=1 BOND_MCPS_AS_BASE_URL=$(AS_BASE_URL) \
	   BOND_MCPS_AS_PRIVATE_KEY_FILE=$(AS_KEY_FILE) \
	   nohup poetry run python -m auth.auth_server --port $(AS_PORT) ) > $(CURDIR)/$(LOG_DIR)/as.log 2>&1 &
	@sleep 1
	@echo "Starting MCPs in JWT mode (issuer=$(AS_BASE_URL))..."
	@$(MAKE) --no-print-directory _start-mcp-mt MCP=microsoft  ENTRY=ms_graph_mcp.py  PORT=$(MS_GRAPH_PORT)  AUDIENCE=ms-graph
	@$(MAKE) --no-print-directory _start-mcp-mt MCP=github     ENTRY=github_mcp.py    PORT=$(GITHUB_PORT)    AUDIENCE=github
	@$(MAKE) --no-print-directory _start-mcp-mt MCP=atlassian  ENTRY=atlassian_mcp.py PORT=$(ATLASSIAN_PORT) AUDIENCE=atlassian
	@$(MAKE) --no-print-directory _start-mcp-mt MCP=databricks ENTRY=databricks_mcp.py PORT=$(DATABRICKS_PORT) AUDIENCE=databricks
	@sleep 3
	@$(MAKE) --no-print-directory status-mt

_start-mcp-mt:
	@echo "Starting $(MCP) MCP on :$(PORT) (audience=$(AUDIENCE))..."
	@( cd mcps/$(MCP) && \
	   BOND_AUTH_PROXY_PORT=$(AUTH_PORT) \
	   BOND_MCPS_JWT_JWKS_URI=$(AS_BASE_URL)/.well-known/jwks.json \
	   BOND_MCPS_JWT_ISSUER=$(AS_BASE_URL) \
	   BOND_MCPS_JWT_AUDIENCE=$(AUDIENCE) \
	   BOND_MCPS_AS_BASE_URL=$(AS_BASE_URL) \
	   BOND_MCPS_PUBLIC_URL=http://localhost:$(PORT) \
	   nohup poetry run fastmcp run $(ENTRY) --transport streamable-http --port $(PORT) ) \
	     > $(CURDIR)/$(LOG_DIR)/$(MCP).log 2>&1 &

status-mt:
	@for entry in "auth:$(AUTH_PORT)" "as:$(AS_PORT)" "microsoft:$(MS_GRAPH_PORT)" "github:$(GITHUB_PORT)" "atlassian:$(ATLASSIAN_PORT)" "databricks:$(DATABRICKS_PORT)"; do \
	  name=$${entry%%:*}; port=$${entry##*:}; \
	  pid=$$(lsof -nP -iTCP:$$port -sTCP:LISTEN -t 2>/dev/null | head -1); \
	  if [ -n "$$pid" ]; then echo "  [up]   $$name :$$port (pid $$pid)"; \
	  else echo "  [down] $$name :$$port"; fi; \
	done

claude-add:
	@for entry in "ms-graph:$(MS_GRAPH_PORT)" "github:$(GITHUB_PORT)" "atlassian:$(ATLASSIAN_PORT)" "databricks:$(DATABRICKS_PORT)"; do \
	  name=$${entry%%:*}; port=$${entry##*:}; \
	  claude mcp remove --scope user $$name >/dev/null 2>&1 || true; \
	  claude mcp add --transport http --scope user $$name http://localhost:$$port/mcp \
	    || { echo "Failed to add $$name to Claude Code (is the claude CLI installed?)" >&2; exit 1; }; \
	done

claude-remove:
	@claude mcp remove --scope user ms-graph   >/dev/null 2>&1 || true
	@claude mcp remove --scope user github     >/dev/null 2>&1 || true
	@claude mcp remove --scope user atlassian  >/dev/null 2>&1 || true
	@claude mcp remove --scope user databricks >/dev/null 2>&1 || true
	@echo "Unregistered ms-graph, github, atlassian, databricks from Claude Code (entries that didn't exist were skipped silently)."

# ---------------------------------------------------------------------------
# OAuth login orchestration
# ---------------------------------------------------------------------------

_check-proxy:
	@if ! curl -sf http://localhost:$(AUTH_PORT)/health >/dev/null 2>&1; then \
	  echo "Auth proxy not reachable on :$(AUTH_PORT). Run 'make dev' first." >&2; \
	  exit 1; \
	fi

login: login-microsoft login-github login-atlassian login-databricks
	@echo
	@echo "All logins complete. Cached tokens in $(TOKEN_DIR)/."

login-microsoft: _check-proxy
	@echo "==> Microsoft  (browser will open if not cached)"
	@cd mcps/microsoft && poetry run ms-graph-cli whoami

login-github: _check-proxy
	@echo "==> GitHub  (browser will open if not cached)"
	@cd mcps/github && poetry run github-cli user

login-atlassian: _check-proxy
	@echo "==> Atlassian  (browser will open if not cached)"
	@cd mcps/atlassian && poetry run atlassian-cli user me

# Databricks supports a PAT fallback for free-tier workspaces that cannot
# register OAuth apps. The CLI detects PAT mode and short-circuits without
# needing the auth proxy — so this target intentionally does NOT depend on
# _check-proxy. In OAuth mode the CLI itself surfaces a clear error if the
# proxy isn't running.
login-databricks:
	@echo "==> Databricks  (browser will open in OAuth mode; PAT mode skips browser)"
	@cd mcps/databricks && poetry run databricks-cli whoami

logout: logout-microsoft logout-github logout-atlassian logout-databricks

# Each logout clears the DB row via the CLI. Legacy file caches (if any)
# are also removed defensively.
logout-microsoft:
	@cd auth && poetry run bond-mcps clear --provider microsoft
	@rm -f $(TOKEN_DIR)/microsoft.json 2>/dev/null || true

logout-github:
	@cd auth && poetry run bond-mcps clear --provider github
	@rm -f $(TOKEN_DIR)/github.json 2>/dev/null || true

logout-atlassian:
	@cd auth && poetry run bond-mcps clear --provider atlassian
	@rm -f $(TOKEN_DIR)/atlassian.json 2>/dev/null || true

logout-databricks:
	@cd auth && poetry run bond-mcps clear --provider databricks
	@rm -f $(TOKEN_DIR)/databricks.json 2>/dev/null || true

# Move tokens from the legacy bond-ai paths to the unified $(TOKEN_DIR).
# Safe to run repeatedly: skips when the destination already exists.
migrate-tokens:
	@mkdir -p $(TOKEN_DIR) && chmod 700 $(TOKEN_DIR)
	@if [ -f $(HOME)/.ms_graph_tokens.json ] && [ ! -f $(TOKEN_DIR)/microsoft.json ]; then \
	  mv $(HOME)/.ms_graph_tokens.json $(TOKEN_DIR)/microsoft.json && \
	  echo "Moved ~/.ms_graph_tokens.json -> $(TOKEN_DIR)/microsoft.json"; \
	fi
	@for p in github atlassian; do \
	  src=$(HOME)/.bond_ai_tokens/$$p.json; dst=$(TOKEN_DIR)/$$p.json; \
	  if [ -f $$src ] && [ ! -f $$dst ]; then \
	    mv $$src $$dst && echo "Moved $$src -> $$dst"; \
	  fi; \
	done
	@if [ -d $(HOME)/.bond_ai_tokens ] && [ -z "$$(ls -A $(HOME)/.bond_ai_tokens 2>/dev/null)" ]; then \
	  rmdir $(HOME)/.bond_ai_tokens && echo "Removed empty ~/.bond_ai_tokens/"; \
	fi
	@if [ -f $(HOME)/.bond_ai_auth_proxy.pid ] && [ ! -f $(TOKEN_DIR)/auth_proxy.pid ]; then \
	  mv $(HOME)/.bond_ai_auth_proxy.pid $(TOKEN_DIR)/auth_proxy.pid && \
	  echo "Moved ~/.bond_ai_auth_proxy.pid -> $(TOKEN_DIR)/auth_proxy.pid"; \
	fi
	@if [ -f $(HOME)/.github_mcp_tokens.json ]; then \
	  rm -f $(HOME)/.github_mcp_tokens.json && \
	  echo "Removed orphan ~/.github_mcp_tokens.json (the old github CLI cache; the CLI now shares $(TOKEN_DIR)/github.json)"; \
	fi
	@echo "Migration complete. Token dir: $(TOKEN_DIR)"
