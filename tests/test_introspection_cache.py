import unittest
from unittest import mock

from src import tools_introspection as ti

ENUM_V1 = {"__type": {"name": "E", "kind": "ENUM", "enumValues": [{"name": "A"}]}}
ENUM_V2 = {"__type": {"name": "E", "kind": "ENUM", "enumValues": [{"name": "A"}, {"name": "B"}]}}


class TestIntrospectionCache(unittest.TestCase):
    def setUp(self):
        ti._CACHE.clear()

    @mock.patch.object(ti.time, "monotonic")
    @mock.patch.object(ti, "_introspect")
    def test_cache_expires_after_the_ttl(self, introspect, monotonic):
        introspect.side_effect = [ENUM_V1, ENUM_V2]
        monotonic.return_value = ti._cache_started_at + 1
        self.assertNotIn("B", ti.get_enum_values.func("E"))
        # within the TTL the cached answer is served
        monotonic.return_value = ti._cache_started_at + ti.CACHE_TTL_SECONDS - 1
        self.assertNotIn("B", ti.get_enum_values.func("E"))
        self.assertEqual(introspect.call_count, 1)
        # after it, the schema is asked again and the new value shows up
        monotonic.return_value = ti._cache_started_at + ti.CACHE_TTL_SECONDS + 1
        self.assertIn("B", ti.get_enum_values.func("E"))
        self.assertEqual(introspect.call_count, 2)


if __name__ == "__main__":
    unittest.main()
