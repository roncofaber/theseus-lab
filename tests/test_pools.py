"""Host-wide pools: a shared limit across projects, priority yielding and expiry of slots nobody refreshes."""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import helpers  # noqa: F401  (isolates the host store for the whole run)
from theseus import pools, store

ISOLATED = ("THESEUS_POOLS", "THESEUS_POOL_STATE", "THESEUS_DB", "THESEUS_REGISTRY", "THESEUS_BACKUPS",
            "THESEUS_STORE_CONFIG", "THESEUS_CONFIG")


class PoolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="theseus-pools-"))
        self.saved = {k: os.environ.get(k) for k in ISOLATED}
        for key in ISOLATED:
            os.environ[key] = str(self.tmp / key.lower())
        os.environ["THESEUS_POOLS"] = str(self.tmp / "pools.toml")
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
        with store.opened() as conn:
            conn.execute("UPDATE pool_leases SET seen_epoch = seen_epoch - ?", (pools.LEASE_TTL + 1,))
        self.assertTrue(pools.may_start(self.first, "gw")[0])
        self.assertEqual(0, pools.status("gw")["active"])

    def test_manual_hold_counts_against_the_pool_and_yields_to_priority(self):
        self.assertTrue(pools.hold("second", "gw", "pane-1", "game task")[0])
        self.assertTrue(pools.hold("second", "gw", "pane-2", "game task")[0])
        allowed, why = pools.hold("second", "gw", "pane-3", "game task")
        self.assertFalse(allowed)
        self.assertIn("full", why)
        pools.sync(self.first, "gw", {})
        self.assertFalse(pools.may_start(self.first, "gw")[0])
        self.assertTrue(pools.release("second", "gw", "pane-1"))
        self.assertFalse(pools.hold("second", "gw", "pane-1", "again")[0], "first's fresh claim wins the freed slot")
        self.assertTrue(pools.may_start(self.first, "gw")[0])


if __name__ == "__main__":
    unittest.main()
