# bond-mcps Atlassian MCP server (Jira + Confluence).
#
# Build (with mcps/atlassian/ as the context, after CI prep):
#   docker build -t bond-mcps-mcp-atlassian -f mcps/atlassian/Dockerfile mcps/atlassian/
#
# CI prep step copies auth/ → mcps/atlassian/_shared_auth_pkg/ first so the
# path dependency `bond-auth = {path = "../../auth"}` resolves inside the
# container (../../auth from /app/pyproject.toml → /auth).

FROM public.ecr.aws/docker/library/python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir poetry==2.1.3

COPY _shared_auth_pkg/ /auth/

COPY pyproject.toml poetry.lock ./

RUN poetry config virtualenvs.create false && \
    poetry install --only main --no-interaction --no-ansi --no-root

COPY atlassian/ ./atlassian/
COPY atlassian_mcp.py ./

# psycopg[binary] comes in transitively via bond-auth's main deps.

RUN useradd --create-home --uid 1000 app && \
    chown -R app:app /app /auth
USER app

ENV PYTHONUNBUFFERED=1 \
    PORT=8000

EXPOSE 8000

CMD ["fastmcp", "run", "atlassian_mcp.py", "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "8000"]
