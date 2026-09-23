# agent-switcher

Switch between Claude Code subscription logins. Sessions are keyed by account; the name is just a nickname.

```sh
claude-switch                   # status
claude-switch NICK              # switch to NICK (new nickname: log in fresh, saved on next run)
claude-switch -                 # switch to the other session, or pick one
claude-switch --rename [OLD] NEW   # default: the current session
```

Quit all `claude` processes before switching (or pass `-f`).

Install: `nix profile install github:mathijshenquet/agent-switcher`
