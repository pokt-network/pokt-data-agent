import asyncio
import unittest

from src.mcp_tools import MAIN_AGENT_TOOL_NAME, SUB_AGENT_TOOL_PREFIX
from src.mcp_utils import create_mcp_server


def _tool_names(server_exposure):
    mcp = create_mcp_server(server_exposure=server_exposure)
    return {t.name for t in asyncio.run(mcp.list_tools())}


class TestServerExposure(unittest.TestCase):
    def test_main_agent_exposes_only_the_main_agent(self):
        names = _tool_names("main-agent")
        self.assertIn(MAIN_AGENT_TOOL_NAME, names)
        self.assertFalse([n for n in names if n.startswith(SUB_AGENT_TOOL_PREFIX)])

    def test_sub_agents_exclude_the_main_agent(self):
        names = _tool_names("sub-agents")
        self.assertNotIn(MAIN_AGENT_TOOL_NAME, names)
        self.assertTrue([n for n in names if n.startswith(SUB_AGENT_TOOL_PREFIX)])


if __name__ == "__main__":
    unittest.main()
