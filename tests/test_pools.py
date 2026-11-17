"""Host-wide pools: a shared limit across projects, priority yielding and expiry of slots nobody refreshes."""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from theseus import pools


class PoolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="theseus-pools-"))
        self.saved = {k: os.environ.get(k) for k in ("THESEUS_POOLS", "THESEUS_POOL_STATE")}
        os.environ["THESEUS_POOLS"] = str(self.tmp / "pools.toml")
        os.environ["THESEUS_POOL_STATE"] = str(self.tmp / "state")
        (self.tmp / "pools.toml").write_text('[pools.gw]\nmax = 2\npriority = ["first", "second"]\n')
        self.first, self.second = SimpleNamespace(name="first"), SimpleNamespace(name="second")

    def tearDown(self):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_full_pool_refuses_and_records_a_claim(self):
        pools.sync(self.second, "gw", {"b1": "t1", "b2": "t2"})
        allowed, why = pools.may_start(self.first, "gw")
        self.assertFalse(allowed)
        self.assertIn("full (2/2)", why)
        self.assertEqual(["first"], pools.status("gw")["claims"])

    def test_lower_priority_project_yields_while_a_higher_claim_is_fresh(self):
        pools.sync(self.second, "gw", {"b1": "t1", "b2": "t2"})
        pools.may_start(self.first, "gw")
        pools.sync(self.second, "gw", {"b1": "t1"})
        allowed, why = pools.may_start(self.second, "gw")
        self.assertFalse(allowed)
        self.assertIn("yielding to first", why)
        self.assertTrue(pools.may_start(self.first, "gw")[0])
        pools.drop_claim(self.first, "gw")
        self.assertTrue(pools.may_start(self.second, "gw")[0])

    def test_slots_a_project_stopped_refreshing_expire(self):
        pools.sync(self.second, "gw", {"b1": "t1", "b2": "t2"})
        path = self.tmp / "state/gw.json"
        state = json.loads(path.read_text())
        for lease in state["leases"].values():
            lease["seen"] = time.time() - pools.LEASE_TTL - 1
        path.write_text(json.dumps(state))
        self.assertTrue(pools.may_start(self.first, "gw")[0])
        self.assertEqual(0, pools.status("gw")["active"])


if __name__ == "__main__":
    unittest.main()
