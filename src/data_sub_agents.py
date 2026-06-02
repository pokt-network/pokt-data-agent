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

from src.models import SubAgentTextResult
from src.rpc_client import RPC_METHODS, POCKET_NETWORK_RPC_ENDPOINT, PocketNetworkRPCClient
from src.graphql_client import GRAPHQL_REGISTRY, POCKET_NETWORK_DATA_ENDPOINT, PocketNetworkAPIClient
from src.rpc_validator import validate_rpc_call
from src.tools_introspection import INTROSPECTION_TOOLS
from src.graphql_validator import validate_graphql_query
from src.query_sub_agents import ALL_SUBAGENTS


logger = logging.getLogger(__name__)


class DataSubAgentStateDict(TypedDict):
    """State dictionary for a subagent LangGraph."""

    user_query: str
    llm_error: Optional[str]
    success: bool
    user_query_result: str

class DataSubAgent(ABC):
    """Base class for query builder sub-agents."""

    def __init__(self, llm: ChatOpenAI):
        self.llm = llm
        self.llm_with_tools = llm.bind_tools(INTROSPECTION_TOOLS)
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
        workflow = StateGraph(DataSubAgentStateDict)

        workflow.add_node("analyze_query", self._analize_query)

        workflow.add_edge("analyze_query", END)

        return workflow.compile()

    
    # ------------------------------------------------------------------
    # Nodes
    # ------------------------------------------------------------------

    def _analize_query(self, state: DataSubAgentStateDict) -> Dict[str, Any]:
        """LLM node: Process the user query."""
        
        
        logger.info("[%s] analize_query", self.name)

        system_prompt = (
            self.get_system_prompt()
        )

        user_message = self._build_user_message(state)

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
                    "[%s] LLM requested %d tool call(s): %s",
                    self.name,
                    len(response.tool_calls),
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
                        logger.warning(
                            "[%s] Tool %r failed: %s", self.name, tool_name, tool_exc
                        )
                    messages.append(
                        ToolMessage(content=str(tool_result), tool_call_id=tc["id"])
                    )

            raw = response.content.strip()

            logger.debug(
                "[%s] LLM produced output: \n%s", self.name, raw
            )
            return {
                "user_query_result": raw,
                "llm_error": None,
                "success": True,
            }

        except Exception as exc:
            logger.exception("[%s] LLM call failed.", self.name)
            return {
                "user_query_result": "",
                "llm_error": f"LLM error: {str(exc)}",
                "success": False,
            }

    # ------------------------------------------------------------------
    # User-message builder
    # ------------------------------------------------------------------

    def _build_user_message(self, state: DataSubAgentStateDict) -> str:
        """Construct the unified user message."""

        # -- GraphQL fields block ----------------------------------------
        agents_section = ""

        for this_subagent in ALL_SUBAGENTS:
            agents_section += f"- {this_subagent.name}: {this_subagent.description}"
            agents_section += "\n\tGRAHPQL METHODS: "
            for field_key in this_subagent.graphql_methods:
                field_info = GRAPHQL_REGISTRY.get(field_key)
                if not field_info:
                    logger.error(
                        "[%s] Field '%s' not found in registry.", self.name, field_key
                    )
                    continue
                agents_section += f"{field_info.name},"
            agents_section += ".\n"
            if len(this_subagent.rpc_methods) > 0:
                agents_section += "\n\tRPC METHODS: "
                for field_key in this_subagent.rpc_methods:
                    field_info = RPC_METHODS.get(field_key)
                    if not field_info:
                        logger.error(
                            "[%s] Field '%s' not found in registry.", self.name, field_key
                        )
                        continue
                    agents_section += f"{field_info.name},"
                agents_section += ".\n"


        return f"""User query: {state["user_query"]}

--------------------------------------------------------------------------------
List of sub-agents and their methods:
{agents_section}
--------------------------------------------------------------------------------

If the user is asking for a specific method, make sure it is one of the available method in any sub-agent.
Allways consult the instropetion tools (if available) and the method's documentation before answering a query.
Do not create nwe example usage, only copy relaevant examples from the method documentation (if any).
If relevant, inform the user of other existing method that might simplify their final query.
If the user query is incomplete, try to ask for more specific data and provide the user with information about methods that might fullfill its needs.

"""


    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def analize_query(self, user_query: str) -> SubAgentTextResult:
        """
        Run the subagent graph and return a SubAgentResult.

        This is the entry point called by the main agent.
        """
        logger.info("[%s] Starting query-build graph for: %r", self.name, user_query)

        initial_state: DataSubAgentStateDict = {
            "user_query": user_query,
            "user_query_result": "",
            "llm_error": None,
            "success": False,
        }

        final_state = self.graph.invoke(initial_state)

  
        # Query build/validation failed – hard error
        if not final_state["success"]:
            error = (
                final_state.get("llm_error")
                or "Unknown error"
            )
            logger.error(
                "[%s] graph failed: %s",
                self.name,
                error,
            )
            return SubAgentTextResult(
                response="",
                success=False,
                error=error,
            )
            

        logger.info("[%s] graph succeeded.", self.name)
        
        return SubAgentTextResult(
                response=final_state["user_query_result"],
                success=False,
                error=error,
            )

class EndpointsDataAgent(DataSubAgentStateDict):
    description = (
        "Provides data about the best endpoints and methods to query Pocket Netwotk data. "
        "When you need further information about a method, pass the method in the query."
        "If you want to discover methods that are suitable for a given task, "
        "pass the task in clear and atomized way (no complex multi-step requests). "
        "To get examples of the methods provided by this agent, use the other "
        "agents, and ask for concrete queries that serve as example."
    )
    name = "EndpointsDataAgent"

    def __init__(self, llm: ChatOpenAI):
        super().__init__(llm)

    def get_field_names(self) -> List[str]:
        return [
            "getRewardsByDate",
            "relayByBlockAndServices",
            "getRelaysByServicePerPointJson",
            "blocks",
            "getAmountOfBlocksAndSuppliersByTimes",
        ]

    def get_system_prompt(self) -> str:
        return """You are an expert on Pocket Network network usage analytics and its data endpoints.
Your job is to interpret the needs for explaining and exploring the different ways of acccessing the available data endpoints, providing precise responses to the user queries. 

Reject queries that ask for data that you cannot access, or if it is not part of the provided endpoint methods or if it is not related to any Pocket Network data endpoint.

The user might use the following expressions:
- “block number”: this is the id of a block, which is a number.
- “relays”: The user normally refers to "estimated relays" which is the expanded value after applying the service difficulty, use that if available.
"""




def create_data_sub_agents(llm: ChatOpenAI) -> List[DataSubAgentStateDict]:
    """Create all available sub-agents."""
    return [
        EndpointsDataAgent(llm),
    ]
