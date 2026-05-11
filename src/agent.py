"""Main LangGraph agent orchestrator."""

from langgraph.graph import StateGraph, START, END
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage
from typing import Any, Dict, Annotated
from typing_extensions import TypedDict
import json

from src.sub_agents import create_sub_agents
from src.validator import validate_graphql_query
from src.api_client import PoktscanAPIClient


class AgentStateDict(TypedDict):
    """State dictionary for the LangGraph agent."""

    user_query: str
    graphql_query: Any
    query_result: Any
    selected_subagent: Any
    error: Any
    agent_notes: str


class PoktscanAgent:
    """Main agent orchestrator using LangGraph."""

    def __init__(
        self, llm_base_url: str = "http://localhost:8087", llm_model: str = "local"
    ):
        """
        Initialize the agent.

        Args:
            llm_base_url: Base URL for the local LLM API
            llm_model: Model name for the LLM
        """
        self.llm = ChatOpenAI(
            base_url=llm_base_url,
            api_key="not-needed",
            model=llm_model,
            temperature=0.1,
        )

        self.sub_agents = create_sub_agents(self.llm)
        self.api_client = PoktscanAPIClient()

        self.graph = self._build_graph()

        self.NO_AGENT_HANDLE = "No sub-agent can handle the user query"
        self.AGENT_CANNOT_BUILD = "Sub-agent cannot build the user query"

    def _build_graph(self):
        """Build the LangGraph workflow."""
        workflow = StateGraph(AgentStateDict)

        # Define nodes
        workflow.add_node("select_agent", self._select_agent)
        workflow.add_node("build_query", self._build_query)
        workflow.add_node("execute_query", self._execute_query)
        workflow.add_node("format_response", self._format_response)
        workflow.add_node("format_refusal", self._format_refusal)

        # Define edges
        workflow.add_edge(START, "select_agent")
        workflow.add_conditional_edges(
            "select_agent",
            lambda state: "build_query"
            if not state.get("error")
            else "format_refusal",
        )
        workflow.add_conditional_edges(
            "build_query",
            lambda state: "execute_query"
            if not state.get("error")
            else "format_refusal",
        )
        workflow.add_edge("execute_query", "format_response")
        workflow.add_edge("format_response", END)
        workflow.add_edge("format_refusal", END)

        return workflow.compile()

    def _select_agent(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """Select the appropriate sub-agent for the query."""

        print("---SELECT AGENT---")

        user_query = state["user_query"]

        # Generate sub-agent list
        agents_list_text = ""
        for sub_agent in self.sub_agents:
            agents_list_text += f"\t- {sub_agent.name}: {sub_agent.description}\n"


        # Prepare the prompt with field information
        system_prompt = f"""
You are a tool selection agent, your job is to select the appropiate sub-agent to handle the user query.
The available sub-agents are:
{agents_list_text}
You should select a single sub-agent for the user query.
Respond ONLY with the selected agent name, if no sub-agent can fullfill thee query, return "None"
"""

        # Call the LLM
        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_query)
        ]
        response = self.llm.invoke(messages)

        response = response.content.strip()
        # Remove markdown code blocks if present
        if response.startswith("```"):
            response = "\n".join(response.split("\n")[1:])
        if response.endswith("```"):
            response = "\n".join(response.split("\n")[:-1])
        response = response.strip()

        # Find the first sub-agent that can handle this query
        selected_agent = None
        for sub_agent in self.sub_agents:
            if sub_agent.name in response:
                selected_agent = sub_agent
                print(f"\tSelected: {sub_agent.name}")
                break
    
        # If no specific agent matches, use a generic approach
        if not selected_agent:
            return {
                "error": self.NO_AGENT_HANDLE,
                "selected_subagent": None
                }
        else:
            notes = f"Selected {selected_agent.name} agent"
            return {
                "agent_notes": notes, 
                "selected_subagent": selected_agent
                }

    def _format_refusal(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """Format the response for the user."""

        print("---FORMAT REFUSAL---")
        

        user_query = state["user_query"]

        if state["error"] == self.NO_AGENT_HANDLE:
            # Generate sub-agent list
            agents_list_text = ""
            for sub_agent in self.sub_agents:
                agents_list_text += f"\t- {sub_agent.name}: {sub_agent.description}\n"
            error_message = "The query did not match any of the following agents:\n" + agents_list_text
        elif state["error"] == self.AGENT_CANNOT_BUILD:
            error_message = state["agent_notes"]
        elif "GraphQL validation error" in state["error"]:
            return {"agent_notes": "Internal error `SA1`, please retry."}
        else:
            return {"agent_notes": "Internal error `A1`, please retry."}



        # Prepare the prompt with field information
        llm_query = f"""
Explain the user why its query failed and, if possible, hint how to improve it.
User query:
{user_query}

Error trace:
{error_message}
"""

        # print(llm_query)
        # Call the LLM
        messages = [
            SystemMessage(content=self.get_system_prompt()),
            HumanMessage(content=llm_query)
        ]
        response = self.llm.invoke(messages)


        return {"agent_notes": response.content.strip()}

    def invoke(self, user_query: str) -> Dict[str, Any]:
        """
        Process a user query through the agent.

        Args:
            user_query: Natural language query from the user

        Returns:
            Dictionary with the result
        """
        initial_state = {
            "user_query": user_query,
            "selected_subagent": None,
            "graphql_query": None,
            "query_result": None,
            "error": None,
            "agent_notes": "",
        }

        final_state = self.graph.invoke(initial_state)

        return {
            "success": final_state.get("error") is None,
            "error": final_state.get("error"),
            "graphql_query": final_state.get("graphql_query"),
            "result": final_state.get("query_result"),
            "notes": final_state.get("agent_notes", ""),
        }
    
    def get_system_prompt(self) -> str:
        return """You are an expert information retriever for Pocket Network data.
You specialize in understanding user intent when a Pocket Network data-related question is given.
Make your responses brief and precise.
"""

    def _build_query(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """Build the GraphQL query using the selected sub-agent."""

        print("---BUILD QUERY---")

        user_query = state["user_query"]
        selected_agent = state["selected_subagent"]

        # Build the query
        result = selected_agent.build_query(user_query)

        if result.success:
            # Proceed to check if the query was built correctly
            is_valid, normalized_query, error_msg = validate_graphql_query(
                result.query
            )

            if is_valid:
                return {
                    "graphql_query": normalized_query,
                    "agent_notes": "Query validated successfully",
                }
            else:
                return {
                    "error": error_msg,
                    "agent_notes": f"\nQuery generation failed: {error_msg}",
                }
        elif result.error is None:
            # This is a problem detected by the agent, pass on the problem to the use
            return {
                    "error": self.AGENT_CANNOT_BUILD,
                    "agent_notes": f"\nQuery generation was not possible due to:\n\t-{result.explanation}",
                }
            
        else:
            # The agent failed in an non-controlled way, probably cannot be recovered
            return {
                "error": result.error,
                "agent_notes": f"\nError: {result.error}",
            }


        

    def _execute_query(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """Execute the GraphQL query against the API."""

        print("---EXECUTE QUERY---")

        if state.get("error"):
            return {}

        if not state.get("graphql_query"):
            return {"error": "No valid query to execute"}

        success, result, error_msg = self.api_client.execute_query(
            state["graphql_query"]
        )

        if success:
            return {
                "query_result": result,
                "agent_notes": state["agent_notes"] + "\nQuery executed successfully",
            }
        else:
            return {
                "error": error_msg,
                "agent_notes": state["agent_notes"] + f"\nAPI error: {error_msg}",
            }

    def _format_response(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """Format the response for the user."""

        print("---FORMAT RESPONSE---")

        return {}
    
