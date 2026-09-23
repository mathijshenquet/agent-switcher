#!/usr/bin/env python3
"""claude-switch: switch between Claude Code subscription logins.

Sessions are keyed by account: $CFG/switch/<accountUuid>.<organizationUuid>.json
holds {nickname, credentials, oauthAccount}. The live session is whichever one
matches oauthAccount in ~/.claude.json. $CFG/switch/pending holds the nickname
for a fresh login that has not happened yet.

What a switch moves (derived from Claude Code 2.1.280's own logout code):
  - secure storage (.credentials.json / macOS Keychain): every key except
    coworkRemoteDevice, which is device identity that logout also keeps
  - ~/.claude.json: oauthAccount; the account-scoped caches logout clears are
    dropped so Claude refetches them
"""

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import unicodedata
from pathlib import Path

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


def load_json(path: Path, what: str):
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise Error(f"{what} ({path}) is not valid JSON: {e}") from e


class Paths:
    def __init__(self, env):
        cfg_dir = env.get("CLAUDE_CONFIG_DIR")
        home = Path(env["HOME"])
        self.cfg = Path(cfg_dir) if cfg_dir else home / ".claude"
        self.global_config = self.cfg / ".claude.json" if cfg_dir else home / ".claude.json"
        self.credentials = self.cfg / ".credentials.json"
        self.store = self.cfg / "switch"
        self.pending = self.store / "pending"


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
            write_atomic(self.path, json.dumps(data, indent=2) + "\n")


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


def claude_running() -> bool:
    r = subprocess.run(["pgrep", "-x", "-u", str(os.getuid()), "claude"], capture_output=True)
    if r.returncode not in (0, 1):
        raise Error(f"pgrep failed (exit {r.returncode})")
    return r.returncode == 0


def account_label(session) -> str:
    acct = (session or {}).get("oauthAccount") or {}
    if not acct:
        return "?"
    return f"{acct.get('emailAddress', '?')} ({acct.get('organizationName', '?')})"


def account_key(oauth_account):
    if not oauth_account or not oauth_account.get("accountUuid"):
        return None
    return f"{oauth_account['accountUuid']}.{oauth_account.get('organizationUuid') or 'none'}"


def default_nickname(oauth_account) -> str:
    local = ((oauth_account or {}).get("emailAddress") or "").split("@")[0]
    return re.sub(r"[^A-Za-z0-9._-]", "-", local)


class Switcher:
    def __init__(self, paths: Paths, secure, prompt=input, running=claude_running, out=print):
        self.paths = paths
        self.secure = secure
        self.prompt = prompt
        self.running = running
        self.out = out

    # --- state -------------------------------------------------------------

    def sessions(self):
        """key -> session record, for every saved session."""
        return {p.stem: load_json(p, f"session {p.name}") for p in sorted(self.paths.store.glob("*.json"))}

    def key_for(self, nickname: str):
        for key, sess in self.sessions().items():
            if sess.get("nickname") == nickname:
                return key
        return None

    def session_path(self, key: str) -> Path:
        return self.paths.store / f"{key}.json"

    def save_session(self, key: str, record) -> None:
        write_atomic(self.session_path(key), json.dumps(record, indent=2) + "\n")

    def pending(self):
        if not self.paths.pending.exists():
            return None
        return self.paths.pending.read_text().strip() or None

    def read_global(self):
        if not self.paths.global_config.exists():
            return None
        return load_json(self.paths.global_config, "global config")

    def live_snapshot(self):
        creds = self.secure.read() or {}
        account_creds = {k: v for k, v in creds.items() if k not in DEVICE_KEYS}
        if not account_creds:
            return None
        return {
            "credentials": account_creds,
            "oauthAccount": (self.read_global() or {}).get("oauthAccount"),
        }

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
        if self.key_for(name) is not None:
            raise Error(f"nickname '{name}' is already taken")

    def capture_live(self, interactive: bool = True):
        """Save the live login's current tokens under its account; return its nickname.

        Non-interactive, a login that would need a nickname prompt is left unsaved.
        """
        snapshot = self.live_snapshot()
        if snapshot is None:
            return None
        key = account_key(snapshot["oauthAccount"])
        if key is None:
            if not interactive:
                return None
            raise Error("live login has no oauthAccount in ~/.claude.json; start claude once so it records it")
        existing = self.sessions().get(key)
        if existing:
            nickname = existing["nickname"]
        elif (pending := self.pending()) is not None:
            nickname = pending
            self.check_new_nickname(nickname)
        elif not interactive:
            return None
        else:
            nickname = self.ask_nickname(
                f"new login {account_label(snapshot)}; nickname", default_nickname(snapshot["oauthAccount"])
            )
        self.save_session(key, {"nickname": nickname, **snapshot})
        if not existing:
            self.paths.pending.unlink(missing_ok=True)
        return nickname

    # --- commands ----------------------------------------------------------

    def switch(self, target: str, force: bool = False) -> None:
        if not NAME_RE.match(target):
            raise Error(f"invalid nickname '{target}' (use A-Z a-z 0-9 . _ -)")
        # Capturing only writes into the store, so it is safe while claude runs.
        current = self.capture_live()
        if current is None and self.pending() is not None:
            current = self.pending()
        if target == current:
            self.out(f"already on '{target}'")
            return
        if not force and self.running():
            raise Error("claude is running; quit it first (it would write its old tokens back), or pass -f")

        target_key = self.key_for(target)
        target_session = self.sessions()[target_key] if target_key else None
        old_creds = self.secure.read()
        old_global = self.read_global()
        old_pending = self.pending()

        device = {k: v for k, v in (old_creds or {}).items() if k in DEVICE_KEYS}
        new_creds = {**device, **((target_session or {}).get("credentials") or {})}
        new_global = {k: v for k, v in (old_global or {}).items() if k not in CACHE_KEYS}
        new_global.pop("oauthAccount", None)
        if target_session and target_session.get("oauthAccount") is not None:
            new_global["oauthAccount"] = target_session["oauthAccount"]

        try:
            self.secure.write(new_creds or None)
            write_atomic(self.paths.global_config, json.dumps(new_global, indent=2) + "\n")
            if target_session:
                self.paths.pending.unlink(missing_ok=True)
            else:
                write_atomic(self.paths.pending, target + "\n")
        except BaseException:
            self.secure.write(old_creds)
            if old_global is None:
                self.paths.global_config.unlink(missing_ok=True)
            else:
                write_atomic(self.paths.global_config, json.dumps(old_global, indent=2) + "\n")
            if old_pending is None:
                self.paths.pending.unlink(missing_ok=True)
            else:
                write_atomic(self.paths.pending, old_pending + "\n")
            raise

        if target_session:
            self.out(f"switched to '{target}': {account_label(target_session)}")
        else:
            self.out(f"new session '{target}': run claude and /login; it is saved on the next switch")

    def pick(self, force: bool = False) -> None:
        current = self.capture_live() or self.pending()
        others = sorted(s["nickname"] for s in self.sessions().values() if s["nickname"] != current)
        if not others:
            raise Error("no other sessions; use: claude-switch NICKNAME")
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
        if new == self.pending():
            raise Error(f"nickname '{new}' is already taken")
        if key is None:
            write_atomic(self.paths.pending, new + "\n")
        else:
            self.save_session(key, {**self.sessions()[key], "nickname": new})
        self.out(f"renamed '{old}' to '{new}'")

    def rename_current(self, new: str) -> None:
        current = self.capture_live(interactive=False)
        if current is not None:
            return self.rename(current, new)
        snapshot = self.live_snapshot()
        key = account_key((snapshot or {}).get("oauthAccount"))
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
        snapshot = self.live_snapshot()
        live_key = account_key((snapshot or {}).get("oauthAccount"))
        sessions = self.sessions()
        if snapshot is not None and live_key not in sessions:
            self.out(f"* {'(unsaved)':<16} {account_label(snapshot)}  (next switch asks for a nickname)")
        elif snapshot is None and self.pending() is not None:
            self.out(f"* {self.pending():<16} (awaiting login)")
        for key, sess in sorted(sessions.items(), key=lambda kv: kv[1]["nickname"]):
            mark = "*" if key == live_key else " "
            self.out(f"{mark} {sess['nickname']:<16} {account_label(sess)}")
        if snapshot is None and self.pending() is None and not sessions:
            self.out("not logged in, no saved sessions")


def main(argv=None, env=None) -> int:
    env = os.environ if env is None else env
    parser = argparse.ArgumentParser(prog="claude-switch", description="Switch between Claude Code logins.")
    parser.add_argument(
        "name",
        nargs="?",
        help="session to switch to (new nickname: start a fresh login; '-': the other one); omit for status",
    )
    parser.add_argument(
        "--rename", nargs="+", metavar="NAME", help="[OLD] NEW: rename a session's nickname (default: the current one)"
    )
    parser.add_argument("-f", "--force", action="store_true", help="switch even if claude is running")
    args = parser.parse_args(argv)

    paths = Paths(env)
    secure = keychain_for(env) if platform.system() == "Darwin" else FileStore(paths.credentials)
    old_umask = os.umask(0o077)
    try:
        paths.store.mkdir(parents=True, exist_ok=True)
        s = Switcher(paths, secure)
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
        print(f"claude-switch: {e}", file=sys.stderr)
        return 1
    finally:
        os.umask(old_umask)
    return 0


if __name__ == "__main__":
    sys.exit(main())
