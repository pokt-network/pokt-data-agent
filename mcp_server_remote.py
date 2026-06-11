"""MCP server (Streamable HTTP transport) for the Pocket Network Data Agent.

Intended for remote deployment: run this on a server and point Claude Code /
OpenCode at it over HTTPS.  Every request must carry a Bearer token that
matches the MCP_API_KEY environment variable.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Starting the server
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    POCKET_NETWORK_RPC_ENDPOINT=https://sauron-api.infra.pocket.network \\
    POCKET_NETWORK_DATA_ENDPOINT=https://data.pocket.network/ \\
    LLM_BASE_URL=https://api.openai.com/v1 \\
    LLM_MODEL=gpt-4o \\
    OPENAI_API_KEY=sk-... \\
    MCP_API_KEY=your-secret-token \\
    uv run mcp_server_remote.py

The server will listen on 0.0.0.0:8000/mcp by default.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Client configuration
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

OpenCode (opencode.jsonc):

    {
      "mcp": {
        "pokt-data-agent": {
          "type": "remote",
          "url": "https://your-server.example.com/mcp",
          "oauth": false,
          "headers": {
            "Authorization": "Bearer {env:POKT_MCP_API_KEY}"
          }
        }
      }
    }

Claude Code (.mcp.json or ~/.claude.json):

    {
      "mcpServers": {
        "pokt-data-agent": {
          "type": "http",
          "url": "https://your-server.example.com/mcp",
          "headers": {
            "Authorization": "Bearer YOUR_API_KEY"
          }
        }
      }
    }

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Environment variables
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Optional:
    MCP_API_KEY    – Static bearer token clients must send in the
                     Authorization header.  If unset, the server accepts
                     unauthenticated requests (safe for local use only).
    LLM_BASE_URL   – Base URL for an OpenAI-compatible LLM endpoint.
                     Required only when using agent tools.
    LLM_MODEL      – Model name to use.  Required only when using agent tools.
    OPENAI_API_KEY – API key forwarded to the LLM endpoint
                     (default: not-needed).
    MCP_HOST       – Bind address (default: 0.0.0.0).
    MCP_PORT       – Bind port    (default: 8000).
    MCP_PATH       – URL path     (default: /mcp).

For local / stdio use see mcp_server.py.
"""

import hmac
import logging
import os
import sys

import uvicorn
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from src.mcp_utils import create_mcp_server

logging.basicConfig(level=logging.WARNING)

# ---------------------------------------------------------------------------
# Bearer-token auth middleware
# ---------------------------------------------------------------------------


_EXPECTED_TOKEN: str | None = os.environ.get("MCP_API_KEY", "").strip() or None

if _EXPECTED_TOKEN is None:
    print(
        "WARNING: MCP_API_KEY is not set — server will accept unauthenticated requests. "
        "Set MCP_API_KEY to a long random secret for any non-local deployment.",
        file=sys.stderr,
    )


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Reject requests whose Authorization header doesn't match MCP_API_KEY.

    When MCP_API_KEY is unset the middleware is a no-op, allowing unauthenticated
    access — suitable for local / trusted-network use.
    """

    async def dispatch(self, request: Request, call_next):
        if _EXPECTED_TOKEN is None:
            return await call_next(request)

        auth_header = request.headers.get("Authorization", "")
        token = auth_header[len("Bearer ") :] if auth_header.startswith("Bearer ") else ""

        # Use constant-time comparison to prevent timing attacks
        if not hmac.compare_digest(token.encode(), _EXPECTED_TOKEN.encode()):
            return JSONResponse(
                {"error": "Unauthorized – invalid or missing Bearer token"},
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer realm="pokt-data-agent"'},
            )

        return await call_next(request)


# ---------------------------------------------------------------------------
# Server instance
# ---------------------------------------------------------------------------

mcp = create_mcp_server()

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    host = os.environ.get("MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("MCP_PORT", "8000"))
    path = os.environ.get("MCP_PATH", "/mcp")

    # Build the Starlette ASGI app and wrap it with auth middleware
    app = mcp.streamable_http_app()
    app.add_middleware(BearerAuthMiddleware)

    print(
        f"Starting pokt-data-agent remote MCP server on http://{host}:{port}{path}",
        file=sys.stderr,
    )
    uvicorn.run(app, host=host, port=port)
