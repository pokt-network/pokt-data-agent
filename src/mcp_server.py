"""MCP server (stdio transport) for the Pocket Network Data Agent.

Intended for local use with Claude Code and OpenCode — the MCP client spawns
this process directly via stdio.

Entry point for uvx / pipx installs."""

import logging

from src.mcp_utils import create_mcp_server

logging.basicConfig(level=logging.WARNING)


mcp = create_mcp_server()


def main():
    mcp.run()


if __name__ == "__main__":
    main()
