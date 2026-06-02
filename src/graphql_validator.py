"""GraphQL query validation."""

from graphql import parse, print_ast
from typing import Tuple, Optional
from src.graphql_client import GRAPHQL_REGISTRY


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

        # Print it back to normalize formatting
        normalized = print_ast(document)

        return True, normalized, None
    except Exception as e:
        print(query)
        error_msg = f"GraphQL validation error: {str(e)}"
        return False, "", error_msg
