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
    MCP_ALLOWED_HOSTS   – Comma-separated Host header allowlist (e.g.
                          "example.com:*,127.0.0.1:*"). If set, enables
                          DNS-rebinding Host/Origin validation on top of the
                          Bearer-token check. Unset (default): disabled, since
                          MCP_API_KEY is already the access boundary and a
                          remote deployment's Host header is unpredictable
                          (domain, reverse proxy, Docker port mapping, ...).
    MCP_ALLOWED_ORIGINS – Comma-separated Origin header allowlist, same rules.
    MCP_JSON_RESPONSE    – "true" to answer Streamable HTTP calls (initialize,
                           tools/call, ...) with a single application/json
                           response instead of an SSE event stream. Useful
                           behind proxies/relays that buffer or mangle SSE.
                           Default: false (upstream SDK default).
    MCP_STATELESS_HTTP   – "true" to disable server-side session state, so
                           each request is handled independently (no
                           mcp-session-id affinity needed) — a fresh
                           transport per request. Useful when running
                           multiple replicas behind a load balancer with no
                           sticky sessions. Default: false.

For local / stdio use see mcp_server.py.
"""

import hmac
import logging
import os
import sys

import uvicorn
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from src.mcp_utils import create_mcp_server

logging.basicConfig(level=logging.WARNING)


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


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
# Transport security (Host / Origin header validation)
# ---------------------------------------------------------------------------
#
# The MCP SDK's DNS-rebinding protection only auto-enables itself for
# host == "127.0.0.1" / "localhost" / "::1" and, when it does, only accepts
# requests whose Host header matches one of those — which breaks any real
# remote deployment (a domain name, a reverse proxy, a Docker port mapping
# reached as e.g. "myhost:8000") with a 421 "Invalid Host header".
#
# This server already gates every request on MCP_API_KEY (see
# BearerAuthMiddleware below), which is the actual access boundary for a
# remote deployment, so Host/Origin header checking is disabled by default.
# Set MCP_ALLOWED_HOSTS / MCP_ALLOWED_ORIGINS (comma-separated, entries may
# end in ":*" to allow any port) to re-enable it as defense-in-depth.

_allowed_hosts = [h.strip() for h in os.environ.get("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
_allowed_origins = [o.strip() for o in os.environ.get("MCP_ALLOWED_ORIGINS", "").split(",") if o.strip()]

_transport_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=bool(_allowed_hosts or _allowed_origins),
    allowed_hosts=_allowed_hosts,
    allowed_origins=_allowed_origins,
)

# ---------------------------------------------------------------------------
# Server instance
# ---------------------------------------------------------------------------

mcp = create_mcp_server(
    transport_security=_transport_security,
    json_response=_env_bool("MCP_JSON_RESPONSE"),
    stateless_http=_env_bool("MCP_STATELESS_HTTP"),
)

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
