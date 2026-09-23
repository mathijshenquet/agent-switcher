# agent-switcher

Switch between Claude Code subscription logins.

```sh
claude-switch NAME   # switch to NAME (new name: log in fresh, saved on next switch)
claude-switch        # switch to the other session, or pick one
claude-switch -l     # list sessions
```

Quit all `claude` processes before switching (or pass `-f`).

Install: `nix profile install github:mathijshenquet/agent-switcher`
