"""GraphQL query validation."""

from typing import Dict, List, Optional, Tuple

from graphql import (
    DocumentNode,
    FieldNode,
    FragmentDefinitionNode,
    FragmentSpreadNode,
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


def _child_fields(selection_set: SelectionSetNode, fragments: Dict[str, FragmentDefinitionNode]) -> List[FieldNode]:
    """Fields selected directly in a selection set, looking through inline and named fragments."""
    fields = []
    for selection in selection_set.selections:
        if isinstance(selection, FieldNode):
            fields.append(selection)
        elif isinstance(selection, InlineFragmentNode):
            fields.extend(_child_fields(selection.selection_set, fragments))
        elif isinstance(selection, FragmentSpreadNode) and selection.name.value in fragments:
            fields.extend(_child_fields(fragments[selection.name.value].selection_set, fragments))
    return fields


def _check_field(field: FieldNode, fragments: Dict[str, FragmentDefinitionNode]) -> Optional[str]:
    name = field.name.value
    if name.startswith(RAW_FIELD_PREFIXES):
        return f'"{name}" reads a raw payout table and is not allowed: {RAW_FIELD_HINT}.'
    if field.selection_set is None:
        return None

    children = _child_fields(field.selection_set, fragments)
    if any(child.name.value in ("nodes", "edges") for child in children):
        first = next((arg.value for arg in field.arguments if arg.name.value == "first"), None)
        if not isinstance(first, IntValueNode) or not 0 < int(first.value) <= MAX_FIRST:
            return (
                f'"{name}" selects nodes/edges and needs a literal "first" between 1 and {MAX_FIRST}; '
                "for larger answers use totalCount/aggregates, paginate with offset, or the matching ...Json function."
            )

    for child in children:
        error = _check_field(child, fragments)
        if error:
            return error
    return None


def check_query_guards(document: DocumentNode) -> Optional[str]:
    """Return why an LLM-generated query must not be sent, or None when it may be."""
    fragments = {d.name.value: d for d in document.definitions if isinstance(d, FragmentDefinitionNode)}
    for definition in document.definitions:
        if isinstance(definition, OperationDefinitionNode):
            for field in _child_fields(definition.selection_set, fragments):
                error = _check_field(field, fragments)
                if error:
                    return f"Query refused: {error}"
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
