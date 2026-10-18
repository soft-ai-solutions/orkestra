"""At max-width 600px the connection chip shows Retry only while data-state is retry (FR-008)."""
from __future__ import annotations

import json
import re
import unittest

from test_dashboard_ux import PageCase

NARROW_HIDE = (
    '#conn[data-state="live"] #conn-retry,#conn[data-state="offline"] #conn-retry'
)


class TestNarrowRetry(PageCase):
    def css(self, code="en"):
        return re.search(r"<style>(.*?)</style>", self.page(code), re.S).group(1)

    def narrow_block(self, css):
        return re.search(r"@media \(max-width:600px\)\{(.*?)\n@media", css, re.S).group(1)

    def test_at_600px_retry_shows_conn_retry_and_live_and_offline_hide_it(self):
        """FR-008: drop the blanket #conn-retry hide. Keep the wide live/offline rule and the banner button."""
        css = self.css()
        block = self.narrow_block(css)
        hidden = [
            sel.strip()
            for sel, body in re.findall(r"([^{]+)\{([^}]*)\}", block)
            if "conn-retry" in sel and "display:none" in body.replace(" ", "")
        ]
        self.assertEqual(hidden, [NARROW_HIDE])
        self.assertNotIn("#offline-retry", block)
        self.assertIn('.conn:not([data-state="retry"]) .btn{display:none}', css)
        self.assertNotIn('.conn:not([data-state="retry"]) .btn{display:none}', block)

    def test_conn_retry_still_calls_retry_now_with_the_same_labels(self):
        for code, label in (("en", "Retry now"), ("tr", "Şimdi dene")):
            page = self.page(code)
            labels = json.loads(re.search(
                r'<script type="application/json" id="labels">(.*?)</script>', page).group(1))
            self.assertEqual(labels["ui_retry_now"], label, code)
            self.assertIn('id="conn-retry"', page)
            self.assertIn('id="offline-retry"', page)
            self.assertIn(
                'setText($("offline-retry"),L.ui_retry_now);setText($("conn-retry"),L.ui_retry_now);',
                page)
            self.assertIn(
                '$("offline-retry").addEventListener("click",retryNow);'
                '$("conn-retry").addEventListener("click",retryNow);',
                page)


if __name__ == "__main__":
    unittest.main()
