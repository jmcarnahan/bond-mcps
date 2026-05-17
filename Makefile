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

.PHONY: install dev stop logs status check-ports claude-add claude-remove \
        login login-microsoft login-github login-atlassian login-databricks \
        logout logout-microsoft logout-github logout-atlassian logout-databricks \
        migrate-tokens _check-proxy

# Login flows open the browser one at a time — never parallelize.
.NOTPARALLEL:

LOG_DIR := tmp/logs
TOKEN_DIR := $(HOME)/.bond_mcps

AUTH_PORT       ?= 8000
MS_GRAPH_PORT   ?= 18001
GITHUB_PORT     ?= 18002
ATLASSIAN_PORT  ?= 18003
DATABRICKS_PORT ?= 18004

install:
	cd auth && poetry install
	cd mcps/microsoft && poetry install
	cd mcps/github && poetry install
	cd mcps/atlassian && poetry install
	cd mcps/databricks && poetry install

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

# Snapshot-then-kill: list PIDs first (so we can report all 4 services even
# when the first pgid-sweep kills the rest), then loop kills. The window
# between snapshot and kill is tiny; signals to already-dead PIDs are
# silenced. Don't "fix" this into a per-port find+kill — that pattern only
# reports the first service stopped, since subsequent iterations find nothing.
stop:
	@pids_to_kill=""; \
	for entry in "auth:$(AUTH_PORT)" "microsoft:$(MS_GRAPH_PORT)" "github:$(GITHUB_PORT)" "atlassian:$(ATLASSIAN_PORT)" "databricks:$(DATABRICKS_PORT)"; do \
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

logout-microsoft:
	@if [ -f $(TOKEN_DIR)/microsoft.json ]; then \
	  rm -f $(TOKEN_DIR)/microsoft.json && echo "Cleared microsoft token."; \
	else echo "(microsoft: no cached token)"; fi

logout-github:
	@if [ -f $(TOKEN_DIR)/github.json ]; then \
	  rm -f $(TOKEN_DIR)/github.json && echo "Cleared github token."; \
	else echo "(github: no cached token)"; fi

logout-atlassian:
	@if [ -f $(TOKEN_DIR)/atlassian.json ]; then \
	  rm -f $(TOKEN_DIR)/atlassian.json && echo "Cleared atlassian token."; \
	else echo "(atlassian: no cached token)"; fi

logout-databricks:
	@if [ -f $(TOKEN_DIR)/databricks.json ]; then \
	  rm -f $(TOKEN_DIR)/databricks.json && echo "Cleared databricks token."; \
	else echo "(databricks: no cached token; PAT mode does not cache)"; fi

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
