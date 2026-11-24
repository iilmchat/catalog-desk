"""Тесты S19: сборщики Перекрёстка не содержат маркеров Пятёрочки."""

from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FILES = {
    "categories": "pr_export_categories_bookmarklet.txt",
    "catalog": "pr_export_current_page_bookmarklet_v14.txt",
    "details": "pr_export_product_details_bookmarklet_v14.txt",
    "userscript": "pr_batch_enrichment_v15.user.js",
}


class PerekrestokCollectorsTests(unittest.TestCase):
    def test_generated_files_exist_in_root_and_static(self):
        for name in FILES.values():
            self.assertTrue((ROOT / name).is_file(), name)
            self.assertTrue((ROOT / "ui_v14" / name).is_file(), f"ui_v14/{name}")

    def test_markers_and_domains(self):
        texts = {k: (ROOT / v).read_text(encoding="utf-8") for k, v in FILES.items()}
        for key, text in texts.items():
            self.assertIn("perekrestok", text.lower(), key)
            self.assertIn("chain", text, key)
        self.assertIn("perekrestok.ru-visible-category-links", texts["categories"])
        self.assertIn("perekrestok.ru-visible-page-export", texts["catalog"])
        self.assertIn("perekrestok.ru-visible-product-details-export", texts["details"])
        self.assertIn("perekrestok.ru-visible-product-details-export", texts["userscript"])

        # Старые маркеры не должны попасть в новые сборщики.
        for key, text in texts.items():
            self.assertNotIn("5ka.ru-visible", text, key)
            self.assertNotIn("35XY", text, key)
            self.assertNotIn("py5ka-", text, key)

        self.assertIn("https://www.perekrestok.ru/*", texts["userscript"])
        self.assertIn("detected||'XSEO'", texts["details"])
        self.assertIn("localStorage.getItem('DeliveryPanelStore')", texts["details"])
        self.assertIn("pr-v15-done-", texts["userscript"])
        self.assertIn("x5static", texts["userscript"])

    @unittest.skipUnless(shutil.which("node"), "node не установлен")
    def test_userscript_js_syntax(self):
        result = subprocess.run(
            ["node", "--check", str(ROOT / FILES["userscript"])],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
