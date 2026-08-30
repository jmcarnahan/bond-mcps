# bond-mcps Microsoft MCP server (Graph API: mail, calendar, files, teams).
#
# Build (with mcps/microsoft/ as the context, after CI prep):
#   docker build -t bond-mcps-mcp-microsoft -f mcps/microsoft/Dockerfile mcps/microsoft/
#
# CI prep step copies auth/ → mcps/microsoft/_shared_auth_pkg/ first so the
# path dependency `bond-auth = {path = "../../auth"}` resolves inside the
# container (../../auth from /app/pyproject.toml → /auth).

FROM public.ecr.aws/docker/library/python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir poetry==2.1.3

COPY _shared_auth_pkg/ /auth/

COPY pyproject.toml poetry.lock ./

RUN poetry config virtualenvs.create false && \
    poetry install --only main --no-interaction --no-ansi --no-root

COPY ms_graph/ ./ms_graph/
COPY ms_graph_mcp.py ./

# psycopg[binary] comes in transitively via bond-auth's main deps.

RUN useradd --create-home --uid 1000 app && \
    chown -R app:app /app /auth
USER app

ARG BUILD_VERSION=dev
ENV PYTHONUNBUFFERED=1 \
    PORT=8000 \
    BUILD_VERSION=$BUILD_VERSION

EXPOSE 8000

CMD ["fastmcp", "run", "ms_graph_mcp.py", "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "8000"]
