import os

# The clients read their endpoints at import time; the tests never send a request.
os.environ.setdefault("POCKET_NETWORK_DATA_ENDPOINT", "http://localhost:3000/")
os.environ.setdefault("POCKET_NETWORK_RPC_ENDPOINT", "http://localhost:1317")
