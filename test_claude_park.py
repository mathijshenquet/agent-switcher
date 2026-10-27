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
                    {"process_info": {"shell_pid": shell, "foreground_process_group_id": shell, "foreground_processes": [{"pid": shell}]}}, {}])
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
    @patch("claude_park.sys.platform", "darwin")
    def test_unsupported_platform_stops_before_discovery_or_signals(self):
        h = Mock()
        lifecycle = claude_park.SwitchLifecycle(Path("/unused"), herdr=h)
        with patch("claude_park.live_sessions") as sessions, patch("claude_park.terminate") as stop:
            with self.assertRaisesRegex(claude_park.Error, "requires Linux"):
                lifecycle.park()
            sessions.assert_not_called()
            h.discover.assert_not_called()
            stop.assert_not_called()

    @patch("claude_park.sys.platform", "linux")
    def test_headless_blocks_before_discovery_or_signals(self):
        h = Mock()
        lifecycle = claude_park.SwitchLifecycle(Path('/unused'), herdr=h)
        with patch('claude_park.live_sessions', return_value=[{'kind': 'headless'}]), patch('claude_park.terminate') as stop:
            with self.assertRaisesRegex(claude_park.Error, 'headless'):
                lifecycle.park()
            h.discover.assert_not_called()
            stop.assert_not_called()

    @patch("claude_park.sys.platform", "linux")
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


class ParallelRestoreTest(unittest.TestCase):
    def test_all_starts_overlap_and_only_coordinator_writes_state(self):
        import threading
        with tempfile.TemporaryDirectory() as d:
            barrier = threading.Barrier(6)
            coordinator = threading.get_ident()
            h = Mock()
            lifecycle = claude_park.SwitchLifecycle(Path(d), out=lambda _: None, herdr=h)
            lifecycle.entries = [{"sessionId": str(i), "cwd": "/work", "herdr": {
                "session": "one", "pane": str(i)}} for i in range(6)]
            manual = {"sessionId": "manual", "cwd": "/elsewhere"}
            claude_park.save_parked([*lifecycle.entries, manual], lifecycle.path)
            lifecycle.entries.append(manual)

            def restart(entry):
                barrier.wait(timeout=3)
                if entry["sessionId"] == "2":
                    raise claude_park.Error("startup failed")

            save = claude_park.save_parked

            def coordinated_save(*args):
                self.assertEqual(threading.get_ident(), coordinator)
                return save(*args)

            h.restart.side_effect = restart
            with patch("claude_park.save_parked", side_effect=coordinated_save):
                with self.assertRaisesRegex(claude_park.Error, "startup failed"):
                    lifecycle.restart()
            self.assertEqual({e["sessionId"] for e in claude_park.load_parked(lifecycle.path)}, {"2", "manual"})

    def entry(self):
        return {"sessionId": "abc", "cwd": "/work", "herdr": {
            "session": "one", "pane": "w1:p1", "terminal": "term1", "shell_pid": 10}}

    def responses(self):
        return [{"pane": {"terminal_id": "term1", "cwd": "/work"}},
                {"process_info": {"shell_pid": 10, "foreground_process_group_id": 10,
                                  "foreground_processes": [{"pid": 10}]}}]

    def test_waits_for_shell_children_to_finish(self):
        busy = self.responses()
        busy[1]["process_info"]["foreground_processes"].append({"pid": 11})
        h = claude_park.Herdr()
        h.call = Mock(side_effect=[*busy, *self.responses(), {}])
        with patch("claude_park.time.sleep") as sleep:
            h.restart(self.entry())
        sleep.assert_called_once()
        self.assertEqual([c.args[:2] for c in h.call.call_args_list].count(("agent", "start")), 1)

    def test_only_busy_errors_are_retried(self):
        for code in ("agent_pane_busy", "agent_not_ready"):
            with self.subTest(code=code):
                h = claude_park.Herdr()
                h.call = Mock(side_effect=[*self.responses(), claude_park.HerdrError("failed", code),
                                          *self.responses(), {}])
                with patch("claude_park.time.sleep"):
                    if code == "agent_pane_busy":
                        h.restart(self.entry())
                        self.assertEqual(h.call.call_count, 6)
                    else:
                        with self.assertRaises(claude_park.HerdrError):
                            h.restart(self.entry())
                        self.assertEqual(h.call.call_count, 3)


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
