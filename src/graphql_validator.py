"""GraphQL query validation."""

import logging
import threading
import time
from typing import Dict, FrozenSet, List, Optional, Set, Tuple

from graphql import (
    DocumentNode,
    FieldNode,
    FragmentDefinitionNode,
    FragmentSpreadNode,
    GraphQLError,
    GraphQLInputObjectType,
    GraphQLSchema,
    InlineFragmentNode,
    IntValueNode,
    ListValueNode,
    ObjectValueNode,
    OperationDefinitionNode,
    SelectionSetNode,
    ValueNode,
    VariableNode,
    build_client_schema,
    get_introspection_query,
    parse,
    print_ast,
    specified_rules,
    validate,
)
from graphql.validation import NoDeprecatedCustomRule

from src import tools_introspection
from src.graphql_client import GRAPHQL_REGISTRY

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
# (src/mappings/dbFunctions/settlement/functions.ts) without ROW_FUNCTIONS. Each has a ...List row variant, paged at
# 1000 rows, and a ...Json twin with every row in one call and the range it covers.
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
        "getBlockStats",
    }
)
# The rest of pocketdex CATALOG_FUNCTIONS: functions that list rows one by one, newest first, at most rowLimit a call
# and paged with before...; the ...List variant and the ...Json twin return the same rows, the twin with the range.
ROW_FUNCTIONS = frozenset(
    {
        "getClaims",
        "getActorLifecycle",
        "getRelayMiningDifficulty",
        "getCreatedClaims",
        "getMorseClaims",
    }
)
# The lists and functions the closed pocketdex schema drops (docs/graphql-migration.md), with what replaces them. The
# event and message tables not named here are hidden too (EVENT_PREFIXES). The live schema's deprecation reason, when
# there is one, is said instead: these are the fallback while the API cannot be introspected.
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
# else is a filter through a relation, which the API no longer serves. The live schema's operators are used instead
# when it has been introspected: this list is the fallback.
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


def _relation_filter(value: ValueNode, operators: FrozenSet[str]) -> Optional[str]:
    """Why a filter value filters through a relation (or holds a misspelled operator, which reads the same, or a
    variable, whose keys cannot be checked), else None."""
    if isinstance(value, VariableNode):
        return f'"${value.name.value}" is a variable, whose keys cannot be checked: write the filter inline'
    if isinstance(value, ListValueNode):
        return next((error for error in (_relation_filter(v, operators) for v in value.values) if error), None)
    if not isinstance(value, ObjectValueNode):
        return None
    for entry in value.fields:
        key = entry.name.value
        if key in FILTER_LOGIC:
            error = _relation_filter(entry.value, operators)
            if error:
                return error
        elif key.endswith("Exist"):
            return f'"{key}" tests a relation'
        elif isinstance(entry.value, VariableNode):
            return (
                f'"{key}" holds the variable "${entry.value.name.value}", whose keys cannot be checked: write it inline'
            )
        elif isinstance(entry.value, ObjectValueNode):
            inner = next((f.name.value for f in entry.value.fields if f.name.value not in operators), None)
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
    facts: Optional["SchemaFacts"] = None,
) -> Optional[str]:
    """Why the field must not be sent, or None.

    level says where the field sits: root (a field of Query), connection (a field of a list: nodes, edges, totalCount),
    edge (a field of edges), row (a field of one entity: a node, or what a lookup by key returns) or free (below
    introspection, _metadata or pageInfo, where no relation rule applies). facts holds what the live schema says, if
    it has been introspected.
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
            f'"{name}" is not allowed: it pages its rows at 1000, and no rows cannot say that a range is not covered, '
            f"so they would read as 0. Use {name[: -len('List')]}Json, which returns every row in one call with the "
            "range it covers."
        )
    if name.endswith("List") and name[: -len("List")] in ROW_FUNCTIONS:
        return (
            f'"{name}" is not allowed: no rows cannot say whether the range is not covered or nothing happened in it. '
            f"Use {name[: -len('List')]}Json: the same rows (at most rowLimit, paged with before...) with the range "
            "it covers."
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
    if level == "root" and (name in HIDDEN_FIELDS or name.startswith(EVENT_PREFIXES)):
        reason = facts.deprecations.get(name) if facts else None
        if reason:
            # pocketdex names the ...List variants too, which the guards refuse
            refused = sorted(f for f in CATALOG_FUNCTIONS | ROW_FUNCTIONS if f"{f}List" in reason)
            return f'"{name}" is leaving the API: {reason}' + "".join(f" Use {f}Json, not {f}List." for f in refused)
        return f'"{name}" is leaving the API: ' + (
            f"use {HIDDEN_FIELDS[name]}." if name in HIDDEN_FIELDS else f"{EVENT_HINT}."
        )
    operators = facts.operators if facts else FILTER_OPERATORS
    for arg in field.arguments:
        error = _relation_filter(arg.value, operators) if arg.name.value == "filter" else None
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
        offset = next((arg.value for arg in field.arguments if arg.name.value == "offset"), None)
        if offset is not None and not (isinstance(offset, IntValueNode) and int(offset.value) == 0):
            # Rows written or removed between two pages shift the offsets: rows are skipped or repeated, silently.
            return (
                f'"{name}" pages by offset, which skips or repeats rows when the list changes between pages: page by '
                "keyset instead, with a fixed orderBy (e.g. ID_ASC), pageInfo { hasNextPage endCursor } and after: "
                '"<endCursor>" (the same blockHeight on every page of a walk).'
            )
        if not isinstance(first, IntValueNode) or not 0 < int(first.value) <= MAX_FIRST:
            return (
                f'"{name}" selects nodes/edges and needs a literal "first" between 1 and {MAX_FIRST}; '
                "for a count use totalCount; for a sum, the function that sums; for more rows, page by keyset "
                '(orderBy: ID_ASC, pageInfo { hasNextPage endCursor }, after: "<endCursor>"), never by offset.'
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
        error = _check_field(child, fragments, child_path, visited, child_level, facts)
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
    facts = _live
    try:
        for definition in document.definitions:
            if isinstance(definition, OperationDefinitionNode):
                for field, path in _child_fields(definition.selection_set, fragments):
                    error = _check_field(field, fragments, path, visited, "root", facts)
                    if error:
                        return f"Query refused: {error}"
    except RecursionError:
        # A chain of about a thousand fragments, each spreading the next, is deeper than Python's recursion limit.
        raise GraphQLError("Query too deeply nested to check.") from None
    return None


# The live schema, built from one introspection of the API (tools_introspection's client) and rebuilt once it is older
# than its cache TTL, so that a long-running server follows the fields the API adds, deprecates and removes. The
# introspection runs in a background thread, one at a time, and a query is never kept waiting for it: until the first
# one finishes the guards check queries alone, and a refresh serves the schema it had. A failed introspection is tried
# again after SCHEMA_RETRY_SECONDS.
SCHEMA_TTL_SECONDS = tools_introspection.CACHE_TTL_SECONDS
SCHEMA_RETRY_SECONDS = 60


class SchemaFacts:
    """The live schema, the operators of its filter types on a column, and the deprecation reasons of its root fields."""

    def __init__(self, schema: GraphQLSchema):
        self.schema = schema
        self.operators = (
            frozenset(
                name
                for type_ in schema.type_map.values()
                if isinstance(type_, GraphQLInputObjectType)
                and type_.name.endswith("Filter")
                and "ToMany" not in type_.name
                and "Aggregate" not in type_.name
                and not set(FILTER_LOGIC) & set(type_.fields)
                for name in type_.fields
            )
            or FILTER_OPERATORS
        )
        self.deprecations = {
            name: field.deprecation_reason
            for name, field in schema.query_type.fields.items()
            if field.deprecation_reason
        }


_live: Optional[SchemaFacts] = None
_next_refresh_at = 0.0
_refreshing = False
_refresh_lock = threading.Lock()


def _fetch_schema() -> GraphQLSchema:
    return build_client_schema(tools_introspection._introspect(get_introspection_query(descriptions=False)))


def refresh_schema() -> None:
    """Introspect the API and replace the live schema; on failure keep the one there is."""
    global _live, _next_refresh_at, _refreshing
    try:
        _live = SchemaFacts(_fetch_schema())
        _next_refresh_at = time.monotonic() + SCHEMA_TTL_SECONDS
    except Exception as e:
        logger.warning("GraphQL schema introspection failed, keeping the schema there was (if any): %s", e)
        _next_refresh_at = time.monotonic() + SCHEMA_RETRY_SECONDS
    finally:
        with _refresh_lock:
            _refreshing = False


def live_schema() -> Optional[GraphQLSchema]:
    """The last introspected API schema, or None before the first one (then only the guards check a query). Starts a
    background introspection when the schema is due, unless one is running; never waits for it."""
    global _refreshing
    with _refresh_lock:
        due = not _refreshing and time.monotonic() >= _next_refresh_at
        if due:
            _refreshing = True
    if due:
        threading.Thread(target=refresh_schema, name="graphql-schema-refresh", daemon=True).start()
    facts = _live
    return facts.schema if facts else None


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
