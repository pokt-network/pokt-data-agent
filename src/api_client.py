"""GraphQL API client for Poktscan."""

import requests
from typing import Dict, Any, Tuple
import json


class PoktscanAPIClient:
    """Client for querying the Poktscan GraphQL API."""

    def __init__(self, endpoint: str = "https://api.poktscan.com/"):
        """
        Initialize the API client.

        Args:
            endpoint: GraphQL endpoint URL
        """
        self.endpoint = endpoint

    def execute_query(self, query: str) -> Tuple[bool, Any, str]:
        """
        Execute a GraphQL query against the Poktscan API.

        Args:
            query: GraphQL query string

        Returns:
            Tuple of (success, result, error_message)
        """
        try:
            headers = {
                "Content-Type": "application/json",
            }

            payload = {"query": query}

            response = requests.post(
                self.endpoint, json=payload, headers=headers, timeout=30
            )

            # Raise for HTTP errors
            response.raise_for_status()

            data = response.json()

            # Check for GraphQL errors
            if "errors" in data and data["errors"]:
                error_msg = "GraphQL errors: " + "; ".join(
                    [str(err) for err in data["errors"]]
                )
                return False, None, error_msg

            if "data" in data:
                return True, data["data"], None
            else:
                return False, None, "No data returned from API"

        except requests.exceptions.Timeout:
            return False, None, "API request timeout (30s)"
        except requests.exceptions.ConnectionError:
            return False, None, "Connection error to API. Please check the endpoint."
        except requests.exceptions.HTTPError as e:
            return (
                False,
                None,
                f"HTTP error: {e.response.status_code} - {e.response.text}",
            )
        except requests.exceptions.RequestException as e:
            return False, None, f"Request error: {str(e)}"
        except json.JSONDecodeError as e:
            return False, None, f"Invalid JSON response from API: {str(e)}"
        except Exception as e:
            return False, None, f"Unexpected error: {str(e)}"
