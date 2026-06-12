import os

from mcp.server.fastmcp import FastMCP

from src.mcp_tools import AGENTS_AS_TOOLS, DATA_TOOLS, GENERAL_TOOLS, INSTROPECTION_TOOLS, MCP_SERVER_DESCRIPTION


def create_mcp_server(
    server_name="pokt-data-agent", server_description=MCP_SERVER_DESCRIPTION, server_exposure="endpoints-tools"
):
    # Check envs
    server_exposure = os.getenv("POCKET_NETWORK_MCP_EXPOSURE", server_exposure)

    # Create MCP server
    mcp = FastMCP(
        server_name,
        instructions=server_description,
    )

    # Select the tool set
    match server_exposure:
        case "endpoints-tools":
            server_tools = DATA_TOOLS + INSTROPECTION_TOOLS
        case "sub-agents":
            server_tools = AGENTS_AS_TOOLS[1:]
        case "main-agent":
            server_tools = AGENTS_AS_TOOLS[1]
        case "all-agents":
            server_tools = AGENTS_AS_TOOLS
        case "everything":
            server_tools = DATA_TOOLS + INSTROPECTION_TOOLS + AGENTS_AS_TOOLS
        case _:
            raise ValueError(f'Unkown selected server exposure: "{server_exposure}"')

    # General tools (server/indexer status) are exposed in every mode
    server_tools = tuple(server_tools) + GENERAL_TOOLS

    set_mcp_tools(server_tools, mcp)

    return mcp


def set_mcp_tools(tools_list, mcp):
    # Register every shared tool function
    for tool_fn, tool_name, tool_description in tools_list:
        mcp.tool(name=tool_name, description=tool_description)(tool_fn)
