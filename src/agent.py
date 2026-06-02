"""Main LangGraph agent orchestrator."""

import logging
from typing import Any, Dict

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from src.query_sub_agents import create_sub_agents

logger = logging.getLogger(__name__)


class AgentStateDict(TypedDict):
    """State dictionary for the LangGraph agent."""

    user_query: str
    query: Any
    query_result: Any
    selected_subagent: Any
    error: Any
    agent_notes: str
    endpoint_type: str


class PocketNetworkAgent:
    """Main agent orchestrator using LangGraph."""

    NO_AGENT_HANDLE = "No sub-agent can handle the user query"
    AGENT_CANNOT_BUILD = "Sub-agent cannot build the user query"

    def __init__(self, llm_base_url: str = "http://localhost:8087", llm_model: str = "local"):
        logger.info(
            "Initialising PocketNetworkAgent (model=%s, url=%s)",
            llm_model,
            llm_base_url,
        )
        self.llm = ChatOpenAI(
            base_url=llm_base_url,
            api_key="not-needed",
            model=llm_model,
        )

        self.sub_agents = create_sub_agents(self.llm)
        self.graph = self._build_graph()
        logger.info(
            "PocketNetworkAgent ready with %d sub-agent(s): %s",
            len(self.sub_agents),
            [a.name for a in self.sub_agents],
        )

    # ------------------------------------------------------------------
    # Graph construction
    # ------------------------------------------------------------------

    def _build_graph(self):
        """Build the LangGraph workflow."""
        workflow = StateGraph(AgentStateDict)

        workflow.add_node("select_agent", self._select_agent)
        workflow.add_node("build_query", self._build_query)
        workflow.add_node("format_response", self._format_response)
        workflow.add_node("format_refusal", self._format_refusal)

        workflow.add_edge(START, "select_agent")
        workflow.add_conditional_edges(
            "select_agent",
            lambda state: "format_refusal" if state.get("error") else "build_query",
        )
        workflow.add_conditional_edges(
            "build_query",
            lambda state: "format_refusal" if state.get("error") else "format_response",
        )
        workflow.add_edge("format_response", END)
        workflow.add_edge("format_refusal", END)

        return workflow.compile()

    # ------------------------------------------------------------------
    # Nodes
    # ------------------------------------------------------------------

    def _select_agent(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """Select the appropriate sub-agent for the query."""
        logger.info("--- select_agent ---")

        user_query = state["user_query"]

        agents_list_text = "".join(f"\t- {a.name}: {a.description}\n" for a in self.sub_agents)

        system_prompt = f"""
You are a tool selection agent, your job is to select the appropriate sub-agent to handle the user query.
The available sub-agents are:
{agents_list_text}
You should select a single sub-agent for the user query.
Respond ONLY with the selected agent name, if no sub-agent can fulfil the query, return "None"
"""

        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_query),
        ]
        response = self.llm.invoke(messages)

        raw = response.content.strip()
        if raw.startswith("```"):
            raw = "\n".join(raw.split("\n")[1:])
        if raw.endswith("```"):
            raw = "\n".join(raw.split("\n")[:-1])
        raw = raw.strip()

        selected_agent = next((a for a in self.sub_agents if a.name in raw), None)

        if not selected_agent:
            logger.warning("No sub-agent matched LLM response: %r", raw)
            return {"error": self.NO_AGENT_HANDLE, "selected_subagent": None}

        logger.info("Selected sub-agent: %s", selected_agent.name)
        return {
            "agent_notes": f"Selected {selected_agent.name} agent",
            "selected_subagent": selected_agent,
        }

    def _build_query(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """Invoke the selected sub-agent's graph to build and execute the query."""
        logger.info("--- build_query ---")

        user_query = state["user_query"]
        selected_agent = state["selected_subagent"]

        logger.info("Delegating to %s graph", selected_agent.name)
        result = selected_agent.build_query(user_query)

        if result.success:
            logger.info("Sub-agent succeeded (endpoint=%s).", result.endpoint_type)
            return {
                "query": result.query,
                "query_result": result.query_result,
                "endpoint_type": result.endpoint_type,
                "agent_notes": state.get("agent_notes", "")
                + f"\n {result.explanation}"
                + "\n\nQuery built, validated, and executed successfully",
            }

        if result.error is None:
            # Agent-level semantic refusal (explanation set, no hard error)
            logger.warning("Sub-agent refused to build query: %s", result.explanation)
            return {
                "error": self.AGENT_CANNOT_BUILD,
                "agent_notes": state.get("agent_notes", "")
                + f"\nQuery generation was not possible due to:\n\t- {result.explanation}",
            }

        # Hard error (LLM failure, max retries, or execution failure)
        logger.error("Sub-agent failed: %s", result.error)
        return {
            "error": result.error,
            "agent_notes": state.get("agent_notes", "") + f"\nError: {result.error}",
        }

    def _format_response(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """Format the successful response for the user."""
        logger.info("--- format_response ---")

        llm_query = f"""
Format a response to the use query, based on the obtained data:
User query:
{state["user_query"]}

Obtained data:
- query: {state.get("query")}
- result: {state.get("query_result")}
- notes: {state.get("agent_notes", "")}
- endpoint_type: {state.get("endpoint_type", "graphql")}
"""
        messages = [
            SystemMessage(content=self.get_system_prompt()),
            HumanMessage(content=llm_query),
        ]
        response = self.llm.invoke(messages)

        return {"agent_notes": response.content.strip()}

    def _format_refusal(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """Format an error/refusal response for the user."""
        logger.info("--- format_refusal ---")

        user_query = state["user_query"]
        error = state.get("error", "")

        if error == self.NO_AGENT_HANDLE:
            agents_list_text = "".join(f"\t- {a.name}: {a.description}\n" for a in self.sub_agents)
            error_message = "The query did not match any of the following agents:\n" + agents_list_text
        elif error == self.AGENT_CANNOT_BUILD:
            error_message = state.get("agent_notes", "")
        elif error and "GraphQL validation error" in error:
            logger.warning("Validation error reached format_refusal: %s", error)
            return {"agent_notes": "Internal error `SA1`, please retry."}
        else:
            logger.error("Unhandled error reached format_refusal: %s", error)
            return {"agent_notes": "Internal error `A1`, please retry."}

        llm_query = f"""
Explain the user why its query failed and, if possible, hint how to improve it.
User query:
{user_query}

Error trace:
{error_message}
"""
        messages = [
            SystemMessage(content=self.get_system_prompt()),
            HumanMessage(content=llm_query),
        ]
        response = self.llm.invoke(messages)

        return {"agent_notes": response.content.strip()}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def invoke(self, user_query: str) -> Dict[str, Any]:
        """
        Process a user query through the agent.

        Args:
            user_query: Natural language query from the user

        Returns:
            Dictionary with the result
        """
        logger.info("PocketNetworkAgent.invoke: %r", user_query)

        initial_state = {
            "user_query": user_query,
            "selected_subagent": None,
            "query": None,
            "query_result": None,
            "error": None,
            "agent_notes": "",
            "endpoint_type": "graphql",
        }

        final_state = self.graph.invoke(initial_state)

        success = final_state.get("error") is None
        logger.info(
            "PocketNetworkAgent.invoke finished – success=%s error=%s endpoint=%s",
            success,
            final_state.get("error"),
            final_state.get("endpoint_type", "graphql"),
        )

        return {
            "success": success,
            "error": final_state.get("error"),
            "query": final_state.get("query"),
            "result": final_state.get("query_result"),
            "notes": final_state.get("agent_notes", ""),
            "endpoint_type": final_state.get("endpoint_type", "graphql"),
        }

    def get_system_prompt(self) -> str:
        return """You are an expert information retriever for Pocket Network data.
You specialize in understanding user intent when a Pocket Network data-related question is given.
Make your responses brief and precise.
"""
