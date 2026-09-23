#!/usr/bin/env python3
"""claude-switch / codex-switch: switch between subscription logins.

Sessions are keyed by account: <store>/<account-key>.json holds
{nickname, credentials, ...}. The live session is whichever one matches the
live login's identity. <store>/pending holds the nickname for a fresh login
that has not happened yet.

Claude (derived from Claude Code 2.1.280's own logout code), store ~/.claude/switch:
  - secure storage (.credentials.json / macOS Keychain): every key except
    coworkRemoteDevice, which is device identity that logout also keeps
  - ~/.claude.json: oauthAccount (the identity); the account-scoped caches
    logout clears are dropped so Claude refetches them

Codex (derived from openai/codex codex-rs/login), store $CODEX_HOME/switch:
  - $CODEX_HOME/auth.json as a whole; identity from the id_token claims.
    Only the default file credential store is supported.
"""

import argparse
import base64
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import tomllib
import unicodedata
from pathlib import Path

NAME_RE = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._-]*$")
KEYCHAIN_NOT_FOUND = 44


class Error(Exception):
    pass


def write_atomic(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_json(path: Path, data) -> None:
    write_atomic(path, json.dumps(data, indent=2) + "\n")


def load_json(path: Path, what: str):
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise Error(f"{what} ({path}) is not valid JSON: {e}") from e


def restore_file(path: Path, text) -> None:
    if text is None:
        path.unlink(missing_ok=True)
    else:
        write_atomic(path, text)


def read_text_or_none(path: Path):
    return path.read_text() if path.exists() else None


def email_nickname(email) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "-", (email or "").split("@")[0])


def process_running(name: str) -> bool:
    r = subprocess.run(["pgrep", "-x", "-u", str(os.getuid()), name], capture_output=True)
    if r.returncode not in (0, 1):
        raise Error(f"pgrep failed (exit {r.returncode})")
    return r.returncode == 0


# --- secure storage ----------------------------------------------------------


class FileStore:
    def __init__(self, path: Path):
        self.path = path

    def read(self):
        if not self.path.exists():
            return None
        return load_json(self.path, "credentials file")

    def write(self, data) -> None:
        if data is None:
            self.path.unlink(missing_ok=True)
        else:
            write_json(self.path, data)


class KeychainStore:
    def __init__(self, account: str, service: str, run=subprocess.run):
        self.account = account
        self.service = service
        self.run = run

    def read(self):
        r = self.run(
            ["security", "find-generic-password", "-a", self.account, "-s", self.service, "-w"],
            capture_output=True,
            text=True,
        )
        if r.returncode == KEYCHAIN_NOT_FOUND:
            return None
        if r.returncode != 0:
            raise Error(f"keychain read failed (security exit {r.returncode}): {r.stderr.strip()}")
        out = r.stdout.strip()
        if not out:
            return None
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            pass
        # security prints non-ASCII passwords as hex
        try:
            return json.loads(bytes.fromhex(out).decode())
        except (ValueError, json.JSONDecodeError) as e:
            raise Error("keychain item is not valid JSON") from e

    def write(self, data) -> None:
        if data is None:
            data = {}
        # -X (hex) through `security -i` keeps the token out of the process list
        payload = json.dumps(data).encode().hex()
        cmd = f'add-generic-password -U -a "{self.account}" -s "{self.service}" -X "{payload}"\n'
        r = self.run(["security", "-i"], input=cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise Error(f"keychain write failed (security exit {r.returncode}): {r.stderr.strip()}")
        # `security -i` does not reliably fail on a failed command; check the result instead
        if self.read() != data:
            raise Error("keychain write did not take effect")


def keychain_for(env) -> KeychainStore:
    account = env.get("USER") or os.getlogin()
    if not NAME_RE.match(account):
        account = "claude-code-user"
    service = "Claude Code-credentials"
    cfg_dir = env.get("CLAUDE_CONFIG_DIR")
    if cfg_dir:
        digest = hashlib.sha256(unicodedata.normalize("NFC", cfg_dir).encode()).hexdigest()
        service += "-" + digest[:8]
    return KeychainStore(account, service)


# --- tools -------------------------------------------------------------------


class ClaudeTool:
    name = "claude"
    login_hint = "run claude and /login"
    blocks_while_running = True
    missing_identity = "live login has no oauthAccount in ~/.claude.json; start claude once so it records it"

    DEVICE_KEYS = {"coworkRemoteDevice"}
    CACHE_KEYS = [
        "additionalModelOptionsCache",
        "additionalModelOptionsAnsweredAt",
        "additionalModelCostsCache",
        "modelAccessCache",
        "orgModelDefaultCache",
        "cachedArtifactRoster",
        "artifactRosterDenied",
        "lastSeenOrgDefaultUpdatedAt",
        "clientDataCache",
        "clientDataCacheSlots",
        "autoCompactWindowsCache",
        "cachedUsageUtilization",
        "githubWebConnectionStatusCache",
        "startupPrefetchedAt",
    ]

    def __init__(self, env, secure=None):
        cfg_dir = env.get("CLAUDE_CONFIG_DIR")
        home = Path(env["HOME"])
        self.cfg = Path(cfg_dir) if cfg_dir else home / ".claude"
        self.global_config = self.cfg / ".claude.json" if cfg_dir else home / ".claude.json"
        self.store = self.cfg / "switch"
        if secure is None:
            darwin = platform.system() == "Darwin"
            secure = keychain_for(env) if darwin else FileStore(self.cfg / ".credentials.json")
        self.secure = secure

    def check_supported(self) -> None:
        pass

    def read_global(self):
        if not self.global_config.exists():
            return None
        return load_json(self.global_config, "global config")

    def snapshot(self):
        creds = self.secure.read() or {}
        account_creds = {k: v for k, v in creds.items() if k not in self.DEVICE_KEYS}
        if not account_creds:
            return None
        return {
            "credentials": account_creds,
            "oauthAccount": (self.read_global() or {}).get("oauthAccount"),
        }

    def key(self, record):
        acct = (record or {}).get("oauthAccount")
        if not acct or not acct.get("accountUuid"):
            return None
        return f"{acct['accountUuid']}.{acct.get('organizationUuid') or 'none'}"

    def label(self, record) -> str:
        acct = (record or {}).get("oauthAccount") or {}
        if not acct:
            return "?"
        return f"{acct.get('emailAddress', '?')} ({acct.get('organizationName', '?')})"

    def default_nickname(self, record) -> str:
        return email_nickname(((record or {}).get("oauthAccount") or {}).get("emailAddress"))

    def apply(self, record):
        """Make `record` (None: logged out) the live login; return an undo callable."""
        old_creds = self.secure.read()
        old_global_text = read_text_or_none(self.global_config)
        old_global = self.read_global()

        device = {k: v for k, v in (old_creds or {}).items() if k in self.DEVICE_KEYS}
        new_creds = {**device, **((record or {}).get("credentials") or {})}
        new_global = {k: v for k, v in (old_global or {}).items() if k not in self.CACHE_KEYS}
        new_global.pop("oauthAccount", None)
        if record and record.get("oauthAccount") is not None:
            new_global["oauthAccount"] = record["oauthAccount"]

        def undo():
            self.secure.write(old_creds)
            restore_file(self.global_config, old_global_text)

        try:
            self.secure.write(new_creds or None)
            write_json(self.global_config, new_global)
        except BaseException:
            undo()
            raise
        return undo


OPENAI_AUTH_CLAIM = "https://api.openai.com/auth"
OPENAI_PROFILE_CLAIM = "https://api.openai.com/profile"


def jwt_claims(jwt) -> dict:
    try:
        payload = jwt.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (AttributeError, IndexError, ValueError):
        return {}


class CodexTool:
    name = "codex"
    login_hint = "run codex login"
    # A running codex re-reads auth.json before refreshing and refuses to
    # refresh when the account changed (reload_if_account_id_matches), so it
    # cannot write stale tokens over a switch.
    blocks_while_running = False
    missing_identity = "live codex login has no account identity in its id_token"

    def __init__(self, env):
        home = env.get("CODEX_HOME")
        self.home = Path(home) if home else Path(env["HOME"]) / ".codex"
        self.auth_file = self.home / "auth.json"
        self.auth = FileStore(self.auth_file)
        self.store = self.home / "switch"

    def check_supported(self) -> None:
        config = self.home / "config.toml"
        if not config.exists():
            return
        try:
            mode = tomllib.loads(config.read_text()).get("cli_auth_credentials_store", "file")
        except tomllib.TOMLDecodeError as e:
            raise Error(f"{config} is not valid TOML: {e}") from e
        if mode != "file":
            raise Error(f'only cli_auth_credentials_store = "file" is supported ({config} has "{mode}")')

    def snapshot(self):
        auth = self.auth.read()
        if not auth or not (auth.get("tokens") or auth.get("OPENAI_API_KEY")):
            return None
        return {"credentials": auth}

    @staticmethod
    def _claims(record):
        tokens = ((record or {}).get("credentials") or {}).get("tokens") or {}
        return tokens, jwt_claims(tokens.get("id_token"))

    def key(self, record):
        auth = (record or {}).get("credentials") or {}
        tokens, claims = self._claims(record)
        if tokens:
            a = claims.get(OPENAI_AUTH_CLAIM) or {}
            user = a.get("chatgpt_user_id") or a.get("user_id") or claims.get("sub")
            account = a.get("chatgpt_account_id") or tokens.get("account_id")
            return f"{user}.{account}" if user and account else None
        if auth.get("OPENAI_API_KEY"):
            return "apikey." + hashlib.sha256(auth["OPENAI_API_KEY"].encode()).hexdigest()[:16]
        return None

    def _email(self, record):
        _, claims = self._claims(record)
        return claims.get("email") or (claims.get(OPENAI_PROFILE_CLAIM) or {}).get("email")

    def label(self, record) -> str:
        auth = (record or {}).get("credentials") or {}
        if not auth.get("tokens") and auth.get("OPENAI_API_KEY"):
            return f"API key …{auth['OPENAI_API_KEY'][-4:]}"
        _, claims = self._claims(record)
        plan = (claims.get(OPENAI_AUTH_CLAIM) or {}).get("chatgpt_plan_type") or "?"
        return f"{self._email(record) or '?'} ({plan})"

    def default_nickname(self, record) -> str:
        return email_nickname(self._email(record)) or "apikey"

    def apply(self, record):
        old_text = read_text_or_none(self.auth_file)
        try:
            self.auth.write((record or {}).get("credentials"))
        except BaseException:
            restore_file(self.auth_file, old_text)
            raise
        return lambda: restore_file(self.auth_file, old_text)


# --- switcher ----------------------------------------------------------------


class Switcher:
    def __init__(self, tool, prompt=input, running=None, out=print):
        self.tool = tool
        self.store = tool.store
        self.pending_path = tool.store / "pending"
        self.prompt = prompt
        self.running = running or (lambda: process_running(tool.name))
        self.out = out

    # --- state -------------------------------------------------------------

    def sessions(self):
        """key -> session record, for every saved session."""
        return {p.stem: load_json(p, f"session {p.name}") for p in sorted(self.store.glob("*.json"))}

    def key_for(self, nickname: str):
        for key, sess in self.sessions().items():
            if sess.get("nickname") == nickname:
                return key
        return None

    def save_session(self, key: str, record) -> None:
        write_json(self.store / f"{key}.json", record)

    def pending(self):
        if not self.pending_path.exists():
            return None
        return self.pending_path.read_text().strip() or None

    # --- capture -----------------------------------------------------------

    def ask_nickname(self, question: str, default: str) -> str:
        try:
            name = self.prompt(f"{question} [{default}]: " if default else f"{question}: ").strip()
        except EOFError:
            raise Error("no nickname given") from None
        name = name or default
        self.check_new_nickname(name)
        return name

    def check_new_nickname(self, name: str) -> None:
        if not NAME_RE.match(name):
            raise Error(f"invalid nickname '{name}' (use A-Z a-z 0-9 . _ -)")
        if self.key_for(name) is not None or name == self.pending():
            raise Error(f"nickname '{name}' is already taken")

    def capture_live(self, interactive: bool = True):
        """Save the live login's current tokens under its account; return its nickname.

        Non-interactive, a login that would need a nickname prompt is left unsaved.
        """
        snapshot = self.tool.snapshot()
        if snapshot is None:
            return None
        key = self.tool.key(snapshot)
        if key is None:
            if not interactive:
                return None
            raise Error(self.tool.missing_identity)
        existing = self.sessions().get(key)
        if existing:
            nickname = existing["nickname"]
        elif (pending := self.pending()) is not None:
            nickname = pending
            if self.key_for(nickname) is not None:
                raise Error(f"nickname '{nickname}' is already taken")
        elif not interactive:
            return None
        else:
            nickname = self.ask_nickname(
                f"new login {self.tool.label(snapshot)}; nickname", self.tool.default_nickname(snapshot)
            )
        self.save_session(key, {"nickname": nickname, **snapshot})
        if not existing:
            self.pending_path.unlink(missing_ok=True)
        return nickname

    # --- commands ----------------------------------------------------------

    def switch(self, target: str, force: bool = False) -> None:
        if not NAME_RE.match(target):
            raise Error(f"invalid nickname '{target}' (use A-Z a-z 0-9 . _ -)")
        # Capturing only writes into the store, so it is safe while the tool runs.
        current = self.capture_live() or self.pending()
        if target == current:
            self.out(f"already on '{target}'")
            return
        running = self.running()
        if running and self.tool.blocks_while_running and not force:
            raise Error(
                f"{self.tool.name} is running; quit it first (it would write its old tokens back), or pass -f"
            )

        target_key = self.key_for(target)
        target_session = self.sessions()[target_key] if target_key else None
        old_pending = read_text_or_none(self.pending_path)

        undo = self.tool.apply(target_session)
        try:
            if target_session:
                self.pending_path.unlink(missing_ok=True)
            else:
                write_atomic(self.pending_path, target + "\n")
        except BaseException:
            undo()
            restore_file(self.pending_path, old_pending)
            raise

        if target_session:
            self.out(f"switched to '{target}': {self.tool.label(target_session)}")
        else:
            self.out(f"new session '{target}': {self.tool.login_hint}; it is saved on the next run")
        if running and not self.tool.blocks_while_running:
            self.out(f"note: running {self.tool.name} sessions keep the old account until restarted")

    def pick(self, force: bool = False) -> None:
        current = self.capture_live() or self.pending()
        others = sorted(s["nickname"] for s in self.sessions().values() if s["nickname"] != current)
        if not others:
            raise Error(f"no other sessions; use: {self.tool.name}-switch NICKNAME")
        if len(others) == 1:
            return self.switch(others[0], force)
        self.list()
        try:
            choice = self.prompt("switch to: ").strip()
        except EOFError:
            raise Error("no nickname given") from None
        if choice not in others:
            raise Error(f"no session '{choice}'")
        self.switch(choice, force)

    def rename(self, old: str, new: str) -> None:
        key = self.key_for(old)
        if key is None and self.pending() != old:
            raise Error(f"no session '{old}'")
        self.check_new_nickname(new)
        if key is None:
            write_atomic(self.pending_path, new + "\n")
        else:
            self.save_session(key, {**self.sessions()[key], "nickname": new})
        self.out(f"renamed '{old}' to '{new}'")

    def rename_current(self, new: str) -> None:
        current = self.capture_live(interactive=False)
        if current is not None:
            return self.rename(current, new)
        snapshot = self.tool.snapshot()
        key = self.tool.key(snapshot)
        if snapshot is not None and key is not None:
            # an unsaved login has no nickname yet: naming it saves it
            self.check_new_nickname(new)
            self.save_session(key, {"nickname": new, **snapshot})
            self.out(f"saved current login as '{new}'")
            return
        if snapshot is None and (pending := self.pending()) is not None:
            return self.rename(pending, new)
        raise Error("no current session to rename")

    def list(self) -> None:
        self.capture_live(interactive=False)
        snapshot = self.tool.snapshot()
        live_key = self.tool.key(snapshot)
        sessions = self.sessions()
        if snapshot is not None and live_key not in sessions:
            self.out(f"* {'(unsaved)':<16} {self.tool.label(snapshot)}  (next switch asks for a nickname)")
        elif snapshot is None and self.pending() is not None:
            self.out(f"* {self.pending():<16} (awaiting login)")
        for key, sess in sorted(sessions.items(), key=lambda kv: kv[1]["nickname"]):
            mark = "*" if key == live_key else " "
            self.out(f"{mark} {sess['nickname']:<16} {self.tool.label(sess)}")
        if snapshot is None and self.pending() is None and not sessions:
            self.out("not logged in, no saved sessions")


TOOLS = {"claude": ClaudeTool, "codex": CodexTool}


def main(argv=None, env=None) -> int:
    env = os.environ if env is None else env
    argv = sys.argv[1:] if argv is None else argv
    default_tool = "codex" if Path(sys.argv[0]).name.startswith("codex") else "claude"

    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--tool", choices=TOOLS, default=default_tool)
    tool_name = pre.parse_known_args(argv)[0].tool

    parser = argparse.ArgumentParser(prog=f"{tool_name}-switch", description=f"Switch between {tool_name} logins.")
    parser.add_argument("--tool", choices=TOOLS, default=tool_name, help=argparse.SUPPRESS)
    parser.add_argument(
        "name",
        nargs="?",
        help="session to switch to (new nickname: start a fresh login; '-': the other one); omit for status",
    )
    parser.add_argument(
        "--rename", nargs="+", metavar="NAME", help="[OLD] NEW: rename a session's nickname (default: the current one)"
    )
    parser.add_argument("-f", "--force", action="store_true", help=f"switch even if {tool_name} is running")
    args = parser.parse_args(argv)

    old_umask = os.umask(0o077)
    try:
        tool = TOOLS[tool_name](env)
        tool.check_supported()
        tool.store.mkdir(parents=True, exist_ok=True)
        s = Switcher(tool)
        if args.rename:
            if len(args.rename) > 2:
                parser.error("--rename takes [OLD] NEW")
            if len(args.rename) == 2:
                s.rename(*args.rename)
            else:
                s.rename_current(args.rename[0])
        elif args.name == "-":
            s.pick(args.force)
        elif args.name:
            s.switch(args.name, args.force)
        else:
            s.list()
    except Error as e:
        print(f"{tool_name}-switch: {e}", file=sys.stderr)
        return 1
    finally:
        os.umask(old_umask)
    return 0


if __name__ == "__main__":
    sys.exit(main())
