"""GraphQL query validation."""

from graphql import parse, print_ast
from typing import Tuple, Optional


def validate_graphql_query(query: str) -> Tuple[bool, str, Optional[str]]:
    """
    Validate a GraphQL query for format correctness.

    Args:
        query: GraphQL query string to validate

    Returns:
        Tuple of (is_valid, normalized_query, error_message)
    """
    try:
        # Parse the query to validate syntax
        document = parse(query)

        # Print it back to normalize formatting
        normalized = print_ast(document)

        return True, normalized, None
    except Exception as e:
        print(query)
        error_msg = f"GraphQL validation error: {str(e)}"
        return False, "", error_msg
