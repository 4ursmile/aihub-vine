# aihub-ui

A local, Claude-style chat and control UI for AI Hub.

```sh
aihub install aihub-ui
aihub run aihub-ui          # background service; the address (with this session's key) is in ~/.aihub/ui/url.txt
```

- **Chat** with the Claude Code harness (`claude -p`): model, effort and mode pickers, attachments, history in folders. Models, login, hooks, MCP servers, skills and agents all come from Claude's own `settings.json`, so everything `aihub install` registered is usable.
- **Answers render in place**: shell sessions, code, sortable tables, charts (`chart` blocks) and forms (`form` blocks, the submit goes back as your next message). Claude is told about them in the chat's frozen system prompt.
- **Context window**: a ring shows usage against the size set in Settings (default 1M tokens; also sent to Claude Code as `CLAUDE_CODE_AUTO_COMPACT_WINDOW`); Compact now, auto-compact and its limit. `/` opens slash commands: the UI's own, Claude Code's, and `slash/*.slash.json` from packages.
- **Live work card**: while Claude works you see the phase (reasoning / tools / answer), a step list with each tool and its result, the reasoning as it streams, counters (time, steps, estimated tokens, context) and two buttons: **Stop** and **Answer now** (stops the tools and asks for a short direct answer in the same session). Finished reasoning is a collapsible block.
- **Talk while it works** (Claude Code mode): a message typed during an answer goes straight into that answer; Claude reads it at its next step (after the current tool call), like typing into Claude Code while it works. It waits as a dashed chip until Claude has read it, then shows in the chat tagged "sent while Claude was working". **`/btw <question>`** asks a quick side question (also mid-answer): a throwaway fork of the chat answers it from the conversation in one step without tools; the answer is a dismissible card above the box and is not added to the chat. Free chat queues the message for when the answer ends.
- **Free chat**: the engine pill in the composer switches a chat to the UI's own harness: one streaming call to the Messages API, no Claude Code, no tools or files, fast and cheap. It needs `ANTHROPIC_API_KEY` (Settings > Encrypted secrets, or the `env` block / `apiKeyHelper` of your Claude settings; `ANTHROPIC_BASE_URL` is honoured; a subscription login only works through Claude Code). Switch at any message; earlier text carries over, tool calls do not. Free chat can use MCP servers: tick the ones you want in the engine pill (servers of installed aihub packages, the MCP servers page, and Claude Code's user servers; stdio only). The model then calls their tools in a loop (up to 12 rounds per message) and the calls show in the live card like Claude Code's. It draws an svg or writes an html report to the canvas only when you ask for one.
- **Working folder**: each chat works in `~/.aihub/workingspace/sessions/<id>` unless you pick a folder (folder chip in the chat header; a missing folder is created, the chat moves into a sidebar project of the same name, and new chats started in that project reuse it). Changing it starts a new Claude session that carries the conversation as text. **Soul**: the person button gives one chat its own soul text (or back to your soul note). **Search chats** in the sidebar looks in titles and messages.
- **Canvas**: a file tree of the working folder, live; Preview, **Edit** (Ctrl/Cmd+S saves) and **Diff** (since your last message, or since the last git commit) tabs, and a split mode that shows the preview beside the editor or diff; `.html` files get a sandboxed live preview; `html`, `svg` and `openui` code blocks render under Elements. Components are [OpenUI](https://openui.com) (`@openuidev/react-lang` + `react-ui`), taught to Claude through the chat's frozen system prompt.
- **Packages**: installed list, find, install (options default on, then a settings form for keys), update, enable/disable, uninstall.
- **Services**, **MCP servers**, **Knowledge** (soul and notes = memory), **Works**: node graphs that start on a cron schedule, a webhook, a click or a package's own event source, with regex filters, Claude tasks (choose model / effort / mode) and package nodes. Mail: **Send email (SMTP)** (any server, any system; send, draft into the account's IMAP Drafts folder, or a `.eml` file) next to the Outlook package node (`aihub-nodes-outlook`, Windows; send, draft, display). **Saved nodes** (Works > Saved nodes): set a node up once, logins included (encrypted), then add it to any work from Add node > Saved nodes; empty fields follow it, typed ones change that work only. Guide: `docs/ui-nodes.md`.
- **Settings** also holds the Vault (git logins), the hub sign-in (highlighted while you are signed out) and the context window.
- Same state as the terminal: it calls `aihub --json ...`, so login, vault and packages are shared.

## Safety

127.0.0.1 only; random per-start key (cookie, HttpOnly, SameSite=Strict); Host/Origin checks; a custom header on every write; generated pages run in a sandbox with no network. Secrets from `settings.json`, `.env` files and the vault are never sent to the browser. Secrets you add under Settings are encrypted at rest (`~/.aihub/ui/secrets.bin`, key file mode 0600) and only reach the Claude process of a chat.

## Cache

The chat's system prompt (soul, note index, OpenUI rules) is built once and frozen, turns resume the same Claude session, and the page shows cache read/write per turn. Changing the model mid-chat or "Reload memory" rewrites the cache.

**Claude sign-in**: if `settings.json` has an `apiKeyHelper` that needs an OAuth sign-in, chats and Claude nodes check it first and offer Sign in. The key it prints is never stored or shown.

Every library ships in `ui/static/vendor/` (the lazy ones in `vendor/lazy/`, built by `python ui/tools/vendor_libs.py`), so the page works offline and loads nothing from a CDN. Map tiles still come from OpenStreetMap. HTML pages Claude writes in a chat may still use jsDelivr.
