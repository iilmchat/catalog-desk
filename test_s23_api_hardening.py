"""S23 regression tests for local API request validation and field provenance."""
from __future__ import annotations

import json
import threading
import unittest

from s23_api_security import parse_json_object, validate_local_request
from test_server_multi_store_v15 import MultiStoreBatchTests, mod


class LocalApiRequestValidationTests(unittest.TestCase):
    def test_accepts_loopback_and_local_browser_origin(self):
        validate_local_request(
            ("127.0.0.1", 51000), "127.0.0.1:8767", "http://127.0.0.1:8767"
        )

    def test_accepts_registered_retailer_origin_for_userscript(self):
        validate_local_request(
            ("::1", 51000), "localhost:8767", "https://www.5ka.ru"
        )
        validate_local_request(
            ("127.0.0.1", 51000), "127.0.0.1:8767", "https://www.perekrestok.ru/"
        )

    def test_rejects_non_loopback_peer_and_untrusted_host_or_origin(self):
        cases = [
            (("192.168.1.20", 51000), "127.0.0.1:8767", None),
            (("127.0.0.1", 51000), "attacker.example", None),
            (("127.0.0.1", 51000), "127.0.0.1:8767", "https://attacker.example"),
            (("127.0.0.1", 51000), "127.0.0.1:8767", "https://5ka.ru.attacker.example"),
        ]
        for args in cases:
            with self.subTest(args=args), self.assertRaises(ValueError):
                validate_local_request(*args)

    def test_rejects_host_on_another_local_port(self):
        with self.assertRaisesRegex(ValueError, "другой порт"):
            validate_local_request(
                ("127.0.0.1", 51000), "localhost:8768", None,
                expected_port=8767,
            )

    def test_rejects_malformed_host_and_origin(self):
        for host in ("", "127.0.0.1/path", "user@127.0.0.1", "127.0.0.1:99999"):
            with self.subTest(host=host), self.assertRaises(ValueError):
                validate_local_request(("127.0.0.1", 1), host, None)
        with self.assertRaises(ValueError):
            validate_local_request(
                ("127.0.0.1", 1), "127.0.0.1:8767", "http://127.0.0.1:8767/evil"
            )

    def test_json_body_requires_json_object_and_exact_bounded_length(self):
        raw = b'{"ok":true}'
        self.assertEqual(
            parse_json_object("application/json; charset=utf-8", str(len(raw)), raw),
            {"ok": True},
        )
        bad = [
            ("text/plain", raw),
            ("application/json", b"[]"),
            ("application/json", b"not json"),
            ("application/json", b"\\xff"),
        ]
        for media_type, body in bad:
            with self.subTest(media_type=media_type, body=body), self.assertRaises(ValueError):
                parse_json_object(media_type, str(len(body)), body)
        with self.assertRaises(ValueError):
            parse_json_object("application/json", str(len(raw) + 1), raw)
        with self.assertRaises(ValueError):
            parse_json_object("application/json", "5", b'{"x":1}', max_bytes=4)


class FieldProvenanceTests(unittest.TestCase):
    def test_captured_fields_have_individual_dom_provenance(self):
        prod = {
            "name": "Тестовый продукт",
            "current_price": 99.9,
            "image_url": "https://example.test/image.jpg",
            "product_url": "https://5ka.ru/product/test--123456/",
            "captured_at": "2026-11-27T08:00:00Z",
            "collection_mode": "single-visible-current-page-dom-only",
            "category_path": "Молочные продукты",
            "composition": "Молоко",
            "description": "Описание",
            "attributes": {"Бренд": "Тест", "Вес": "1 кг"},
        }
        ingredients, materials, attrs, meta = mod._prepare_product_semantics(
            prod,
            {"Белки": "3", "Жиры": "2", "Углеводы": "4", "Ккал": "50"},
            prod["attributes"],
            "Состав: Молоко",
        )
        fields = meta["provenance"]["fields"]
        self.assertEqual(meta["schema_version"], 2)
        self.assertEqual(meta["extraction_status"]["name"], "captured")
        self.assertEqual(fields["name"]["source_field"], "name")
        self.assertEqual(fields["name"]["method"], "visible_dom")
        self.assertEqual(fields["classification"]["method"], "taxonomy")
        self.assertEqual(fields["name"]["source_url"], prod["product_url"])
        self.assertEqual(fields["name"]["captured_at"], prod["captured_at"])
        self.assertNotIn("not_found_field", fields)
        self.assertEqual(meta["extraction_status"]["brand"], "captured")

    def test_not_found_and_not_applicable_are_not_fabricated_as_captured(self):
        prod = {
            "name": "Ёлочная игрушка",
            "current_price": 10,
            "product_url": "https://www.perekrestok.ru/product/test--123456/",
            "category_path": "Новогодние товары",
        }
        _, _, _, meta = mod._prepare_product_semantics(prod, {}, {}, "")
        self.assertEqual(meta["extraction_status"]["nutrition"], "not_applicable")
        self.assertEqual(meta["extraction_status"]["ingredients"], "not_applicable")
        self.assertEqual(meta["extraction_status"]["image"], "not_found")
        self.assertEqual(meta["provenance"]["fields"]["nutrition"]["status"], "not_applicable")
        self.assertEqual(meta["provenance"]["fields"]["nutrition"]["method"], "applicability_policy")
        self.assertEqual(meta["provenance"]["fields"]["image"]["status"], "not_found")
        self.assertEqual(meta["provenance"]["fields"]["image"]["scope"], "attempt_context")

    def test_recovered_description_provenance_points_to_details_text(self):
        prod = {
            "name": "Товар",
            "product_url": "https://5ka.ru/product/item--123456/",
            "category_path": "Новогодние товары",
        }
        _, _, _, meta = mod._prepare_product_semantics(
            prod, {}, {}, "Описание Очень подробное описание товара для проверки provenance."
        )
        self.assertEqual(meta["extraction_status"]["description"], "captured")
        self.assertEqual(
            meta["provenance"]["fields"]["description"]["source_field"],
            "description_or_details_text",
        )

    def test_legacy_metadata_shape_remains_readable(self):
        legacy = {
            "schema_version": 1,
            "provenance": {"source_url": "https://5ka.ru/product/x", "captured_at": None},
        }
        self.assertEqual(legacy["provenance"]["source_url"], "https://5ka.ru/product/x")
        # S23 adds a nested fields map without requiring a database schema change.
        self.assertNotIn("fields", legacy["provenance"])



class SingleRefreshTokenHardeningTests(unittest.TestCase):
    def setUp(self):
        self.fixture = MultiStoreBatchTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def _request(self):
        return mod.create_detail_refresh_request({
            "chain": "perekrestok", "sap_code": "XSEO", "plu": "100001"
        })

    def test_claim_is_read_only_but_successful_record_consumes_token(self):
        created = self._request()
        first = mod.claim_detail_refresh({"token": [created["token"]]})
        second = mod.claim_detail_refresh({"token": [created["token"]]})
        self.assertEqual(first, second)
        payload = self.fixture._payload()
        payload["product"]["category_path"] = "Молочные продукты"
        result = mod.record_single_detail_refresh({
            "token": created["token"], "payload": payload
        })
        self.assertTrue(result["accepted"])
        with self.assertRaises(ValueError):
            mod.claim_detail_refresh({"token": [created["token"]]})

    def test_expired_token_is_rejected_and_removed(self):
        original_ttl = mod._DETAIL_REFRESH_TTL_SECONDS
        try:
            mod._DETAIL_REFRESH_TTL_SECONDS = 0
            created = self._request()
        finally:
            mod._DETAIL_REFRESH_TTL_SECONDS = original_ttl
        with self.assertRaisesRegex(ValueError, "истёк"):
            mod.claim_detail_refresh({"token": [created["token"]]})
        self.assertNotIn(created["token"], mod._DETAIL_REFRESH_REQUESTS)

    def test_concurrent_record_attempt_is_rejected_while_first_is_in_flight(self):
        created = self._request()
        payload = self.fixture._payload()
        entered = threading.Event()
        release = threading.Event()
        outcome = {}
        original_import = mod.import_single_detail_snapshot

        def slow_import(_payload, _expected):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test synchronization timeout")
            return {"accepted": True, "queue_changed": False}

        def first_attempt():
            try:
                outcome["first"] = mod.record_single_detail_refresh({
                    "token": created["token"], "payload": payload
                })
            except Exception as exc:
                outcome["first_error"] = exc

        try:
            mod.import_single_detail_snapshot = slow_import
            worker = threading.Thread(target=first_attempt)
            worker.start()
            self.assertTrue(entered.wait(2), "first record did not enter importer")
            with self.assertRaisesRegex(ValueError, "обрабатывается"):
                mod.record_single_detail_refresh({
                    "token": created["token"], "payload": payload
                })
            release.set()
            worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertNotIn("first_error", outcome)
            self.assertTrue(outcome["first"]["accepted"])
            self.assertNotIn(created["token"], mod._DETAIL_REFRESH_REQUESTS)
        finally:
            release.set()
            mod.import_single_detail_snapshot = original_import


if __name__ == "__main__":
    unittest.main()
