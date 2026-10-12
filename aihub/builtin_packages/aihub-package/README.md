# aihub-package

Built-in AI Hub skill, installed automatically by the hub installer and `aihub welcome`.

Teaches Claude Code, Codex and OpenCode to turn a new or existing project into a valid AI Hub package. The packaging steps come from the hub's `publishing` doc ("Assistant packaging workflow"), so they are read live from the hub. A folder that should hold several packages is set up as one git repository with `aihub dev init --mono`, one package per sub folder.

```sh
aihub install aihub-package
aihub uninstall aihub-package
```
