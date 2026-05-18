# bond-mcps GitHub MCP server.
#
# Build (with mcps/github/ as the context, after CI prep):
#   docker build -t bond-mcps-mcp-github -f mcps/github/Dockerfile mcps/github/
#
# CI prep step copies auth/ → mcps/github/_shared_auth_pkg/ first so the path
# dependency `bond-auth = {path = "../../auth"}` in pyproject.toml resolves
# inside the container (../../auth from /app/pyproject.toml → /auth).

FROM public.ecr.aws/docker/library/python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir poetry==2.1.3

# bond-auth path-dep target.
COPY _shared_auth_pkg/ /auth/

# Layered cache: deps before source.
COPY pyproject.toml poetry.lock ./

RUN poetry config virtualenvs.create false && \
    poetry install --only main --no-interaction --no-ansi --no-root

COPY github/ ./github/
COPY github_mcp.py ./

# psycopg[binary] comes in transitively via bond-auth's main deps; no
# separate install needed.

# Non-root runtime.
RUN useradd --create-home --uid 1000 app && \
    chown -R app:app /app /auth
USER app

ENV PYTHONUNBUFFERED=1 \
    PORT=8000

EXPOSE 8000

CMD ["fastmcp", "run", "github_mcp.py", "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "8000"]
