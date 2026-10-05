"""LangChain data tools for pocket network data (general)."""

import json
import logging
from dataclasses import asdict
from typing import Any, Dict, List, Optional, Tuple

from graphql import GraphQLError, parse
from langchain_core.tools import tool

from src.graphql_client import (
    GRAPHQL_REGISTRY,
    POCKET_NETWORK_DATA_ENDPOINT,
    PocketNetworkAPIClient,
)
from src.graphql_validator import MAX_FIRST, check_query_guards
from src.query_sub_agents import ALL_SUBAGENTS
from src.rpc_client import (
    POCKET_NETWORK_RPC_ENDPOINT,
    RPC_METHODS,
    PocketNetworkRPCClient,
)

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
- Once you selected a method, consider calling \"data_get_method_data\" to obtain more information about it, and \"data_get_method_examples\" to obtain curated working example queries.

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


GET_METHOD_DATA_DESCRIPTION = """Return data (description, field meanings, call data, etc) for a Pocket Network GraphQL or RPC API method.

Args:
    method_name: The name of the requested method (case-sensitive).
    protocol: the name of the requested protocol GraphQL or RPC.
"""
GET_METHOD_DATA_NAME = "data_get_method_data"


@tool(GET_METHOD_DATA_NAME, description=GET_METHOD_DATA_DESCRIPTION)
def get_method_data(method_name: str, protocol: str) -> Dict[str, Any]:
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

    # Return all data except the examples, which are served by "data_get_method_examples"
    method_dict = asdict(methods_data)
    method_dict.pop("examples", None)
    return method_dict


GET_METHOD_EXAMPLES_DESCRIPTION = """Return curated, working example queries for a Pocket Network GraphQL or RPC API method.

The examples are taken from production consumers of the API (such as the Pocket Network explorer) and show
the exact argument names, filters, orderings and aggregations the method supports. Use them as templates
when building a query or writing code against the API. Each example starts with a "#" comment stating its
purpose. Placeholders like "pokt1..." must be replaced with real values before executing.

GraphQL examples are query strings ready for "data_execute_graphql". RPC examples are JSON call
descriptors for "data_execute_rpc", holding the "method_name" and, when needed, "params" (query
parameters) and "path_params" (URL path substitutions).

An empty list means no examples have been curated for that method yet.

Args:
    method_name: The name of the requested method (case-sensitive).
    protocol: the name of the requested protocol GraphQL or RPC.
"""
GET_METHOD_EXAMPLES_NAME = "data_get_method_examples"


@tool(GET_METHOD_EXAMPLES_NAME, description=GET_METHOD_EXAMPLES_DESCRIPTION)
def get_method_examples(method_name: str, protocol: str) -> List[str]:
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

    # Return only the curated examples
    return methods_data.examples


################################################################################
# ---------------------------- EXECUTION TOOLS ---------------------------------
################################################################################

# Longest result handed back to the calling LLM, in characters of JSON.
MAX_RESULT_CHARS = 100_000

EXECUTE_GRAPHQL_DESCRIPTION = f"""Executes a GraphQL method call and returns the result ("data" field) along with a sucess flag and an error string if not success.

Guards, checked before the query is sent:
- Raw payout tables (modToAcctTransfers...) are refused: use the settlement catalog (getIncomeJson, ...).
- Every connection that selects "nodes" or "edges" needs a literal "first" between 1 and {MAX_FIRST}.
A result longer than {MAX_RESULT_CHARS} characters is cut, and the error string says so.

The settlement catalog and legacy... functions raise an error for a range their data does not cover yet: report
that range as "not covered yet", never as 0.

Args:
    query: GraphQL query string to be wrapped into "{{"query": query}}" and posted to the endpoint.

Returns:
    Tuple of (success, result, error_message)
"""
EXECUTE_GRAPHQL_NAME = "data_execute_graphql"


@tool(EXECUTE_GRAPHQL_NAME, description=EXECUTE_GRAPHQL_DESCRIPTION)
def execute_graphql(query: str) -> Tuple[bool, Any, str | None]:
    try:
        guard_error = check_query_guards(parse(query))
    except GraphQLError as e:
        return False, None, f"GraphQL validation error: {e}"
    except RecursionError:
        # parse() recurses once per nested selection: a few hundred levels exceed Python's recursion limit.
        return False, None, "Query too deeply nested"
    if guard_error:
        return False, None, guard_error

    success, result, error = PocketNetworkAPIClient().execute_query(query)
    if success:
        text = json.dumps(result)
        if len(text) > MAX_RESULT_CHARS:
            return (
                True,
                text[:MAX_RESULT_CHARS],
                f"Result truncated to {MAX_RESULT_CHARS} of {len(text)} characters (the JSON is cut): "
                "narrow the range or the filter, or use a coarser bucket.",
            )
    return success, result, error


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


GET_ENDPOINTS_DESCRIPTION = """Return the endpoint URLs this server is currently querying.

Use this to know exactly which Pocket Network backends the data/RPC tools hit, e.g. to confirm
whether the server is pointed at mainnet, testnet or a custom indexer/node before trusting results.

Returns:
    A dict mapping protocol to its configured endpoint URL:
        - graphql: the Pocket Network GraphQL (indexer) endpoint used by "data_execute_graphql".
        - rpc: the Pocket Network RPC (Cosmos SDK REST) endpoint used by "data_execute_rpc".
"""
GET_ENDPOINTS_NAME = "data_get_endpoints"


@tool(GET_ENDPOINTS_NAME, description=GET_ENDPOINTS_DESCRIPTION)
def get_endpoints() -> Dict[str, str]:
    # Report the endpoint URLs the clients are configured with
    return {
        "graphql": POCKET_NETWORK_DATA_ENDPOINT,
        "rpc": POCKET_NETWORK_RPC_ENDPOINT,
    }
