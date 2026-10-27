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


from unittest.mock import Mock, patch
import subprocess


class HerdrTest(unittest.TestCase):
    def test_all_running_servers_and_pid_matching(self):
        h = claude_park.Herdr()
        pane = {"agent": "claude", "pane_id": "w1:p1", "terminal_id": "term1"}
        calls = []

        def call(*args, session=None):
            calls.append((args, session))
            if args[:2] == ("session", "list"):
                return {"sessions": [{"name": "one", "running": True},
                                     {"name": "two", "running": True},
                                     {"name": "stopped", "running": False}]}
            if args[:2] == ("agent", "list"):
                return {"agents": [pane, {"agent": "codex"}]}
            return {"process_info": {"shell_pid": 10, "foreground_processes": [
                {"pid": 1 if session == "one" else 2, "argv": ["claude", "--dangerously-skip-permissions"]}]}}

        h.call = call
        with patch("claude_park.shutil.which", return_value="herdr"):
            found = h.discover([{"pid": 1, "sessionId": "a"}, {"pid": 2, "sessionId": "b"}])
        self.assertEqual({k: v["session"] for k, v in found.items()}, {"a": "one", "b": "two"})
        self.assertFalse(any(session == "stopped" for _, session in calls))

    def test_explicit_server_ignores_inherited_socket(self):
        run = Mock(return_value=subprocess.CompletedProcess([], 0, '{"result": {"agents": []}}', ''))
        h = claude_park.Herdr(run=run, env={"PATH": "/bin", "HERDR_SOCKET_PATH": "/wrong", "HERDR_PANE_ID": "wrong"})
        h.call("agent", "list", session="other")
        self.assertEqual(run.call_args.args[0], ["herdr", "--session", "other", "agent", "list"])
        self.assertEqual(run.call_args.kwargs["env"], {"PATH": "/bin"})

    def test_restart_checks_terminal_shell_and_cwd(self):
        entry = {"sessionId": "abc", "cwd": "/work", "herdr": {
            "session": "one", "pane": "w1:p1", "terminal": "term1", "shell_pid": 10,
            "args": ["--permission-mode", "plan"]}}
        for terminal, shell, cwd, valid in [("term1", 10, "/work", True),
                                             ("new", 10, "/work", False),
                                             ("term1", 11, "/work", False),
                                             ("term1", 10, "/other", False)]:
            with self.subTest(terminal=terminal, shell=shell, cwd=cwd):
                h = claude_park.Herdr()
                h.call = Mock(side_effect=[
                    {"pane": {"terminal_id": terminal, "cwd": cwd}},
                    {"process_info": {"shell_pid": shell, "foreground_process_group_id": shell}}, {}])
                if valid:
                    h.restart(entry)
                    self.assertEqual(h.call.call_args.args[-5:], ("--", "--permission-mode", "plan", "--resume", "abc"))
                else:
                    with self.assertRaises(claude_park.Error):
                        h.restart(entry)
                    self.assertEqual(h.call.call_count, 2)

    def test_restart_flags_exclude_old_resume_and_prompts(self):
        self.assertEqual(claude_park.restart_flags([
            "claude", "--resume", "old", "--model=opus", "--effort", "high",
            "--dangerously-skip-permissions", "old prompt"]),
            ["--model=opus", "--effort", "high", "--dangerously-skip-permissions"])


class LifecycleTest(unittest.TestCase):
    def test_headless_blocks_before_discovery_or_signals(self):
        h = Mock()
        lifecycle = claude_park.SwitchLifecycle(Path('/unused'), herdr=h)
        with patch('claude_park.live_sessions', return_value=[{'kind': 'headless'}]), patch('claude_park.terminate') as stop:
            with self.assertRaisesRegex(claude_park.Error, 'headless'):
                lifecycle.park()
            h.discover.assert_not_called()
            stop.assert_not_called()

    def test_save_before_stop_and_keep_failed_restarts(self):
        with tempfile.TemporaryDirectory() as d:
            h = Mock()
            h.discover.return_value = {'a': {'pane': 'w1:p1', 'session': 'one'}}
            lifecycle = claude_park.SwitchLifecycle(Path(d), out=lambda _: None, herdr=h)
            sessions = [{'pid': 123, 'sessionId': 'a', 'cwd': '/work'}]

            def stop(_):
                self.assertEqual(claude_park.load_parked(lifecycle.path)[0]['sessionId'], 'a')
                return []

            with patch('claude_park.live_sessions', return_value=sessions), patch('claude_park.helper_pids', return_value=[]), patch('claude_park.terminate', side_effect=stop):
                lifecycle.park()
            h.restart.side_effect = claude_park.Error('not ready')
            with self.assertRaisesRegex(claude_park.Error, 'account switched'):
                lifecycle.restart()
            self.assertEqual(len(claude_park.load_parked(lifecycle.path)), 1)
            h.restart.side_effect = None
            lifecycle.restart()
            self.assertEqual(claude_park.load_parked(lifecycle.path), [])
