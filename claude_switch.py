#!/usr/bin/env python3
"""claude-switch: switch between Claude Code subscription logins.

Saved sessions live in $CFG/switch/NAME.json; $CFG/switch/active names the
live one. No `active` file means the live login is unmanaged.

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
NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
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
        self.active = self.store / "active"


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


class Switcher:
    def __init__(self, paths: Paths, secure, prompt=input, running=claude_running, out=print):
        self.paths = paths
        self.secure = secure
        self.prompt = prompt
        self.running = running
        self.out = out

    # --- state -------------------------------------------------------------

    def active_name(self):
        if not self.paths.active.exists():
            return None
        return self.paths.active.read_text().strip() or None

    def session_path(self, name: str) -> Path:
        return self.paths.store / f"{name}.json"

    def session_names(self):
        return sorted(p.stem for p in self.paths.store.glob("*.json"))

    def load_session(self, name: str):
        path = self.session_path(name)
        return load_json(path, f"session '{name}'") if path.exists() else None

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

    # --- commands ----------------------------------------------------------

    def ask_name(self, question: str, target: str) -> str:
        try:
            name = self.prompt(question).strip()
        except EOFError:
            raise Error("no name given") from None
        if not NAME_RE.match(name):
            raise Error(f"invalid name '{name}'")
        if name == target or self.session_path(name).exists():
            raise Error(f"'{name}' already exists")
        return name

    def save_name_for(self, snapshot, current, target):
        """Name to save the live login under, or None if there is nothing to save."""
        if snapshot is None:
            return None
        if current is None:
            return self.ask_name("current login is unmanaged; save it as: ", target)
        saved = self.load_session(current)
        live_uuid = (snapshot.get("oauthAccount") or {}).get("accountUuid")
        saved_uuid = ((saved or {}).get("oauthAccount") or {}).get("accountUuid")
        if saved_uuid and live_uuid and saved_uuid != live_uuid:
            return self.ask_name(
                f"live login is {account_label(snapshot)}, not '{current}' "
                f"({account_label(saved)}); save it as: ",
                target,
            )
        return current

    def switch(self, target: str, force: bool = False) -> None:
        if not NAME_RE.match(target):
            raise Error(f"invalid name '{target}' (use A-Z a-z 0-9 . _ -)")
        current = self.active_name()
        if target == current:
            self.out(f"already on '{target}'")
            return
        if not force and self.running():
            raise Error("claude is running; quit it first (it would write its old tokens back), or pass -f")

        old_creds = self.secure.read()
        old_global = self.read_global()
        snapshot = self.live_snapshot()
        save_name = self.save_name_for(snapshot, current, target)
        target_session = self.load_session(target)

        device = {k: v for k, v in (old_creds or {}).items() if k in DEVICE_KEYS}
        new_creds = {**device, **((target_session or {}).get("credentials") or {})}
        new_global = {k: v for k, v in (old_global or {}).items() if k not in CACHE_KEYS}
        new_global.pop("oauthAccount", None)
        if target_session and target_session.get("oauthAccount") is not None:
            new_global["oauthAccount"] = target_session["oauthAccount"]

        # Saving the snapshot only adds data, so it goes first and is never rolled back.
        if save_name:
            write_atomic(self.session_path(save_name), json.dumps(snapshot, indent=2) + "\n")
        try:
            self.secure.write(new_creds or None)
            write_atomic(self.paths.global_config, json.dumps(new_global, indent=2) + "\n")
            write_atomic(self.paths.active, target + "\n")
        except BaseException:
            self.secure.write(old_creds)
            if old_global is None:
                self.paths.global_config.unlink(missing_ok=True)
            else:
                write_atomic(self.paths.global_config, json.dumps(old_global, indent=2) + "\n")
            raise

        if target_session:
            self.out(f"switched to '{target}': {account_label(target_session)}")
        else:
            self.out(f"new session '{target}': run claude and /login; it is saved on the next switch")

    def pick(self, force: bool = False) -> None:
        current = self.active_name()
        others = [n for n in self.session_names() if n != current]
        if not others:
            raise Error("no other sessions; use: claude-switch NAME")
        if len(others) == 1:
            return self.switch(others[0], force)
        self.list()
        try:
            choice = self.prompt("switch to: ").strip()
        except EOFError:
            raise Error("no name given") from None
        if choice not in others:
            raise Error(f"no session '{choice}'")
        self.switch(choice, force)

    def list(self) -> None:
        current = self.active_name()
        if current is None and self.live_snapshot() is not None:
            self.out("* (unmanaged)")
        for name in self.session_names():
            mark = "*" if name == current else " "
            self.out(f"{mark} {name:<16} {account_label(self.load_session(name))}")
        if current is not None and not self.session_path(current).exists():
            self.out(f"* {current} (pending login)")


def main(argv=None, env=None) -> int:
    env = os.environ if env is None else env
    parser = argparse.ArgumentParser(prog="claude-switch", description="Switch between Claude Code logins.")
    parser.add_argument("name", nargs="?", help="session to switch to (new name: start a fresh login)")
    parser.add_argument("-l", "--list", action="store_true", help="list sessions")
    parser.add_argument("-f", "--force", action="store_true", help="switch even if claude is running")
    args = parser.parse_args(argv)

    paths = Paths(env)
    secure = keychain_for(env) if platform.system() == "Darwin" else FileStore(paths.credentials)
    old_umask = os.umask(0o077)
    try:
        paths.store.mkdir(parents=True, exist_ok=True)
        s = Switcher(paths, secure)
        if args.list:
            s.list()
        elif args.name:
            s.switch(args.name, args.force)
        else:
            s.pick(args.force)
    except Error as e:
        print(f"claude-switch: {e}", file=sys.stderr)
        return 1
    finally:
        os.umask(old_umask)
    return 0


if __name__ == "__main__":
    sys.exit(main())
