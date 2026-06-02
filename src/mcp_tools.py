"""Shared MCP tool definitions for both stdio and remote server variants.

This module owns the lazy singletons (LLM, API client, agents) and defines
all seven tool functions as plain async coroutines.  Each server entry-point
(mcp_server.py for stdio, mcp_server_remote.py for Streamable HTTP) imports
these functions and registers them with its own FastMCP instance.
"""

import logging
import os
from typing import Any, Dict, List, Optional, Tuple

import anyio

from src.agent import PocketNetworkAgent
from src.models import QueryFieldInfo
from src.query_sub_agents import (
    AccountStateAgent,
    GovernanceAdminAgent,
    NetworkUsageAgent,
    ServiceEconomicsAgent,
    SettlementRewardsAgent,
    StakingParticipantStateAgent,
    TokenomicsAgent,
)
from src.tools_data import (
    EXECUTE_GRAPHQL_DESCRIPTION,
    EXECUTE_GRAPHQL_NAME,
    EXECUTE_RPC_DESCRIPTION,
    EXECUTE_RPC_NAME,
    GET_METHOD_DATA_DESCRIPTION,
    GET_METHOD_DATA_NAME,
    LIST_VALID_METHODS_DESCRIPTION,
    LIST_VALID_METHODS_NAME,
)
from src.tools_data import (
    execute_graphql as _execute_graphql,
)
from src.tools_data import (
    execute_rpc as _execute_rpc,
)
from src.tools_data import (
    get_method_data as _get_method_data,
)
from src.tools_data import (
    list_valid_methods as _list_valid_methods,
)
from src.tools_introspection import (
    GET_ENUM_VALUES_DESCRIPTION,
    GET_ENUM_VALUES_NAME,
    GET_FIELD_SCHEMA_DESCRIPTION,
    GET_FIELD_SCHEMA_NAME,
    GET_TYPE_INFO_DESCRIPTION,
    GET_TYPE_INFO_NAME,
)
from src.tools_introspection import (
    get_enum_values as _get_enum_values,
)
from src.tools_introspection import (
    get_field_schema as _get_field_schema,
)
from src.tools_introspection import (
    get_type_info as _get_type_info,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# MCP Server General Cofigs
# ---------------------------------------------------------------------------
MCP_SERVER_DESCRIPTION = """Tools for querying Pocket Network on-chain data via the Pocket Network GraphQL API."""

################################################################################
# ------------------------------- BASE TOOLS -----------------------------------
################################################################################


async def mcp_list_valid_methods(partition_name: str, protocol: str) -> List[str]:
    return await anyio.to_thread.run_sync(
        lambda: _list_valid_methods.func(partition_name=partition_name, protocol=protocol)
    )


async def mcp_get_method_data(method_name: str, protocol: str) -> QueryFieldInfo:
    return await anyio.to_thread.run_sync(lambda: _get_method_data.func(method_name=method_name, protocol=protocol))


async def mcp_execute_graphql(query: str) -> Tuple[bool, Any, str | None]:
    return await anyio.to_thread.run_sync(lambda: _execute_graphql.func(query=query))


async def mcp_execute_rpc(
    method_name: str,
    params: Optional[Dict[str, Any]] = None,
    path_params: Optional[Dict[str, str]] = None,
) -> Tuple[bool, Any, Optional[str]]:
    return await anyio.to_thread.run_sync(
        lambda: _execute_rpc.func(method_name=method_name, params=params, path_params=path_params)
    )


DATA_TOOLS = (
    [mcp_list_valid_methods, LIST_VALID_METHODS_NAME, LIST_VALID_METHODS_DESCRIPTION],
    [mcp_get_method_data, GET_METHOD_DATA_NAME, GET_METHOD_DATA_DESCRIPTION],
    [mcp_execute_graphql, EXECUTE_GRAPHQL_NAME, EXECUTE_GRAPHQL_DESCRIPTION],
    [mcp_execute_rpc, EXECUTE_RPC_NAME, EXECUTE_RPC_DESCRIPTION],
)

################################################################################
# -------------------------- INSTROPECTION TOOLS -------------------------------
################################################################################


async def mcp_get_field_schema(field_name: str) -> str:
    return await anyio.to_thread.run_sync(lambda: _get_field_schema.func(field_name))


async def mcp_get_type_info(type_name: str) -> str:
    return await anyio.to_thread.run_sync(lambda: _get_type_info.func(type_name))


async def mcp_get_enum_values(enum_name: str) -> str:
    return await anyio.to_thread.run_sync(lambda: _get_enum_values.func(enum_name))


# Convenience tuple used by both server entry-points to register all tools
INSTROPECTION_TOOLS = (
    [mcp_get_field_schema, GET_FIELD_SCHEMA_NAME, GET_FIELD_SCHEMA_DESCRIPTION],
    [mcp_get_type_info, GET_TYPE_INFO_NAME, GET_TYPE_INFO_DESCRIPTION],
    [mcp_get_enum_values, GET_ENUM_VALUES_NAME, GET_ENUM_VALUES_DESCRIPTION],
)

################################################################################
# --------------------------- SUBAGENTS AS TOOLS -------------------------------
################################################################################
MAIN_AGENT_TOOL_NAME = "mainagent"
SUB_AGENT_TOOL_PREFIX = "subagent_"
MCP_TOOL_APPENDIX = """

The returned token (POKT) values are denominated in uPOKT unless it is stated otherwise.
Prefer making multiple simple and well defined queries vs making a big and complex one. Passing specific dates and terminology is advised.

The tool only executes the most relavant GRAPHQL/RCP query related to the natrual language query provided, is up to the user to interpret results which are raw data responses.
The tool may not provide direct answers to your queries, instead returning relevant data that can be operated (by other means) to obtain the query response.

Note that you can get example queries from this tool be asking for a specific query (instead of just asking for data), which are usefull to write code using the returned "notes" field.

Args:
    query: Natural language question.

Returns:
    A dict with keys:
        - success (bool): whether the query succeeded end-to-end.
        - result (dict | None): the raw GraphQL or RPC response data.
        - query (str | None): the generated query.
        - endpoint_type (str | None): the encpoint used: GraphQL or RPC.
        - error (str | None): error message if success is False.
        - notes (str): agent processing notes / explanation and notes for reproducing the query.
"""

MAIN_AGENT_DESCRIPTION = """Query Pocket Network data using natural language.

Automatically routes the query to the most appropriate specialist sub-agent
(network usage, tokenomics, rewards, service economics, governance, or
staking / participant state).

Use this tool when you are unsure which data domain covers the question,
or when the question spans multiple domains.
"""


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------


def _llm_base_url() -> str:
    url = os.environ.get("LLM_BASE_URL", None)
    if url is None:
        raise ValueError("Backend LLM url not set!")
    return url


def _llm_model() -> str:
    name = os.environ.get("LLM_MODEL", None)
    if name is None:
        raise ValueError("Backend LLM model name not set!")
    return name


# ---------------------------------------------------------------------------
# Lazy singletons
# ---------------------------------------------------------------------------

_main_agent: PocketNetworkAgent | None = None
_llm: Any = None
_sub_agents: dict[str, Any] = {}


def _get_llm():
    """Return a shared ChatOpenAI instance, created once on first call."""
    global _llm
    if _llm is None:
        from langchain_openai import ChatOpenAI

        _llm = ChatOpenAI(
            base_url=_llm_base_url(),
            api_key=os.environ.get("OPENAI_API_KEY", "not-needed"),
            model=_llm_model(),
        )
    return _llm


def _get_main_agent() -> PocketNetworkAgent:
    global _main_agent
    if _main_agent is None:
        _main_agent = PocketNetworkAgent(
            llm_base_url=_llm_base_url(),
            llm_model=_llm_model(),
        )
    return _main_agent


def _get_sub_agent(name: str, cls):
    """Return a lazily-initialised sub-agent singleton by class."""
    if name not in _sub_agents:
        _sub_agents[name] = cls(_get_llm())
    return _sub_agents[name]


# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------


def _run_sub_agent(cls, query: str) -> dict:
    """Run *cls* sub-agent: builds, validates, and executes the query."""
    agent = _get_sub_agent(cls.__name__, cls)
    result = agent.build_query(query)
    return {
        "success": result.success,
        "error": result.error or None,
        "query": result.query or None,
        "result": result.query_result,
        "notes": result.explanation or "",
        "endpoint_type": result.endpoint_type,
    }


async def query_pocket_network(query: str) -> dict:
    return await anyio.to_thread.run_sync(lambda: _get_main_agent().invoke(query))


async def query_network_usage(query: str) -> dict:
    return await anyio.to_thread.run_sync(lambda: _run_sub_agent(NetworkUsageAgent, query))


async def query_tokenomics(query: str) -> dict:
    return await anyio.to_thread.run_sync(lambda: _run_sub_agent(TokenomicsAgent, query))


async def query_settlement_rewards(query: str) -> dict:
    return await anyio.to_thread.run_sync(lambda: _run_sub_agent(SettlementRewardsAgent, query))


async def query_service_economics(query: str) -> dict:
    return await anyio.to_thread.run_sync(lambda: _run_sub_agent(ServiceEconomicsAgent, query))


async def query_governance(query: str) -> dict:
    return await anyio.to_thread.run_sync(lambda: _run_sub_agent(GovernanceAdminAgent, query))


async def query_staking_participants(query: str) -> dict:
    return await anyio.to_thread.run_sync(lambda: _run_sub_agent(StakingParticipantStateAgent, query))


async def query_account_state(query: str) -> dict:
    return await anyio.to_thread.run_sync(lambda: _run_sub_agent(AccountStateAgent, query))


AGENTS_AS_TOOLS = (
    [query_pocket_network, MAIN_AGENT_TOOL_NAME, MAIN_AGENT_DESCRIPTION + MCP_TOOL_APPENDIX],
    [
        query_network_usage,
        f"{SUB_AGENT_TOOL_PREFIX}{NetworkUsageAgent.name}",
        NetworkUsageAgent.description + MCP_TOOL_APPENDIX,
    ],
    [
        query_tokenomics,
        f"{SUB_AGENT_TOOL_PREFIX}{TokenomicsAgent.name}",
        TokenomicsAgent.description + MCP_TOOL_APPENDIX,
    ],
    [
        query_settlement_rewards,
        f"{SUB_AGENT_TOOL_PREFIX}{SettlementRewardsAgent.name}",
        SettlementRewardsAgent.description + MCP_TOOL_APPENDIX,
    ],
    [
        query_service_economics,
        f"{SUB_AGENT_TOOL_PREFIX}{ServiceEconomicsAgent.name}",
        ServiceEconomicsAgent.description + MCP_TOOL_APPENDIX,
    ],
    [
        query_governance,
        f"{SUB_AGENT_TOOL_PREFIX}{GovernanceAdminAgent.name}",
        GovernanceAdminAgent.description + MCP_TOOL_APPENDIX,
    ],
    [
        query_staking_participants,
        f"{SUB_AGENT_TOOL_PREFIX}{StakingParticipantStateAgent.name}",
        StakingParticipantStateAgent.description + MCP_TOOL_APPENDIX,
    ],
    [
        query_account_state,
        f"{SUB_AGENT_TOOL_PREFIX}{AccountStateAgent.name}",
        AccountStateAgent.description + MCP_TOOL_APPENDIX,
    ],
)
