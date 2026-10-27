#!/usr/bin/env python3
"""claude-park / claude-resume: stop every running Claude Code session at once
and get each one back later, from its own directory, with `claude --resume`.

Claude Code records every live process in ~/.claude/sessions/<pid>.json
(pid, sessionId, cwd, kind). `claude-park` reads those, remembers the
interactive ones in ~/.claude/switch/parked.json, then terminates the
sessions together with the background daemon and its pty hosts (the pieces
that keep a session alive after "push to background"). `claude-resume` picks
the parked session for the current directory (or by name/index) and execs
`claude --resume`.
"""

import argparse
import json
import os
import signal
import shutil
import subprocess
import sys
import time
from pathlib import Path

CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
SESSIONS_DIR = CLAUDE_DIR / "sessions"
PARKED_FILE = CLAUDE_DIR / "switch" / "parked.json"
# The daemon and pty hosts are the processes that outlive a terminal; they
# do not carry a sessions/<pid>.json of their own.
HELPER_PATTERN = r"claude (daemon run|bg-pty-host|bg-spare)"


class Error(Exception):
    pass


def proc_start(pid: int):
    """Kernel start time of pid (field 22 of /proc/pid/stat), or None."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    # comm may contain spaces; it ends at the last ')'
    return stat[stat.rindex(")") + 2 :].split()[19]


def live_sessions(sessions_dir: Path = SESSIONS_DIR, start_of=proc_start):
    """Sessions whose recorded pid is still the same process."""
    out = []
    for path in sorted(sessions_dir.glob("*.json")):
        try:
            info = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        pid = info.get("pid")
        if not isinstance(pid, int) or not info.get("sessionId"):
            continue
        start = start_of(pid)
        if start is None or (info.get("procStart") and str(info["procStart"]) != str(start)):
            continue
        out.append(info)
    return out


def load_parked(path=PARKED_FILE):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return []


def save_parked(entries, path=PARKED_FILE):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(entries, indent=2) + "\n")
    os.replace(tmp, path)


def helper_pids():
    r = subprocess.run(["pgrep", "-u", str(os.getuid()), "-f", HELPER_PATTERN], capture_output=True, text=True)
    if r.returncode not in (0, 1):
        raise Error(f"pgrep failed (exit {r.returncode})")
    return [int(p) for p in r.stdout.split()]


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def terminate(pids, grace: float = 5.0):
    """SIGTERM, wait up to `grace` seconds, SIGKILL the rest. Returns survivors."""
    pids = [p for p in pids if p != os.getpid() and alive(p)]
    for p in pids:
        try:
            os.kill(p, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline and any(alive(p) for p in pids):
        time.sleep(0.2)
    for p in pids:
        if alive(p):
            try:
                os.kill(p, signal.SIGKILL)
            except ProcessLookupError:
                pass
    time.sleep(0.3)
    return [p for p in pids if alive(p)]


def park(args) -> int:
    sessions = live_sessions()
    interactive = [s for s in sessions if s.get("kind", "interactive") == "interactive"]
    others = [s for s in sessions if s not in interactive]
    if not interactive and not helper_pids():
        print("claude-park: nothing running")
        return 0

    parked = [e for e in load_parked() if e["sessionId"] not in {s["sessionId"] for s in interactive}]
    for s in interactive:
        parked.append(
            {
                "sessionId": s["sessionId"],
                "cwd": s.get("cwd", ""),
                "name": s.get("name", ""),
                "parkedAt": int(time.time()),
            }
        )
        print(f"parking {s.get('name') or s['sessionId'][:8]:<12} {s.get('cwd', '')}")
    if args.dry_run:
        return 0
    save_parked(parked)

    targets = [s["pid"] for s in interactive]
    # Two rounds: pty hosts survive their daemon's exit and respawn nothing,
    # but only show up in pgrep after the sessions are gone from the pty.
    survivors = terminate(targets + helper_pids())
    survivors = terminate(survivors + helper_pids())
    if survivors:
        print(f"claude-park: could not stop pids {survivors}", file=sys.stderr)
        return 1
    for s in others:
        print(f"left running: {s.get('kind')} session {s['sessionId'][:8]} (pid {s['pid']}) in {s.get('cwd', '')}")
    print(f"claude-park: stopped {len(interactive)} session(s); `claude-resume` in a directory brings one back")
    return 0


def resume(args) -> int:
    parked = load_parked()
    if args.list or (not parked and not args.which):
        if not parked:
            print("claude-resume: no parked sessions")
            return 0
        for i, e in enumerate(parked, 1):
            print(f"{i:>2}  {e.get('name') or e['sessionId'][:8]:<12} {e['cwd']}")
        return 0

    cwd = os.getcwd()
    if args.which:
        matches = [e for e in parked if e.get("name") == args.which or e["sessionId"].startswith(args.which)]
        if not matches and args.which.isdigit() and 1 <= int(args.which) <= len(parked):
            matches = [parked[int(args.which) - 1]]
    else:
        matches = [e for e in parked if e["cwd"] == cwd]
    if not matches:
        raise Error(f"no parked session {'named ' + args.which if args.which else 'for ' + cwd}; see --list")
    if len(matches) > 1 and not args.which:
        names = ", ".join(e.get("name") or e["sessionId"][:8] for e in matches)
        raise Error(f"{len(matches)} parked sessions here ({names}); pick one by name")
    entry = matches[-1]

    save_parked([e for e in parked if e["sessionId"] != entry["sessionId"]])
    if entry["cwd"] and Path(entry["cwd"]).is_dir():
        os.chdir(entry["cwd"])
    cmd = ["claude", "--resume", entry["sessionId"], *args.claude_args]
    print(f"claude-resume: {' '.join(cmd)}  (in {os.getcwd()})", file=sys.stderr)
    os.execvp(cmd[0], cmd)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # The nix wrappers pass the mode as the first argument.
    mode = argv.pop(0) if argv and argv[0] in ("park", "resume") else "park"
    tool = f"claude-{mode}"
    if mode == "resume":
        p = argparse.ArgumentParser(prog="claude-resume", description="resume a session parked by claude-park")
        p.add_argument("which", nargs="?", help="session name, id prefix or list index (default: the one parked in this directory)")
        p.add_argument("-l", "--list", action="store_true", help="list parked sessions")
        p.add_argument("claude_args", nargs=argparse.REMAINDER, help="extra arguments for claude")
        run = resume
    else:
        p = argparse.ArgumentParser(prog="claude-park", description="stop all Claude Code sessions, remembering them for claude-resume")
        p.add_argument("-n", "--dry-run", action="store_true", help="record sessions but do not stop anything")
        run = park
    try:
        return run(p.parse_args(argv))
    except Error as e:
        print(f"{tool}: {e}", file=sys.stderr)
        return 1



class Herdr:
    def __init__(self, run=subprocess.run, env=None):
        self.run = run
        self.env = dict(os.environ if env is None else env)
        for key in list(self.env):
            if key.startswith("HERDR_") and key != "HERDR_CONFIG_PATH":
                del self.env[key]

    def call(self, *args, session=None):
        cmd = ["herdr"]
        if session is not None:
            cmd += ["--session", session]
        try:
            result = self.run(cmd + list(args), env=self.env, capture_output=True, text=True, timeout=45)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise Error(f"Herdr command failed: {e}") from e
        if result.returncode:
            raise Error(f"Herdr {' '.join(args)}: {result.stderr.strip()}")
        try:
            payload = json.loads(result.stdout)
            if "error" in payload:
                raise Error(f"Herdr: {payload['error']}")
            return payload.get("result", payload)
        except (ValueError, AttributeError) as e:
            raise Error("Herdr returned invalid JSON") from e

    def discover(self, sessions):
        found = {}
        if not shutil.which("herdr"):
            return found
        by_pid = {s["pid"]: s for s in sessions}
        for server in self.call("session", "list", "--json")["sessions"]:
            if not server["running"]:
                continue
            name = server["name"]
            for pane in self.call("agent", "list", session=name)["agents"]:
                if pane.get("agent") != "claude":
                    continue
                info = self.call("pane", "process-info", "--pane", pane["pane_id"], session=name)["process_info"]
                for proc in info.get("foreground_processes", []):
                    source = by_pid.get(proc["pid"])
                    if source is None:
                        continue
                    identity = pane.get("agent_session") or {}
                    if identity.get("kind") == "id" and identity.get("value") != source["sessionId"]:
                        raise Error(f"Herdr session identity changed in {name}/{pane['pane_id']}; retry")
                    if not info.get("shell_pid"):
                        raise Error(f"Herdr pane {name}/{pane['pane_id']} has no shell to restart in")
                    found[source["sessionId"]] = {
                        "session": name, "pane": pane["pane_id"], "terminal": pane["terminal_id"],
                        "shell_pid": info["shell_pid"], "name": pane.get("name"),
                        "args": restart_flags(proc.get("argv") or []),
                    }
        return found

    def restart(self, entry):
        target = entry["herdr"]
        session, pane = target["session"], target["pane"]
        deadline = time.monotonic() + 10
        while True:
            current = self.call("pane", "get", pane, session=session)["pane"]
            info = self.call("pane", "process-info", "--pane", pane, session=session)["process_info"]
            if current["terminal_id"] != target["terminal"] or info.get("shell_pid") != target["shell_pid"]:
                raise Error(f"Herdr pane {session}/{pane} was replaced; leaving session parked")
            if info.get("foreground_process_group_id") == info["shell_pid"]:
                break
            if time.monotonic() >= deadline:
                raise Error(f"Herdr pane {session}/{pane} did not return to its shell; leaving session parked")
            time.sleep(0.2)
        if current.get("foreground_cwd", current.get("cwd")) != entry["cwd"]:
            raise Error(f"Herdr pane {session}/{pane} changed directory; leaving session parked")
        name = target.get("name") or "park-" + entry["sessionId"].replace("-", "")[:20]
        self.call("agent", "start", name, "--kind", "claude", "--pane", pane,
                  "--", *target.get("args", []), "--resume", entry["sessionId"], session=session)


def restart_flags(argv):
    flags = []
    values = {"--model", "--permission-mode", "--effort"}
    switches = {"--dangerously-skip-permissions", "--allow-dangerously-skip-permissions"}
    args = iter(argv[1:])
    for arg in args:
        if arg in switches:
            flags.append(arg)
        elif arg in values:
            value = next(args, None)
            if value is not None:
                flags.extend([arg, value])
        elif any(arg.startswith(option + "=") for option in values):
            flags.append(arg)
    return flags


class SwitchLifecycle:
    def __init__(self, cfg, out=print, herdr=None):
        self.cfg = cfg
        self.path = cfg / "switch" / "parked.json"
        self.out = out
        self.herdr = Herdr() if herdr is None else herdr
        self.entries = []

    def park(self):
        if sys.platform != "linux":
            raise Error("automatic parking currently requires Linux; quit Claude first")
        sessions = live_sessions(self.cfg / "sessions")
        others = [s for s in sessions if s.get("kind", "interactive") != "interactive"]
        if others:
            raise Error("headless Claude sessions are running; stop them before switching")
        targets = self.herdr.discover(sessions)
        self.entries = [
            {"sessionId": s["sessionId"], "cwd": s.get("cwd", ""), "name": s.get("name", ""),
             "parkedAt": int(time.time()), **({"herdr": targets[s["sessionId"]]} if s["sessionId"] in targets else {})}
            for s in sessions
        ]
        ids = {e["sessionId"] for e in self.entries}
        save_parked([e for e in load_parked(self.path) if e["sessionId"] not in ids] + self.entries, self.path)
        for entry in self.entries:
            self.out(f"parking {entry['name'] or entry['sessionId'][:8]}  {entry['cwd']}")
        survivors = terminate([s["pid"] for s in sessions] + helper_pids())
        survivors = terminate(survivors + helper_pids())
        if survivors:
            raise Error(f"could not stop Claude pids {survivors}; credentials unchanged")

    def restart(self):
        errors = []
        for entry in self.entries:
            if "herdr" not in entry:
                self.out(f"parked {entry['sessionId'][:8]}: use claude-resume in {entry['cwd']}")
                continue
            try:
                self.herdr.restart(entry)
            except Error as e:
                errors.append(str(e))
                continue
            save_parked([e for e in load_parked(self.path) if e["sessionId"] != entry["sessionId"]], self.path)
            self.out(f"resumed {entry['sessionId'][:8]} in {entry['herdr']['session']}/{entry['herdr']['pane']}")
        if errors:
            raise Error("account switched, but some panes could not resume; use claude-resume --list: " + "; ".join(errors))


if __name__ == "__main__":
    sys.exit(main())
