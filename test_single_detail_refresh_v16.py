"""V16 tests for token-bound single-product refresh with no batch queue side effects."""
from __future__ import annotations

import sqlite3
import unittest

from test_server_multi_store_v15 import MultiStoreBatchTests, mod


class SingleDetailRefreshV16Tests(unittest.TestCase):
    def setUp(self):
        self.fixture = MultiStoreBatchTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def test_single_refresh_imports_one_product_without_touching_queue(self):
        conn = sqlite3.connect(mod.DB_PATH)
        before_tasks = conn.execute(
            "SELECT chain,sap_code,plu,status FROM enrichment_tasks ORDER BY chain,sap_code,plu"
        ).fetchall()
        before_events = conn.execute("SELECT COUNT(*) FROM enrichment_task_events").fetchone()[0]
        conn.close()

        created = mod.create_detail_refresh_request({
            "chain": "perekrestok", "sap_code": "XSEO", "plu": "100001"
        })
        claimed = mod.claim_detail_refresh({"token": [created["token"]]})
        self.assertEqual((claimed["chain"], claimed["sap_code"], claimed["plu"]),
                         ("perekrestok", "XSEO", "100001"))

        result = mod.record_single_detail_refresh({
            "token": created["token"], "payload": self.fixture._payload()
        })
        self.assertTrue(result["accepted"])
        self.assertFalse(result["queue_changed"])

        conn = sqlite3.connect(mod.DB_PATH)
        after_tasks = conn.execute(
            "SELECT chain,sap_code,plu,status FROM enrichment_tasks ORDER BY chain,sap_code,plu"
        ).fetchall()
        after_events = conn.execute("SELECT COUNT(*) FROM enrichment_task_events").fetchone()[0]
        details = conn.execute(
            "SELECT chain,sap_code,plu,composition FROM product_details"
        ).fetchall()
        history_count = conn.execute("SELECT COUNT(*) FROM product_detail_history").fetchone()[0]
        imports_count = conn.execute("SELECT COUNT(*) FROM product_detail_imports").fetchone()[0]
        conn.close()

        self.assertEqual(before_tasks, after_tasks)
        self.assertEqual(before_events, after_events)
        self.assertEqual(len(details), 1)
        self.assertEqual(tuple(details[0][:3]), ("perekrestok", "XSEO", "100001"))
        self.assertEqual(details[0][3], "Сыры, масло сливочное")
        self.assertEqual(history_count, 1)
        self.assertEqual(imports_count, 1)

        detail_dir = mod.chain_path("perekrestok", "details")
        self.assertEqual(list(detail_dir.glob("*.json")), [])
        self.assertEqual(len(list((detail_dir / "_single_refresh").glob("*.json"))), 1)
        self.assertEqual(mod.import_pending_details()[:3], (0, 0, 0))

        with self.assertRaises(ValueError):
            mod.claim_detail_refresh({"token": [created["token"]]})

    def test_refresh_rejects_wrong_sap_and_wrong_product_identity(self):
        with self.assertRaises(ValueError):
            mod.create_detail_refresh_request({
                "chain": "perekrestok", "sap_code": "35XY", "plu": "100001"
            })

        created = mod.create_detail_refresh_request({
            "chain": "perekrestok", "sap_code": "XSEO", "plu": "100001"
        })
        payload = self.fixture._payload()
        payload["product"]["plu"] = "999999"
        with self.assertRaises(ValueError):
            mod.record_single_detail_refresh({"token": created["token"], "payload": payload})

        # A failed validation leaves the token reusable for the correct product.
        result = mod.record_single_detail_refresh({
            "token": created["token"], "payload": self.fixture._payload()
        })
        self.assertTrue(result["accepted"])
