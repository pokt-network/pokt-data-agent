"""LangChain data tools for pocket network data (general)."""

import logging
from typing import Any, Dict, List, Optional, Tuple

from langchain_core.tools import tool

from src.graphql_client import GRAPHQL_REGISTRY, PocketNetworkAPIClient
from src.models import QueryFieldInfo
from src.query_sub_agents import ALL_SUBAGENTS
from src.rpc_client import RPC_METHODS, PocketNetworkRPCClient

logger = logging.getLogger(__name__)


################################################################################
# ----------------------------- LISTING TOOLS ----------------------------------
################################################################################
partitions_dict = {a.name.split("Agent")[0]: a for a in ALL_SUBAGENTS}
partitions_list = ""
for a in partitions_dict.keys():
    partitions_list += f"-{a}\n"
LIST_VALID_METHODS_DESCRIPTION = f"""Return all valid methods from the Pocket Network GraphQL or RPC API

The list is divided by partition, and the returned methdos is a curated list of methods holding correct data. Select one from:
{partitions_list}

Please note:
- GraphQL endpoint have indexed data which is prefered for complex queries.
- Use RPC when you need only real time (last block) data for simple requests.
- Once you selected a method, consider calling \"data_get_method_data\" to obtain more information about it.

Args:
    partition_name: The name of the data partition.
    protocol: the name of the requested protocol GraphQL or RPC.
"""
LIST_VALID_METHODS_NAME = "data_get_valid_methods_by_category"


@tool(LIST_VALID_METHODS_NAME, description=LIST_VALID_METHODS_DESCRIPTION)
def list_valid_methods(partition_name: str, protocol: str) -> List[str]:
    # Check protocol
    protocol = protocol.lower()
    valid_protocols = ["rpc", "graphql"]
    if protocol not in valid_protocols:
        raise RuntimeError(f'Cannot find selected protocol: "{protocol}". Please choose from: {valid_protocols}')

    # Get the reference sub agent for the selected data partition
    subagent_ref = None
    for partition in partitions_dict:
        if partition.lower() == partition_name.strip().lower():
            subagent_ref = partitions_dict[partition]
    if subagent_ref is None:
        raise RuntimeError(
            f'Cannot find selected data partition: "{partition_name}". Please choose from: {list(partitions_dict.keys())}'
        )

    # Get all the methods and registry from this agent
    if protocol == "graphql":
        methods_list = subagent_ref.graphql_methods
        registry = GRAPHQL_REGISTRY
    elif protocol == "rpc":
        methods_list = subagent_ref.rpc_methods
        registry = RPC_METHODS
    else:
        raise ValueError("Internal Error [T1]")

    # Build output
    return [registry.get(m).name for m in methods_list]


GET_METHOD_DATA_DESCRIPTION = """Return data (description, meanings, examples, etc) for a Pocket Network GraphQL or RPC API method.

Args:
    method_name: The name of the requested method (case-sensitive).
    protocol: the name of the requested protocol GraphQL or RPC.
"""
GET_METHOD_DATA_NAME = "data_get_method_data"


@tool(GET_METHOD_DATA_NAME, description=GET_METHOD_DATA_DESCRIPTION)
def get_method_data(method_name: str, protocol: str) -> QueryFieldInfo:
    # Check protocol
    protocol = protocol.lower()
    valid_protocols = ["rpc", "graphql"]
    if protocol not in valid_protocols:
        raise RuntimeError(f'Cannot find selected protocol: "{protocol}". Please choose from: {valid_protocols}')

    # Get selected registry
    if protocol == "graphql":
        registry = GRAPHQL_REGISTRY
    elif protocol == "rpc":
        registry = RPC_METHODS
    else:
        raise ValueError("Internal Error [T1]")

    # Get the method data from registry
    methods_data = registry.get(method_name, None)
    if methods_data is None:
        raise RuntimeError(
            f'Selected method "{method_name}" not found in the list of curated enpoints in the selected protocol "{protocol}". Please note that method names are case-sensitive.'
        )

    # Return all data
    return methods_data


################################################################################
# ---------------------------- EXECUTION TOOLS ---------------------------------
################################################################################

EXECUTE_GRAPHQL_DESCRIPTION = """Executes a GraphQL method call and returns the result ("data" field) along with a sucess flag and an error string if not success.

Args:
    query: GraphQL query string to be wrapped into "{"query": query}" and posted to the endpoint.

Returns:
    Tuple of (success, result, error_message)
"""
EXECUTE_GRAPHQL_NAME = "data_execute_graphql"


@tool(EXECUTE_GRAPHQL_NAME, description=EXECUTE_GRAPHQL_DESCRIPTION)
def execute_graphql(query: str) -> Tuple[bool, Any, str | None]:
    # Execute (this is just a wrapper)
    return PocketNetworkAPIClient().execute_query(query)


EXECUTE_RPC_DESCRIPTION = """Executes a RPC method call and returns the result (json response) along with a sucess flag and an error string if not success.

Args:
    method_name: Logical method name (key in RPC_METHODS).
    params: Optional query-parameter overrides merged on top of the
            method's default_params. Used for query/POST body parameters only.
    path_params: Optional dictionary of path template parameter substitutions
                (e.g., {"address": "pokt1..."} for paths like
                "/cosmos/bank/v1beta1/balances/{address}").
                Required if the method's path contains placeholders.

Returns:
    Tuple of (success, result, error_message).

Raises:
    ValueError: If a required path parameter is missing or if unused
                path_params are provided.
"""
EXECUTE_RPC_NAME = "data_execute_rpc"


@tool(EXECUTE_RPC_NAME, description=EXECUTE_RPC_DESCRIPTION)
def execute_rpc(
    method_name: str,
    params: Optional[Dict[str, Any]] = None,
    path_params: Optional[Dict[str, str]] = None,
) -> Tuple[bool, Any, Optional[str | None]]:
    # Execute (this is just a wrapper)
    return PocketNetworkRPCClient().execute_query(method_name, params, path_params)


################################################################################
# ------------------------------ GENERAL TOOLS ----------------------------------
################################################################################

GET_INDEXER_STATUS_DESCRIPTION = """Return the live status of the Pocket Network GraphQL indexer.

Provides the chain target height, the last indexed (processed) height and timestamp, and a health flag.
Use this to check how fresh the indexed (GraphQL) data is before trusting it: if "lastProcessedHeight"
lags far behind "targetHeight", prefer RPC methods for live-state questions.

Returns:
    Tuple of (success, result, error_message) where result holds the "_metadata" fields:
    targetHeight, lastProcessedHeight, lastProcessedTimestamp (epoch milliseconds),
    lastFinalizedVerifiedHeight and indexerHealthy.
"""
GET_INDEXER_STATUS_NAME = "data_get_indexer_status"

_INDEXER_STATUS_QUERY = (
    "{ _metadata { targetHeight lastProcessedHeight lastProcessedTimestamp "
    "lastFinalizedVerifiedHeight indexerHealthy } }"
)


@tool(GET_INDEXER_STATUS_NAME, description=GET_INDEXER_STATUS_DESCRIPTION)
def get_indexer_status() -> Tuple[bool, Any, str | None]:
    # Execute (this is just a wrapper)
    return PocketNetworkAPIClient().execute_query(_INDEXER_STATUS_QUERY)
