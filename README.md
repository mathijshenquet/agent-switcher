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

- **Claude**: swaps the OAuth credentials (`~/.claude/.credentials.json`, or the Keychain on macOS) and `oauthAccount` in `~/.claude.json`, and drops the account-scoped caches that `/logout` also clears. Saved sessions live in `~/.claude/switch/`. Quit all `claude` processes first (or pass `-f`), or a running session writes its old tokens back.
- **Codex**: swaps `$CODEX_HOME/auth.json` (default `~/.codex`). Saved sessions live in `$CODEX_HOME/switch/`. Running `codex` sessions can stay open: they refuse to refresh once the account changes, so restart them to use the new one. Only the default file credential store is supported.

The macOS Keychain path is untested.

## Install

```sh
nix profile install github:mathijshenquet/agent-switcher
```

Or as a flake input: `agent-switcher.packages.${system}.default`.
