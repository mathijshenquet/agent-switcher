import base64
import json
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path

import agent_switch as cs

ACCT_A = {"accountUuid": "ua", "organizationUuid": "oa", "emailAddress": "alice@x", "organizationName": "orgA"}
ACCT_B = {"accountUuid": "ub", "organizationUuid": "ob", "emailAddress": "bob@y", "organizationName": "orgB"}
# same account, different org: a separate login
ACCT_A_TEAM = {**ACCT_A, "organizationUuid": "oteam", "organizationName": "team"}
DEVICE = {"coworkRemoteDevice": {"id": "DEV"}}


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        (self.home / ".claude").mkdir()
        self.tool = cs.ClaudeTool(
            {"HOME": str(self.home)}, secure=cs.FileStore(self.home / ".claude" / ".credentials.json")
        )
        self.tool.store.mkdir()
        self.secure = self.tool.secure
        self.answers = []
        self.questions = []
        self.output = []

    def tearDown(self):
        self._tmp.cleanup()

    def switcher(self, running=False):
        def prompt(question):
            self.questions.append(question)
            if not self.answers:
                raise EOFError
            return self.answers.pop(0)

        return cs.Switcher(self.tool, prompt=prompt, running=lambda: running, out=self.output.append)

    def login(self, token, account):
        """Simulate Claude writing a fresh login."""
        self.secure.write({**DEVICE, "claudeAiOauth": {"accessToken": token, "refreshToken": "R" + token}})
        g = self.global_config() or {"projects": {"/x": {"trust": True}}, "machineID": "M"}
        g["oauthAccount"] = account
        g["modelAccessCache"] = [token]
        self.tool.global_config.write_text(json.dumps(g))

    def refresh(self, refresh_token):
        creds = self.secure.read()
        creds["claudeAiOauth"]["refreshToken"] = refresh_token
        self.secure.write(creds)

    def global_config(self):
        p = self.tool.global_config
        return json.loads(p.read_text()) if p.exists() else None

    def token(self):
        creds = self.secure.read() or {}
        return creds.get("claudeAiOauth", {}).get("accessToken")

    def saved(self, account):
        return json.loads(self.tool.store.joinpath(self.tool.key({"oauthAccount": account}) + ".json").read_text())

    def status(self):
        self.output.clear()
        self.switcher().list()
        return self.output


class TestSwitch(Base):
    def test_full_cycle(self):
        self.login("A", ACCT_A)
        self.answers = ["personal"]
        self.switcher().switch("work")
        self.assertIn("alice@x", self.questions[0])
        self.assertEqual(self.saved(ACCT_A)["nickname"], "personal")

        # fresh slot: logged out, device and unrelated config kept, caches dropped
        self.assertEqual(self.secure.read(), DEVICE)
        self.assertEqual(self.global_config(), {"projects": {"/x": {"trust": True}}, "machineID": "M"})
        self.assertEqual(self.status(), ["* work             (awaiting login)", "  personal         alice@x (orgA)"])

        # the new login takes the pending nickname without asking
        self.login("B", ACCT_B)
        self.switcher().pick()
        self.assertEqual(len(self.questions), 1)
        self.assertEqual(self.saved(ACCT_B)["nickname"], "work")
        self.assertFalse((self.tool.store / "pending").exists())
        self.assertEqual(self.token(), "A")
        self.assertEqual(self.global_config()["oauthAccount"], ACCT_A)
        self.assertNotIn("modelAccessCache", self.global_config())
        self.assertEqual(self.secure.read()["coworkRemoteDevice"], DEVICE["coworkRemoteDevice"])

        # a refresh while on personal is captured on the next switch
        self.refresh("R-rotated")
        self.switcher().switch("work")
        self.assertEqual(self.token(), "B")
        saved = self.saved(ACCT_A)
        self.assertEqual(saved["credentials"]["claudeAiOauth"]["refreshToken"], "R-rotated")
        self.assertNotIn("coworkRemoteDevice", saved["credentials"])
        self.assertEqual(self.status(), ["  personal         alice@x (orgA)", "* work             bob@y (orgB)"])

    def test_manual_login_to_known_account_is_recognized(self):
        self.login("A", ACCT_A)
        self.answers = ["personal"]
        self.switcher().switch("work")
        self.login("B", ACCT_B)
        self.switcher().switch("personal")
        # user runs /login as bob by hand while on personal
        self.login("B2", ACCT_B)
        self.switcher().switch("personal")
        self.assertEqual(len(self.questions), 1)
        self.assertEqual(self.saved(ACCT_B)["credentials"]["claudeAiOauth"]["accessToken"], "B2")
        self.assertEqual(self.saved(ACCT_A)["credentials"]["claudeAiOauth"]["accessToken"], "A")
        self.assertEqual(self.token(), "A")

    def test_same_account_other_org_is_separate(self):
        self.login("A", ACCT_A)
        self.answers = ["personal"]
        self.switcher().switch("team")
        self.login("T", ACCT_A_TEAM)
        self.switcher().switch("personal")
        self.assertEqual(self.saved(ACCT_A_TEAM)["nickname"], "team")
        self.assertEqual(self.token(), "A")

    def test_default_nickname_from_email(self):
        self.login("A", {**ACCT_A, "emailAddress": "a+b.c@x"})
        self.answers = [""]
        self.switcher().switch("work")
        self.assertTrue(self.questions[0].endswith("nickname [a-b.c]: "))
        self.assertEqual(self.saved(ACCT_A)["nickname"], "a-b.c")

    def test_already_active(self):
        self.login("A", ACCT_A)
        self.answers = ["personal"]
        self.switcher().switch("work")
        self.switcher(running=True).switch("work")
        self.assertEqual(self.output[-1], "already on 'work'")
        self.login("B", ACCT_B)
        self.switcher(running=True).switch("work")
        self.assertEqual(self.output[-1], "already on 'work'")
        self.assertEqual(self.saved(ACCT_B)["nickname"], "work")

    def test_refuses_when_running(self):
        self.login("A", ACCT_A)
        self.answers = ["personal"]
        with self.assertRaisesRegex(cs.Error, "running"):
            self.switcher(running=True).switch("work")
        self.assertEqual(self.token(), "A")
        self.assertIsNone(self.switcher().pending())

    def test_force_skips_running_check(self):
        self.login("A", ACCT_A)
        self.answers = ["personal"]
        self.switcher(running=True).switch("work", force=True)
        self.assertIsNone(self.token())

    def test_unknown_login_without_nickname_changes_nothing(self):
        self.login("A", ACCT_A)
        with self.assertRaisesRegex(cs.Error, "no nickname"):
            self.switcher().switch("work")
        self.assertEqual(self.token(), "A")
        self.assertEqual(self.switcher().sessions(), {})

    def test_login_without_oauth_account_aborts(self):
        self.secure.write({"claudeAiOauth": {"accessToken": "X"}})
        with self.assertRaisesRegex(cs.Error, "no oauthAccount"):
            self.switcher().switch("work")
        self.assertEqual(self.token(), "X")

    def test_invalid_nicknames(self):
        for bad in ["a/b", "-x", ""]:
            with self.assertRaisesRegex(cs.Error, "invalid nickname"):
                self.switcher().switch(bad) if bad else self.switcher().check_new_nickname(bad)
        self.login("A", ACCT_A)
        self.answers = ["../evil"]
        with self.assertRaisesRegex(cs.Error, "invalid nickname"):
            self.switcher().switch("work")

    def test_nickname_must_be_unique(self):
        self.login("A", ACCT_A)
        self.answers = ["personal"]
        self.switcher().switch("work")
        self.login("B", ACCT_B)
        self.switcher().switch("personal")
        self.login("T", ACCT_A_TEAM)
        self.answers = ["work"]
        with self.assertRaisesRegex(cs.Error, "already taken"):
            self.switcher().switch("personal")

    def test_rollback_when_global_write_fails(self):
        self.login("A", ACCT_A)
        self.answers = ["personal"]
        self.switcher().switch("work")
        self.login("B", ACCT_B)
        self.switcher().switch("personal")
        before_global = self.tool.global_config.read_text()

        real = cs.write_atomic

        def failing(path, text):
            if path == self.tool.global_config:
                raise OSError("disk full")
            real(path, text)

        cs.write_atomic = failing
        try:
            with self.assertRaises(OSError):
                self.switcher().switch("work")
        finally:
            cs.write_atomic = real
        self.assertEqual(self.token(), "A")
        self.assertEqual(self.tool.global_config.read_text(), before_global)

    def test_rollback_restores_pending(self):
        self.login("A", ACCT_A)
        self.answers = ["personal"]
        self.switcher().switch("work")
        self.login("B", ACCT_B)

        real = cs.write_atomic

        def failing(path, text):
            if path == (self.tool.store / "pending"):
                raise OSError("disk full")
            real(path, text)

        cs.write_atomic = failing
        try:
            with self.assertRaises(OSError):
                self.switcher().switch("third")
        finally:
            cs.write_atomic = real
        self.assertEqual(self.token(), "B")
        self.assertEqual(self.global_config()["oauthAccount"], ACCT_B)

    def test_invalid_credentials_json_aborts(self):
        self.secure.path.write_text("{nope")
        with self.assertRaisesRegex(cs.Error, "not valid JSON"):
            self.switcher().switch("work")
        self.assertEqual(self.secure.path.read_text(), "{nope")

    def test_status(self):
        self.assertEqual(self.status(), ["not logged in, no saved sessions"])
        self.login("A", ACCT_A)
        self.assertEqual(
            self.status(), ["* (unsaved)        alice@x (orgA)  (next switch asks for a nickname)"]
        )
        self.assertEqual(self.questions, [])
        self.assertEqual(self.switcher().sessions(), {})

    def test_status_applies_pending_nickname(self):
        self.login("A", ACCT_A)
        self.answers = ["personal"]
        self.switcher().switch("work")
        self.login("B", ACCT_B)
        self.assertEqual(self.status(), ["  personal         alice@x (orgA)", "* work             bob@y (orgB)"])
        self.assertEqual(self.saved(ACCT_B)["nickname"], "work")
        self.assertFalse((self.tool.store / "pending").exists())
        self.assertEqual(len(self.questions), 1)

    def test_status_refreshes_known_account(self):
        self.login("A", ACCT_A)
        self.answers = ["personal"]
        self.switcher().switch("work")
        self.login("B", ACCT_B)
        self.switcher().switch("personal")
        self.refresh("R-rotated")
        self.status()
        self.assertEqual(self.saved(ACCT_A)["credentials"]["claudeAiOauth"]["refreshToken"], "R-rotated")

    def test_pick_multiple_prompts(self):
        self.login("A", ACCT_A)
        self.answers = ["a"]
        self.switcher().switch("b")
        self.login("B", ACCT_B)
        self.switcher().switch("c")
        self.login("C", ACCT_A_TEAM)
        self.answers = ["b"]
        self.switcher().pick()
        self.assertEqual(self.token(), "B")

    def test_rename(self):
        self.login("A", ACCT_A)
        self.answers = ["personal"]
        self.switcher().switch("work")
        self.switcher().rename("personal", "me")
        self.switcher().rename("work", "job")
        self.assertEqual(self.saved(ACCT_A)["nickname"], "me")
        self.assertEqual(self.switcher().pending(), "job")
        with self.assertRaisesRegex(cs.Error, "already taken"):
            self.switcher().rename("me", "job")
        with self.assertRaisesRegex(cs.Error, "no session"):
            self.switcher().rename("nope", "x")


class TestRenameCurrent(Base):
    def test_renames_live_session(self):
        self.login("A", ACCT_A)
        self.answers = ["personal"]
        self.switcher().switch("work")
        self.login("B", ACCT_B)
        self.switcher().rename_current("job")
        self.assertEqual(self.saved(ACCT_B)["nickname"], "job")
        self.assertEqual(self.saved(ACCT_A)["nickname"], "personal")

    def test_names_unsaved_login(self):
        self.login("A", ACCT_A)
        self.switcher().rename_current("personal")
        self.assertEqual(self.saved(ACCT_A)["nickname"], "personal")
        self.assertEqual(self.output[-1], "saved current login as 'personal'")
        self.assertEqual(self.questions, [])

    def test_renames_pending_when_logged_out(self):
        self.login("A", ACCT_A)
        self.answers = ["personal"]
        self.switcher().switch("work")
        self.switcher().rename_current("job")
        self.assertEqual(self.switcher().pending(), "job")

    def test_nothing_to_rename(self):
        with self.assertRaisesRegex(cs.Error, "no current session"):
            self.switcher().rename_current("x")

    def test_taken_nickname(self):
        self.login("A", ACCT_A)
        self.answers = ["personal"]
        self.switcher().switch("work")
        self.login("B", ACCT_B)
        with self.assertRaisesRegex(cs.Error, "already taken"):
            self.switcher().rename_current("personal")


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
        self.tool.secure = self.secure

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
        self.assertIsNone(self.switcher().pending())

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


def fake_jwt(claims):
    b64 = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return f"{b64({'alg': 'none'})}.{b64(claims)}.sig"


def codex_auth(token, user, account, email, plan="pro"):
    claims = {
        "email": email,
        "sub": "sub-" + user,
        "https://api.openai.com/auth": {
            "chatgpt_user_id": user,
            "chatgpt_account_id": account,
            "chatgpt_plan_type": plan,
        },
    }
    return {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": fake_jwt(claims),
            "access_token": "AT-" + token,
            "refresh_token": "RT-" + token,
            "account_id": account,
        },
        "last_refresh": "2026-09-23T00:00:00Z",
    }


class TestCodex(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name) / ".codex"
        self.home.mkdir()
        self.tool = cs.CodexTool({"HOME": self._tmp.name})
        self.tool.store.mkdir()
        self.answers = []
        self.output = []
        self.running = False

    def tearDown(self):
        self._tmp.cleanup()

    def switcher(self):
        def prompt(_question):
            if not self.answers:
                raise EOFError
            return self.answers.pop(0)

        return cs.Switcher(self.tool, prompt=prompt, running=lambda: self.running, out=self.output.append)

    def login(self, auth):
        (self.home / "auth.json").write_text(json.dumps(auth))

    def live(self):
        p = self.home / "auth.json"
        return json.loads(p.read_text()) if p.exists() else None

    def test_cycle(self):
        a = codex_auth("A", "user-a", "acct-a", "alice@x")
        b = codex_auth("B", "user-b", "acct-b", "bob@y", plan="plus")
        self.login(a)
        self.answers = [""]
        self.switcher().switch("work")
        self.assertIsNone(self.live())
        self.login(b)
        self.switcher().switch("alice")
        self.assertEqual(self.live(), a)
        self.output.clear()
        self.switcher().list()
        self.assertEqual(self.output, ["* alice            alice@x (pro)", "  work             bob@y (plus)"])

        # refreshed tokens are captured on the next switch
        a["tokens"]["refresh_token"] = "RT-rotated"
        self.login(a)
        self.switcher().switch("work")
        self.assertEqual(self.live(), b)
        saved = json.loads((self.tool.store / "user-a.acct-a.json").read_text())
        self.assertEqual(saved["credentials"]["tokens"]["refresh_token"], "RT-rotated")

    def test_same_user_other_workspace_is_separate(self):
        self.login(codex_auth("A", "user-a", "acct-a", "alice@x"))
        self.answers = ["personal"]
        self.switcher().switch("team")
        self.login(codex_auth("T", "user-a", "acct-team", "alice@x"))
        self.switcher().switch("personal")
        self.assertEqual(len(self.switcher().sessions()), 2)

    def test_running_codex_does_not_block_but_warns(self):
        self.login(codex_auth("A", "user-a", "acct-a", "alice@x"))
        self.answers = ["alice"]
        self.running = True
        self.switcher().switch("work")
        self.assertIsNone(self.live())
        self.assertIn("keep the old account until restarted", self.output[-1])

    def test_api_key_login(self):
        self.login({"auth_mode": "apikey", "OPENAI_API_KEY": "sk-test-1234"})
        self.output.clear()
        self.switcher().list()
        self.assertIn("API key …1234", self.output[0])
        self.answers = [""]
        self.switcher().switch("work")
        self.assertEqual(self.switcher().sessions()["apikey." + cs.hashlib.sha256(b"sk-test-1234").hexdigest()[:16]]["nickname"], "apikey")

    def test_rollback_restores_auth_file(self):
        a = codex_auth("A", "user-a", "acct-a", "alice@x")
        self.login(a)
        self.answers = ["alice"]
        self.switcher().switch("work")
        self.login(codex_auth("B", "user-b", "acct-b", "bob@y"))
        before = (self.home / "auth.json").read_text()

        real = cs.write_atomic

        def failing(path, text):
            if path.name == "pending":
                raise OSError("disk full")
            real(path, text)

        cs.write_atomic = failing
        try:
            with self.assertRaises(OSError):
                self.switcher().switch("third")
        finally:
            cs.write_atomic = real
        self.assertEqual((self.home / "auth.json").read_text(), before)

    def test_keyring_mode_refused(self):
        (self.home / "config.toml").write_text('model = "x"\ncli_auth_credentials_store = "keyring"\n')
        with self.assertRaisesRegex(cs.Error, "only cli_auth_credentials_store"):
            self.tool.check_supported()
        (self.home / "config.toml").write_text('cli_auth_credentials_store = "file"\n')
        self.tool.check_supported()

    def test_codex_home_env(self):
        tool = cs.CodexTool({"HOME": "/h", "CODEX_HOME": "/c"})
        self.assertEqual(tool.auth_file, Path("/c/auth.json"))
        self.assertEqual(tool.store, Path("/c/switch"))


class TestMain(unittest.TestCase):
    def test_tool_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"HOME": tmp}
            self.assertEqual(cs.main(["--tool", "codex"], env), 0)
            self.assertTrue((Path(tmp) / ".codex" / "switch").is_dir())
            self.assertFalse((Path(tmp) / ".claude").exists())


if __name__ == "__main__":
    unittest.main()
