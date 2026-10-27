# agent-switcher

Switch between Claude Code and Codex subscription logins: `claude-switch` and `codex-switch`.

```sh
claude-switch                      # status
claude-switch NICK                 # switch to NICK
claude-switch -                    # switch to the other session, or pick one
claude-switch --rename [OLD] NEW   # rename a session (default: the current one)
```

`codex-switch` takes the same arguments.

## How it works

Sessions are keyed by account (and org/workspace), so a nickname is just a label. Every run saves the live login's current tokens under its account, so refreshed tokens are never lost. A login the tool hasn't seen yet asks for a nickname once (default: your email's local part).

To add an account, switch to a new nickname. That logs you out; log in (`claude` → `/login`, or `codex login`) and the next run saves the new login under that nickname.

- **Claude**: swaps the OAuth credentials (`~/.claude/.credentials.json`, or the Keychain on macOS) and `oauthAccount` in `~/.claude.json`, and drops the account-scoped caches that `/logout` also clears. Saved sessions live in `~/.claude/switch/`. On Linux and macOS, switching automatically parks interactive Claude sessions and resumes those in Herdr after the account changes. Headless runs block the switch. `-f` bypasses parking and the process check; running sessions can then write old tokens back.
- **Codex**: swaps `$CODEX_HOME/auth.json` (default `~/.codex`). Saved sessions live in `$CODEX_HOME/switch/`. Running `codex` sessions can stay open: they refuse to refresh once the account changes, so restart them to use the new one. Only the default file credential store is supported.

The macOS Keychain path is untested.

## Stopping and resuming all sessions

On Linux and macOS, `claude-switch NICK` automatically remembers and stops interactive
Claude sessions before changing accounts. It discovers every running local Herdr
session belonging to your user, matches Claude processes to panes, and resumes
each conversation in its original pane with `claude --resume ID`. All pane
restarts run concurrently; readiness checks overlap. Shell initialization such
as direnv/devenv is allowed up to 120 seconds before a pane is left parked. Other agent
kinds and remote machines are not restarted. Herdr itself stays running.

Resuming preserves the model, effort and permission-mode options (including
`--dangerously-skip-permissions`) recognized on the original command line.
Other custom launch options are not copied. Resuming reopens the conversation;
it does not automatically submit a prompt or continue an interrupted turn.

Sessions outside Herdr, and panes whose restart fails, remain parked for manual
recovery. A replaced pane or changed shell directory is never reused. If switching
to a new nickname, log in first and resume manually. A failed switch also leaves
sessions parked. The Chrome native messaging host is also stopped so no old
Claude process remains alive across an account switch. Chrome can reconnect
when its Claude integration is next used.

```sh
claude-switch NICK     # park, switch, resume Herdr panes
claude-resume          # manually resume the session for this directory
claude-resume --list   # list parked sessions
claude-resume 2        # resume a session by list index
claude-park            # park without changing accounts or restarting panes
claude-park --dry-run  # show sessions without stopping or saving anything
```

Headless `claude -p` runs block an automatic switch before anything is stopped.
Unrecognized Claude processes left running also prevent the credential change.
Standalone `claude-park` leaves headless runs alone and reports them.

## Install

```sh
nix profile install github:mathijshenquet/agent-switcher
```

Or as a flake input: `agent-switcher.packages.${system}.default`.
