import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import claude_switch as cs

ACCT_A = {"accountUuid": "uuid-a", "emailAddress": "a@x", "organizationName": "orgA"}
ACCT_B = {"accountUuid": "uuid-b", "emailAddress": "b@y", "organizationName": "orgB"}
DEVICE = {"coworkRemoteDevice": {"id": "DEV"}}


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        (self.home / ".claude").mkdir()
        self.paths = cs.Paths({"HOME": str(self.home)})
        self.paths.store.mkdir()
        self.secure = cs.FileStore(self.paths.credentials)
        self.answers = []
        self.output = []

    def tearDown(self):
        self._tmp.cleanup()

    def switcher(self, running=False):
        def prompt(_question):
            if not self.answers:
                raise EOFError
            return self.answers.pop(0)

        return cs.Switcher(self.paths, self.secure, prompt=prompt, running=lambda: running, out=self.output.append)

    def login(self, token, account):
        """Simulate Claude writing a fresh login."""
        self.secure.write({**DEVICE, "claudeAiOauth": {"accessToken": token, "refreshToken": "R" + token}})
        g = self.global_config() or {"projects": {"/x": {"trust": True}}, "machineID": "M"}
        g["oauthAccount"] = account
        g["modelAccessCache"] = [token]
        self.paths.global_config.write_text(json.dumps(g))

    def global_config(self):
        p = self.paths.global_config
        return json.loads(p.read_text()) if p.exists() else None

    def token(self):
        creds = self.secure.read() or {}
        return creds.get("claudeAiOauth", {}).get("accessToken")


class TestSwitch(Base):
    def test_full_cycle(self):
        self.login("A", ACCT_A)
        self.answers = ["personal"]
        self.switcher().switch("work")

        # fresh slot: logged out, device and unrelated config kept, caches dropped
        self.assertEqual(self.secure.read(), DEVICE)
        self.assertEqual(self.global_config(), {"projects": {"/x": {"trust": True}}, "machineID": "M"})
        self.assertEqual(self.switcher().active_name(), "work")

        self.login("B", ACCT_B)
        self.switcher().pick()  # only one other session: no prompt
        self.assertEqual(self.token(), "A")
        self.assertEqual(self.global_config()["oauthAccount"], ACCT_A)
        self.assertNotIn("modelAccessCache", self.global_config())
        self.assertEqual(self.secure.read()["coworkRemoteDevice"], DEVICE["coworkRemoteDevice"])

        # a refresh while on personal is captured on the next switch
        creds = self.secure.read()
        creds["claudeAiOauth"]["refreshToken"] = "R-rotated"
        self.secure.write(creds)
        self.switcher().switch("work")
        self.assertEqual(self.token(), "B")
        saved = json.loads(self.paths.store.joinpath("personal.json").read_text())
        self.assertEqual(saved["credentials"]["claudeAiOauth"]["refreshToken"], "R-rotated")
        self.assertNotIn("coworkRemoteDevice", saved["credentials"])

    def test_already_active(self):
        self.login("A", ACCT_A)
        self.answers = ["personal"]
        self.switcher().switch("work")
        self.switcher().switch("work")
        self.assertEqual(self.output[-1], "already on 'work'")

    def test_refuses_when_running(self):
        self.login("A", ACCT_A)
        with self.assertRaisesRegex(cs.Error, "running"):
            self.switcher(running=True).switch("work")
        self.assertEqual(self.token(), "A")

    def test_force_skips_running_check(self):
        self.login("A", ACCT_A)
        self.answers = ["personal"]
        self.switcher(running=True).switch("work", force=True)
        self.assertIsNone(self.token())

    def test_unmanaged_without_name_changes_nothing(self):
        self.login("A", ACCT_A)
        with self.assertRaisesRegex(cs.Error, "no name"):
            self.switcher().switch("work")
        self.assertEqual(self.token(), "A")
        self.assertEqual(self.switcher().session_names(), [])

    def test_invalid_names(self):
        with self.assertRaisesRegex(cs.Error, "invalid name"):
            self.switcher().switch("a/b")
        self.login("A", ACCT_A)
        self.answers = ["../evil"]
        with self.assertRaisesRegex(cs.Error, "invalid name"):
            self.switcher().switch("work")

    def test_save_name_must_not_collide(self):
        self.login("A", ACCT_A)
        self.answers = ["work"]
        with self.assertRaisesRegex(cs.Error, "already exists"):
            self.switcher().switch("work")

    def test_manual_relogin_is_not_saved_under_wrong_name(self):
        self.login("A", ACCT_A)
        self.answers = ["personal"]
        self.switcher().switch("work")
        self.login("B", ACCT_B)
        self.switcher().switch("personal")
        # now on personal (A); user runs /login as B by hand
        self.login("B2", ACCT_B)
        self.answers = ["work2"]
        self.switcher().switch("work")
        saved_personal = json.loads(self.paths.store.joinpath("personal.json").read_text())
        self.assertEqual(saved_personal["oauthAccount"], ACCT_A)
        saved_work2 = json.loads(self.paths.store.joinpath("work2.json").read_text())
        self.assertEqual(saved_work2["credentials"]["claudeAiOauth"]["accessToken"], "B2")

    def test_logged_out_pending_slot_keeps_saved_session(self):
        self.login("A", ACCT_A)
        self.answers = ["personal"]
        self.switcher().switch("work")  # work pending, never logged in
        self.switcher().switch("personal")
        self.assertEqual(self.token(), "A")
        self.assertFalse(self.paths.store.joinpath("work.json").exists())

    def test_rollback_when_global_write_fails(self):
        self.login("A", ACCT_A)
        self.answers = ["personal"]
        self.switcher().switch("work")
        self.login("B", ACCT_B)
        before_global = self.paths.global_config.read_text()

        real = cs.write_atomic

        def failing(path, text):
            if path == self.paths.global_config:
                raise OSError("disk full")
            real(path, text)

        cs.write_atomic = failing
        try:
            with self.assertRaises(OSError):
                self.switcher().switch("personal")
        finally:
            cs.write_atomic = real
        self.assertEqual(self.token(), "B")
        self.assertEqual(self.paths.global_config.read_text(), before_global)
        self.assertEqual(self.switcher().active_name(), "work")

    def test_invalid_credentials_json_aborts(self):
        self.paths.credentials.write_text("{nope")
        with self.assertRaisesRegex(cs.Error, "not valid JSON"):
            self.switcher().switch("work")
        self.assertEqual(self.paths.credentials.read_text(), "{nope")

    def test_list(self):
        self.login("A", ACCT_A)
        self.switcher().list()
        self.assertEqual(self.output, ["* (unmanaged)"])
        self.answers = ["personal"]
        self.switcher().switch("work")
        self.output.clear()
        self.switcher().list()
        self.assertEqual(self.output, ["  personal         a@x (orgA)", "* work (pending login)"])

    def test_pick_multiple_prompts(self):
        self.login("A", ACCT_A)
        self.answers = ["a"]
        self.switcher().switch("b")
        self.login("B", ACCT_B)
        self.switcher().switch("c")
        self.login("C", ACCT_A)
        self.answers = ["b"]
        self.switcher().pick()
        self.assertEqual(self.token(), "B")


class FakeSecurity:
    """Stands in for macOS `security`: find-generic-password and `-i` add-generic-password."""

    def __init__(self, fail_read_code=None, ignore_writes=False):
        self.items = {}
        self.fail_read_code = fail_read_code
        self.ignore_writes = ignore_writes

    def __call__(self, cmd, input=None, **_kw):
        if cmd[:2] == ["security", "find-generic-password"]:
            if self.fail_read_code is not None:
                return subprocess.CompletedProcess(cmd, self.fail_read_code, "", "locked")
            key = (cmd[cmd.index("-a") + 1], cmd[cmd.index("-s") + 1])
            if key not in self.items:
                return subprocess.CompletedProcess(cmd, 44, "", "not found")
            return subprocess.CompletedProcess(cmd, 0, self.items[key] + "\n", "")
        if cmd == ["security", "-i"]:
            import shlex

            args = shlex.split(input)
            assert args[0] == "add-generic-password" and "-U" in args
            key = (args[args.index("-a") + 1], args[args.index("-s") + 1])
            if not self.ignore_writes:
                self.items[key] = bytes.fromhex(args[args.index("-X") + 1]).decode()
            return subprocess.CompletedProcess(cmd, 0, "", "")
        raise AssertionError(cmd)


class TestKeychain(Base):
    def use_keychain(self, fake):
        self.fake = fake
        self.secure = cs.KeychainStore("mthq", "Claude Code-credentials", run=fake)

    def test_cycle(self):
        self.use_keychain(FakeSecurity())
        self.login("A", ACCT_A)
        self.answers = ["personal"]
        self.switcher().switch("work")
        self.assertEqual(self.secure.read(), DEVICE)
        self.login("B", ACCT_B)
        self.switcher().switch("personal")
        self.assertEqual(self.token(), "A")

    def test_locked_keychain_aborts_without_writing(self):
        self.use_keychain(FakeSecurity(fail_read_code=51))
        with self.assertRaisesRegex(cs.Error, "keychain read failed"):
            self.switcher().switch("work")
        self.assertEqual(self.fake.items, {})

    def test_write_verified(self):
        self.use_keychain(FakeSecurity(ignore_writes=True))
        with self.assertRaisesRegex(cs.Error, "did not take effect"):
            self.secure.write({"claudeAiOauth": {}})

    def test_hex_output(self):
        fake = FakeSecurity()
        fake.items[("mthq", "Claude Code-credentials")] = json.dumps({"k": "é"}, ensure_ascii=False).encode().hex()
        self.use_keychain(fake)
        self.assertEqual(self.secure.read(), {"k": "é"})

    def test_service_name(self):
        self.assertEqual(cs.keychain_for({"USER": "mthq"}).service, "Claude Code-credentials")
        self.assertEqual(cs.keychain_for({"USER": "bad name"}).account, "claude-code-user")
        svc = cs.keychain_for({"USER": "mthq", "CLAUDE_CONFIG_DIR": "/tmp/cfg"}).service
        self.assertRegex(svc, r"^Claude Code-credentials-[0-9a-f]{8}$")


if __name__ == "__main__":
    unittest.main()
