"""Sub-agents for building GraphQL queries for different data types."""

from abc import ABC, abstractmethod
from typing import Optional, List
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage
from src.models import QueryFieldInfo, SubAgentResult, QUERY_FIELDS_REGISTRY
import json

from datetime import datetime

class QueryBuilderSubAgent(ABC):
    """Base class for query builder sub-agents."""

    def __init__(self, llm: ChatOpenAI):
        """
        Initialize the sub-agent.

        Args:
            llm: Language model instance
        """
        self.llm = llm

    @abstractmethod
    def get_field_names(self) -> str:
        """Return the GraphQL field names this agent handles."""
        pass

    @abstractmethod
    def get_system_prompt(self) -> str:
        """Return the system prompt for this sub-agent."""
        pass



    def build_query(self, user_query: str) -> SubAgentResult:
        """
        Build a GraphQL query based on the user's natural language query.

        Args:
            user_query: User's natural language query

        Returns:
            SubAgentResult with the generated query
        """
        print("######### BUILD QUERY ##########")

        system_prompt = self.get_system_prompt() + f"\nCURRENT DATE: {datetime.now().isoformat()}"

        try:
            field_schema = ""
            for field_key in self.get_field_names():
                field_info = QUERY_FIELDS_REGISTRY.get(field_key)
                if not field_info:
                    return SubAgentResult(
                        query="",
                        explanation="",
                        success=False,
                        error=f"Field {field_key} not found in registry",
                    )

                # Prepare the prompt with field information
                
                field_schema += self._format_field_info(field_info)

            user_message = f"""User query: {user_query}

GraphQL Fields Information:
{field_schema}

Generate a GraphQL query that answers the user's question. 
The query should use one of the given fields with appropriate filters and aggregations (if needed).
Use the provided fields, if the the list is empty, then the result is a scalar and you dont need to provide any leaf.

Return ONLY the GraphQL query, no explanations.
If the query cannot be constructed (missing data or cannot be handled) return a json explaining the error in a brief and precise way, using the format:
```
{{
    "error": "..."
}}
```

"""

            # Call the LLM
            messages = [
                SystemMessage(content=system_prompt),
                HumanMessage(content=user_message)
            ]
            response = self.llm.invoke(messages)

            query = response.content.strip()

            # Remove markdown code blocks if present
            if query.startswith("```"):
                query = "\n".join(query.split("\n")[1:])
            if query.endswith("```"):
                query = "\n".join(query.split("\n")[:-1])
            query = query.strip()

            # Check if the LM detected an error condition
            try:
                error_ck = json.loads(query)
                return SubAgentResult(
                    query="",
                    explanation=error_ck["error"],
                    success=False,
                    error=None,
                )
            except Exception as e:
                # This is not a controlled error, it should be a correct querry
                return SubAgentResult(
                    query=query,
                    explanation="",
                    success=True,
                    error=None,
                )

        except Exception as e:
            return SubAgentResult(
                query="",
                explanation="",
                success=False,
                error=f"Error building query: {str(e)}",
            )

    def _format_field_info(self, field_info: QueryFieldInfo) -> str:
        """Format field information for the LLM."""
        return f"""
--------------------------------------------------------------------------------
Field: {field_info.name}
Description: {field_info.description}

Field Arguments: : {", ".join(field_info.filters)}
Available Filter: {", ".join(field_info.filters)}
Keys for filtering: {", ".join(field_info.filter_keys)}
Operators for filtering: {", ".join(field_info.filter_operators)}
Available Grouped Aggregators: {", ".join(field_info.grouped_aggregates)}
Available Aggregations: {", ".join(field_info.available_aggregations)}
Key Fields for Grouping: {", ".join(field_info.key_fields)}
Numeric Fields for Aggregation: {", ".join(field_info.numeric_fields)}
--------------------------------------------------------------------------------
"""


class ClaimsAgent(QueryBuilderSubAgent):
    """Sub-agent for building relay claims related queries."""

    def __init__(self, llm: ChatOpenAI):
        """
        Initialize the sub-agent.

        Args:
            llm: Language model instance
        """
        super().__init__(llm)
        self.description = "Searches for relays amunts, claims, proofs and settlements data. Tracks spending and earnings related to relaying operations for specific Appllications and Suppliers."
        self.name = "ClaimsAgent"

    def get_field_names(self) -> List[str]:
        return ["eventClaimSettleds"]

    def get_system_prompt(self) -> str:
        return """You are an expert GraphQL query builder for the Pocket Network claim process.
You specialize in creating queries that retrieve claims settlement data with relays, computed units, and payout information.
Your queries should use appropriate filters (by date, application ID, supplier ID, etc.) and aggregations (sum, average, min, max).
Structure aggregations with groupBy when the user asks about data grouped by specific fields.
You will allways apply a filter, i.e. a date range, a matching field, etc. The database is big.
If the user asks for a "node" request disambiguation, as it can be a Supplier or Application entity.
When no absolute date is provided, assume that the user is referring to periods in the current date.

The user might use the following expressions:
- "block number": This is the "id" of a "block", which is a number.



# Some usefull examples:

Total relays by session on May, for application XYZ:
```
eventClaimSettleds(
  filter: {{block: {{
                timestamp: {{
                    greaterThan: "2026-05-01", lessThanOrEqualTo: "2026-06-01"
                    }}
                }}, 
            applicationId: {{equalTo: "XYZ"}}
        }}
  ) {{
    groupedAggregates(groupBy: SESSION_END_HEIGHT) {{
      sum {{
        numRelays
      }}
      keys
    }}
    pageInfo {{
      endCursor
      startCursor
      hasNextPage
      hasPreviousPage
    }}
  }}
```

"""


class GlobalDataAgent(QueryBuilderSubAgent):
    """Sub-agent for building global network data."""

    def __init__(self, llm: ChatOpenAI):
        """
        Initialize the sub-agent.

        Args:
            llm: Language model instance
        """
        super().__init__(llm)
        self.description = "Searches for generalistic data of the network as a whole. Provides rewards, validation, transaction and monetary data allways viewed by date or block intervals."
        self.name = "GlobalDataAgent"

    def get_field_names(self) -> List[str]:
        return ["getRewardsByDate", "getTotalSupplyBetweenDates", "getDaoBalanceAtHeight"]

    def get_system_prompt(self) -> str:
        return """You are an expert GraphQL query builder for the Pocket Network claim process.
You specialize in creating queries that retrieve global data for the network in a given time period.
When no absolute date is provided, assume that the user is referring to periods in the current date (i.e. if year is missing assume current).

# Some usefull examples:

Total computed units on November 2025:
```
getRewardsByDate(
    endDate: "2025-11-30"
    startDate: "2025-11-01"
  )
```


"""



def create_sub_agents(llm: ChatOpenAI) -> List[QueryBuilderSubAgent]:
    """Create all available sub-agents."""
    return [
        ClaimsAgent(llm),
        GlobalDataAgent(llm),
    ]
