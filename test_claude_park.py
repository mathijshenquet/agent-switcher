import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import claude_park


class LiveSessionsTest(unittest.TestCase):
    def test_filters_dead_and_reused_pids(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "1.json").write_text(json.dumps({"pid": 1, "sessionId": "a", "procStart": "10"}))
            (d / "2.json").write_text(json.dumps({"pid": 2, "sessionId": "b", "procStart": "20"}))
            (d / "3.json").write_text(json.dumps({"pid": 3, "sessionId": "c", "procStart": "30"}))
            (d / "4.json").write_text("not json")
            starts = {1: "10", 2: "99"}  # 2 was reused, 3 is gone
            live = claude_park.live_sessions(d, start_of=lambda pid: starts.get(pid))
            self.assertEqual([s["sessionId"] for s in live], ["a"])


class ParkedFileTest(unittest.TestCase):
    def test_migrates_out_of_switch_store(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            legacy = d / "switch" / "parked.json"
            legacy.parent.mkdir()
            legacy.write_text(json.dumps([{"sessionId": "a"}]))
            with (
                mock.patch.object(claude_park, "PARKED_FILE", d / "parked.json"),
                mock.patch.object(claude_park, "LEGACY_PARKED_FILE", legacy),
            ):
                entries = claude_park.load_parked()
                self.assertEqual(entries, [{"sessionId": "a"}])
                claude_park.save_parked(entries)
                self.assertFalse(legacy.exists())
                self.assertEqual(claude_park.load_parked(), [{"sessionId": "a"}])


if __name__ == "__main__":
    unittest.main()
