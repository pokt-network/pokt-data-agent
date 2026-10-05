"""GraphQL query validation."""

from typing import Dict, List, Optional, Set, Tuple

from graphql import (
    DocumentNode,
    FieldNode,
    FragmentDefinitionNode,
    FragmentSpreadNode,
    GraphQLError,
    InlineFragmentNode,
    IntValueNode,
    OperationDefinitionNode,
    SelectionSetNode,
    parse,
    print_ast,
)

from src.graphql_client import GRAPHQL_REGISTRY

# Fields that read raw payout tables: a broad filter on them scans millions of rows. The settlement catalog answers
# the same questions from precomputed tables.
RAW_FIELD_PREFIXES = ("modToAcctTransfer", "domainServiceDailyReward")
RAW_FIELD_HINT = (
    "use getIncomeJson (by_supplier / by_service / by_reason, bucket hour for the finest grain) or "
    "getSupplierDistributionJson instead"
)
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


def _check_field(
    field: FieldNode, fragments: Dict[str, FragmentDefinitionNode], path: Tuple[str, ...], visited: List[int]
) -> Optional[str]:
    # visited counts the fields checked so far in the query (one shared counter).
    visited[0] += 1
    if visited[0] > MAX_VISITED_FIELDS:
        raise GraphQLError(f"Query too large to check: it expands to more than {MAX_VISITED_FIELDS} fields.")
    name = field.name.value
    if name.startswith(RAW_FIELD_PREFIXES):
        return f'"{name}" reads a raw payout table and is not allowed: {RAW_FIELD_HINT}.'
    if field.selection_set is None:
        return None

    children = _child_fields(field.selection_set, fragments, path)
    if any(child.name.value in ("nodes", "edges") for child, _ in children):
        first = next((arg.value for arg in field.arguments if arg.name.value == "first"), None)
        if not isinstance(first, IntValueNode) or not 0 < int(first.value) <= MAX_FIRST:
            return (
                f'"{name}" selects nodes/edges and needs a literal "first" between 1 and {MAX_FIRST}; '
                "for larger answers use totalCount/aggregates, paginate with offset, or the matching ...Json function."
            )

    for child, child_path in children:
        error = _check_field(child, fragments, child_path, visited)
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

        # TODO: Some queries pass this check with some fields that are not valid, like using "gte" in place of "greaterThanOrEqualTo". This leads to a query and retry that should be avoided.

        guard_error = check_query_guards(document)
        if guard_error:
            return False, "", guard_error

        # Print it back to normalize formatting
        normalized = print_ast(document)

        return True, normalized, None
    except Exception as e:
        print(query)
        error_msg = f"GraphQL validation error: {str(e)}"
        return False, "", error_msg
