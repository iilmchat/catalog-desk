from pathlib import Path
import unittest
import ast

ROOT = Path(__file__).resolve().parent
APP_JS = (ROOT / 'ui_v12' / 'app.js').read_text(encoding='utf-8')
STYLE = (ROOT / 'ui_v12' / 'style.css').read_text(encoding='utf-8')
HTML = (ROOT / 'ui_v12' / 'index.html').read_text(encoding='utf-8')
SERVER = (ROOT / 'catalog_ui_server_v12.py').read_text(encoding='utf-8')
BAT = (ROOT / 'start_ui_v12.bat').read_bytes()
BOOKMARKLET = (ROOT / '5ka_export_product_details_bookmarklet_v12.txt').read_text(encoding='utf-8').strip()
IMPORTER = (ROOT / 'import_5ka_catalog_v9.py').read_text(encoding='utf-8')

class V12UiTests(unittest.TestCase):
    def test_gallery_and_card_controls_exist(self):
        self.assertIn('product-hero-v12', APP_JS)
        self.assertIn('gallery-thumbs', APP_JS)
        self.assertIn('bindGallery()', APP_JS)
        self.assertIn('data-gallery-list', APP_JS)

    def test_photo_backfill_is_an_independent_action(self):
        self.assertIn('id="run-photo-backfill"', HTML)
        self.assertIn('/api/photos/backfill', APP_JS)
        self.assertIn('def run_photo_backfill()', SERVER)
        self.assertIn('--photos-only', IMPORTER)

    def test_details_have_queue_and_separate_import_action(self):
        self.assertIn('id="detail-queue-body"', HTML)
        self.assertIn('id="run-details-import"', HTML)
        self.assertIn('/api/enrichment/queue', SERVER)
        self.assertIn('/api/details/import', APP_JS)

    def test_bookmarklet_exports_gallery_urls(self):
        self.assertIn('image_gallery:gallery.map', BOOKMARKLET)
        self.assertIn('catalog-images', BOOKMARKLET)
        self.assertIn('visible-product-details-export', BOOKMARKLET)

    def test_localhost_launcher_is_ascii_crlf(self):
        self.assertTrue(all(b < 128 for b in BAT))
        self.assertEqual(BAT.replace(b'\r\n', b'').count(b'\n'), 0)
        self.assertIn(b'chcp 65001 >nul', BAT)

    def test_server_only_binds_loopback(self):
        self.assertIn('args.host not in {"127.0.0.1", "localhost", "::1"}', SERVER)

if __name__ == '__main__':
    unittest.main(verbosity=2)
