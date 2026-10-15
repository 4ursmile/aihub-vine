# aihub-policy

Rule file for the guard built into the `aihub` CLI. The CLI already ships default rules; this package lets the hub change them for
everyone. The CLI checks the hub every few hours and installs a newer version by itself.

`policy.json`:

- `revision` shown by `aihub policy`.
- `rules`: `{id, action: deny|ask, reason, pattern, tool?, field?}`. A rule with the id of a built-in rule replaces it; a new id adds one.
- `retire`: built-in or earlier rule ids to drop.

Bump `version` in `aihub.toml` to roll out a change. Users can still switch a rule off for themselves (`aihub policy disable ID`).
