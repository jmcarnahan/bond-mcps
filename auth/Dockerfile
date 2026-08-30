# bond-mcps OAuth callback proxy + encrypted token DB CLI.
#
# Build with auth/ as the context:
#   docker build -t bond-mcps-auth -f auth/Dockerfile auth/
#
# Same base image + poetry version as the MCP Dockerfiles (mcps/*/Dockerfile).
# The container exposes port 8000 and runs `python -m auth`, bound to 0.0.0.0
# so it's reachable from outside the container (default in code is 127.0.0.1).
#
# Two CLI entrypoints are also installed at /usr/local/bin/bond-mcps:
#   - bond-mcps migrate-db   (used by the chart's auth-only migrate initContainer)
#   - bond-mcps doctor       (used by the chart's preflight initContainer)

FROM public.ecr.aws/docker/library/python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir poetry==2.1.3

# Layered cache: deps first, then source.
COPY pyproject.toml poetry.lock ./

RUN poetry config virtualenvs.create false && \
    poetry install --only main --no-interaction --no-ansi --no-root

COPY auth/ ./auth/

# Real install registers the `bond-mcps` console script. psycopg[binary]
# is pulled in via auth/pyproject.toml main deps, so the Aurora driver
# is available without a separate pip install.
RUN poetry install --only main --no-interaction --no-ansi

# Non-root runtime user.
RUN useradd --create-home --uid 1000 app && chown -R app:app /app
USER app

ARG BUILD_VERSION=dev
ENV PYTHONUNBUFFERED=1 \
    PORT=8000 \
    BUILD_VERSION=$BUILD_VERSION

EXPOSE 8000

# --host 0.0.0.0 required for containerized runtime (default is 127.0.0.1).
CMD ["python", "-m", "auth", "--host", "0.0.0.0", "--port", "8000"]
