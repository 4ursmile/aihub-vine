# Writing nodes for aihub-ui Works

A **Work** is a graph in the `aihub-ui` Works editor: a *Start* node produces an event, the event text flows along the edges through *action* nodes, and the run ends. You can add your own nodes by shipping them in an ordinary aihub package. Install the package and its nodes appear in the editor's **Add node** list, grouped under the group you chose.

Before writing a node, check whether you need one:

| You want to... | Use |
| --- | --- |
| Ask an LLM, summarise, classify, write a file, send a mail, call an MCP tool | The built-in **Claude task** node. Pick the model, effort and permission mode on the node. It runs Claude Code, so every skill, agent and MCP server aihub installed is available. A skill such as "send mail" makes a node unnecessary. |
| Start from a cron schedule, a webhook call or a button | Built-in **Start: schedule / webhook / manual**. |
| Keep only matching text | Built-in **Filter (regex)**, or the **filter** field every Start node has. |
| Listen to something that is not a webhook (a chat, a queue, a file) or call a service deterministically, with no LLM | **A package node** (this guide). |

## The built-in nodes

| Type | Kind | What it does |
| --- | --- | --- |
| `start.manual` | trigger | The Run button. |
| `start.cron` | trigger | `schedule` = cron (`0 8 * * 1-5`) or interval (`30s`, `5m`, `2h`, `1d`, minimum 10 s); optional event `text`. Fires only while the work is **On** and the UI service runs; missed times are not replayed. |
| `start.webhook` | trigger | `POST http://127.0.0.1:PORT/hook/<work>/<node>/<token>` with plain text or `{"text": "...", "data": {...}}`. Local only; the URL is shown in the node's panel. |
| `ai.claude` | action | Prompt with `{{input}}` (text from the previous node), `{{trigger}}` (the event text) and `{{data.key}}`. Model, effort, permission mode. All Claude nodes of one run share one chat (context and prompt cache carry over) unless `fresh` is ticked. Unattended runs use *Accept edits* by default; a tool that would need approval is denied. |
| `flow.filter` | action | Regex on the incoming text. No match: this branch ends. `invert` flips it. |
| `flow.template` | action | New text from `{{input}}`, `{{trigger}}`, `{{data.key}}`. |

Every **trigger** also has a `filter` field: a case-insensitive regex tested against the event text **before a run is created**. An event that does not match never starts anything, never shows in the run list and never reaches your nodes below it. Your trigger code does not need to implement this, send everything.

## Create, share and install nodes in the UI

Works > **Nodes: create, share, install** (also at the bottom of **Add node**):

- **My nodes**: *New node* opens a form (id, title, kind, group, description, settings fields, the data keys it returns) and a Python program that starts from a working example. It is saved under `~/.aihub/ui/nodes/<id>/` and appears at once in Add node as `pkg:local/<id>`. *Share* builds an aihub package of type `node` (`nodes/<id>.node.json` + program) and opens the same Publish steps as a workflow.
- **Installed**: packages that bring nodes. **Find on the hub** searches packages of type `node` and installs them in the install dialog.

Works that use a `pkg:local/...` node need that node on the other machine: share the node and install it there (its type becomes `pkg:<package>/<id>`).

## Prompt help: suggest and highlight

A text field lists what it may suggest in `"assist"` (any of `input`, `data`, `file`, `skill`), in a `*.node.json` field or in the node form:

| Value | Typing | Suggests / highlights |
| --- | --- | --- |
| `input` | `{{` | `{{input}}` (text of the node(s) connected before this one, named in the list), `{{trigger}}` |
| `data` | `{{` | `{{data.key}}` for the inputs of a Start: manual and the keys that upstream nodes list in `"outputs"` (for example `path` of the git/file pointer nodes) |
| `file` | `@` | files and folders under your home folder (names only), highlighted as `@path` |
| `skill` | `/` | skills, commands and package slash commands, highlighted when the name is known |

The list opens under the caret as you type `{{` (typing `{{` closes itself with `}}`), narrows as you type (prefix first, matches in bold, grouped: input / one parent / data), `Ctrl+Space` opens everything, and the **Insert** chips under the field put a variable at the caret with one click. Single-line fields (a URL, a path) get the same help. `OUTPUT:` lines are highlighted.

## How data flows between nodes

Every node hands on `{text, data}`. `text` is a string; `data` is a dict (only pointer, trigger and package nodes fill it: `path`, `status`, your Start inputs). The editor shows, for the selected node, what it **Receives** (each parent with its kind: text, path, json, link), what it **Produces** and what it **Needs** (a warning when a Publish node has nothing before it that can give a path).

- **One parent**: `{{input}}` is that parent's text.
- **Several parents** (a join): the node waits for ALL of them. `{{input}}` is their texts one after another, each under `## From <node name>`, in the order the lines were drawn. `{{in.1}}`, `{{in.2}}` (same order) or `{{in.<node name>}}` (lower case, other characters `_`, e.g. `{{in.git_read}}`, a second node with the same name is `claude_task_2`) is ONE parent's text. `{{data.x}}` merges the parents' data; on the same key the later parent wins.
- **Claude prompt**: if the prompt contains `{{input}}` or an `{{in.x}}`, you place the text yourself. If it contains none, the engine puts `Input: <text>` before `Task: <prompt>`. Claude nodes of a run share one chat (earlier answers are already in it, so identical text is not repeated).
- **Claude to Publish**: Claude only answers with text. A node after it (Publish to Space, email attachments) takes the path from the LAST line of that text that names an existing file or folder (`OUTPUT: /full/path`; backticks and quotes are ignored), or from the `path` data of a pointer node, or from its own File field. The prompt box offers **Add the instruction** (writes the `OUTPUT:` request into the prompt) and says so when a Publish node follows. The Data tab and the Publish log show which path was used and where it came from.
- **Parallel branches**: nodes that do not depend on each other run at the same time (at most 4 per run); a node starts when all its parents are done. Claude nodes running side by side each get a chat of their own (the shared chat stays with whichever is alone); a sequence still shares one.
- **Generate** (Works > describe a work, `/workflow` in chat): the generator is told the rules above, each node's output kind and needs, and a working example. Its answer is repaired before saving (duplicate ids, edges to nothing, `{{in.x}}` that names no parent, a Publish node with no path source gets an `OUTPUT:` request added to the Claude node before it); an answer that cannot be used is sent back once with the reason. The result is saved switched off.
- A package node can declare `"out": "text|path|json|url"`, `"out_note"`, `"needs": "path"` and `"needs_note"` so the editor shows the same Receives/Produces strip.

Without `assist`, a textarea or a `templated` field gets `input` + `data`; the prompt of Claude task and Free chat gets all four. `"outputs": ["path"]` at node level names the data keys the program returns in `data`.

## Layout of generated works

`/create-work <description>` in a chat (alias `/workflow`) and Works > Generate design a work with Claude. Cards are 230 wide and ~100 tall: positions that overlap or are missing are replaced by a layered left-to-right layout (`works.tidy`), good ones are kept. The editor has a **Tidy** button, and a new node takes the first free grid slot.

## A node package

Use `type = "node"` (`aihub dev init ./my-node --type node --name my-node` writes a working starter; `dev validate` checks each `nodes/*.node.json`). Any other package type with a `nodes/` folder also works, as before. Share it with `aihub dev publish`, install it with `aihub install NAME`. Put one JSON file per node in `nodes/` and the program it runs next to it:

```
aihub-nodes-teams/
  aihub.toml
  nodes/
    teams-message.node.json     # trigger
    teams_message.py
    teams-send.node.json        # action
    teams_send.py
```

A complete, tested example lives in [`aihub/builtin_packages/aihub-nodes-teams`](../aihub/builtin_packages/aihub-nodes-teams), shipped with the hub (`aihub install aihub-nodes-teams`). Publish your own like any package (`aihub dev publish`); the editor reads the catalogue from the installed packages (`aihub list`), refreshing every 10 s.

### `*.node.json`

```json
{
  "id": "teams-send",
  "title": "Send to Teams",
  "kind": "action",
  "group": "Teams",
  "description": "Posts a message to a Teams channel.",
  "run": ["{python}", "nodes/teams_send.py"],
  "timeout": 30,
  "fields": [
    {"name": "webhook_url", "label": "Webhook URL", "type": "password", "required": true},
    {"name": "text", "label": "Message", "type": "textarea", "default": "{{input}}"}
  ]
}
```

| Key | Meaning |
| --- | --- |
| `id` | Lowercase letters, digits, `. _ -`. The node type is `pkg:<package>/<id>`. |
| `kind` | `trigger` (starts runs, no input) or `action`. |
| `group`, `title`, `description` | Where it appears in **Add node** and what the panel shows. |
| `run` | The command as a list, never a shell string. `{python}` = the interpreter running the UI, `{pkg}` = your package folder. The working directory is the package folder. |
| `timeout` | Seconds for an action (5..3600, default 120). Triggers run until stopped. |
| `fields` | The settings form. `type`: `text`, `textarea`, `number`, `checkbox`, `select` (+ `options`), `password`, `model`, `effort`, `mode`. Also `label`, `default`, `required`, `help`, `secret`. |

**Secrets.** A field with `"type": "password"` or `"secret": true` is stored encrypted (`~/.aihub/ui/secrets.bin`), the work file keeps only a marker, the editor never shows the value again, and your program receives the real value on **stdin** (never on the command line, never in the environment). A node package must not log it.

Malformed node files are ignored silently, so check the node shows up in the editor.

### Action nodes: one request, one answer

The program is started for each run. It reads **one JSON document** from stdin:

```json
{"config": {"webhook_url": "https://...", "text": "{{input}}"},
 "input": "text of the previous node(s), joined",
 "inputs": [{"text": "...", "data": {"from": "alice"}}],
 "run": {"id": "20261028110105-fb1517", "work": "8e24267e", "node": "n1a2b"},
 "workdir": "/Users/me/.aihub/workingspace/sessions/ab12cd34"}
```

and prints **one JSON line** as the last thing on stdout:

```json
{"ok": true, "output": "text for the next node", "data": {"id": 42}, "log": "HTTP 200"}
```

- `output` is a string or a JSON value (stringified for downstream). `data` is a dict that travels along as `{{data.key}}`.
- `{"ok": false, "error": "why"}` or a non-zero exit fails the node: the branch stops and the run is `failed`. Write diagnostics to stderr.
- `{"output": {"skip": true}}` ends the branch quietly (like a filter that did not match).
- Template placeholders inside your own `config` values (`{{input}}`) are **not** expanded for you unless the node lists those fields in `"templated": ["to", "subject"]` (then the engine fills `{{input}}`, `{{trigger}}`, `{{data.x}}` before the program starts); otherwise do it in the program (the example does).
- `workdir` is the run's chat folder when a Claude node ran before you, else your home folder. Files written there show in the chat's canvas.

### Trigger nodes: a process that stays up

A trigger program is started when the work is switched **On** and stopped when it is switched off, edited, or deleted. A crash restarts it after 30 s, doubling to 15 minutes. It reads **one line of JSON** from stdin (`{"config": {...}, "run": {"work", "node"}}`) and then prints **one JSON line per event** on stdout, flushed:

```json
{"event": {"text": "deploy prod", "data": {"from": "alice", "chat": "ops"}}}
```

For each line aihub-ui applies the trigger's `filter` regex to `text`, and if it matches, queues a run (runs of one work execute one after the other, 50 queued at most). Everything else on stdout is ignored; stderr is discarded.

The example trigger `teams-message` does exactly this:

```python
cfg = json.loads(sys.stdin.readline())["config"]
...
print(json.dumps({"event": {"text": text, "data": {"from": who, "chat": chat}}}), flush=True)
```

### Example: start from a Teams message (regex first), then send to Teams

1. Install `aihub-nodes-teams`.
2. New work: **Start: Teams message** (port `8791`, filter `^deploy `) connected to **Claude task** (prompt: `Plan the rollout for: {{input}}`, model `sonnet`) connected to **Send to Teams** (paste the channel's Workflows/incoming webhook URL; it is encrypted).
3. Switch the work **On**.
4. Point your Teams bot at the trigger. The node only listens on `127.0.0.1` and accepts `POST /message` with `{"from", "chat", "text"}`. A bot that already runs on this machine (for example `aihub-teams-bot`) forwards each incoming message with two lines of `urllib.request`; test by hand:

```sh
curl -s -X POST http://127.0.0.1:8791/message -d '{"from":"alice","chat":"ops","text":"deploy prod now"}'   # {"taken": true}  -> a run starts
curl -s -X POST http://127.0.0.1:8791/message -d '{"from":"alice","chat":"ops","text":"lunch?"}'          # dropped by the filter, no run
```

Filtering happens in layers, cheapest first: the node's own settings (`from_regex`, `chat`) inside your process, then the trigger's `filter` regex before a run exists, then **Filter** nodes inside the graph for branching.

Sending a mail is the same shape as `teams_send.py` with `smtplib` (put the password in a `password` field), or leave it to a Claude task with a mail skill.

### Rules for good nodes

- Do one thing, take everything from `config` and `input`, and keep no state between calls (a trigger may keep state while it runs).
- Listen on `127.0.0.1` only. A trigger that opens a network port to other machines turns a chat message into remote control of the work.
- Validate untrusted text (message bodies are input from other people): never put `input` into a shell command or a file path unchecked. Hand it to Claude only as data in the prompt.
- Use `timeout`, exit non-zero on failure, and print nothing secret.
- Add a test: run your program with a sample stdin and assert on the last stdout line (see `tests/test_ui_works.py` for the example package).

## Memory, history and the Teams nodes

| Node | Use |
| --- | --- |
| `flow.output` (Output) | The answer of the work (see below). |
| `flow.memory` (Memory) | Adds remembered context to the text that flows through. `source`: `work` = this work's earlier runs (trigger text + the result of the last node), `chat` = the last messages of a chat (empty id = your latest chat), `other` = another work's runs (its id). `limit` items (1-50), `chars` cap, `mode` = prepend (memory, then the input) or memory only. A Memory block that an earlier run added to its result is cut out again, so memory does not snowball. |
| `start.teams` (Start: Teams message) | Starts a run for each Teams message a Power Automate flow forwards. It runs `nodes/teams_in.py`: a listener on `127.0.0.1:PORT/teams`, every request must carry the shared secret (`X-Hook-Secret` header or `?key=`). Teams (a cloud service) cannot reach your machine, so tunnel only that port (Dev Tunnels, cloudflared or ngrok). Text comes from `text`, `message` or `body.content` (HTML turned into plain text), the sender from `from`, the chat from `chat`; `from_regex`, `chat` and the generic `filter` drop messages before a run exists. |
| `send.teams` (Send to Teams) | POSTs the message to a Teams Workflows URL ("When a Teams webhook request is received"). Payload `card` = Adaptive Card (what that template expects) or `text` = `{"text": ...}`. The URL is a secret field: stored encrypted, passed on stdin. |

Both Teams nodes carry a `guide` that the node inspector shows above the settings ("How to set this up"). Any package node can have one too: add `"guide": "text"` to its `*.node.json`.

**History limit.** The Works toolbar has a *History* number (1-500, default 40): runs older than that are deleted, and Memory nodes can only see what is kept.

**Run a work from a chat.** Type `/` in a chat: every work is listed as `/work:<title>`. Pick one and add the text that should start it; the result appears under your message.

## Data nodes: read file, HTTP request, read from git

| Node | What it does |
| --- | --- |
| `io.read_file` (Read file) | A local text file (or a folder listing, optionally with the text of its first files). `~` works, a relative path starts in the run's working folder; `{{data.x}}` is filled in. Refuses key, secret and `.env` files, `~/.ssh`, `~/.aws`, `~/.gnupg`, most of `~/.aihub`, Claude settings: a shared workflow must not be able to read your logins. |
| `io.request` (HTTP request) | GET/POST/PUT/PATCH/DELETE/HEAD to an `http(s)` URL; headers, JSON/form/text body, optional *Pick* (`a.b.0.c`) from a JSON answer, fails on 4xx/5xx unless unticked. The *Authorization value* is a secret field (encrypted, stdin only). Output data: `status`, `json`. |
| `git.read` (Read from git) | A file or folder of a remote **https** repository, using the git logins you saved with `aihub vault` (aihub's own git credential helper answers git, exactly as in your terminal; no token is put in the URL or the work). Shallow temp clone (`core.symlinks=false`, no hooks), read, delete. Folder = listing (+ the text of the first 10 files if ticked). A missing login is reported with the `aihub vault add` hint. |

URL, header, body, path and ref fields of these nodes are templated (`{{input}}`, `{{trigger}}`, `{{data.key}}`).

## Description, sharing metadata and the demo

Every work has a short **description** (the line under the title bar; shown in lists and used as the package description). **Share** opens a form: package name, version, description, authors (default: your hub user name, else the OS user), license, tags, the operating systems it works on, node packages and commands it needs (detected from the nodes, editable) and a generated README you can edit. It writes `~/.aihub/ui/export/<name>/` (`aihub.toml`, `works/*.work.json`, `README.md`), runs `aihub dev validate` on it and shows the publish command. Risky nodes (files, web requests, git, Claude with checks bypassed, node packages) are listed as chips in the form and again when someone adds the workflow, which always arrives switched off.

The **Repo briefing** sample (Works > Workflows from packages > Samples) is a full demo: an input form, README read from git with your Vault login in parallel with live repo stats over HTTP, a Gather step, earlier briefings from the work's own history (Memory), a Claude node, and an Output node.

## Workflows: share a work as a package

Package type `workflow`: no code, just `works/*.work.json` (the work without its id; it is added switched off). Works toolbar > **Share** writes the package to `~/.aihub/ui/export/workflow-<name>/` with every secret field emptied and any `pkg:` node packages listed under `[requires] packages`, and shows the command to publish it (`aihub dev publish` in that folder). To use one: `aihub install workflow-<name>`, or Works > **Workflows from packages** (installed list + search the hub, with the usual install dialog). *Add to Works* copies it into your works; fill in the secret fields yourself.

**Publishing from the page.** After *Build package* the dialog walks through three steps: (1) the package was built and validated, (2) you are signed in to a hub (it links to Settings > Hub account when not), (3) the publish command, shown in full with two options (*Upload to the hub* = `-i`, on by default because the exported folder is not a git repository; *Bump the patch version*), run in the same kind of terminal as installs. The terminal only ever runs `aihub dev publish` on a folder Share made under `~/.aihub/ui/export/`, so a question or login prompt can be answered; the command can also be copied and run by hand.

## Installs that ask questions

The install dialog opens a terminal by default (xterm.js from jsDelivr). It runs `aihub install NAME` in a pseudo-terminal, so an install script that asks something on stdin can be answered in the page; it is not a shell and cannot run anything else. Output is kept in memory, the dialog continues to the package settings form when the install ends. Windows has no pty: untick *Open a terminal* there.

## Inputs and outputs of a work (running it from a chat)

- **Inputs.** The *Start: manual* node has an **Inputs** field, one input per line: `name|Label|type|options or default|*` (types `text`, `textarea`, `number`, `select` with comma separated options, `checkbox`; `*` = required). Example: `topic|Topic|text||*` and `level|Level|select|easy,hard`. Later nodes read the values as `{{data.topic}}`. In a chat, `/work:NAME` shows these as a small form (nothing is asked when there are none; the text after the command is then the event text); the server checks required values, numbers and select options again before the run starts.
- **Output.** Add an **Output** node (*Flow > Output*, heading + markdown template with `{{input}}`, `{{data.x}}`). Its text is the work's answer. Without one the last text any node produced is used. The chat card shows live step chips while the run goes, then the answer rendered as markdown, with *Copy*, *Send to Claude* (puts the answer into the chat as context), and links to the run's chat/files and to Works. The answer is also stored in the run record (`result`).

## Slides, documents and themes

Claude writes one `.html` file; the canvas (Files > click the file) recognises it and shows it natively.

| Kind | Marker | Shown as | Export |
| --- | --- | --- | --- |
| Slides | `<meta name="aihub-kind" content="slides">` or `<section class="slide">` blocks (1280x720 px each) | one slide at a time, scaled to fit; arrows, `g` = all slides, full screen; `aside.notes` are hidden | PowerPoint `.pptx` (editable: headings, text, lists, tables, pictures, notes, placed where they are), PDF (print, one slide per page), HTML |
| Document | `<meta name="aihub-kind" content="doc">` or `<article class="doc">` | a page | Word `.docx`, PDF (A4), Markdown, HTML |

How it works: the file is opened with `?view=deck` / `?view=doc`; the server injects `static/deckrt.js` into the HTML (only then) and the page and the viewer talk by `postMessage` (the preview iframe stays sandboxed, no network). Export asks the page to *extract* its blocks (text with runs, lists, tables, pictures, computed colours/fonts, position on the slide) and builds the file in the browser with PptxGenJS / docx from jsDelivr (loaded when you press Export). The built-in skill package `aihub-slides` and the chat's system prompt teach Claude the format. `SKILL.md` has the rules (variables on `:root`, no scripts, one idea per slide).

**Themes** (Settings > Themes): name, six colours (background, text, muted, accent, dark accent, panels) and two fonts, plus notes for Claude. *Import from a PowerPoint* reads `ppt/theme/theme1.xml` in the browser (JSZip): colour scheme and major/minor fonts become the theme. Stored in `~/.aihub/ui/themes/<id>.json`. Mention one in a chat with `@theme:<id>` (typing `@` lists them): the server appends a `[Theme "Name" ... --bg:#...]` block to that message for Claude only; the visible message and the cached system prompt do not change.

## Slash commands from packages

Typing `/` in the chat composer opens a menu with three sources: the UI's own commands (`/compact`, `/clear`, `/model`, `/effort`, `/mode`, `/context`, `/canvas`), everything Claude Code knows (`~/.claude/commands`, skills and plugin skills, which Claude runs itself), and commands from installed aihub packages. A package adds one with a file `slash/<name>.slash.json`:

```json
{"name": "standup", "description": "Summarise yesterday's work", "prompt": "Summarise what I did yesterday from git log. Focus: $ARGUMENTS"}
```

Typing `/standup backend` sends the `prompt` with `$ARGUMENTS` replaced by `backend`. `name` is letters, digits, `. _ - :`; `prompt` is at most 8000 characters; the package must be installed and enabled. A package command never runs code: it only expands to a prompt, so it can use whatever skills and tools the chat already has. (A command that needs code is a Works node.)

## Context window, auto-compact and the policy guard

The ring next to the model picker shows how much of the model's context window the last request used (input + cache read + cache write). `[1m]` models count 1M tokens, others 200k. The popover has **Compact now** (sends `/compact`; Claude Code reports the before and after sizes) and the auto-compact switch and limit. They are stored in `~/.aihub/ui/prefs.json` and become `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` / `DISABLE_AUTO_COMPACT` for every chat and every Claude node.

Settings also has **aihub policy guard in these chats**. It is **off by default while the UI is being tested**: the Claude process of a UI chat gets `AIHUB_JUDGE=1`, which makes aihub's hook return at once. That affects only chats and Claude nodes started from this UI (no usage events are recorded for them either); your terminal and the global policy (`aihub policy`) are untouched. Switch it on to guard UI chats like terminal ones.

## Model list

The picker shows the aliases (Opus, Sonnet, Haiku and their full ids), then **Your model picker**: the `modelPicker.options` of the settings file, each `{"model": id, "label": text}`, shown with that label; then the ids in `availableModels`, `model` and `ANTHROPIC_*_MODEL`. Nothing is fetched from a gateway. A search box appears above ten entries.

## Which Claude settings.json is used

The *Browse…* button opens a file picker over your home folder (folders and `.json` files only, sizes but never contents; `.ssh`, `.gnupg`, `.aws`, `Library` are hidden). Settings > *Claude settings file*: empty = `~/.claude/settings.json` (+ `settings.local.json`); or the path of any `.json` object file. The model list, effort, environment (secrets masked) and `apiKeyHelper` come from it, and chats and Claude nodes start with `claude --settings <file>`, merged over your normal Claude settings. Policy guard, auto-compact and themes follow the UI's own settings. Saved as pref `claude_settings` in `~/.aihub/ui/prefs.json`; a missing or non-JSON-object file is refused.

## Where things are stored

| What | Where |
| --- | --- |
| Works | `~/.aihub/ui/works/<id>.json` (nodes, edges, `enabled`; secrets replaced by a marker) |
| Runs | `~/.aihub/ui/runs/<work>/<run>.json`, last 40 per work |
| Node secrets | `~/.aihub/ui/secrets.bin` (encrypted), key `~/.aihub/ui/.key` (0600) |
| Preferences (policy guard, auto-compact) | `~/.aihub/ui/prefs.json` |
| Webhook key | `~/.aihub/ui/.hooks.key` (0600); tokens survive restarts |
| A run's chat and files | `~/.aihub/workingspace/sessions/<id>` (opened from the run list) |

## Claude sign-in with an `apiKeyHelper`

If `~/.claude/settings.json` sets `apiKeyHelper` (a command that prints a key, often after an OAuth sign-in whose token the helper stores itself), Claude Code runs it on its own. The UI adds a check before every chat turn and every Claude node: if the helper fails, the chat shows **Sign in** (Settings has the same). The sign-in window runs the helper in the background, shows what it prints on stderr (device codes, links), and remembers only "valid until" in memory. The key the helper prints is never stored, logged or sent to the browser. A helper that needs a real terminal must be run there once.


## Builtin pointer, publish and mail nodes (aihub-ui 1.16)

Data moves between nodes as **pointers**: a path, never the content, so parquet, csv or image files never pass through a prompt.

| Node | What it does |
| --- | --- |
| `git.pull` Read from git (path) | Clones or updates the repo in `~/.aihub/workingspace/data/git/NAME` (Vault login, Git LFS files fetched) and outputs the path(s). Data: `path`, `paths`, `commit`, `files`. |
| `io.path` File / folder (path) | Checks local files / folders / `*` patterns exist and outputs the path(s). |
| `ai.claude` | Gets pointers in `{{input}}` and `{{data.path}}`; the folders are added with `--add-dir`, so Claude opens the files itself. End a node that creates a file with `OUTPUT: /full/path`. |
| `ai.free` Free chat | Direct API call (no Claude Code). Optional MCP servers (tool loop); "paste the text of small files" is opt-in (30 KB). |
| `space.publish` Publish to Space | `aihub space publish` of a file or folder (index.html) as doc / slides / report / pdf / sheet / dashboard / html. Same name = new revision. Private unless set. Path empty = the last existing path of the input. |
| `send.email` (legacy) | Moved to the package `aihub-nodes-outlook` (see below). Old works keep working once it is installed; without it the node fails with the install command. |

`data` of upstream nodes is merged into `{{data.x}}` of the next node (later inputs win). Example: `ui/samples/git-data-dashboard.work.json` (git data -> Claude builds `dashboard/` -> Space).

**Generate a work**: Works page "Generate a workflow" box, `/workflow <description>` in a chat (both `POST /api/works/generate`), or an answer with a ```` ```workflow ```` JSON block (button -> `POST /api/works/make`). Generated works only use known node types, have secrets emptied and are saved switched off (`works.checked_work`).


## Node contract: inputs and outputs (standard keys)

Every node reads one document and hands on `{text, data}` (see "Action nodes"). To make nodes combine without glue, follow these conventions:

| Part | Rule |
| --- | --- |
| **Input** | `config` (the node's own fields), `input` / `inputs` (text and `data` of the parents), `workdir`. Declare each setting as a field; give it a `help` that says what an empty value does. |
| **`takes`** | `"takes": ["to", "subject", "body"]` in the `*.node.json`: when such a field is EMPTY, the engine fills it from `{{data.<same name>}}` of the nodes before (a list becomes one value per line). This is how a Loop over `[{"to": ..., "subject": ...}]` drives a node without any `{{data.x}}` typed into fields. Secret fields are never filled. |
| **`templated`** | Fields in which `{{input}}`, `{{trigger}}`, `{{data.key}}` are expanded before the program starts. |
| **Output** | `output` (text for the next node), `data` (a dict that travels as `{{data.key}}`), `log`. List the keys in `"outputs": [...]` so the editor suggests them; `"out": "text|path|json|url"` plus `"out_note"` say what kind of thing `output` is, `"needs"` / `"needs_note"` what must come before. |
| **Standard data keys** | Mail: `to`, `cc`, `bcc`, `subject`, `body`, `attachments` (a list of file paths); mail triggers also give `from`, `from_name`, `received`, `message_id`. Files and repos: `path` (one pointer), `paths`. HTTP: `status`, `json`. Loop: `item`, `index`, `total`. Use these names so nodes of different packages fit together. |
| **Environment** | A package whose node needs libraries declares `[python] requires` in `aihub.toml`; the node then runs with the package's own venv (`{python}` = that interpreter, otherwise the UI's). An OS limit goes in `[requires] os = [...]` so the install refuses other systems. A program still checks for what it needs and fails with the fix in `error`. |
| **Errors** | `{"ok": false, "error": "what to do"}`. Never include a secret. |

## Loop and End loop

`flow.loop` (Loop, for each) repeats every node after it once per item of a list, one item after the other:

- **The list**: field *The list* (default `{{input}}`), written as `lines`, `comma`, `regex` (separator) or `json` (a list of strings or objects, also inside a ```` ```json ```` fence). Repeated items are skipped (untick *Skip repeated items*). `limit` (default 100, max 1000) refuses longer lists up front; `delay` pauses between items (rate limits); `on_error` = `stop` (default; the run fails at the first bad item) or `continue`.
- **Inside a pass**: `{{input}}` and `{{data.item}}` = the item, `{{data.index}}` (from 1), `{{data.total}}`. An object item also puts its keys in `{{data.<key>}}`, so `[{"to": "a@x.com", "body": "Hi A"}]` feeds a mail node through `takes`.
- **Body**: everything connected after the loop, up to an `flow.endloop`. Claude / Free chat nodes start a fresh chat for every pass (untick *fresh chat* to share one; long lists then grow the context).
- **End loop (collect)**: runs ONCE after the last pass. `{{input}}` = the last text of each pass joined (`lines`, `blank line` or `json`), `{{data.results}}` = the list, `{{data.count}}`, `{{data.failed}}`. Without it nothing runs after the loop.
- Not supported: a loop inside a loop. Passes run in order, not in parallel. The loop node's log lists every pass with ok / FAILED.

Example: *Start* -> *Claude* ("list the senders of last week, one address per line") -> *Loop* -> *Claude* ("write a thank-you mail for `{{data.item}}`") -> *Send email (Outlook)* (To empty; put `{{data.item}}` in To) -> *End loop* -> *Output*.

## The Outlook package (`aihub-nodes-outlook`, Windows)

The Outlook nodes are not built in. `aihub install aihub-nodes-outlook` refuses other systems (`requires.os = ["windows"]`), creates `~/.aihub/venvs/aihub-nodes-outlook` with `pywin32` and the UI runs these nodes with that python. Needs classic Outlook, signed in (the new Outlook has no COM).

| Node | What it does |
| --- | --- |
| **Start: Outlook mail received** (`outlook-receive`, trigger) | Polls a folder (default every 20 s) and starts a run per NEW mail (only mail that arrives while the work is On) that matches ALL filled conditions, each a case-insensitive regex: *Sender* (tested on `Name <address>`), *Subject*, *Headers* (raw internet headers, e.g. `^List-Id:.*alerts`), *Content* (the body). Options: unread only, mark as read, save attachments. Event text = subject, sender and body; data = `from`, `from_name`, `subject`, `to`, `cc`, `body`, `received`, `attachments`, `message_id`. The generic *Only start when the text matches* filter still applies. |
| **Send email (Outlook)** (`outlook-send`) | Send, save as draft or display. Empty `to` / `cc` / `bcc` / `subject` / `body` / `attachments` take the standard keys from the node before (`takes`); a blank body uses the incoming text. |

Example: *Start: mail received* (Sender `@customer\.com`, Subject `^\[urgent\]`) -> *Claude* ("draft a reply to `{{data.body}}`") -> *Send email* (To `{{data.from}}`, Subject `Re: {{data.subject}}`, Action draft).

## Free chat and MCP servers

Free chat can use stdio MCP servers (from installed packages, the UI's own list, Claude Code's user servers). **MCP servers page > Free chat tools**: *Allowed* (master switch), *On in new chats* (ticked automatically the first time a chat switches to Free chat), *Test / Retry* (starts the server, lists its tools, shows the error when it fails) and a switch per tool to hide it from the model. Stored in `~/.aihub/ui/mcp_state.json`; the engine menu of a chat ticks servers for that chat and shows servers that are switched off.

The model also gets the tool `aihub_mcp` (`status`, `retry`, `enable`, `disable`; optional `server`, `tool`). So in a chat you can say "the github server failed, try again", "turn off the delete tools of filesystem" or "which tools do you have?" and the model does it; a change is kept for that chat (`mcp`, `mcp_off` fields). A server switched off on the MCP page cannot be re-enabled by the model. A failed start is noted in the chat; a tool error that looks like a dead server tells the model to retry.

## Updating aihub-ui by itself

A thread (`aihubui/selfupdate.py`) asks the hub for the newest `aihub-ui` every `UI_UPDATE_MINUTES` (default 360). Settings > Updates (pref `auto_update`): **Update and restart** (default), **Only tell me**, **Off**; *Check now* forces a check. With a newer version it waits until nothing runs for 60 s (no chat answering, no free chat, no work run, no install terminal), runs `aihub update aihub-ui`, then restarts: a clean exit when started by `aihub run` (the supervisor restarts the service, same address and key) or an in-place exec otherwise. A failed update is reported on the card and tried again later; the running copy is not touched until the update succeeded. `GET /api/update`, `POST /api/update/check`.
