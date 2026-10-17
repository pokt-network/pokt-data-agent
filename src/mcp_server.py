"""MCP server (stdio transport) for the Pocket Network Data Agent.

Intended for local use with Claude Code and OpenCode — the MCP client spawns
this process directly via stdio.

Entry point for uvx / pipx installs."""

import logging

from src.graphql_validator import live_schema
from src.mcp_utils import create_mcp_server

logging.basicConfig(level=logging.WARNING)


mcp = create_mcp_server()


def main():
    # Load the GraphQL schema the queries are checked against now, in the background, not on the first query
    live_schema()
    mcp.run()


if __name__ == "__main__":
    main()
