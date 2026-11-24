from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parent
APP_JS = (ROOT / 'ui_v13' / 'app.js').read_text(encoding='utf-8')
STYLE = (ROOT / 'ui_v13' / 'style.css').read_text(encoding='utf-8')
HTML = (ROOT / 'ui_v13' / 'index.html').read_text(encoding='utf-8')
SERVER = (ROOT / 'catalog_ui_server_v13.py').read_text(encoding='utf-8')
BAT = (ROOT / 'start_ui_v13.bat').read_bytes()
BOOKMARKLET = (ROOT / '5ka_export_product_details_bookmarklet_v13.txt').read_text(encoding='utf-8').strip()

class V13UiTests(unittest.TestCase):
    def test_version_is_consistent_and_distinct_port_used(self):
        self.assertIn('Catalog Desk V13.0', HTML)
        self.assertIn('CATALOG DESK <b>V13.0</b>', HTML)
        self.assertIn('PyaterochkaCatalogUI/13.0', SERVER)
        self.assertIn('default=8766', SERVER)
        self.assertIn('8766', BAT.decode('ascii'))

    def test_mass_enrichment_navigation_and_filters_exist(self):
        for text in ('data-view="enrichment"', 'id="view-enrichment"', 'id="mass-category-filter"',
                     'id="mass-status-filter"', 'id="mass-completeness-filter"', 'id="mass-queue-body"'):
            self.assertIn(text, HTML)
        self.assertIn("enrichment:'Массовое обогащение'", APP_JS)
        self.assertIn('loadMassEnrichment', APP_JS)
        self.assertIn('data-mass-category', APP_JS)

    def test_task_buttons_and_backend_routes_exist(self):
        self.assertIn('data-mass-open', APP_JS)
        self.assertIn('data-mass-exported', APP_JS)
        self.assertIn('data-mass-skip', APP_JS)
        self.assertIn('/api/enrichment/task', SERVER)
        self.assertIn('def update_enrichment_task', SERVER)

    def test_completion_progress_styles_exist(self):
        self.assertIn('.mass-progress', STYLE)
        self.assertIn('.mass-status.processed', STYLE)
        self.assertIn('.mass-category-track', STYLE)

    def test_bookmarklet_exports_characteristics_and_gallery(self):
        self.assertIn('image_gallery:gallery.map', BOOKMARKLET)
        self.assertIn('composition', BOOKMARKLET)
        self.assertIn('nutrition', BOOKMARKLET)

    def test_launcher_ascii_crlf_and_correct_script(self):
        self.assertTrue(all(b < 128 for b in BAT))
        self.assertEqual(BAT.replace(b'\r\n', b'').count(b'\n'), 0)
        self.assertIn(b'chcp 65001 >nul', BAT)
        self.assertIn(b'catalog_ui_server_v13.py', BAT)

if __name__ == '__main__':
    unittest.main(verbosity=2)
