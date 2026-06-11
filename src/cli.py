"""Entry point for uvx / pipx installs."""

import logging

from src.mcp_utils import create_mcp_server

logging.basicConfig(level=logging.WARNING)


def main():
    mcp = create_mcp_server()
    mcp.run()
