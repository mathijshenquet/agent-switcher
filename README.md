# agent-switcher

Switch between Claude Code and Codex subscription logins. Sessions are keyed by account; the name is just a nickname.

```sh
claude-switch                   # status
claude-switch NICK              # switch to NICK (new nickname: log in fresh, saved on next run)
claude-switch -                 # switch to the other session, or pick one
claude-switch --rename [OLD] NEW   # default: the current session
```

`codex-switch` works the same.

Quit all `claude` processes before switching (or pass `-f`). Running `codex` sessions don't need to quit: they refuse to refresh once the account changes, so restart them to pick up the new one.

Install: `nix profile install github:mathijshenquet/agent-switcher`
