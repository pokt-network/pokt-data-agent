"""Data models for the Poktscan agent."""

from dataclasses import dataclass, field
from typing import Optional, List


ALL_FILTERS = [
            "startsWithInsensitive", 
            "notStartsWithInsensitive", 
            "startsWith",
            "notStartsWith",
            "notLikeInsensitive",
            "notLike",
            "notIncludesInsensitive",
            "notIncludes",
            "notInInsensitive",
            "notIn",
            "notEqualToInsensitive",
            "notEqualTo",
            "notEndsWithInsensitive",
            'notEndsWith',
            "notDistinctFromInsensitive",
            'notDistinctFrom',
            'like',
            'likeInsensitive',
            "lessThanOrEqualTo",
            "lessThanOrEqualToInsensitive",
            "lessThanInsensitive",
            "isNull",
            "lessThan",
            "includesInsensitive",
            'includes',
            "inInsensitive",
            "in", 
            "greaterThanOrEqualToInsensitive", 
            "greaterThanOrEqualTo", 
            "greaterThan", 
            "greaterThanInsensitive", 
            "equalToInsensitive", 
            "equalTo", 
            "endsWithInsensitive", 
            "endsWith", 
            "distinctFromInsensitive", 
            "distinctFrom"
        ]


@dataclass
class QueryFieldInfo:
    """Information about a GraphQL query field."""

    name: str
    description: str
    field_arguments: List[str] = field(default_factory=list)
    filters: List[str] = field(default_factory=list)
    filter_keys: List[str] = field(default_factory=list)
    filter_operators: List[str] = field(default_factory=list)
    available_aggregations: List[str] = field(default_factory=list)
    grouped_aggregates: List[str] = field(default_factory=list)
    key_fields: List[str] = field(default_factory=list)
    numeric_fields: List[str] = field(default_factory=list)


# Registry of available GraphQL query fields with descriptions
QUERY_FIELDS_REGISTRY = {
    "getRewardsByDate": QueryFieldInfo(
        name="getRewardsByDate",
        description="Total relays, computed units, and claimed tokens data on a given period for the whole network",
        field_arguments=["startDate", "endDate"],
        filters=[],
        filter_keys=[],
        filter_operators=[],
        available_aggregations=[],
        grouped_aggregates=[],
        key_fields=[],
        numeric_fields=[]
    ),
    "getTotalSupplyBetweenDates": QueryFieldInfo(
        name="getTotalSupplyBetweenDates",
        description="Total network supplu, staked, unstaked, supplier stakes, apps stakes, etc. on a given period for the whole network",
        field_arguments=["startDate", "endDate"],
        filters=[],
        filter_keys=[],
        filter_operators=[],
        available_aggregations=[],
        grouped_aggregates=[],
        key_fields=[],
        numeric_fields=[]
    ),
    "getDaoBalanceAtHeight": QueryFieldInfo(
        name="getDaoBalanceAtHeight",
        description="DAO holdings in upokt at the given block hieght",
        field_arguments=["height"],
        filters=[],
        filter_keys=[],
        filter_operators=[],
        available_aggregations=[],
        grouped_aggregates=[],
        key_fields=[],
        numeric_fields=[]
    ),
    "eventClaimSettleds": QueryFieldInfo(
        name="eventClaimSettleds",
        description="Returns claims settlement events with relays, computed units, and claimed tokens data",
        field_arguments=["filter", "last", "first"],
        filters=[
            "block",
            "supplierId",
            "applicationId"
        ],
        filter_keys=[
            "id",
            "hash",
            "timestamp"
        ],
        filter_operators=ALL_FILTERS,
        available_aggregations=[
            "sum",
            "average",
            "min",
            "max",
            "distinctCount",
            "stddevSample",
            "stddevPopulation",
            "varianceSample",
            "variancePopulation",
        ],
        grouped_aggregates=[
            "SUPPLIER_ID",
            "APPLICATION_ID"
        ],
        key_fields=["applicationId", "supplierId", "serviceId"],
        numeric_fields=[
            "numRelays",
            "numClaimedComputedUnits",
            "numEstimatedComputedUnits",
            "claimedAmount",
        ],
    ),
    "eventClaimExpireds": QueryFieldInfo(
        name="eventClaimExpireds",
        description="Returns expired claim events, these are claims that received no proof after the proof window closed. They are result in a penalty (burn) for the supplier that placed the claim.",
        available_aggregations=[
            "sum",
            "average",
            "min",
            "max",
            "distinctCount",
            "stddevSample",
            "stddevPopulation",
            "varianceSample",
            "variancePopulation",
        ],
        key_fields=[],
        numeric_fields=[
            "numRelays",
            "numClaimedComputedUnits",
            "numEstimatedComputedUnits",
            "claimedAmount",
        ],
    ),
}


@dataclass
class AgentState:
    """State for the LangGraph agent."""

    user_query: str
    graphql_query: Optional[str] = None
    query_result: Optional[dict] = None
    error: Optional[str] = None
    agent_notes: str = ""


@dataclass
class SubAgentResult:
    """Result from a sub-agent query builder."""

    query: str
    explanation: str
    success: bool
    error: Optional[str] = None
