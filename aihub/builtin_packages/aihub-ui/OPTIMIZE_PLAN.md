# aihub-ui optimisation plan (from 3 audits, 2026-10-29)

Five implementers, disjoint files. All paths under `aihub/builtin_packages/aihub-ui/ui/`.

| Lane | Files | Theme |
| --- | --- | --- |
| A Works | `aihubui/works.py`, `nodes/*.py` | engine thread safety, catalogue cache, run hygiene, process kill |
| B Free chat / MCP | `aihubui/freechat.py`, `aihubui/mcpclient.py`, `aihubui/auth.py` | MCP pool + leaks, cancel, thinking blocks, history budget, key cache |
| C Store / chat | `aihubui/store.py`, `aihubui/chat.py` | slim sessions.json, fewer rewrites, tails, stop before Popen, secret env filter |
| D Server side | `server.py`, `aihubui/aihub.py`, `term.py`, `selfupdate.py`, `usagereport.py`, `officeview.py`, `spacebundle.py` | `aihub list` cache, terminals, chat_send order, streaming files, errors |
| E Frontend | `static/*` | lost sends, scroll, per-token render, pollers, mobile nav, Esc, a11y, lazy libs |

Cross-lane contracts:
- `files/content` GET returns `mtime`; PUT with `mtime` that differs from disk -> 409 `{"error":"changed"}` (D implements, E uses).
- `chat.turn` drops `extra_env` keys that start with `node:` or are not env names (C); callers unchanged.
- `store.get_session` still returns `system`/`extra`/`seed` (loaded from side files); list route gets slim rows (C); `store.delete_session(sid)` used by A to drop work-run chats.
- `store.write_private` is the atomic writer (D uses it for notes).
