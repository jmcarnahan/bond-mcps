FROM public.ecr.aws/docker/library/python:3.12-slim

WORKDIR /app

# Install poetry
RUN pip install poetry==2.1.3

# Copy shared_auth to /shared_auth/ so the path dependency (../shared_auth)
# resolves correctly from /app/pyproject.toml
COPY _shared_auth_pkg/ /shared_auth/

# Copy dependency files
COPY pyproject.toml poetry.lock ./

# Install production dependencies only
RUN poetry config virtualenvs.create false && \
    poetry install --only main --no-interaction --no-ansi --no-root

# Copy application code
COPY ms_graph/ ./ms_graph/
COPY ms_graph_mcp.py ./

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV PORT=8000

# Run the MCP server
CMD ["fastmcp", "run", "ms_graph_mcp.py", "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "8000"]
