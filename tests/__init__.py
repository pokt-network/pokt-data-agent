import os

# The clients read their endpoints at import time; the tests never send a request.
os.environ.setdefault("POCKET_NETWORK_DATA_ENDPOINT", "http://localhost:3000/")
os.environ.setdefault("POCKET_NETWORK_RPC_ENDPOINT", "http://localhost:1317")

# Nor does the schema check introspect the API: without a schema, it leaves a query to the guards.
from unittest import mock  # noqa: E402

mock.patch("src.graphql_validator._fetch_schema", side_effect=OSError("the tests do not introspect the API")).start()
