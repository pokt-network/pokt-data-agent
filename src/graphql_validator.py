"""GraphQL query validation."""

import hashlib
import json
import logging
import os
import random
import re
import threading
import time
from typing import Any, Dict, FrozenSet, List, Optional, Set, Tuple

from graphql import (
    BREAK,
    DocumentNode,
    EnumValueNode,
    FieldNode,
    FragmentDefinitionNode,
    FragmentSpreadNode,
    GraphQLError,
    GraphQLInputObjectType,
    GraphQLObjectType,
    GraphQLSchema,
    InlineFragmentNode,
    IntValueNode,
    ListValueNode,
    ObjectValueNode,
    OperationDefinitionNode,
    SelectionSetNode,
    TypeInfo,
    TypeInfoVisitor,
    ValueNode,
    VariableNode,
    Visitor,
    build_client_schema,
    get_introspection_query,
    get_named_type,
    is_enum_type,
    is_input_object_type,
    is_interface_type,
    is_object_type,
    parse,
    print_ast,
    specified_rules,
    validate,
    visit,
)
from graphql.validation import NoDeprecatedCustomRule

from src import tools_introspection
from src.graphql_client import GRAPHQL_REGISTRY, unwrap_range

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
# (DEFAULT_ROW_LIMIT when omitted), paged with before...; their ...List variant and their ...Json twin return the same
# rows, the twin with the range.
ROW_FUNCTIONS = frozenset(
    {
        "getClaims",
        "getActorLifecycle",
        "getRelayMiningDifficulty",
        "getCreatedClaims",
        "getMorseClaims",
        "getNativeTransfers",
    }
)
DEFAULT_ROW_LIMIT = 100
# Function lists paged by their own keyset (rowLimit and the after... keys of the last row): an offset is refused.
KEYSET_LISTS = frozenset(
    {
        "getSupplierFleetList",
        "getStakedSuppliersList",
        "getStakedSupplierServicesList",
        "getStakedDomainServicesList",
        "getGatewayDelegationsList",
    }
)
# The most rows one call of a function list returns, whatever "first" or "rowLimit" asks: the API's query limit (both
# endpoints, 2026-10-14), which cuts the list with no signal (getSupplierFleetList Staked: 1000 of 4,558 with
# first: 2000). A call that returns that many may have more.
MAX_LIST_ROWS = 1000
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
    "eventRelayMiningDifficultyUpdateds": "getRelayMiningDifficultyJson (the history); the current value: getServiceSummaryList",
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
    "relayByBlockAndServices": "getServiceUsageJson",
    "relays": "nothing: the table is empty",
}
EVENT_PREFIXES = ("event", "msg")
EVENT_HINT = "the event and message tables are hidden: use the function that answers the question (introspect Query)"
# Sums and groupings over a list, which the API no longer serves, and where to find them instead.
AGGREGATE_FIELDS = ("aggregates", "groupedAggregates")
RELATION_HINT = (
    "select its id column and query that entity as its own root field filtered by that id, in the same query or the "
    'next one (e.g. balances(filter: {accountId: {equalTo: "pokt1..."}}, first: 10))'
)
FILTER_HINT = (
    "Use the operators, on the entity's own columns only; relations are not served, so query the other entity on "
    "its own and filter by its id column (supplierId, applicationId, blockId)."
)
AGGREGATE_HINT = (
    "use totalCount for a count, a function that sums the rows (getSupplierFleetList, getApplicationFleetList, getGatewayFleetList, "
    "getServiceSummaryList, getBlockStatsJson, getMorseClaimSummaryList or the settlement catalog ...Json), or the "
    "newest blocks row's staked* and unstaking* columns"
)
# The operators of the filter types on a column (pocketdex schema, 2026-10-14). A filter key whose value holds anything
# else is a filter through a relation, which the API no longer serves. This list is the fallback before the live schema
# loads; then the schema-typed walk reads each filter type's own operators.
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
# orderBy values that sort by a related row (ACCOUNT_BY_OWNER_ID__ID_ASC) or by a sum over related rows
# (EVENT_CLAIM_SETTLEDS_SUM_CLAIMED_AMOUNT_DESC): the fallback before the live schema loads. It misses the counts
# (SERVICE_CONFIGS_COUNT_DESC), which read like a column such as SUPPLIERS_COUNT_ASC; the schema-typed walk does not.
RELATION_ORDER = re.compile(
    r"__|_(SUM|DISTINCT_COUNT|MIN|MAX|AVERAGE|STDDEV_SAMPLE|STDDEV_POPULATION|VARIANCE_SAMPLE|VARIANCE_POPULATION)_"
)
ORDER_HINT = "sums and counts over a relation are not served. Order by one of the list's own columns (e.g. ID_ASC)."
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


def _argument(field: FieldNode, name: str) -> Optional[ValueNode]:
    return next((arg.value for arg in field.arguments if arg.name.value == name), None)


def _int_argument(field: FieldNode, name: str) -> Optional[int]:
    """The literal integer value of an argument, or None (absent, a variable, or not an integer)."""
    value = _argument(field, name)
    return int(value.value) if isinstance(value, IntValueNode) else None


def _relation_filter(value: ValueNode, operators: Optional[FrozenSet[str]]) -> Optional[str]:
    """Why a filter value filters through a relation (or holds a misspelled operator, which reads the same, or a
    variable, whose keys cannot be checked), else None. With operators None only the variables are checked: the
    schema-typed walk checks the rest."""
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
        elif isinstance(entry.value, VariableNode):
            return (
                f'"{key}" holds the variable "${entry.value.name.value}", whose keys cannot be checked: write it inline'
            )
        elif operators is None:
            continue
        elif key.endswith("Exist"):
            return f'"{key}" tests a relation'
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
    # With the live schema loaded, relations and relation filters are found by type (_closed_schema_errors); these
    # name patterns are the fallback before it loads.
    operators = None if facts else FILTER_OPERATORS
    for arg in field.arguments:
        error = _relation_filter(arg.value, operators) if arg.name.value == "filter" else None
        if error:
            return f'the filter of "{name}" is refused: {error}. {FILTER_HINT}'
        values = arg.value.values if isinstance(arg.value, ListValueNode) else [arg.value]
        if arg.name.value == "orderBy" and not facts:
            order = next(
                (v.value for v in values if isinstance(v, EnumValueNode) and RELATION_ORDER.search(v.value)), None
            )
            if order:
                return f'"{name}" orders by {order}, which is not one of its own columns: {ORDER_HINT}'
    if name in KEYSET_LISTS and _argument(field, "offset") is not None and _int_argument(field, "offset") != 0:
        return (
            f'"{name}" pages by its keyset, never by offset: pass rowLimit, and for the next page the keys of the last '
            "row as the after... arguments, until a page comes back short."
        )
    if field.selection_set is None:
        return None
    if level == "row" and not facts:
        return f'"{name}" is a relation to another entity, which is not served: {RELATION_HINT}.'

    children = _child_fields(field.selection_set, fragments, path)
    if any(child.name.value in ("nodes", "edges") for child, _ in children):
        first = _int_argument(field, "first")
        if _argument(field, "offset") is not None and _int_argument(field, "offset") != 0:
            # Rows written or removed between two pages shift the offsets: rows are skipped or repeated, silently.
            return (
                f'"{name}" pages by offset, which skips or repeats rows when the list changes between pages: page by '
                "keyset instead, with a fixed orderBy (e.g. ID_ASC), pageInfo { hasNextPage endCursor } and after: "
                '"<endCursor>" (the same blockHeight on every page of a walk).'
            )
        if first is None or not 0 < first <= MAX_FIRST:
            return (
                f'"{name}" selects nodes/edges and needs a literal "first" between 1 and {MAX_FIRST}; '
                "for a count use totalCount; for a sum, the function that sums; for more rows, page by keyset "
                '(orderBy: ID_ASC, pageInfo { hasNextPage endCursor }, after: "<endCursor>"), never by offset.'
            )

    if level == "root" and name == "query":
        # PostGraphile's Query.query re-enters the root: its fields are root fields
        child_level = "root"
    elif level == "root" and name.startswith("_"):
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


def check_query(document: DocumentNode) -> Optional[str]:
    """Why an LLM-generated query must not be sent, or None when it may be: the guards, then the API schema, both
    against one snapshot of the live schema (starting its refresh when due).

    Raises GraphQLError for a fragment that reaches itself, directly or through nested fields, and for a query that
    expands to more than MAX_VISITED_FIELDS fields or nests deeper than the recursion limit.
    """
    facts = _live_facts()
    return _guards(document, facts) or _schema_errors(document, facts)


def _guards(document: DocumentNode, facts: Optional["SchemaFacts"]) -> Optional[str]:
    fragments = {d.name.value: d for d in document.definitions if isinstance(d, FragmentDefinitionNode)}
    visited = [0]
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
    error = _closed_schema_errors(document, facts) if facts else None
    return f"Query refused: {error}" if error else None


def _constant_case(name: str) -> str:
    """A field name as PostGraphile spells it in an orderBy value: stakeStatus -> STAKE_STATUS."""
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name).upper()


def _column_order_values(columns: List[str]) -> Set[str]:
    """The orderBy values of a list that sort by its own columns."""
    values = {"NATURAL", "PRIMARY_KEY_ASC", "PRIMARY_KEY_DESC"}
    for column in columns:
        values |= {f"{_constant_case(column)}_ASC", f"{_constant_case(column)}_DESC"}
    return values


class _ClosedSchemaVisitor(Visitor):
    """Walks a query with the live schema's types and stops at the first thing the closed schema does not serve: a
    field of an entity whose type is an object (a relation), a filter field whose type is another entity's filter or a
    to-many filter (or tests that a related row exists), a key of a column filter that is not one of its operators (a
    misspelled one), and an orderBy value that the pruned schema does not hold (prune_introspection keeps the columns of
    the list's own entity; the rest sort by a relation)."""

    def __init__(self, type_info: TypeInfo, facts: "SchemaFacts"):
        super().__init__()
        self.type_info, self.facts = type_info, facts
        self.fields: List[str] = []
        self.error: Optional[str] = None

    def enter_field(self, node, *_):
        parent, field = self.type_info.get_parent_type(), self.type_info.get_field_def()
        self.fields.append(node.name.value)
        if parent is not None and parent.name in self.facts.entities and field is not None:
            target = get_named_type(field.type)
            if is_object_type(target) or is_interface_type(target):
                self.error = (
                    f'"{node.name.value}" is a relation to another entity, which is not served: {RELATION_HINT}.'
                )
                return BREAK
        return None

    def leave_field(self, *_):
        self.fields.pop()

    def enter_object_field(self, node, *_):
        parent = get_named_type(self.type_info.get_parent_input_type())
        key = node.name.value
        name = self.fields[-1] if self.fields else "the query"
        if parent is not None and parent.name in self.facts.column_filters and key not in parent.fields:
            self.error = (
                f'the filter of "{name}" is refused: "{key}" is not an operator of {parent.name} '
                f"({', '.join(parent.fields)})."
            )
            return BREAK
        if parent is None or parent.name not in self.facts.entity_filters or key in FILTER_LOGIC:
            return None
        target = get_named_type(self.type_info.get_input_type())
        exists = key.endswith("Exist") and key[: -len("Exist")] in parent.fields
        if exists or (target is not None and target.name in self.facts.relation_filters):
            self.error = f'the filter of "{name}" is refused: "{key}" filters through a relation. {FILTER_HINT}'
            return BREAK
        return None

    def enter_argument(self, node, *_):
        argument = self.type_info.get_argument()
        order = get_named_type(argument.type) if argument is not None and node.name.value == "orderBy" else None
        if not is_enum_type(order):
            return None
        values = node.value.values if isinstance(node.value, ListValueNode) else [node.value]
        for value in values:
            if isinstance(value, EnumValueNode) and value.value not in order.values:
                name = self.fields[-1] if self.fields else "the query"
                self.error = f'"{name}" orders by {value.value}, which is not one of its own columns: {ORDER_HINT}'
                return BREAK
        return None


def _closed_schema_errors(document: DocumentNode, facts: "SchemaFacts") -> Optional[str]:
    type_info = TypeInfo(facts.schema)
    visitor = _ClosedSchemaVisitor(type_info, facts)
    visit(document, TypeInfoVisitor(type_info, visitor))
    return visitor.error


def _page_note(field: FieldNode, key: str, value: Any) -> Optional[str]:
    """Why the rows of one field are a page of a longer list, or None. A lookup of one row (first or rowLimit 1, or a
    lookup by key, which returns no list) is never a page."""
    name = field.name.value
    first, row_limit = _int_argument(field, "first"), _int_argument(field, "rowLimit")
    if first == 1 or row_limit == 1:
        return None
    if isinstance(value, dict) and isinstance(value.get("nodes", value.get("edges")), list):
        rows = len(value.get("nodes", value.get("edges")))
        total = value.get("totalCount")
        if isinstance(total, int):
            more = total > rows
        else:
            more = (value.get("pageInfo") or {}).get("hasNextPage")
            more = more if more is not None else first is not None and rows >= first
        if more:
            of = f" of {total}" if isinstance(total, int) else ""
            return f"{key} returned {rows} rows{of}: one page of a longer list (the next page: after its endCursor)."
        return None
    if isinstance(value, list):
        limit = min([n for n in (first, row_limit) if n is not None] + [MAX_LIST_ROWS])
        if len(value) >= limit:
            return f"{key} returned {len(value)} rows, the most this call returns: there may be more."
        return None
    if name.endswith("Json") and name[: -len("Json")] in ROW_FUNCTIONS:
        data, _ = unwrap_range(value)
        limit = row_limit or DEFAULT_ROW_LIMIT
        if isinstance(data, list) and len(data) >= limit:
            return (
                f"{key} returned {len(data)} rows, its rowLimit: there may be more (the next page: the before... "
                "arguments with the last row's keys)."
            )
    return None


def _page_notes_in(
    selection_set: SelectionSetNode, fragments: Dict[str, FragmentDefinitionNode], result: Any, notes: List[str]
) -> None:
    """The page notes of the fields of one selection set (through fragments, and through Query.query)."""
    if not isinstance(result, dict):
        return
    for field, _ in _child_fields(selection_set, fragments):
        key = field.alias.value if field.alias else field.name.value
        value = result.get(key)
        if field.name.value == "query" and field.selection_set is not None:
            _page_notes_in(field.selection_set, fragments, value, notes)
            continue
        note = _page_note(field, key, value)
        if note:
            notes.append(note + " Say the answer is a part of the list, never all of it.")


def page_notes(document: DocumentNode, result: Any) -> List[str]:
    """What the answer must say about a list that came back as one page of a longer one: never all of it."""
    fragments = {d.name.value: d for d in document.definitions if isinstance(d, FragmentDefinitionNode)}
    notes: List[str] = []
    try:
        for definition in document.definitions:
            if isinstance(definition, OperationDefinitionNode):
                _page_notes_in(definition.selection_set, fragments, result, notes)
    except (GraphQLError, RecursionError):
        pass
    return list(dict.fromkeys(notes))


# The live schema, built from one introspection of the API (tools_introspection's client) and rebuilt once it is older
# than its cache TTL, so that a long-running server follows the fields the API adds, deprecates and removes. The
# introspection runs in a background thread, one at a time, and a query is never kept waiting for it: until the first
# one finishes the guards check queries alone, and a refresh serves the schema it had. A failed introspection is tried
# again after SCHEMA_RETRY_SECONDS.
SCHEMA_TTL_SECONDS = tools_introspection.CACHE_TTL_SECONDS
SCHEMA_RETRY_SECONDS = 60
SCHEMA_REFRESH_JITTER_SECONDS = 60


class SchemaFacts:
    """The live schema (pruned), the deprecation reasons of its root fields, and what the schema-typed walk needs: its
    entities (the types with a node id), their filter types, the filter types of their relations, and the filter types
    on a column."""

    def __init__(self, schema: GraphQLSchema):
        self.schema = schema
        node = schema.type_map.get("Node")
        self.entities = frozenset(
            type_.name
            for type_ in schema.type_map.values()
            if isinstance(type_, GraphQLObjectType) and node in type_.interfaces and type_ is not schema.query_type
        )
        self.entity_filters = frozenset(f"{name}Filter" for name in self.entities)
        self.relation_filters = self.entity_filters | frozenset(
            name for name, type_ in schema.type_map.items() if is_input_object_type(type_) and "ToMany" in name
        )
        self.column_filters = frozenset(
            name
            for name, type_ in schema.type_map.items()
            if isinstance(type_, GraphQLInputObjectType)
            and name.endswith("Filter")
            and name not in self.relation_filters
            and not set(FILTER_LOGIC) & set(type_.fields)
        )
        self.deprecations = {
            name: field.deprecation_reason
            for name, field in schema.query_type.fields.items()
            if field.deprecation_reason
        }


# The types of what the closed schema drops wholesale and the guards refuse by name (aggregates, group-bys, the
# many-to-many connections), and the orderBy values over relations: about two thirds of the introspection. They are
# pruned before the schema is built, with every field that refers to them, so that they cost no memory; a query that
# uses them is refused by the guards first, or else by the schema check as unknown.
PRUNED_TYPES = re.compile(
    r"(Aggregates|AggregatesFilter|AggregateFilter|GroupBy|ManyToManyConnection|ManyToManyEdge|Having(Sum|DistinctCount"
    r"|Min|Max|Average|StddevSample|StddevPopulation|VarianceSample|VariancePopulation)?Input)$|^Having[A-Z][a-z]+Filter$"
)
# Bumped whenever prune_introspection changes what it keeps: a cache file of another version is not read.
PRUNE_VERSION = 1


def _named(type_ref: Dict[str, Any]) -> str:
    while type_ref.get("ofType"):
        type_ref = type_ref["ofType"]
    return type_ref["name"]


def prune_introspection(data: Dict[str, Any]) -> Dict[str, Any]:
    """The introspection result without PRUNED_TYPES, the fields and arguments that refer to them, the orderBy values
    that are not a column of the list's own entity, and the types nothing refers to any more."""
    schema = data["__schema"]
    types = {t["name"]: t for t in schema["types"]}
    removed = {name for name in types if PRUNED_TYPES.search(name)}
    changed = True
    while changed:
        changed = False
        for name, type_ in types.items():
            if name in removed:
                continue
            for key in ("fields", "inputFields"):
                entries = type_.get(key)
                if not entries:
                    continue
                kept = []
                for entry in entries:
                    if _named(entry["type"]) in removed:
                        continue
                    args = entry.get("args") or []
                    if any(_named(a["type"]) in removed and a["type"]["kind"] == "NON_NULL" for a in args):
                        continue
                    if args:
                        entry["args"] = [a for a in args if _named(a["type"]) not in removed]
                    kept.append(entry)
                if len(kept) != len(entries):
                    type_[key] = kept
                    changed = True
                    if not kept:
                        removed.add(name)
    # orderBy values: keep the list's own columns, found through each connection field's nodes type
    for type_ in types.values():
        for field in type_.get("fields") or []:
            order = next((a for a in field.get("args") or [] if a["name"] == "orderBy"), None)
            nodes = next(
                (f for f in types.get(_named(field["type"]), {}).get("fields") or [] if f["name"] == "nodes"), None
            )
            if order is None or nodes is None:
                continue
            entity = types.get(_named(nodes["type"]))
            columns = [
                f["name"]
                for f in entity.get("fields") or []
                if types[_named(f["type"])]["kind"] not in ("OBJECT", "INTERFACE")
            ]
            allowed = _column_order_values(columns)
            enum = types[_named(order["type"])]
            enum["enumValues"] = [v for v in enum["enumValues"] if v["name"] in allowed]
    # keep what the root types and the directives still reach
    reached: Set[str] = set()
    pending = [schema[k]["name"] for k in ("queryType", "mutationType", "subscriptionType") if schema.get(k)]
    pending += [_named(a["type"]) for d in schema.get("directives") or [] for a in d.get("args") or []]
    while pending:
        name = pending.pop()
        if name in reached or name not in types or name in removed:
            continue
        reached.add(name)
        type_ = types[name]
        for entry in (type_.get("fields") or []) + (type_.get("inputFields") or []):
            pending.append(_named(entry["type"]))
            pending += [_named(a["type"]) for a in entry.get("args") or []]
        pending += [i["name"] for i in type_.get("interfaces") or []]
        pending += [p["name"] for p in type_.get("possibleTypes") or []]
    for type_ in types.values():
        if type_.get("possibleTypes"):
            type_["possibleTypes"] = [p for p in type_["possibleTypes"] if p["name"] in reached]
    schema["types"] = [
        t for name, t in types.items() if name in reached or name.startswith("__") or t["kind"] == "SCALAR"
    ]
    return data


# The pruned introspection is kept on disk for the TTL, so that every process of a deployment (and a restart) builds
# the schema without introspecting the API again. POCKET_NETWORK_SCHEMA_CACHE names the file; a failed read or write
# only means one more introspection.
SCHEMA_CACHE_PATH = os.getenv("POCKET_NETWORK_SCHEMA_CACHE") or os.path.join(
    os.path.expanduser("~"),
    ".cache",
    "pokt-data-agent",
    f"schema-{hashlib.sha256(str(tools_introspection._ENDPOINT).encode()).hexdigest()[:12]}.json",
)


def _introspection() -> Tuple[Dict[str, Any], float]:
    """The pruned introspection and when it was taken (epoch seconds): from the disk cache while it is younger than the
    TTL, of this PRUNE_VERSION and of this endpoint, else from the API."""
    try:
        written_at = os.path.getmtime(SCHEMA_CACHE_PATH)
        if time.time() - written_at < SCHEMA_TTL_SECONDS:
            with open(SCHEMA_CACHE_PATH) as cached:
                content = json.load(cached)
            if content.get("version") == PRUNE_VERSION and content.get("endpoint") == tools_introspection._ENDPOINT:
                return content["introspection"], written_at
    except (OSError, ValueError, AttributeError, KeyError):
        pass
    data = prune_introspection(tools_introspection._introspect(get_introspection_query(descriptions=False)))
    written_at = time.time()
    try:
        os.makedirs(os.path.dirname(SCHEMA_CACHE_PATH), exist_ok=True)
        partial = f"{SCHEMA_CACHE_PATH}.{os.getpid()}.tmp"
        with open(partial, "w") as cache:
            json.dump(
                {"version": PRUNE_VERSION, "endpoint": tools_introspection._ENDPOINT, "introspection": data}, cache
            )
        os.replace(partial, SCHEMA_CACHE_PATH)
        written_at = os.path.getmtime(SCHEMA_CACHE_PATH)
    except OSError as e:
        logger.warning("Could not cache the GraphQL schema on disk: %s", e)
    return data, written_at


_live: Optional[SchemaFacts] = None
_next_refresh_at = 0.0
_refreshing = False
_refresh_lock = threading.Lock()


def _fetch_schema() -> Tuple[GraphQLSchema, float]:
    """The schema and when its introspection was taken (epoch seconds)."""
    data, taken_at = _introspection()
    return build_client_schema(data), taken_at


def refresh_schema() -> None:
    """Introspect the API and replace the live schema; on failure keep the one there is. The next refresh is due when
    the introspection it used (maybe another process's, from the disk cache) is SCHEMA_TTL_SECONDS old: one TTL."""
    global _live, _next_refresh_at, _refreshing
    try:
        schema, taken_at = _fetch_schema()
        _live = SchemaFacts(schema)
        # The processes that loaded the same cache file would all fall due at once and each introspect the API: a
        # random delay lets the first one write the file the others then read.
        due_in = max(0.0, taken_at + SCHEMA_TTL_SECONDS - time.time()) + random.uniform(
            0, SCHEMA_REFRESH_JITTER_SECONDS
        )
        _next_refresh_at = time.monotonic() + due_in
    except Exception as e:
        logger.warning("GraphQL schema introspection failed, keeping the schema there was (if any): %s", e)
        _next_refresh_at = time.monotonic() + SCHEMA_RETRY_SECONDS
    finally:
        with _refresh_lock:
            _refreshing = False


def _live_facts() -> Optional[SchemaFacts]:
    """The last introspected schema facts, or None before the first one (then only the fallback guards check a query).
    Starts a background introspection when the schema is due, unless one is running; never waits for it."""
    global _refreshing
    with _refresh_lock:
        due = not _refreshing and time.monotonic() >= _next_refresh_at
        if due:
            _refreshing = True
    if due:
        try:
            threading.Thread(target=refresh_schema, name="graphql-schema-refresh", daemon=True).start()
        except Exception as e:
            logger.warning("Could not start the GraphQL schema introspection: %s", e)
            with _refresh_lock:
                _refreshing = False
    return _live


def live_schema() -> Optional[GraphQLSchema]:
    """The last introspected API schema, or None before the first one; starts a background introspection when due."""
    facts = _live_facts()
    return facts.schema if facts else None


def _schema_errors(document: DocumentNode, facts: Optional[SchemaFacts]) -> Optional[str]:
    """Why the API would refuse the query, or None: every field, argument and type it uses must exist, and none may be
    deprecated (a deprecated field is about to leave the API; its deprecation reason names the replacement)."""
    if facts is None:
        return None
    errors = validate(facts.schema, document, [*specified_rules, NoDeprecatedCustomRule])
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

        guard_error = check_query(document)
        if guard_error:
            return False, "", guard_error

        # Print it back to normalize formatting
        normalized = print_ast(document)

        return True, normalized, None
    except Exception as e:
        print(query)
        error_msg = f"GraphQL validation error: {str(e)}"
        return False, "", error_msg
