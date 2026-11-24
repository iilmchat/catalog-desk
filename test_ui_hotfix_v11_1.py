from pathlib import Path
import unittest
import ast

ROOT = Path(__file__).resolve().parent
APP_JS = (ROOT / 'ui_v11' / 'app.js').read_text(encoding='utf-8')
STYLE = (ROOT / 'ui_v11' / 'style.css').read_text(encoding='utf-8')
SERVER = (ROOT / 'catalog_ui_server_v11.py').read_text(encoding='utf-8')
BAT = (ROOT / 'start_ui_v11.bat').read_bytes()

class HotfixRegressionTests(unittest.TestCase):
    def test_views_are_hidden_and_active_view_shown(self):
        self.assertIn('.view:not(.active){display:none!important}', STYLE)
        self.assertIn('.view.active{display:block}', STYLE)
        self.assertIn('e.hidden=!active', APP_JS)

    def test_sidebar_navigation_is_delegated(self):
        self.assertIn("document.addEventListener('click',e=>{const nav=e.target.closest('[data-view]')", APP_JS)
        self.assertIn("setView(nav.dataset.view)", APP_JS)
        self.assertIn("setView(goto.dataset.goto)", APP_JS)

    def test_product_rows_open_on_double_click(self):
        self.assertIn('data-product-row', APP_JS)
        self.assertIn("row.addEventListener('dblclick'", APP_JS)
        self.assertIn('Двойной щелчок — открыть карточку', APP_JS)
        self.assertIn("row.addEventListener('keydown'", APP_JS)

    def test_importer_child_uses_utf8(self):
        self.assertIn('env["PYTHONIOENCODING"] = "utf-8"', SERVER)
        self.assertIn('env["PYTHONUTF8"] = "1"', SERVER)
        self.assertIn('encoding="utf-8"', SERVER)

    def test_static_text_content_type_declares_utf8(self):
        self.assertIn('ctype += "; charset=utf-8"', SERVER)

    def test_windows_launcher_is_ascii_crlf_and_utf8(self):
        self.assertTrue(all(byte < 128 for byte in BAT))
        self.assertIn(b'chcp 65001 >nul', BAT)
        self.assertIn(b'PYTHONIOENCODING=utf-8', BAT)
        self.assertEqual(BAT.replace(b'\r\n', b'').count(b'\n'), 0)

if __name__ == '__main__':
    unittest.main(verbosity=2)
