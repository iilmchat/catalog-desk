from __future__ import annotations

import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

SERVER = Path(__file__).with_name('catalog_ui_server_v14.py')
spec = importlib.util.spec_from_file_location('catalog_ui_server_v14', SERVER)
mod = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(mod)


class NutritionParsingTests(unittest.TestCase):
    def test_value_before_label(self):
        text = 'Пищевая ценность на 100 г 3.5 Белки 1.8 Жиры 6.9 Углеводы 58.0 Ккал Состав перец'
        self.assertEqual(mod.parse_nutrition_from_details_text(text), {
            'Белки': '3.5', 'Жиры': '1.8', 'Углеводы': '6.9', 'Ккал': '58.0'
        })

    def test_multiline_value_before_label(self):
        text = 'Пищевая ценность на 100 г\n3.5\nБелки\n1.8\nЖиры\n6.9\nУглеводы\n58.0\nКкал\nСостав\nперец'
        self.assertEqual(mod.parse_nutrition_from_details_text(text), {
            'Белки': '3.5', 'Жиры': '1.8', 'Углеводы': '6.9', 'Ккал': '58.0'
        })

    def test_label_before_value(self):
        text = 'Пищевая ценность на 100 г Белки 3,5 г Жиры 1,8 г Углеводы 6,9 г Энергетическая ценность 58 ккал'
        self.assertEqual(mod.parse_nutrition_from_details_text(text), {
            'Белки': '3.5', 'Жиры': '1.8', 'Углеводы': '6.9', 'Ккал': '58'
        })


class GalleryFilterTests(unittest.TestCase):
    def test_recommendation_images_are_not_linked_to_product(self):
        product = {
            'name': 'Аджика Апсны Абхазская 130г',
            'image_url': 'https://catalog-images.x5static.net/product/main/800x800.jpeg',
            'image_alt': 'Аджика Апсны Абхазская 130г',
            'image_gallery': [
                {'url': 'https://catalog-images.x5static.net/product/main/800x800.jpeg', 'alt': 'Аджика Апсны Абхазская 130г'},
                {'url': 'https://catalog-images.x5static.net/product/back/800x800.jpeg', 'alt': 'Аджика Апсны — задняя сторона банки'},
                {'url': 'https://catalog-images.x5static.net/product/onion/320x320.jpeg', 'alt': 'Лук красный'},
                {'url': 'https://catalog-images.x5static.net/product/garlic/320x320.jpeg', 'alt': ''},
            ]
        }
        urls = mod.product_gallery_urls(product)
        self.assertEqual(urls, [
            'https://catalog-images.x5static.net/product/main/800x800.jpeg',
            'https://catalog-images.x5static.net/product/back/800x800.jpeg',
        ])


class BatchFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.old = {k: getattr(mod, k) for k in ('BASE_DIR', 'DB_PATH', 'EXPORT_DIR', 'CATEGORY_DIR', 'DETAIL_DIR', 'PHOTO_DIR')}
        mod.BASE_DIR = root
        mod.DB_PATH = root / 'test.sqlite3'
        mod.EXPORT_DIR = root / '5ka_exports'
        mod.CATEGORY_DIR = root / '5ka_category_lists'
        mod.DETAIL_DIR = root / '5ka_product_details'
        mod.PHOTO_DIR = root / 'photos5ka'
        for folder in (mod.EXPORT_DIR, mod.CATEGORY_DIR, mod.DETAIL_DIR, mod.PHOTO_DIR):
            folder.mkdir(parents=True, exist_ok=True)
        self._create_base_schema()
        mod.ensure_ui_columns()

    def tearDown(self):
        for k, v in self.old.items(): setattr(mod, k, v)
        self.tmp.cleanup()

    def _create_base_schema(self):
        conn = sqlite3.connect(mod.DB_PATH)
        conn.executescript('''
        CREATE TABLE exports(
            file_name TEXT, category TEXT, sap_code TEXT, page_number INTEGER,
            product_rows INTEGER, unique_products INTEGER, captured_at TEXT, imported_at TEXT, source_url TEXT
        );
        CREATE TABLE products(
            sap_code TEXT NOT NULL, plu TEXT NOT NULL, name TEXT, categories TEXT, product_url TEXT,
            current_price REAL, regular_price REAL, promo_price REAL, discount_pct REAL, promo_label TEXT,
            image_url TEXT, image_file TEXT, first_seen_at TEXT, last_seen_at TEXT,
            first_imported_at TEXT, last_imported_at TEXT,
            PRIMARY KEY(sap_code,plu)
        );
        CREATE TABLE product_categories(sap_code TEXT, plu TEXT, category TEXT);
        CREATE TABLE price_history(
            id INTEGER PRIMARY KEY AUTOINCREMENT, sap_code TEXT, plu TEXT, current_price REAL,
            regular_price REAL,promo_price REAL,discount_pct REAL,promo_label TEXT,
            captured_at TEXT,imported_at TEXT,source_hash TEXT,product_name TEXT,category TEXT
        );
        INSERT INTO products(sap_code,plu,name,categories,product_url,current_price,image_url,first_seen_at,last_seen_at,first_imported_at,last_imported_at)
        VALUES
          ('35XY','100001','Тестовый товар 1','Тесты','https://5ka.ru/product/test-1--100001/',100,'https://catalog-images.x5static.net/product/1-main/320x320.jpeg','2026-11-24','2026-11-24','2026-11-24','2026-11-24'),
          ('35XY','100002','Тестовый товар 2','Тесты','https://5ka.ru/product/test-2--100002/',200,'https://catalog-images.x5static.net/product/2-main/320x320.jpeg','2026-11-24','2026-11-24','2026-11-24','2026-11-24');
        INSERT INTO product_categories VALUES('35XY','100001','Тесты'),('35XY','100002','Тесты');
        ''')
        conn.commit(); conn.close()

    def _payload(self, plu, name):
        return {
            'schema_version': 1,
            'source': '5ka.ru-visible-product-details-export',
            'captured_at': '2026-11-24T12:00:00Z',
            'source_url': f'https://5ka.ru/product/test--{plu}/',
            'store': {'sap_code': '35XY'},
            'product': {
                'plu': str(plu), 'name': name,
                'product_url': f'https://5ka.ru/product/test--{plu}/',
                'composition': 'Вода, соль',
                'nutrition': {'Белки': '3.5', 'Жиры': '1.8', 'Углеводы': '6.9', 'Ккал': '58.0'},
                'attributes': {'Бренд': 'Тест'},
                'details_text': 'Пищевая ценность на 100 г 3.5 Белки 1.8 Жиры 6.9 Углеводы 58.0 Ккал'
            }
        }

    def test_batch_two_products(self):
        first = mod.batch_start({'limit': 2, 'delay_seconds': 15, 'category': 'Тесты'})
        self.assertTrue(first['started'])
        self.assertEqual(first['current']['plu'], '100001')
        r1 = mod.batch_record(self._payload('100001', 'Тестовый товар 1'))
        self.assertFalse(r1['batch_finished'])
        self.assertEqual(r1['next']['plu'], '100002')
        r2 = mod.batch_record(self._payload('100002', 'Тестовый товар 2'))
        self.assertTrue(r2['batch_finished'])
        state = mod.batch_status()
        self.assertFalse(state['active'])
        self.assertEqual(state['processed_count'], 2)
        self.assertEqual(state['processed_total'], 2)

    def test_batch_pause_resume(self):
        first = mod.batch_start({'limit': 3, 'delay_seconds': 15, 'category': 'Тесты'})
        self.assertTrue(first['started'])
        paused = mod.batch_pause('Проверка паузы')
        self.assertFalse(paused['active'])
        resumed = mod.batch_resume()
        self.assertTrue(resumed['resumed'])
        self.assertTrue(resumed['status']['active'])
        self.assertEqual(resumed['current_url'], first['first_url'])

    def test_continue_starts_fresh_packet_after_limit(self):
        first = mod.batch_start({'limit': 1, 'delay_seconds': 15, 'category': 'Тесты'})
        result = mod.batch_record(self._payload('100001', 'Тестовый товар 1'))
        self.assertTrue(result['batch_finished'])
        resumed = mod.batch_resume()
        self.assertTrue(resumed['resumed'])
        self.assertEqual(resumed['status']['processed_count'], 0)
        self.assertTrue(resumed['current_url'].endswith('100002/'))
        second = mod.batch_record(self._payload('100002', 'Тестовый товар 2'))
        self.assertTrue(second['batch_finished'])

    def test_reject_wrong_plu(self):
        mod.batch_start({'limit': 2, 'delay_seconds': 15, 'category': 'Тесты'})
        with self.assertRaises(ValueError):
            mod.batch_record(self._payload('100002', 'Не тот товар'))

    def test_migration_repairs_legacy_shifted_nutrition(self):
        conn = sqlite3.connect(mod.DB_PATH)
        conn.execute("""INSERT INTO product_details(
            sap_code,plu,product_name,product_url,image_url,description,composition,nutrition_json,
            attributes_json,details_text,first_imported_at,last_imported_at,last_captured_at,last_source_hash
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            '35XY','100001','Тестовый товар 1','https://5ka.ru/product/test-1--100001/',None,None,'Состав',
            json.dumps({'Белки':'1.8','Жиры':'6.9','Углеводы':'58.0'},ensure_ascii=False),
            json.dumps({'Белки':'1.8 г','Жиры':'6.9 г','Углеводы':'58.0 г'},ensure_ascii=False),
            'Пищевая ценность на 100 г 3.5 Белки 1.8 Жиры 6.9 Углеводы 58.0 Ккал',
            '2026-11-24','2026-11-24','2026-11-24', 'legacy'
        ))
        conn.execute("UPDATE enrichment_tasks SET status='processed',processed_at='2026-11-24' WHERE sap_code='35XY' AND plu='100001'")
        conn.commit(); conn.close()
        mod.ensure_ui_columns()
        conn = sqlite3.connect(mod.DB_PATH)
        task_status = conn.execute("SELECT status FROM enrichment_tasks WHERE sap_code='35XY' AND plu='100001'").fetchone()[0]
        row = conn.execute("SELECT nutrition_json,attributes_json FROM product_details WHERE sap_code='35XY' AND plu='100001'").fetchone()
        nutrition, attrs = json.loads(row[0]), json.loads(row[1])
        conn.close()
        self.assertEqual(nutrition, {'Белки':'3.5','Жиры':'1.8','Углеводы':'6.9','Ккал':'58.0'})
        self.assertEqual(attrs['Белки'], '3.5 г')
        self.assertEqual(attrs['Жиры'], '1.8 г')
        self.assertEqual(attrs['Углеводы'], '6.9 г')
        self.assertEqual(attrs['Ккал'], '58.0 ккал')
        self.assertEqual(task_status, 'pending')  # partial legacy cards return to the V14 queue

    def test_gallery_cleanup_removes_legacy_recommendations(self):
        conn = sqlite3.connect(mod.DB_PATH)
        main_url = 'https://catalog-images.x5static.net/product/1-main/320x320.jpeg'
        recommendation = 'https://catalog-images.x5static.net/product/999-main/320x320.jpeg'
        # The main URL is already seeded with source=catalog by the migration.
        conn.execute("INSERT INTO product_images(sap_code,plu,image_url,image_order,source) VALUES('35XY','100001',?,1,'details')", (recommendation,))
        conn.execute("DELETE FROM catalog_ui_meta WHERE meta_key='gallery_recommendations_cleaned_v14'")
        conn.commit(); conn.close()
        mod.ensure_ui_columns()
        conn = sqlite3.connect(mod.DB_PATH)
        urls = [r[0] for r in conn.execute("SELECT image_url FROM product_images WHERE sap_code='35XY' AND plu='100001'")]
        conn.close()
        self.assertIn(main_url, urls)
        self.assertNotIn(recommendation, urls)


if __name__ == '__main__':
    unittest.main(verbosity=2)
