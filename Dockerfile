# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# Dockerized remote MCP server (Streamable HTTP transport) for the Pocket
# Network Data Agent. Builds and runs `mcp_server_remote.py` behind uvicorn.
#
# Build:
#   docker build -t pokt-data-agent-mcp .
#
# Run:
#   docker run -p 8000:8000 \
#     -e POCKET_NETWORK_RPC_ENDPOINT=https://sauron-api.infra.pocket.network \
#     -e POCKET_NETWORK_DATA_ENDPOINT=https://data.pocket.network/ \
#     -e MCP_API_KEY=your-secret-token \
#     pokt-data-agent-mcp
#
# See README.md and mcp_server_remote.py for the full list of environment
# variables.
# ---------------------------------------------------------------------------

FROM python:3.11-slim AS base

# Install uv (fast Python package manager) via the official static binary.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies first (cached layer) using the lockfile.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-install-project --no-dev

# Copy the rest of the project and install it.
COPY src ./src
COPY mcp_server_remote.py ./
COPY README.md ./
RUN uv sync --locked --no-dev

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8000

CMD ["python", "mcp_server_remote.py"]
