"""TerryCalls is retired; live BTC monitoring must not request or show it."""
import unittest
from pathlib import Path
import app
from fastapi.testclient import TestClient


class NoTerryIntegrationTests(unittest.TestCase):
    def test_provider_is_not_imported_or_started(self):
        source=Path(app.__file__).read_text(encoding="utf-8")
        self.assertNotIn("import terry_calls",source)
        self.assertNotIn("terry_task",source)
        self.assertNotIn('"terry_calls"',source)
        root=Path(__file__).parent
        self.assertFalse((root/"terry_calls.py").exists())
        self.assertFalse((root/"terry_research.py").exists())

    def test_dashboard_omits_provider_panel_and_scripts(self):
        html=Path(__file__).with_name("terminal.html").read_text(encoding="utf-8")
        self.assertNotIn("terryCallsPanel",html)
        self.assertNotIn("renderTerry",html)
        self.assertNotIn("terrycalls.com",html.lower())
        self.assertIn('id="structureWatch"',html)
        self.assertIn('id="checkpointBoard"',html)

    def test_live_api_has_no_provider_payload(self):
        with TestClient(app.app) as client:
            result=client.get("/api/live")
        self.assertEqual(result.status_code,200)
        self.assertNotIn("terry_calls",result.json())


if __name__=="__main__":
    unittest.main()
