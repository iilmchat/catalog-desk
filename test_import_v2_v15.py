"""Тесты S16: контракт Export v2, импорт по chain и колонка «Сеть»."""

from __future__ import annotations

import csv
import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("importer_v10", ROOT / "import_5ka_catalog_v9.py")
mod = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(mod)


class ExportV2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.payload_path = root / "pr_export.json"
        payload = {
            "schema_version": 2,
            "chain": "perekrestok",
            "source": "visible-page-export",
            "collection_mode": "manual-current-page-dom-only",
            "captured_at": "2026-11-24T20:05:47.446Z",
            "source_url": "https://www.perekrestok.ru/catalog/moloko--268C62016?page=12",
            "category": "Молоко, сыр, яйца",
            "store": {"sap_code": "XSEO", "name": None},
            "products": [
                {
                    "plu": "24139",
                    "name": "Сыр плавленый Hochland Сэндвич 45%, 150г",
                    "product_url": "https://www.perekrestok.ru/product/syr--24139",
                    "image_url": "https://catalog-images.x5static.net/product/1-main/320x320.jpeg",
                    "image_alt": "Сыр плавленый Hochland",
                    "raw_text": "Нет оценок 159,99 ₽/шт Сыр плавленый Hochland 150 г Цена 159 99 ₽",
                },
                {
                    "plu": "70723",
                    "name": "Сливки Parmalat 23%, 200г",
                    "product_url": "https://www.perekrestok.ru/product/slivki--70723",
                    "image_url": "https://catalog-images.x5static.net/product/2-main/320x320.jpeg",
                    "image_alt": "Сливки Parmalat",
                    "raw_text": "Старая цена 179 99 ₽ Цена 139 99 ₽ Сливки Parmalat 200 г",
                },
            ],
        }
        self.payload_path.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

        self.old = {
            k: getattr(mod, k)
            for k in ("BASE_DIR", "DB_PATH", "EXPORT_DIR", "OUTPUT_XLSX", "OUTPUT_CSV", "REPORT_JSON", "PHOTO_DIR")
        }
        mod.BASE_DIR = root
        mod.DB_PATH = root / "catalog_test.sqlite3"
        mod.EXPORT_DIR = root / "exports"
        mod.OUTPUT_XLSX = root / "catalog.xlsx"
        mod.OUTPUT_CSV = root / "catalog.csv"
        mod.REPORT_JSON = root / "report.json"
        mod.PHOTO_DIR = root / "photos"
        mod.EXPORT_DIR.mkdir(exist_ok=True)
        mod.PHOTO_DIR.mkdir(exist_ok=True)

    def tearDown(self):
        for key, value in self.old.items():
            setattr(mod, key, value)
        self.tmp.cleanup()

    def test_chain_detection_v1_v2_and_url(self):
        rows, meta = mod.load_json_rows(self.payload_path)
        self.assertEqual(meta["chain"], "perekrestok")
        self.assertTrue(all(row["Сеть"] == "perekrestok" for row in rows))

        legacy = self.tmp.name + "/legacy.json"
        Path(legacy).write_text(
            json.dumps({
                "schema_version": 1,
                "source": "5ka.ru-visible-page-export",
                "source_url": "https://5ka.ru/catalog/test--251C51627/?page=1",
                "category": "Тесты",
                "store": {"sap_code": "35XY"},
                "products": [{
                    "plu": "100001", "name": "Товар", "raw_text": "Цена 100 00 ₽",
                    "product_url": "https://5ka.ru/product/test--100001/",
                }],
            }, ensure_ascii=False),
            encoding="utf-8",
        )
        legacy_rows, legacy_meta = mod.load_json_rows(Path(legacy))
        self.assertEqual(legacy_meta["chain"], "5ka")
        self.assertEqual(legacy_rows[0]["Сеть"], "5ka")

        # Старый маркер 5ka.ru, но домен Перекрёстка — домен имеет приоритет.
        mixed = self.tmp.name + "/mixed.json"
        mixed_payload = json.loads(self.payload_path.read_text(encoding="utf-8"))
        mixed_payload["schema_version"] = 1
        mixed_payload["source"] = "5ka.ru-visible-page-export"
        del mixed_payload["chain"]
        Path(mixed).write_text(json.dumps(mixed_payload, ensure_ascii=False), encoding="utf-8")
        mixed_meta = mod.load_json_rows(Path(mixed))[1]
        self.assertEqual(mixed_meta["chain"], "perekrestok")

    def test_import_rejects_wrong_sap_domain_and_mixed_chain(self):
        db = mod.CatalogDB(mod.DB_PATH)
        try:
            rows, meta = mod.load_json_rows(self.payload_path)
            wrong_sap = [dict(row, **{"SAP магазина": "35XY"}) for row in rows]
            with self.assertRaisesRegex(ValueError, "не относится к сети"):
                db.import_rows("wrong-sap-hash", "wrong-sap.json", wrong_sap, meta)

            wrong_domain = dict(meta)
            wrong_domain["source_url"] = "https://5ka.ru/catalog/test--ABC123/?page=1"
            with self.assertRaisesRegex(ValueError, "URL относится к сети"):
                db.import_rows("wrong-domain-hash", "wrong-domain.json", rows, wrong_domain)

            mixed = [dict(rows[0], **{"Сеть": "5ka"})]
            with self.assertRaisesRegex(ValueError, "смешаны сети"):
                db.import_rows("mixed-chain-hash", "mixed.json", mixed, meta)
        finally:
            db.close()

    def test_import_into_v10_and_csv_has_chain_column(self):
        db = mod.CatalogDB(mod.DB_PATH)
        try:
            rows, meta = mod.load_json_rows(self.payload_path)
            added, unique = db.import_rows("hash-v2-test", self.payload_path.name, rows, meta)
            self.assertEqual((added, unique), (2, 2))
            stored = db.conn.execute(
                "SELECT chain,sap_code,plu,current_price,regular_price FROM products ORDER BY plu"
            ).fetchall()
            self.assertEqual(
                [tuple(r) for r in stored],
                [
                    ("perekrestok", "XSEO", "24139", 159.99, None),
                    ("perekrestok", "XSEO", "70723", 139.99, 179.99),
                ],
            )
            report = mod.export_outputs(db, "XSEO", chain="perekrestok")
            self.assertEqual(report["schema_version"], 10)
            self.assertEqual(report["chain"], "perekrestok")
            csv_path = Path(self.tmp.name) / "catalog_perekrestok_v10.csv"
            with csv_path.open(encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                self.assertIn("Сеть", reader.fieldnames)
                values = list(reader)
            self.assertEqual(len(values), 2)
            self.assertTrue(all(v["Сеть"] == "perekrestok" for v in values))
        finally:
            db.close()


    def test_export_outputs_are_isolated_between_chains(self):
        db = mod.CatalogDB(mod.DB_PATH)
        try:
            pr_rows, pr_meta = mod.load_json_rows(self.payload_path)
            db.import_rows("hash-pr-chain", self.payload_path.name, pr_rows, pr_meta)

            fiveka_row = {
                "Сеть": "5ka",
                "SAP магазина": "35XY",
                "PLU": "900001",
                "название": "Тестовый товар Пятёрочки",
                "цена сейчас, ₽": 89.99,
                "обычная цена, ₽": 99.99,
                "цена по акции, ₽": 89.99,
                "скидка, %": 10.0,
                "маркер акции": "-10%",
                "категории": "Тестовая категория 5ka",
                "фото URL": "https://catalog-images.x5static.net/product/3-main/320x320.jpeg",
                "карточка товара": "https://5ka.ru/product/testovyy-tovar--900001/",
                "время получения": "2026-11-25T10:00:00Z",
                "сырой текст карточки": "Тестовый товар 89,99 ₽",
            }
            fiveka_meta = {
                "chain": "5ka",
                "category": "Тестовая категория 5ka",
                "source_url": "https://5ka.ru/catalog/test--251C51627/?page=1",
                "sap_code": "35XY",
                "captured_at": "2026-11-25T10:00:00Z",
            }
            db.import_rows("hash-5ka-chain", "5ka_export.json", [fiveka_row], fiveka_meta)

            pr_report = mod.export_outputs(db, "XSEO", chain="perekrestok")
            self.assertEqual(pr_report["products"], 2)
            self.assertEqual(pr_report["price_observations"], 2)
            self.assertEqual(pr_report["source_exports"], 1)
            self.assertEqual(pr_report["category_progress_rows"], 1)
            self.assertEqual(pr_report["store_sap_codes"], ["XSEO"])
            self.assertFalse(pr_report["sap_mismatch"])

            pr_csv = Path(self.tmp.name) / "catalog_perekrestok_v10.csv"
            with pr_csv.open(encoding="utf-8-sig", newline="") as handle:
                pr_values = list(csv.DictReader(handle))
            self.assertEqual(len(pr_values), 2)
            self.assertTrue(all(row["Сеть"] == "perekrestok" for row in pr_values))

            pr_xlsx = Path(self.tmp.name) / "catalog_perekrestok_v10.xlsx"
            wb = mod.load_workbook(pr_xlsx, read_only=True, data_only=True)
            try:
                expected_rows = {
                    "Товары": 2,
                    "История цен": 2,
                    "События карточек": 2,
                    "Экспорты": 1,
                    "Прогресс категорий": 1,
                }
                for sheet, data_rows in expected_rows.items():
                    self.assertEqual(wb[sheet].max_row - 1, data_rows, sheet)
                    if sheet in {"Товары", "История цен", "События карточек", "Экспорты", "Прогресс категорий"}:
                        headers = [cell.value for cell in wb[sheet][1]]
                        chain_col = headers.index("Сеть") + 1
                        self.assertTrue(
                            all(wb[sheet].cell(row, chain_col).value == "perekrestok"
                                for row in range(2, wb[sheet].max_row + 1)),
                            sheet,
                        )
            finally:
                wb.close()

            fiveka_report = mod.export_outputs(db, "35XY", chain="5ka")
            self.assertEqual(fiveka_report["products"], 1)
            self.assertEqual(fiveka_report["store_sap_codes"], ["35XY"])
            self.assertFalse(fiveka_report["sap_mismatch"])
            fiveka_csv = Path(self.tmp.name) / "catalog.csv"
            with fiveka_csv.open(encoding="utf-8-sig", newline="") as handle:
                fiveka_values = list(csv.DictReader(handle))
            self.assertEqual(len(fiveka_values), 1)
            self.assertEqual(fiveka_values[0]["Сеть"], "5ka")
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
