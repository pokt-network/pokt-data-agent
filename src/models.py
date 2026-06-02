"""Data models for the Pocket Network agent."""

from dataclasses import dataclass, field
from typing import Any, Optional, List, Dict


@dataclass
class QueryFieldInfo:
    """Information about a GraphQL or RPC query field."""

    name: str
    description: str
    fields_notes: Dict[str, str] = field(default_factory=dict)
    method_data: Optional[dict] = None


@dataclass
class AgentState:
    """State for the LangGraph agent."""

    user_query: str
    query: Optional[str] = None
    endpoint_type: Optional[str] = None
    query_result: Optional[dict] = None
    error: Optional[str] = None
    agent_notes: str = ""


@dataclass
class SubAgentResult:
    """Result from a sub-agent query builder and executor."""

    query: str
    explanation: str
    success: bool    
    error: Optional[str] = None
    endpoint_type: str = "graphql"
    used_method_description: str = ""
    # Raw data returned by the GraphQL or RPC client after execution.
    # None when success is False.
    query_result: Optional[Any] = None
    

@dataclass
class SubAgentTextResult:
    """Result from a sub-agent raw data retriever."""

    response: str
    success: bool    
    error: Optional[str] = None