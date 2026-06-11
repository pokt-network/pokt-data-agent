"""MCP server (stdio transport) for the Pocket Network Data Agent.

Intended for local use with Claude Code and OpenCode — the MCP client spawns
this process directly via stdio.

Quick start (no clone required) – Claude Code / Cowork:

claude mcp add pokt-data-agent -- uvx --from git+https://github.com/pokt-foundation/pokt-data-agent pokt-data-agent-mcp

  Or add manually to ~/.claude/claude_mcp_config.json:
    {
      "mcpServers": {
        "pokt-data-agent": {
          "command": "uvx",
          "args": ["--from", "git+https://github.com/pokt-foundation/pokt-data-agent", "pokt-data-agent-mcp"]
        }
      }
    }

  No environment variables are required for the default endpoints-only mode
  (POCKET_NETWORK_MCP_EXPOSURE=endpoints-tools).  LLM variables are only needed
  when using agent tools (see POCKET_NETWORK_MCP_EXPOSURE below).

Advanced – local clone (OpenCode / Claude Code):

  OpenCode (opencode.jsonc):
    {
      "mcp": {
        "pokt-data-agent": {
          "type": "local",
          "command": ["uv", "run", "mcp_server.py"]
        }
      }
    }

  Claude Code (.mcp.json):
    {
      "mcpServers": {
        "pokt-data-agent": {
          "command": "uv",
          "args": ["run", "mcp_server.py"],
          "cwd": "/path/to/pokt-data-agent"
        }
      }
    }

Environment variables (all optional for default mode):
    POCKET_NETWORK_MCP_EXPOSURE – Tool set to expose (default: endpoints-tools).
                     Options: endpoints-tools | sub-agents | main-agent |
                              all-agents | everything
    LLM_BASE_URL   – Base URL for an OpenAI-compatible endpoint.
                     Required only for agent tool modes.
    LLM_MODEL      – Model name to use.  Required only for agent tool modes.
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


def main():
    mcp.run()  # stdio transport – default for Claude Code / OpenCode


if __name__ == "__main__":
    main()
