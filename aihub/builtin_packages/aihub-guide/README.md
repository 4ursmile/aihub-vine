# aihub-guide

Built-in AI Hub skill, installed automatically by the hub installer and `aihub welcome`.

Teaches Claude Code, Codex and OpenCode to use AI Hub. Before answering or setting up a project, it fetches the current Markdown docs from the hub API (`/api/v1/docs/<slug>/raw`), so its answers follow the running hub rather than a fixed copy.

```sh
aihub install aihub-guide
aihub uninstall aihub-guide
```
