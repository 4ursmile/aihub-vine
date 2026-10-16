# aihub-prices

Price table for `aihub usage`. The CLI ships a copy and checks the hub every few hours (`policy_check_hours`) for a newer version.

`prices.json`: `{"prices": [{match, input, output, cache_write?, cache_read?}]}`, USD per 1M tokens. `match` is a regex on the model
name, first match wins. Order of use: `~/.aihub/prices.json` (user) > this package from the hub > the copy bundled in the CLI.

Bump `version` in `aihub.toml` to roll out a change.
