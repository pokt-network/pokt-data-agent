"""MCP server (stdio transport) for the Pocket Network Data Agent.

Intended for local use with Claude Code and OpenCode — the MCP client spawns
this process directly via stdio.

Usage – add to your MCP config:

  OpenCode (opencode.jsonc):
    {
      "mcp": {
        "pokt-data-agent": {
          "type": "local",
          "command": ["uv", "run", "mcp_server.py"],
          "environment": {
            "LLM_BASE_URL": "https://api.openai.com/v1",
            "LLM_MODEL":    "gpt-4o",
            "OPENAI_API_KEY": "sk-..."
          }
        }
      }
    }

  Claude Code (.mcp.json or ~/.claude.json):
    {
      "mcpServers": {
        "pokt-data-agent": {
          "command": "uv",
          "args": ["run", "mcp_server.py"],
          "cwd": "/path/to/pokt-data-agent",
          "env": {
            "LLM_BASE_URL":  "https://api.openai.com/v1",
            "LLM_MODEL":     "gpt-4o",
            "OPENAI_API_KEY": "sk-..."
          }
        }
      }
    }

Environment variables:
    LLM_BASE_URL   – Base URL for an OpenAI-compatible endpoint
                     (default: http://localhost:8087)
    LLM_MODEL      – Model name to use (default: local)
    OPENAI_API_KEY – API key forwarded to the LLM endpoint (default: not-needed)

For remote/HTTP deployment see mcp_server_remote.py.
"""

import logging

from src.mcp_utils import create_mcp_server

logging.basicConfig(level=logging.WARNING)

# ---------------------------------------------------------------------------
# Server instance
# ---------------------------------------------------------------------------
mcp = create_mcp_server()

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()  # stdio transport – default for Claude Code / OpenCode
