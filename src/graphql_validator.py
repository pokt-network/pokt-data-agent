"""GraphQL query validation."""

import logging
import time
from typing import Dict, List, Optional, Set, Tuple

import requests
from graphql import (
    DocumentNode,
    FieldNode,
    FragmentDefinitionNode,
    FragmentSpreadNode,
    GraphQLError,
    GraphQLSchema,
    InlineFragmentNode,
    IntValueNode,
    ListValueNode,
    ObjectValueNode,
    OperationDefinitionNode,
    SelectionSetNode,
    ValueNode,
    build_client_schema,
    get_introspection_query,
    parse,
    print_ast,
    specified_rules,
    validate,
)
from graphql.validation import NoDeprecatedCustomRule

from src.graphql_client import GRAPHQL_REGISTRY, POCKET_NETWORK_DATA_ENDPOINT

logger = logging.getLogger(__name__)

# Fields that read raw payout tables: a broad filter on them scans millions of rows. The settlement catalog answers
# the same questions from precomputed tables.
RAW_FIELD_PREFIXES = ("modToAcctTransfer", "domainServiceDailyReward")
RAW_FIELD_HINT = (
    "use getIncomeJson (by_supplier / by_service / by_reason, bucket hour for the finest grain) or "
    "getSupplierDistributionJson instead"
)
# Live reward functions that the legacy... fields replace: each call scans the raw payout tables. The legacy twin
# (the same name with "legacy" in place of "get") takes the same arguments and returns the same JSON (under "data"
# once pocketdex answers {range, data}). getMintBreakdownBetweenDatesV2 is a leftover of an older pocketdex release
# (no current source creates it): its twin is legacyMintBreakdownBetweenDates.
LIVE_REWARD_FIELDS = (
    "getRewardsByAddressesAndTime",
    "getRewardsByAddressesAndTimeGroupByAddressAndDate",
    "getRewardsByAddressesAndTimeGroupByDate",
    "getRewardsByAddressesAndTimeGroupByService",
    "getRewardsOfAddressesBySuppliersAndTime",
    "getRewardsBySuppliersAndTimeGroupByAddressAndDate",
    "getRewardsBySuppliersAndTimeGroupByService",
    "getMintBreakdownBetweenDates",
    "getMintBreakdownBetweenDatesV2",
    "getBurnBreakdownBetweenDates",
)
# The settlement catalog functions, as GraphQL names: pocketdex CATALOG_FUNCTIONS
# (src/mappings/dbFunctions/settlement/functions.ts). Each has a ...List row variant and a ...Json twin.
CATALOG_FUNCTIONS = frozenset(
    {
        "moneyCoverage",
        "getApplicationSpend",
        "getGatewaySpend",
        "getSupplierEarnings",
        "getSupplierDistribution",
        "getIncome",
        "getValidatorRewards",
        "getDelegatorIncome",
        "getSupplyFlows",
        "getSupplierPenalties",
        "getServiceUsage",
        "getAppAutoUnstakes",
        "getSupplierProofs",
        "getParamHistory",
        "getClaims",
        "getActorLifecycle",
        "getBlockStats",
        "getRelayMiningDifficulty",
        "getCreatedClaims",
        "getMorseClaims",
    }
)
# The lists and functions the closed pocketdex schema drops (docs/graphql-migration.md), with what replaces them. The
# event and message tables not named here are hidden too (EVENT_PREFIXES).
HIDDEN_FIELDS = {
    "eventClaimSettleds": "getClaimsJson (one row per claim); sums in getSupplierEarningsJson, getApplicationSpendJson, "
    "getServiceUsageJson",
    "eventClaimExpireds": 'getClaimsJson with statuses: ["expired"]; totals in getSupplierPenaltiesJson',
    "msgStakeApplications": 'getActorLifecycleJson with kind: "application"; current stake in applications',
    "msgStakeGateways": 'getActorLifecycleJson with kind: "gateway"; current stake in gateways',
    "msgStakeSupplierServices": "supplierServiceConfigs",
    "eventRelayMiningDifficultyUpdateds": "getRelayMiningDifficultyJson (the current value is its newest row, rowLimit: 1)",
    "eventApplicationOverserviceds": "getApplicationSpendJson or getOverservicedByAddressesAndTime",
    "eventGatewayUnbondingBegins": 'getActorLifecycleJson with kind: "gateway"',
    "eventGatewayUnbondingEnds": 'getActorLifecycleJson with kind: "gateway"',
    "eventSupplierUnbondingBegins": 'getActorLifecycleJson with kind: "supplier"',
    "eventSupplierUnbondingEnds": 'getActorLifecycleJson with kind: "supplier"',
    "eventApplicationUnbondingBegins": 'getActorLifecycleJson with kind: "application"',
    "eventApplicationUnbondingEnds": 'getActorLifecycleJson with kind: "application"',
    "eventSupplierSlasheds": 'getActorLifecycleJson with kind: "supplier" and events: ["slash"]; totals in '
    "getSupplierPenaltiesJson",
    "getSupplierStatsByDomains": 'getSupplierFleetList with domains, stakeStatuses: ["Staked"] and bySupplier: false',
}
EVENT_PREFIXES = ("event", "msg")
EVENT_HINT = "the event and message tables are hidden: use the function that answers the question (introspect Query)"
# Sums and groupings over a list, which the API no longer serves, and where to find them instead.
AGGREGATE_FIELDS = ("aggregates", "groupedAggregates")
AGGREGATE_HINT = (
    "use totalCount for a count, a function that sums the rows (getSupplierFleetList, getApplicationFleetList, "
    "getServiceSummaryList, getBlockStatsJson, getMorseClaimSummaryList or the settlement catalog ...Json), or the "
    "newest blocks row's staked* and unstaking* columns"
)
# The operators of the filter types on a column (pocketdex schema, 2026-10-14). A filter key whose value holds anything
# else is a filter through a relation, which the API no longer serves.
FILTER_OPERATORS = frozenset(
    {
        "anyEqualTo",
        "anyGreaterThan",
        "anyGreaterThanOrEqualTo",
        "anyLessThan",
        "anyLessThanOrEqualTo",
        "anyNotEqualTo",
        "containedBy",
        "contains",
        "containsAllKeys",
        "containsAnyKeys",
        "containsKey",
        "distinctFrom",
        "distinctFromInsensitive",
        "endsWith",
        "endsWithInsensitive",
        "equalTo",
        "equalToInsensitive",
        "greaterThan",
        "greaterThanInsensitive",
        "greaterThanOrEqualTo",
        "greaterThanOrEqualToInsensitive",
        "in",
        "inInsensitive",
        "includes",
        "includesInsensitive",
        "isNull",
        "lessThan",
        "lessThanInsensitive",
        "lessThanOrEqualTo",
        "lessThanOrEqualToInsensitive",
        "like",
        "likeInsensitive",
        "notDistinctFrom",
        "notDistinctFromInsensitive",
        "notEndsWith",
        "notEndsWithInsensitive",
        "notEqualTo",
        "notEqualToInsensitive",
        "notIn",
        "notInInsensitive",
        "notIncludes",
        "notIncludesInsensitive",
        "notLike",
        "notLikeInsensitive",
        "notStartsWith",
        "notStartsWithInsensitive",
        "overlaps",
        "startsWith",
        "startsWithInsensitive",
    }
)
FILTER_LOGIC = ("and", "or", "not")
# The fields of a list (connection) that hold its rows; any other field with a selection set below a row is a relation
# to another entity.
CONNECTION_FIELDS = ("nodes", "edges", "pageInfo", "totalCount")
# Largest page a connection may ask for; the API caps a connection at this many rows without saying so.
MAX_FIRST = 1000
# Most fields the guards visit in one query. Fragments spread in nested fields multiply the visits (2^n for n nested
# levels), so the check stops there instead of taking minutes; no real query comes close.
MAX_VISITED_FIELDS = 5000


def _child_fields(
    selection_set: SelectionSetNode,
    fragments: Dict[str, FragmentDefinitionNode],
    path: Tuple[str, ...] = (),
    seen: Optional[Set[str]] = None,
) -> List[Tuple[FieldNode, Tuple[str, ...]]]:
    """Fields selected directly in a selection set, looking through inline and named fragments, each with the
    fragments expanded to reach it.

    path holds the fragments being expanded on the way down from the operation, through nested fields too, to refuse
    a fragment that reaches itself (the API would refuse it too); seen holds those already expanded in this selection
    set, whose fields a second spread would only repeat.
    """
    seen = set() if seen is None else seen
    fields = []
    for selection in selection_set.selections:
        if isinstance(selection, FieldNode):
            fields.append((selection, path))
        elif isinstance(selection, InlineFragmentNode):
            fields.extend(_child_fields(selection.selection_set, fragments, path, seen))
        elif isinstance(selection, FragmentSpreadNode) and selection.name.value in fragments:
            name = selection.name.value
            if name in path:
                raise GraphQLError(f'Cannot spread fragment "{name}" within itself.')
            if name in seen:
                continue
            seen.add(name)
            fields.extend(_child_fields(fragments[name].selection_set, fragments, path + (name,), seen))
    return fields


def _relation_filter(value: ValueNode) -> Optional[str]:
    """Why a filter value filters through a relation (or holds a misspelled operator, which reads the same), else
    None."""
    if isinstance(value, ListValueNode):
        return next((error for error in map(_relation_filter, value.values) if error), None)
    if not isinstance(value, ObjectValueNode):
        return None
    for entry in value.fields:
        key = entry.name.value
        if key in FILTER_LOGIC:
            error = _relation_filter(entry.value)
            if error:
                return error
        elif key.endswith("Exist"):
            return f'"{key}" tests a relation'
        elif isinstance(entry.value, ObjectValueNode):
            inner = next((f.name.value for f in entry.value.fields if f.name.value not in FILTER_OPERATORS), None)
            if inner:
                return (
                    f'"{key}" holds "{inner}", which is not an operator (equalTo, in, greaterThanOrEqualTo, '
                    "includes, isNull, ...): a misspelled operator, or a filter through a relation"
                )
    return None


def _check_field(
    field: FieldNode,
    fragments: Dict[str, FragmentDefinitionNode],
    path: Tuple[str, ...],
    visited: List[int],
    level: str = "root",
) -> Optional[str]:
    """Why the field must not be sent, or None.

    level says where the field sits: root (a field of Query), connection (a field of a list: nodes, edges, totalCount),
    edge (a field of edges), row (a field of one entity: a node, or what a lookup by key returns) or free (below
    introspection, _metadata or pageInfo, where no relation rule applies).
    """
    # visited counts the fields checked so far in the query (one shared counter).
    visited[0] += 1
    if visited[0] > MAX_VISITED_FIELDS:
        raise GraphQLError(f"Query too large to check: it expands to more than {MAX_VISITED_FIELDS} fields.")
    name = field.name.value
    if name.startswith(RAW_FIELD_PREFIXES):
        return f'"{name}" reads a raw payout table and is not allowed: {RAW_FIELD_HINT}.'
    if name.endswith("List") and name[: -len("List")] in CATALOG_FUNCTIONS:
        # The row variant of a settlement catalog function: once pocketdex answers a range it does not cover with
        # what it has, no rows cannot tell "not covered" from "nothing happened".
        return (
            f'"{name}" is not allowed: its rows cannot say that a range is not covered, so no rows would read as 0. '
            f"Use {name[: -len('List')]}Json, which returns the same rows with the range it covers."
        )
    if name in LIVE_REWARD_FIELDS:
        return (
            f'"{name}" scans the raw payout tables and is not allowed: use legacy{name[len("get") :].removesuffix("V2")} '
            '(same arguments and JSON, which a newer API puts under "data" next to a "range"), or the settlement '
            "catalog (getIncomeJson, getSupplyFlowsJson)."
        )
    if name in AGGREGATE_FIELDS:
        return f'"{name}" is not served: {AGGREGATE_HINT}.'
    if name == "nodeId" or (level == "root" and (name == "node" or name.endswith("ByNodeId"))):
        return f'"{name}" is not served: look an entity up by its key, e.g. supplier(id: "pokt1...") or block(id: "1").'
    if level == "root" and name in HIDDEN_FIELDS:
        return f'"{name}" is leaving the API: use {HIDDEN_FIELDS[name]}.'
    if level == "root" and name.startswith(EVENT_PREFIXES):
        return f'"{name}" is leaving the API: {EVENT_HINT}.'
    for arg in field.arguments:
        error = _relation_filter(arg.value) if arg.name.value == "filter" else None
        if error:
            return (
                f'the filter of "{name}" is refused: {error}. Use the operators, on the entity\'s own columns only; '
                "relations are not served, so query the other entity on its own and filter by its id column "
                "(supplierId, applicationId, blockId)."
            )
    if field.selection_set is None:
        return None
    if level == "row":
        return (
            f'"{name}" is a relation to another entity, which is not served: select its id column (e.g. {name}Id) '
            "and query that entity as its own root field filtered by that id, in the same query or the next one "
            '(e.g. balances(filter: {accountId: {equalTo: "pokt1..."}}, first: 10)).'
        )

    children = _child_fields(field.selection_set, fragments, path)
    if any(child.name.value in ("nodes", "edges") for child, _ in children):
        first = next((arg.value for arg in field.arguments if arg.name.value == "first"), None)
        if not isinstance(first, IntValueNode) or not 0 < int(first.value) <= MAX_FIRST:
            return (
                f'"{name}" selects nodes/edges and needs a literal "first" between 1 and {MAX_FIRST}; '
                "for a count use totalCount; for a sum, the function that sums; for more rows, page by keyset "
                '(orderBy: ID_ASC with filter {id: {greaterThan: "<last id>"}}), never by offset.'
            )

    if level == "root" and name.startswith("_"):
        child_level = "free"
    elif level == "root":
        is_list = any(child.name.value in CONNECTION_FIELDS + AGGREGATE_FIELDS for child, _ in children)
        child_level = "connection" if is_list else "row"
    elif level == "connection":
        child_level = {"nodes": "row", "edges": "edge"}.get(name, "free")
    elif level == "edge":
        child_level = "row" if name == "node" else "free"
    else:
        child_level = "free"
    for child, child_path in children:
        error = _check_field(child, fragments, child_path, visited, child_level)
        if error:
            return error
    return None


def check_query_guards(document: DocumentNode) -> Optional[str]:
    """Return why an LLM-generated query must not be sent, or None when it may be.

    Raises GraphQLError for a fragment that reaches itself, directly or through nested fields, and for a query that
    expands to more than MAX_VISITED_FIELDS fields or nests deeper than the recursion limit.
    """
    fragments = {d.name.value: d for d in document.definitions if isinstance(d, FragmentDefinitionNode)}
    visited = [0]
    try:
        for definition in document.definitions:
            if isinstance(definition, OperationDefinitionNode):
                for field, path in _child_fields(definition.selection_set, fragments):
                    error = _check_field(field, fragments, path, visited)
                    if error:
                        return f"Query refused: {error}"
    except RecursionError:
        # A chain of about a thousand fragments, each spreading the next, is deeper than Python's recursion limit.
        raise GraphQLError("Query too deeply nested to check.") from None
    return None


# The live schema, built from one introspection of the API and rebuilt once it is older than SCHEMA_TTL_SECONDS (the
# introspection tools' TTL), so that a long-running server follows the fields the API adds, deprecates and removes. A
# failed introspection keeps the schema it had (none at first) and is tried again after SCHEMA_RETRY_SECONDS.
SCHEMA_TTL_SECONDS = 3600
SCHEMA_RETRY_SECONDS = 60
_schema: Optional[GraphQLSchema] = None
_schema_fetched_at: Optional[float] = None


def _fetch_schema() -> GraphQLSchema:
    response = requests.post(
        POCKET_NETWORK_DATA_ENDPOINT,
        json={"query": get_introspection_query(descriptions=False)},
        headers={"Content-Type": "application/json"},
        timeout=30,
    )
    response.raise_for_status()
    return build_client_schema(response.json()["data"])


def live_schema() -> Optional[GraphQLSchema]:
    """The API schema, or None while it cannot be introspected (then only the guards check a query)."""
    global _schema, _schema_fetched_at
    now = time.monotonic()
    if _schema_fetched_at is None or now - _schema_fetched_at > SCHEMA_TTL_SECONDS:
        try:
            _schema = _fetch_schema()
            _schema_fetched_at = now
        except Exception as e:
            logger.warning("GraphQL schema introspection failed, checking queries with the guards only: %s", e)
            _schema_fetched_at = now - SCHEMA_TTL_SECONDS + SCHEMA_RETRY_SECONDS
    return _schema


def check_against_schema(document: DocumentNode) -> Optional[str]:
    """Why the API would refuse the query, or None: every field, argument and type it uses must exist, and none may be
    deprecated (a deprecated field is about to leave the API; its deprecation reason names the replacement)."""
    schema = live_schema()
    if schema is None:
        return None
    errors = validate(schema, document, [*specified_rules, NoDeprecatedCustomRule])
    if not errors:
        return None
    return "Query refused by the API schema: " + " ".join(error.message for error in errors[:5])


def validate_graphql_query(query: str, selected_method: str) -> Tuple[bool, str, Optional[str]]:
    """
    Validate a GraphQL query for format correctness.

    Args:
        query: GraphQL query string to validate

    Returns:
        Tuple of (is_valid, normalized_query, error_message)
    """
    if GRAPHQL_REGISTRY.get(selected_method, None) is None:
        return False, "", "Selected method not in the available methods list."

    try:
        # Parse the query to validate syntax
        document = parse(query)

        guard_error = check_query_guards(document) or check_against_schema(document)
        if guard_error:
            return False, "", guard_error

        # Print it back to normalize formatting
        normalized = print_ast(document)

        return True, normalized, None
    except Exception as e:
        print(query)
        error_msg = f"GraphQL validation error: {str(e)}"
        return False, "", error_msg
