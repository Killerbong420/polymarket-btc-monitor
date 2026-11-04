"""Regression checks for the mobile-only terminal readability rules."""
from pathlib import Path
import unittest


class MobileReadabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = Path(__file__).with_name("terminal.html").read_text(encoding="utf-8")

    def test_mobile_styles_present_once(self):
        self.assertEqual(self.html.count('mobile-readability-v1'), 1)
        self.assertIn("@media (max-width: 600px)", self.html)
        self.assertIn("@media (max-width: 390px)", self.html)

    def test_existing_dashboard_sections_preserved(self):
        for marker in ('#brtiCanvas', '#brtiHero', 'id="signalTiles"'):
            self.assertIn(marker, self.html)

    def test_accessibility_preferences_preserved(self):
        self.assertIn("prefers-reduced-motion: reduce", self.html)
        self.assertIn("touch-action: manipulation", self.html)


if __name__ == "__main__":
    unittest.main()
