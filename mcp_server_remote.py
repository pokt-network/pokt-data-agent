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

  Required:
    MCP_API_KEY    – Static bearer token clients must send in the
                     Authorization header.
    LLM_BASE_URL   – Base URL for an OpenAI-compatible LLM endpoint.
    LLM_MODEL      – Model name to use.

  Optional:
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


def _require_api_key() -> str:
    key = os.environ.get("MCP_API_KEY", "").strip()
    if not key:
        print(
            "ERROR: MCP_API_KEY environment variable is not set. "
            "Set it to a long random secret before starting the remote server.",
            file=sys.stderr,
        )
        sys.exit(1)
    return key


_EXPECTED_TOKEN = _require_api_key()


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Reject requests whose Authorization header doesn't match MCP_API_KEY."""

    async def dispatch(self, request: Request, call_next):
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[len("Bearer ") :]
        else:
            token = ""

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
