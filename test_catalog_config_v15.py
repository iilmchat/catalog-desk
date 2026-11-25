"""Тесты S15: реестр магазинов и миграция V9 → V10."""

from __future__ import annotations

import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


config = _load("catalog_config", "catalog_config.py")
migrations = _load("catalog_migrations", "catalog_migrations.py")


class StoreConfigTests(unittest.TestCase):
    def test_chain_from_url(self):
        self.assertEqual(
            config.chain_from_url("https://5ka.ru/product/test--100001/"), "5ka"
        )
        self.assertEqual(
            config.chain_from_url(
                "https://www.perekrestok.ru/product/test--24139"
            ),
            "perekrestok",
        )
        self.assertEqual(config.chain_from_url(None), "5ka")
        self.assertEqual(config.chain_from_url("https://shop.perekrestok.ru/product/a--1"), "perekrestok")
        self.assertEqual(config.chain_from_url("https://not5ka.ru.attacker.invalid/product/a--1"), "5ka")
        with self.assertRaises(ValueError):
            config.validate_chain_url("5ka", "https://www.perekrestok.ru/product/a--1")
        with self.assertRaises(ValueError):
            config.validate_chain_url("perekrestok", "https://attacker.invalid/product/a--1")

    def test_chain_from_source(self):
        self.assertEqual(config.chain_from_source("5ka.ru-visible-page-export"), "5ka")
        self.assertEqual(
            config.chain_from_source("perekrestok.ru-visible-page-export"),
            "perekrestok",
        )
        self.assertIsNone(config.chain_from_source("visible-page-export"))

    def test_allowed_sap_and_paths(self):
        self.assertEqual(config.allowed_sap("5ka"), ["35XY"])
        self.assertIn("XSEO", config.allowed_sap("perekrestok"))
        self.assertEqual(config.path_for("5ka", "photos").name, "photos5ka")
        self.assertEqual(config.path_for("perekrestok", "exports").name, "pr_exports")
        with self.assertRaises(ValueError):
            config.normalize_chain("unknown-store")


class MigrationV10Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "test.sqlite3"
        conn = sqlite3.connect(self.db)
        conn.executescript("""
        CREATE TABLE exports(
            source_hash TEXT PRIMARY KEY, file_name TEXT, category TEXT,
            sap_code TEXT, captured_at TEXT, imported_at TEXT,
            product_rows INTEGER, unique_products INTEGER, page_number TEXT
        );
        CREATE TABLE products(
            sap_code TEXT NOT NULL, plu TEXT NOT NULL, name TEXT, categories TEXT,
            product_url TEXT, current_price REAL, first_seen_at TEXT, last_seen_at TEXT,
            PRIMARY KEY(sap_code,plu)
        );
        CREATE TABLE product_categories(
            sap_code TEXT, plu TEXT, category TEXT,
            PRIMARY KEY(sap_code,plu,category),
            FOREIGN KEY(sap_code,plu) REFERENCES products(sap_code,plu) ON DELETE CASCADE
        );
        CREATE TABLE price_history(
            id INTEGER PRIMARY KEY AUTOINCREMENT, source_hash TEXT, sap_code TEXT,
            plu TEXT, current_price REAL, captured_at TEXT, imported_at TEXT,
            UNIQUE(source_hash,sap_code,plu)
        );
        INSERT INTO products VALUES('35XY','100001','Товар 1','Тесты','https://5ka.ru/product/a--100001/',100,'2026-11-24','2026-11-24');
        INSERT INTO products VALUES('35XY','100002','Товар 2','Тесты','https://5ka.ru/product/b--100002/',200,'2026-11-24','2026-11-24');
        INSERT INTO product_categories VALUES('35XY','100001','Тесты'),('35XY','100002','Тесты');
        INSERT INTO price_history(source_hash,sap_code,plu,current_price,captured_at,imported_at)
            VALUES('hash-1','35XY','100001',100,'2026-11-24','2026-11-24');
        """)
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_migration_preserves_rows_and_adds_chain(self):
        conn = sqlite3.connect(self.db)
        self.assertTrue(migrations.migrate_v9_to_v10(conn))

        self.assertEqual(conn.execute("SELECT COUNT(*) FROM products").fetchone()[0], 2)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM price_history").fetchone()[0], 1
        )
        self.assertEqual(
            conn.execute("SELECT DISTINCT chain FROM products").fetchall(), [("5ka",)]
        )
        self.assertEqual(
            conn.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()[0],
            "10",
        )
        self.assertIn(
            "chain",
            conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='products'"
            ).fetchone()[0],
        )
        product_fk = conn.execute("PRAGMA foreign_key_list(product_categories)").fetchall()
        self.assertEqual(len(product_fk), 3)
        self.assertEqual({row[3] for row in product_fk}, {"chain", "sap_code", "plu"})
        self.assertEqual({row[4] for row in product_fk}, {"chain", "sap_code", "plu"})
        self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        conn.close()

    def test_same_plu_in_two_chains_does_not_collide(self):
        conn = sqlite3.connect(self.db)
        try:
            migrations.migrate_v9_to_v10(conn)
            conn.execute(
                "INSERT INTO products(chain,sap_code,plu,name,first_seen_at,last_seen_at) "
                "VALUES(?,?,?,?,?,?)",
                (
                    "perekrestok",
                    "XSEO",
                    "100001",
                    "Тот же PLU в другой сети",
                    "2026-11-24",
                    "2026-11-24",
                ),
            )
            conn.commit()
            rows = conn.execute(
                "SELECT chain FROM products WHERE plu='100001'"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(len(rows), 2)
        self.assertEqual(sorted(r[0] for r in rows), ["5ka", "perekrestok"])

    def test_migration_is_idempotent(self):
        conn = sqlite3.connect(self.db)
        self.assertTrue(migrations.migrate_v9_to_v10(conn))
        self.assertFalse(migrations.migrate_v9_to_v10(conn))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM products").fetchone()[0], 2)
        conn.close()

    def test_existing_chain_schema_reconciles_stale_schema_version(self):
        conn = sqlite3.connect(self.db)
        try:
            self.assertTrue(migrations.migrate_v9_to_v10(conn))
            conn.execute(
                "UPDATE schema_meta SET value='9' WHERE key='schema_version'"
            )
            conn.commit()

            # The physical columns are already V10; only the metadata is stale.
            self.assertFalse(migrations.migrate_v9_to_v10(conn))
            self.assertFalse(conn.in_transaction)
            self.assertEqual(
                conn.execute(
                    "SELECT value FROM schema_meta WHERE key='schema_version'"
                ).fetchone()[0],
                "10",
            )
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM products").fetchone()[0], 2)
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
