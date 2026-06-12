"""Sub-agents for building GraphQL or RPC queries for different data types."""

import json
import logging
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Dict, List, Optional

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from src.graphql_client import GRAPHQL_REGISTRY, POCKET_NETWORK_DATA_ENDPOINT, PocketNetworkAPIClient
from src.graphql_validator import validate_graphql_query
from src.models import QueryFieldInfo, SubAgentResult
from src.rpc_client import POCKET_NETWORK_RPC_ENDPOINT, RPC_METHODS, PocketNetworkRPCClient
from src.rpc_validator import validate_rpc_call
from src.tools_introspection import INTROSPECTION_TOOLS

logger = logging.getLogger(__name__)

MAX_RETRIES = 5


class QuerySubAgentStateDict(TypedDict):
    """State dictionary for a subagent LangGraph."""

    user_query: str
    attempt_count: int
    query: str
    validation_error: Optional[str]
    llm_error: Optional[str]
    success: bool
    explanation: str
    endpoint_type: str
    endpoint_method: str
    # Raw result from the API/RPC execution – populated by execute_query node.
    query_result: Optional[Any]
    execution_error: Optional[str]


class QueryBuilderSubAgent(ABC):
    """Base class for query builder sub-agents."""

    def __init__(self, llm: ChatOpenAI):
        self.llm = llm
        self.llm_with_tools = llm.bind_tools(INTROSPECTION_TOOLS)
        self.graphql_client = PocketNetworkAPIClient()
        self.rpc_client = PocketNetworkRPCClient()
        self.graph = self._build_graph()

    @abstractmethod
    def get_system_prompt(self) -> str:
        """Return the system prompt for this sub-agent."""
        pass

    # ------------------------------------------------------------------
    # Graph construction
    # ------------------------------------------------------------------

    def _build_graph(self):
        """Build the subagent LangGraph with retry logic."""
        workflow = StateGraph(QuerySubAgentStateDict)

        workflow.add_node("build_query", self._node_build_query)
        workflow.add_node("validate_query", self._node_validate_query)
        workflow.add_node("execute_query", self._node_execute_query)

        workflow.add_edge(START, "build_query")
        workflow.add_edge("build_query", "validate_query")
        workflow.add_conditional_edges(
            "validate_query",
            self._should_retry,
            {
                "retry": "build_query",
                "done": "execute_query",
            },
        )
        workflow.add_conditional_edges(
            "execute_query",
            self._should_retry,
            {
                "retry": "build_query",
                "done": END,
            },
        )

        return workflow.compile()

    def _should_retry(self, state: QuerySubAgentStateDict) -> str:
        """Decide whether to retry or finish."""
        if state["success"]:
            logger.debug(
                "[%s] Query valid on attempt %d. Done.",
                self.name,
                state["attempt_count"],
            )
            return "done"

        # LLM detected a semantic error (explained refusal) – no point retrying.
        if state.get("explanation"):
            logger.warning(
                "[%s] Agent-level refusal after attempt %d: %s",
                self.name,
                state["attempt_count"],
                state["explanation"],
            )
            return "done"

        if state["attempt_count"] >= MAX_RETRIES:
            logger.warning(
                "[%s] Max retries (%d) reached. Giving up.",
                self.name,
                MAX_RETRIES,
            )
            return "done"

        logger.info(
            "[%s] Attempt %d failed, retrying…",
            self.name,
            state["attempt_count"],
        )
        return "retry"

    # ------------------------------------------------------------------
    # Nodes
    # ------------------------------------------------------------------

    def _node_build_query(self, state: QuerySubAgentStateDict) -> Dict[str, Any]:
        """LLM node: build (or retry) the query (GraphQL or RPC)."""
        attempt = state.get("attempt_count", 0) + 1
        logger.info("[%s] build_query – attempt %d/%d", self.name, attempt, MAX_RETRIES)

        tools_hint = (
            "GrapQL tool hints:\n"
            "1. NEVER assume a GraphQL field or filter is available/unavailable without first verifying it with the tools.\n"
            "2. Only return a JSON error object after you have confirmed the request is impossible via both GraphQL and RPC.\n"
            "3. They hold NO information for RPC methods.\n"
        )
        system_prompt = (
            self.get_system_prompt()
            + tools_hint
            + f"""
When a partial date is provided, assume that the user is referring to current year, month or day (depending what is missing).
For example:
    - A user requesting "data for March" means from "20xx-03-01" to "20xx-03-31" (inclusive), where the year is the current year.
    - A user requesting "first quarter" means from "20xx-01-01" to "20xx-04-31" (inclusive), where the year is the current year.
(Allways ensure start and end-dates are not the same)
CURRENT DATE: {datetime.now().isoformat()}"""
        )

        user_message = self._build_user_message(state, attempt)

        try:
            messages = [
                SystemMessage(content=system_prompt),
                HumanMessage(content=user_message),
            ]

            # Tool-call loop: keep invoking until the LLM stops requesting tools
            tool_map = {t.name: t for t in INTROSPECTION_TOOLS}
            while True:
                response = self.llm_with_tools.invoke(messages)
                messages.append(response)

                if not response.tool_calls:
                    break  # LLM produced a final answer

                logger.info(
                    "[%s] LLM requested %d tool call(s) on attempt %d: %s",
                    self.name,
                    len(response.tool_calls),
                    attempt,
                    [tc["name"] for tc in response.tool_calls],
                )
                for tc in response.tool_calls:
                    tool_name = tc["name"]
                    tool_args = tc["args"]
                    logger.debug(
                        "[%s] Calling tool %r with args %s",
                        self.name,
                        tool_name,
                        tool_args,
                    )
                    try:
                        tool_result = tool_map[tool_name].invoke(tool_args)
                    except Exception as tool_exc:
                        tool_result = f"Tool error: {tool_exc}"
                        logger.warning("[%s] Tool %r failed: %s", self.name, tool_name, tool_exc)
                    messages.append(ToolMessage(content=str(tool_result), tool_call_id=tc["id"]))

            raw = response.content.strip()

            # Strip markdown fences
            if raw.startswith("```"):
                raw = "\n".join(raw.split("\n")[1:])
            if raw.endswith("```"):
                raw = "\n".join(raw.split("\n")[:-1])
            raw = raw.strip()

            # The LLM always returns a JSON envelope. Parse it here so we can
            # detect an agent-level refusal early (envelope with "error" key).
            # The raw JSON string is passed forward as-is; _node_validate_query
            # is responsible for full structural validation.
            try:
                envelope = json.loads(raw)
                if isinstance(envelope, dict) and envelope.get("error") is not None:
                    logger.warning(
                        "[%s] LLM returned refusal on attempt %d: %s",
                        self.name,
                        attempt,
                        envelope["error"],
                    )
                    return {
                        "attempt_count": attempt,
                        "query": "",
                        "validation_error": None,
                        "llm_error": None,
                        "success": False,
                        "explanation": envelope.get("error", "Unknown agent error"),
                        "endpoint_type": "",
                    }
            except json.JSONDecodeError as exc:
                error_msg = f"LLM output is not valid JSON: {exc}"
                logger.warning("[%s] %s", self.name, error_msg)
                return {
                    "attempt_count": attempt,
                    "query": "",
                    "validation_error": None,
                    "llm_error": None,
                    "success": False,
                    "explanation": error_msg,
                    "endpoint_type": "",
                }

            logger.debug("[%s] LLM produced output on attempt %d:\n%s", self.name, attempt, raw)
            return {
                "attempt_count": attempt,
                "query": raw,
                "validation_error": None,
                "llm_error": None,
                "success": False,  # confirmed by validate_query
                "explanation": "",
                "endpoint_type": "",  # written by validate_query after parsing envelope
            }

        except Exception as exc:
            logger.exception("[%s] LLM call failed on attempt %d.", self.name, attempt)
            return {
                "attempt_count": attempt,
                "query": "",
                "validation_error": None,
                "llm_error": f"LLM error: {str(exc)}",
                "success": False,
                "explanation": "",
                "endpoint_type": "",
            }

    # ------------------------------------------------------------------
    # User-message builder
    # ------------------------------------------------------------------

    def _build_user_message(self, state: QuerySubAgentStateDict, attempt: int) -> str:
        """Construct the unified user message exposing both GraphQL fields and RPC methods."""

        # -- GraphQL fields block ----------------------------------------
        graphql_section = ""
        methods_lines = []
        for field_key in self.graphql_methods:
            field_info = GRAPHQL_REGISTRY.get(field_key)
            if not field_info:
                logger.error("[%s] Field '%s' not found in registry.", self.name, field_key)
                continue
            methods_lines.append(self._format_field_info(field_info))

        if methods_lines:
            graphql_section = "\n## GRAPHQL methods\n" + "\n".join(methods_lines) + "\n"
            graphql_section += """
--------------------------------------------------------------------------------
"""

        # -- RPC methods block (only if this agent has any) ---------------
        rpc_section = ""
        available_rpc = self.rpc_methods
        if available_rpc:
            methods_lines = []
            for method_name in available_rpc:
                field_info = RPC_METHODS.get(method_name)
                if field_info:
                    methods_lines.append(self._format_field_info(field_info))
                else:
                    logger.error(
                        "[%s] RPC method '%s' not found in RPC_METHODS registry.",
                        self.name,
                        method_name,
                    )
            if methods_lines:
                rpc_section = (
                    "\n## RPC methods (live chain state – use when GraphQL cannot answer)\n"
                    + "\n".join(methods_lines)
                    + "\n"
                )
                rpc_section += """
--------------------------------------------------------------------------------
"""

        # -- Retry hint ---------------------------------------------------
        retry_hint = ""
        if attempt > 1:
            if state.get("validation_error"):
                retry_hint = (
                    f"\n\nPREVIOUS ATTEMPT FAILED WITH THIS VALIDATION ERROR – fix it:\n{state['validation_error']}\n"
                )
                logger.debug("[%s] Injecting retry hint: %s", self.name, state["validation_error"])
            else:
                # This is an excution error
                retry_hint = (
                    f"\n\nPREVIOUS ATTEMPT FAILED WITH THIS EXECUTION ERROR – fix it:\n{state['execution_error']}\n"
                )
                logger.debug("[%s] Injecting retry hint: %s", self.name, state["execution_error"])

            retry_hint += """
--------------------------------------------------------------------------------
"""

        # -- Output format instructions -----------------------------------
        if available_rpc:
            output_instructions = """\
Decide which data source best answers the query, then return a single JSON object.

For GraphQL (preferred – indexed/historical data):
{
    "endpoint_type": "graphql",
    "endpoint_method": "<selected_method_name>",
    "query": "<valid GraphQL query string>"
}

For RPC (live chain state):
{
    "endpoint_type": "rpc",
    "endpoint_method": "<selected_method_name>",
    "params": { ... },
    "path_params": { ... }
}
Omit "params" or leave it as {} if the method defaults are sufficient.
Omit "path_params" or leave it as {} if the method does not requiere path parameters.

If the request cannot be fulfilled by either source:
{
    "endpoint_type": "error",
    "endpoint_method": "error",
    "error": "<brief explanation>"
}
"""
        else:
            output_instructions = """\
Return a single JSON object:
{
    "endpoint_type": "graphql",
    "endpoint_method": "<selected_method_name>",
    "query": "<valid GraphQL query string>"
}

If the query cannot be constructed:
{
    "endpoint_type": "error",
    "endpoint_method": "error",
    "error": "<brief explanation>"
}
"""

        return f"""User query: {state["user_query"]}

{graphql_section}
{rpc_section}
{retry_hint}
Generate a query that answers the user's question.
For GraphQL: use one of the given fields with appropriate filters and aggregations (if needed).
Keep queries simple. Check for pagination. Pass numerical fields in quotes.

{output_instructions}

Return ONLY the JSON object, no extra text or markdown."""

    def _node_validate_query(self, state: QuerySubAgentStateDict) -> Dict[str, Any]:
        """Validation node: parse the JSON envelope and validate the inner payload."""
        logger.info(
            "[%s] validate_query – attempt %d/%d",
            self.name,
            state["attempt_count"],
            MAX_RETRIES,
        )

        # If upstream already flagged an error, skip validation
        if state.get("llm_error") or state.get("explanation"):
            logger.debug("[%s] Skipping validation due to upstream error/refusal.", self.name)
            return {}

        if not state.get("query"):
            logger.warning("[%s] Empty query, marking as failed.", self.name)
            return {"validation_error": "Empty query generated", "success": False}

        # -- Parse the JSON envelope --------------------------------------
        try:
            envelope = json.loads(state["query"])
        except json.JSONDecodeError as exc:
            error_msg = f"LLM output is not valid JSON: {exc}"
            logger.warning("[%s] %s", self.name, error_msg)
            return {"validation_error": error_msg, "success": False}

        if not isinstance(envelope, dict):
            return {
                "validation_error": "LLM output must be a JSON object.",
                "success": False,
            }

        endpoint_type = envelope.get("endpoint_type", "")
        endpoint_method = envelope.get("endpoint_method", "")

        # -- Route by endpoint_type declared in the envelope --------------
        if endpoint_type == "graphql":
            inner_query = envelope.get("query", "")
            if not inner_query:
                return {
                    "validation_error": 'Envelope has endpoint_type "graphql" but "query" field is missing or empty.',
                    "success": False,
                }
            is_valid, normalized, error_msg = validate_graphql_query(inner_query, endpoint_method)
            if is_valid:
                logger.info(
                    "[%s] GraphQL query validated on attempt %d.",
                    self.name,
                    state["attempt_count"],
                )
                return {
                    "query": normalized,
                    "endpoint_type": "graphql",
                    "endpoint_method": endpoint_method,
                    "success": True,
                    "validation_error": None,
                }
            logger.warning(
                "[%s] GraphQL validation failed on attempt %d: %s",
                self.name,
                state["attempt_count"],
                error_msg,
            )
            return {"validation_error": error_msg, "success": False}

        elif endpoint_type == "rpc":
            if not self.rpc_methods:
                return {
                    "validation_error": (
                        'Envelope declares endpoint_type "rpc" but this agent has no RPC methods configured.'
                    ),
                    "success": False,
                }
            # Re-serialise only the RPC fields so validate_rpc_call can parse them
            rpc_descriptor = json.dumps(envelope)
            is_valid, normalized, error_msg = validate_rpc_call(rpc_descriptor)
            if is_valid:
                logger.info(
                    "[%s] RPC descriptor validated on attempt %d.",
                    self.name,
                    state["attempt_count"],
                )
                return {
                    "query": json.dumps(normalized),
                    "endpoint_type": "rpc",
                    "endpoint_method": endpoint_method,
                    "success": True,
                    "validation_error": None,
                }
            logger.warning(
                "[%s] RPC validation failed on attempt %d: %s",
                self.name,
                state["attempt_count"],
                error_msg,
            )
            return {"validation_error": error_msg, "success": False}

        else:
            error_msg = (
                f'Unknown or missing "endpoint_type" in envelope: {endpoint_type!r}. Expected "graphql" or "rpc".'
            )
            logger.warning("[%s] %s", self.name, error_msg)
            return {"validation_error": error_msg, "success": False}

    def _node_execute_query(self, state: QuerySubAgentStateDict) -> Dict[str, Any]:
        """Execution node: run the validated query against the correct endpoint."""
        logger.info(
            "[%s] execute_query – endpoint=%s",
            self.name,
            state.get("endpoint_type"),
        )

        # If the query was not successfully built/validated, skip execution.
        if not state.get("success"):
            logger.debug("[%s] Skipping execution – query not valid.", self.name)
            return {}

        endpoint_type = state.get("endpoint_type", "graphql")

        if endpoint_type == "rpc":
            try:
                descriptor = json.loads(state["query"])
            except json.JSONDecodeError as exc:
                logger.error("[%s] Invalid RPC descriptor: %s", self.name, exc)
                return {"execution_error": f"Invalid RPC descriptor: {exc}"}

            method = descriptor.get("method")
            params = descriptor.get("params", {})
            path_params = descriptor.get("path_params", {})
            success, result, error_msg = self.rpc_client.execute_query(method, params, path_params)
        else:
            success, result, error_msg = self.graphql_client.execute_query(state["query"])

        if success:
            logger.info("[%s] %s query executed successfully.", self.name, endpoint_type.upper())
            return {
                "query_result": result,
                "execution_error": None,
            }

        logger.error("[%s] %s execution failed: %s", self.name, endpoint_type.upper(), error_msg)
        return {"execution_error": error_msg}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def build_query(self, user_query: str) -> SubAgentResult:
        """
        Run the subagent graph and return a SubAgentResult.

        This is the entry point called by the main agent.
        """
        logger.info("[%s] Starting query-build graph for: %r", self.name, user_query)

        initial_state: QuerySubAgentStateDict = {
            "user_query": user_query,
            "attempt_count": 0,
            "query": "",
            "validation_error": None,
            "llm_error": None,
            "success": False,
            "explanation": "",
            "endpoint_type": "",
            "query_result": None,
            "execution_error": None,
        }

        final_state = self.graph.invoke(initial_state)

        endpoint_type = final_state.get("endpoint_type")
        endpoint_method = final_state.get("endpoint_method")

        # Query build/validation failed – agent-level refusal
        if final_state.get("explanation"):
            logger.warning(
                "[%s] graph: agent refusal – %s",
                self.name,
                final_state["explanation"],
            )
            return SubAgentResult(
                query="",
                explanation=final_state["explanation"],
                success=False,
                error=None,
                endpoint_type=endpoint_type,
            )

        # Query build/validation failed – hard error
        if not final_state["success"]:
            error = final_state.get("llm_error") or final_state.get("validation_error") or "Unknown error"
            logger.error(
                "[%s] graph failed after %d attempts: %s",
                self.name,
                final_state["attempt_count"],
                error,
            )
            return SubAgentResult(
                query="",
                explanation="",
                success=False,
                error=error,
                endpoint_type=endpoint_type,
            )

        # Execution failed
        if final_state.get("execution_error"):
            error_msg = final_state["execution_error"]
            logger.error("[%s] execution failed: %s", self.name, error_msg)
            return SubAgentResult(
                query=final_state["query"],
                explanation="",
                success=False,
                error=error_msg,
                endpoint_type=endpoint_type,
            )

        # Full success
        logger.info("[%s] graph succeeded.", self.name)
        # build used method description
        if endpoint_type == "rpc":
            this_method = RPC_METHODS.get(endpoint_method)
            used_method_description = f"Endpoint: {POCKET_NETWORK_RPC_ENDPOINT}\n"
            used_method_description += this_method.description + "\n"
            used_method_description += json.dumps(this_method.method_data, indent=4)
        else:
            used_method_description = f"Endpoint: {POCKET_NETWORK_DATA_ENDPOINT}\n"
            used_method_description += GRAPHQL_REGISTRY.get(endpoint_method).description
        return SubAgentResult(
            query=final_state["query"],
            explanation=f"Used method description: {used_method_description}",
            success=True,
            error=None,
            endpoint_type=endpoint_type,
            query_result=final_state.get("query_result"),
        )

    def _format_field_info(self, field_info: QueryFieldInfo) -> str:
        """Format field information for the LLM.

        Only includes stable, high-level hints. Filter details, aggregation
        keys and enum values are intentionally omitted — the model should
        discover those via the introspection tools to get the authoritative
        live schema instead of relying on a potentially stale static list.
        """

        formated_field_info = f"- {field_info.name}: {field_info.description}"
        # Build notes
        if len(field_info.fields_notes) > 0:
            formated_field_info += "\t[Hint] Associated name meanings:"
            for field_name in field_info.fields_notes.keys():
                formated_field_info += f"\t\t{field_name}: {field_info.fields_notes[field_name]}\n"
        return formated_field_info


class NetworkUsageAgent(QueryBuilderSubAgent):
    description = (
        "Analyzes network-wide usage and throughput over time, including "
        "relays, computed units, claimed tokens, per-block activity, service "
        "traffic, and average supplier presence by service."
        "Not suitable for granular queries that specify an entity address "
        "like a supplier, application, wallet/address, etc."
    )
    name = "NetworkUsageAgent"
    graphql_methods = [
        "getRewardsByDate",
        "relayByBlockAndServices",
        "getRelaysByServicePerPointJson",
        "blocks",
        "getAmountOfBlocksAndSuppliersByTimes",
        "getLatestBlocksByDay",
        "getSuppliersStakedAndBlocksByPointJson",
    ]
    rpc_methods = []

    def __init__(self, llm: ChatOpenAI):
        super().__init__(llm)

    def get_system_prompt(self) -> str:
        return """You are an expert query builder and executor for Pocket Network network usage analytics.
You specialize in retrieving and summarizing network-wide activity such as relays, computed units, claimed tokens, block-level metrics, and service traffic distribution.
Your queries should prefer the most aggregated get... methods available, especially for date-range analysis, service-level traffic, and block/time summaries.
Always apply a filter when possible, such as a date range, block range, service identifier, or time window, because the dataset is large.

If the user asks for service traffic concentration, averages, or trends across services, use service-grouped aggregations and time truncation when possible.
If the user asks for the daily evolution of staked actors (validators, suppliers, apps, gateways) or supply, prefer getLatestBlocksByDay, which returns one block snapshot per day.
If the user asks about blocks, interpret “block number” as the id of a block, which is numeric.
If the user provides a service name or identifier, filter by that service when applicable.

The user might use the following expressions:
- “block number”: this is the id of a block, which is a number.
- “relays”: The user normally refers to "estimated relays" which is the expanded value after applying the service difficulty, use that if available.
"""


class TokenomicsAgent(QueryBuilderSubAgent):
    description = (
        "Tracks the network’s monetary behavior, including total and daily "
        "supply, supply composition, mint and burn flows, DAO treasury "
        "balance, staked/unstaking tokens and the evolution of key pricing "
        " parameters like the compute-units-to-tokens multiplier."
        " Use this agent when the question is about the supply, its "
        "composition (staked/migrated tokens) or growth."
    )
    name = "TokenomicsAgent"
    graphql_methods = [
        "getTotalSupplyBetweenDates",
        "getSupplyCompositionBetweenDates",
        "getTotalSupplyByDay",
        "getMintBreakdownBetweenDates",
        "getBurnBreakdownBetweenDates",
        "getComputeUnitsToTokensMultiplierEvolution",
        "getDaoBalanceAtHeight",
        "morseClaimableAccounts",
    ]
    rpc_methods = [
        "get_total_supply",
    ]

    def __init__(self, llm: ChatOpenAI):
        super().__init__(llm)

    def get_system_prompt(self) -> str:
        return """You are an expert query builder and executor for Pocket Network tokenomics analytics.
You specialize in supply behavior, minting, burning, inflation, reimbursement, DAO holdings, and token conversion mechanics over time.
Your queries should prefer get... methods that summarize monetary behavior over a time range, especially when the user asks for trends, snapshots, or comparisons across periods.
Always apply a filter when possible, such as a date range or block height, because the dataset is large and the time dimension is essential for tokenomics.

If the user asks for the DAO balance, use the block height when provided; if height is 0, treat it as the latest block.
If the user asks about “supply composition,” prefer the method that breaks supply into staked, unstaked, supplier, application, DAO treasury, wrapped POKT, and related categories.
If the user asks about migration progress or un-migrated supply, use morseClaimableAccounts with groupedAggregates(groupBy: CLAIMED); the unclaimed amounts are supply not yet migrated from Morse to Shannon.
If the user asks for trends, use grouped-by-date outputs and the coarsest useful interval.

The user might use the following expressions:
- “block height”: this is the numeric block height used for snapshot queries.
"""


class SettlementRewardsAgent(QueryBuilderSubAgent):
    description = (
        "Explains how network usage turns into payouts or penalties by "
        "tracking claim settlement and expiration, proof outcomes, "
        "sessions, granular transfers, and reward attribution across "
        "addresses, suppliers, dates, and services."
        "Usefull for queries that requiere actor (applications, suppliers, "
        "etc) specific data."
    )
    name = "SettlementRewardsAgent"

    graphql_methods = [
        "eventClaimSettleds",
        "eventClaimExpireds",
        "modToAcctTransfers",
        "getClaimProofsDataByTime",
        "getClaimProofsDataByDelegatorsAndTime",
        "getRewardsByAddressesAndTime",
        "getRewardsByAddressesAndTimeGroupByService",
        "getRewardsBySuppliersAndTimeGroupByAddressAndDate",
        "getRewardsBySuppliersAndTimeGroupByService",
    ]
    rpc_methods = [
        "get_claim",
        "get_all_claims",
        "get_proof",
        "get_all_proofs",
        "get_session",
    ]

    def __init__(self, llm: ChatOpenAI):
        super().__init__(llm)

    def get_system_prompt(self) -> str:
        return """You are an expert query builder and executor for Pocket Network claim settlement and reward attribution.
You specialize in claims, proofs, settlements, expirations, account-level payouts, supplier earnings, and reward flows across addresses, suppliers, services, and time ranges.
Your queries should strongly prefer the most specific get... methods for grouped reward analysis, and use event-level data when the user needs claim lifecycle details or penalties.
Always apply a filter when possible, such as a date range, address list, supplier list, or service grouping, because the dataset is large.

If the user asks only for the total rewards of one or more addresses with no breakdown, prefer getRewardsByAddressesAndTime, which is the cheapest rollup.
If the user asks for exact account earnings or the most granular payout view, prefer modToAcctTransfers.
If the user asks for claim lifecycle health, use settlement and expiration events together with claim-proof summaries.
If the user provides an address, it is usually a pokt1... entity. For reward attribution, distinguish carefully between output addresses, supplier/operator addresses, and delegator-related addresses.
If the user asks for a “node” and the context is ambiguous, disambiguate between supplier and application.

The user might use the following expressions:
- “address”: usually a pokt1... account.
- “node”: may mean supplier or application depending on context.
- “relays”: The user normally refers to "estimated relays" which is the expanded value after applying the service difficulty, use that if available.
"""


class ServiceEconomicsAgent(QueryBuilderSubAgent):
    description = (
        "Studies service-level demand and profitability, including traffic, "
        "claimed value, supplier density, service metadata, domain-level "
        "performance, and relay-mining difficulty changes that affect CU "
        "estimation and rewards."
        "Not suitable for granular queries that specify an entity (like) a "
        "supplier, application, wallet/address, etc."
    )
    name = "ServiceEconomicsAgent"
    graphql_methods = [
        "services",
        "relayByBlockAndServices",
        "getRelaysByServicePerPointJson",
        "getAmountOfBlocksAndSuppliersByTimes",
        "eventRelayMiningDifficultyUpdateds",
        "getRewardsByDomainsAndTimeGroupByService",
        "getSupplierStatsByDomains",
        "getRewardsBySuppliersAndTimeGroupByService",
        "servicesPerformanceBetweenTimes",
        "getSuppliersStakedAndBlocksByPointJson",
        "applicationServices",
    ]
    rpc_methods = [
        "get_all_services",
        "get_service",
        "get_relay_mining_difficulty",
        "get_all_relay_mining_difficulties",
    ]

    def __init__(self, llm: ChatOpenAI):
        super().__init__(llm)

    def get_system_prompt(self) -> str:
        return """You are an expert query builder and executor for Pocket Network service-level economics.
You specialize in service metadata, traffic concentration, service profitability, relay-mining difficulty, supplier density, and domain-based service analysis.
Your queries should prefer get... methods that summarize service performance or reward distribution by service, and use service metadata or difficulty events when the user needs context about why a service behaves a certain way.
Always apply a filter when possible, such as service ID, domain list, or date range, because service-level analysis is large and time-dependent.

If the user asks which services are most active, most profitable, or have the most supplier support, use grouped and time-truncated service analytics.
If the user asks to compare service performance between two periods (growth/decline), prefer servicesPerformanceBetweenTimes, which compares a current and a previous window in one call.
If the user asks about relay-mining difficulty, interpret it as the mechanism that affects the relationship between claimed computed units and estimated computed units.
If the user provides a service identifier or service name, filter by that service whenever possible.
If the user asks for does not clarify it, supose that i

The user might use the following expressions:
- “service ID”/“chain”: the on-chain service identifier used in queries.
- “relays”: The user normally refers to "estimated relays" which is the expanded value after applying the service difficulty, use that if available.
"""


class GovernanceAdminAgent(QueryBuilderSubAgent):
    description = (
        "Tracks protocol configuration and administrative control, "
        "including parameter values over time, account authorizations, "
        "and treasury visibility needed to understand who can change "
        "network behavior and under what rules."
    )
    name = "GovernanceAdminAgent"
    graphql_methods = [
        "params",
        "authzs",
        "getDaoBalanceAtHeight",
        "services",
    ]
    rpc_methods = [
        "get_application_params",
        "get_gateway_params",
        "get_supplier_params",
        "get_service_params",
        "get_session_params",
        "get_proof_params",
        "get_migration_params",
        "get_shared_params",
        "get_tokenomics_params",
    ]

    def __init__(self, llm: ChatOpenAI):
        super().__init__(llm)

    def get_system_prompt(self) -> str:
        return """You are an expert query builder and executor for Pocket Network governance and administration.
You specialize in network parameters, authorization relationships, administrative execution, and DAO treasury visibility.
Your queries should prefer direct snapshot or configuration methods, and only use broader context methods when they help interpret a parameter change or authorization relationship.
Always apply a filter when possible, such as a block height, parameter identifier, or account relationship, because governance data can be large and highly state-dependent.

If the user asks who can modify what, use authorization relationships and any available execution context.
If the user asks about protocol behavior at a given time, prefer the parameter value active at that block or the nearest relevant block context.
If the user asks about “latest settings,” use the most recent parameter values available.

The user might use the following expressions:
- “height”, “block height”: a numeric block reference for snapshot context.
"""


class StakingParticipantStateAgent(QueryBuilderSubAgent):
    description = (
        "Monitors the lifecycle and health of staked participants, "
        "including application, gateway, supplier and validator staking, "
        "delegator  performance, overservicing, unbonding flows, and "
        "slashing events. Use this agent only when an specific participant "
        "data is reqeusted (i.e. validator, supplier, gateway, application, etc.)."
    )
    name = "StakingParticipantStateAgent"
    graphql_methods = [
        "suppliers",
        "applications",
        "gateways",
        "validators",
        "supplierServiceConfigs",
        "applicationGateways",
        "msgStakeApplications",
        "msgStakeGateways",
        "msgStakeSupplierServices",
        "getDataByDelegatorAddressesAndTimes",
        "getDataByDelegatorAddressesAndBlocks",
        "getOverservicedByAddressesAndTime",
        "eventApplicationOverserviceds",
        "eventGatewayUnbondingBegins",
        "eventGatewayUnbondingEnds",
        "eventSupplierUnbondingBegins",
        "eventSupplierUnbondingEnds",
        "eventApplicationUnbondingBegins",
        "eventApplicationUnbondingEnds",
        "eventSupplierSlasheds",
    ]
    rpc_methods = [
        "get_active_validators",
        "get_application",
        "get_all_applications",
        "get_gateway",
        "get_all_gateways",
        "get_supplier",
        "get_all_suppliers",
    ]

    def __init__(self, llm: ChatOpenAI):
        super().__init__(llm)

    def get_system_prompt(self) -> str:
        return """You are an expert query builder for Pocket Network staking and participant lifecycle analysis.
You specialize in application staking, gateway staking, supplier staking, delegator state, overservicing, unbonding flows, slashing events, and live validator information.
Your queries should prefer the most specific get... GraphQL methods for participant health and history, and use event-level data to understand lifecycle transitions such as unbonding, overservicing, or slashing.
Always apply a filter when possible, such as address, address list, time range, block range, or service context, because participant-state data is large and entity-specific.

If the user asks about application health, gateway status, or supplier lifecycle, combine stake history with overservicing and unbonding events.
If the user asks for counts or total stake of applications, gateways, suppliers or validators, use the entity tables (applications, gateways, suppliers, validators) with a stakeStatus filter and aggregates instead of accumulating events.
If the user asks which suppliers serve a service (or which services a supplier is staked in), use supplierServiceConfigs filtered by serviceId or supplierId.
If the user asks about delegators, treat them as addresses providing stake to suppliers and use delegator-oriented summaries.
If the user asks for node state, disambiguate between supplier, gateway, and application based on context.

"""


class AccountStateAgent(QueryBuilderSubAgent):
    name = "AccountStateAgent"
    description = (
        "Queries the state and history of general user accounts as plain "
        " accounts and not protocol actors (i.e. not suppliers, "
        " applications, or gateways). Covers current token balances, "
        "native transfer history (tokens sent/received between wallets), "
        "Morse migration claimable accounts, and historical on-chain "
        "balance snapshots. Not suitable for queries about staking, "
        "rewards, governance, or any named protocol actor."
    )
    graphql_methods = [
        "balances",
        "accounts",
        "nativeTransfers",
        "morseClaimableAccounts",
    ]
    rpc_methods = [
        "get_account_balance",
        "get_morse_claimable_account",
        "get_all_morse_claimable_accounts",
    ]

    def __init__(self, llm: ChatOpenAI):
        super().__init__(llm)

    def get_system_prompt(self) -> str:
        return """You are an expert query builder for Pocket Network general account state.
You specialize in querying the token balance and migration state of plain user accounts — addresses that are not staked as suppliers, applications, or gateways.

If the user provides an address, it is a bech32 pokt1… address for Shannon queries, or a hex address for Morse queries.
If the user asks where a wallet sent or received tokens, use nativeTransfers filtered by senderId and/or recipientId.
If the user asks about "claiming" or "migration" in the context of a specific address, use the RPC method get_morse_claimable_account; for migration status or aggregates over many accounts, use the GraphQL morseClaimableAccounts.

Hint – get_account_balance requires the address as a path parameter substituted directly into the URL path, not as a query parameter.
Hint – get_morse_claimable_account requires the Morse hex address as a path parameter.
"""


class ChainActivityAgent(QueryBuilderSubAgent):
    description = (
        "Explorer-style chain activity lookups: transactions (by hash, "
        "signer address or block), native token transfers between "
        "accounts, block data, and validator identity and uptime. Use "
        "this agent for questions about specific transactions, transfers, "
        "blocks or validators. Not suitable for staking lifecycle, reward "
        "attribution, or aggregated network usage analytics."
    )
    name = "ChainActivityAgent"
    graphql_methods = [
        "transactions",
        "nativeTransfers",
        "blocks",
        "accounts",
        "validators",
        "getProducedBlocksByValidator",
        "getMissingValidatorBlocks",
    ]
    rpc_methods = []

    def __init__(self, llm: ChatOpenAI):
        super().__init__(llm)

    def get_system_prompt(self) -> str:
        return """You are an expert query builder and executor for Pocket Network chain activity (explorer-style lookups).
You specialize in transactions, native token transfers, blocks, and validator identity and uptime.
Always apply a filter when possible, such as a transaction hash, signer address, sender/recipient address, block height or range, or validator address, because these tables are very large.

If the user provides a 64-character hex string, treat it as a transaction hash (the transaction "id") or a block hash.
If the user asks for the transaction history of an address, filter transactions by signerAddress and order by BLOCK_ID_DESC.
If the user asks where a wallet sent or received tokens, use nativeTransfers filtered by senderId and/or recipientId.
If the user asks about validator uptime, combine getProducedBlocksByValidator and getMissingValidatorBlocks from a starting block id; note their validatorAddress argument is the hex consensus address, not the poktvaloper... one.

The user might use the following expressions:
- "tx"/"transaction hash": the transaction id, a 64-character hex string.
- "block number"/"height": the id of a block, which is numeric.
- "transfer": a native send between two accounts (nativeTransfers), not a reward payout.

A transaction "code" of 0 means success; any other value means the transaction failed.
"""


ALL_SUBAGENTS = [
    GovernanceAdminAgent,
    NetworkUsageAgent,
    ServiceEconomicsAgent,
    SettlementRewardsAgent,
    StakingParticipantStateAgent,
    TokenomicsAgent,
    AccountStateAgent,
    ChainActivityAgent,
]


def create_sub_agents(llm: ChatOpenAI) -> List[QueryBuilderSubAgent]:
    """Create all available sub-agents."""
    return [a(llm) for a in ALL_SUBAGENTS]
