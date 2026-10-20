import json
import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
