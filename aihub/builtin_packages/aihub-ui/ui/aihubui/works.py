"""Works: node graphs that start on a trigger (manual, cron, webhook, or a package's own event source) and run to the end.

  trigger node ──▶ filter / template / Claude task / package action nodes ──▶ ...

* One run = one trigger event. The event text flows along the edges; a node runs when at least one incoming branch is active.
  A filter that does not match ends its branch (that is how "react only to messages matching REGEX" works). Every trigger also gets a
  built-in `filter` regex that is applied BEFORE a run is created, so package authors do not have to implement it.
* Nodes that do not depend on each other (parallel branches) run at the same time, at most WORKERS. A node with several parents waits for all of them;
  it sees their outputs in edge order: `{{input}}` = the texts joined under `## From <node>` headings, `{{in.1}}` / `{{in.<node>}}` = one parent.
* `ai.claude` nodes run the Claude Code harness (chat.turn) with the node's model / effort / mode; all Claude nodes of a run share one chat,
  so context carries over and the prompt cache stays warm. Anything else (send mail, post to Teams ...) is either a Claude task that uses a skill
  or a PACKAGE NODE: `<package>/nodes/<id>.node.json` + a program, see docs/ui-nodes.md.
* Secrets typed into a package node's `secret` fields are encrypted (secretbox) and handed to the program on stdin only.
* Runs are recorded under ~/.aihub/ui/runs/<work>/<run>.json (last 40 kept)."""
import collections
import concurrent.futures as cf
import datetime
import hashlib
import hmac
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import uuid

from . import aihub as A, chat, sources, store
from .secretbox import write_private

MARK = "__stored__"                      # what a stored secret looks like inside the work file
KEEP_RUNS = 40
WORKERS = 4                              # nodes of one run that may run at the same time (branches that do not depend on each other)
OUT_KINDS = ("text", "path", "json", "url")
NODE_ID = re.compile(r"^[a-z][a-z0-9_.-]{0,40}$")
KINDS = ("trigger", "action")
FIELD_TYPES = ("text", "textarea", "number", "checkbox", "select", "password", "model", "effort", "mode", "folder", "code")
FILTER = {"name": "filter", "label": "Only start when the text matches (regex)", "type": "text", "default": "", "help": "Checked first; empty = every event starts a run."}

TEAMS_OUT_GUIDE = """Send a message to Teams (no app registration, no admin consent):
1. In Teams open the Workflows app (or Power Automate) > Create > the template "Post to a channel when a webhook request is received" (for a chat: "Post to a chat when a webhook request is received").
2. Pick the team and channel (or chat), name the flow, create it.
3. Copy the "HTTP POST URL" it shows. It starts with https:// and contains a signature: treat it like a password.
4. Paste it into "Workflow URL" here. It is encrypted on this machine and never shown again.
5. Payload "card" matches that template. Choose "text" only if your own flow reads {"text": ...}.
Test: put Start: manual > Send to Teams, set Message, press Run."""

TEAMS_IN_GUIDE = """Start a work from a Teams message. Teams cannot call your laptop, so a Power Automate flow forwards messages to a tunnel that points at this node's port.
1. Pick a Local port (8791) and type a long random Shared secret. Turn the work on: the node now listens on 127.0.0.1:PORT.
2. Expose only that port: `devtunnel host -p 8791 --allow-anonymous` (Microsoft Dev Tunnels), or `cloudflared tunnel --url http://127.0.0.1:8791`, or `ngrok http 8791`. Note the public https address.
3. Power Automate > Create > Automated cloud flow. Trigger: "When a new channel message is added" (channel) or "When a new message is added to a chat" (chat), or "When keywords are mentioned" / "When I am @mentioned".
4. Add the action "HTTP". Method POST. URI: https://YOUR-TUNNEL/teams. Headers: Content-Type = application/json and X-Hook-Secret = your secret.
5. Body: {"text": <Message body content>, "from": <From display name>, "chat": <Channel or chat name>} (pick those from the dynamic content list).
6. Use "Only from senders" / "Only in this chat" here, and the Start filter regex (for example ^deploy), to ignore everything else before a run exists.
The answer can go back with a "Send to Teams" node. Anyone who knows the tunnel address and the secret can start this work: keep both private and turn the tunnel off when you do not need it."""

GIT_READ_GUIDE = """Reads a file or folder from a remote git repository with the logins you already saved for the aihub CLI.
1. Add the host once: Vault page > Add (or `aihub vault add https://git.example.com --user NAME --token-stdin`). The same login serves your terminal, the CLI and this node: aihub's git credential helper answers git's question.
2. Repository: a plain https address (no password in it). Branch, tag or commit: empty = the default branch.
3. Path: a file (its text is the output) or a folder (a listing; tick "Read the text of the files" to include the first 10 files). Empty = the repository root.
It makes a shallow temporary clone, reads, and deletes it. Key, secret and .env files are refused."""

HTTP_GUIDE = """Calls a URL and passes the answer on. {{input}}, {{trigger}}, {{data.key}} work in the URL, headers and body.
Put a token in "Authorization value" (for example `Bearer abc123`): it is stored encrypted and never shown again. Other headers are one `Name: value` per line and are saved in the work, so keep secrets out of them.
JSON answers are pretty-printed; "Pick" takes one value (`data.items.0.name`). Output data: {{data.status}}. A 4xx/5xx answer fails the run unless you untick that."""

GIT_PULL_GUIDE = """Pulls a git repository first and passes on WHERE the files are, never what is in them.
1. Log in once in Vault (or `aihub vault add https://git.example.com --user NAME --token-stdin`), as for Read from git.
2. The repository is cloned (or fast-forwarded when it was pulled before) into ~/.aihub/workingspace/data/git/NAME. Git LFS files are fetched for real.
3. The output is the full path (one per line when you list several sub paths). {{input}} in the next node is that path; Claude (it may read the folder) opens the parquet, csv, images ... itself with its own tools.
4. Next: a Claude task that analyses the data and writes a dashboard, then Publish to Space."""

PYFN_SAMPLE = """from pydantic import BaseModel


class In(BaseModel):
    text: str
    times: int = 2


class Out(BaseModel):
    result: str
    length: int


def main(args: In) -> Out:
    result = args.text * args.times
    return Out(result=result, length=len(result))
"""
PYFN_GUIDE = ("Write one function (`main`, else `run`, else the last one) with ONE argument and a return, both typed as a pydantic BaseModel or a dict / TypedDict. "
              "The argument is filled for you: {{data.x}} keys of the nodes before, the input text when it is a JSON object (a plain text input fills a field named `input` or `text`), then the Arguments field. "
              "The return becomes this node's output (JSON); each key is {{data.key}} later. print() goes to the run log. "
              "Runs on your machine with the UI's Python (pydantic must be installed for it: python -m pip install pydantic). Dict-only code needs nothing.")
SPACE_GUIDE = """Publishes a file or folder to the hub as a Space (document, slides, report, PDF, sheet, dashboard, HTML page).
- File or folder: the path to publish. Empty = the path the previous node ended with (a Claude task should end its answer with `OUTPUT: /full/path`). A folder must have index.html.
- Space name: lower-case slug. The same name again publishes a new revision of that space; empty = named after the file.
- Private by default; choose public to let anyone with the link read it. Needs `aihub login` to the hub.
Output: the space reference and its link."""

MAIL_PKG_NODE = "pkg:aihub-nodes-outlook/outlook-send"
MAIL_KEYS = ("to", "cc", "bcc", "subject", "body", "attachments")      # the standard data keys of a mail node (docs/ui-nodes.md, Node contract)
LOOP_GUIDE = """Repeats the nodes after it for every item of a list, for example one mail per sender.
1. Put the list in the node before (a Claude task that answers one address per line, an HTTP request, a Read file ...), or type it in "The list".
2. Everything connected after the loop runs once per item, in order. Inside: {{input}} and {{data.item}} are the item; {{data.index}} (from 1) and {{data.total}}.
3. A JSON list of objects such as [{"to": "a@x.com", "subject": "Hi A"}, ...] puts every key in {{data.<key>}}; nodes that take such a key (Send email takes to, subject, body ...) use it when their own field is empty.
4. Add "End loop (collect)" after the last node of the body when something must run once afterwards (a summary, Output). Without it the loop is the last thing in its branch.
Limits: items are capped (default 100, max 1000), nested loops are not supported, a failed item stops the loop unless you choose "continue"."""

BUILTIN = {
    "start.manual": {"title": "Start: manual", "kind": "trigger", "group": "Start", "desc": "Run it with the Run button, or from a chat with /work:NAME. Inputs (optional) become a form in the chat; later nodes read them as {{data.name}}.",
                     "fields": [{"name": "inputs", "label": "Inputs asked when run from a chat", "type": "textarea", "default": "", "help": "One per line: name|Label|type|options or default|*  (type text, textarea, number, select, checkbox; * = required). Example: topic|Topic|text||*"}]},
    "start.cron": {"title": "Start: schedule", "kind": "trigger", "group": "Start", "desc": "Cron expression (0 8 * * 1-5) or interval (30s, 5m, 2h, 1d). Fires while the UI service runs and the work is on.",
                   "fields": [{"name": "schedule", "label": "Schedule", "type": "text", "default": "0 9 * * *", "required": True}, {"name": "text", "label": "Event text", "type": "text", "default": "scheduled run"}]},
    "start.webhook": {"title": "Start: webhook", "kind": "trigger", "group": "Start", "desc": "POST text or JSON {text, data} to the node's local URL (127.0.0.1 only).", "fields": []},
    "ai.claude": {"title": "Claude task", "kind": "action", "group": "AI", "desc": "Runs Claude Code with the prompt. Use {{input}}, {{trigger}}, {{data.key}}. Skills, agents and MCP tools of aihub are available (send mail, post a message ...).",
                  "fields": [{"name": "prompt", "label": "Prompt", "type": "textarea", "required": True, "default": ""}, {"name": "model", "label": "Model", "type": "model", "default": ""},
                             {"name": "effort", "label": "Effort", "type": "effort", "default": ""}, {"name": "mode", "label": "Permission mode", "type": "mode", "default": "auto"},
                             {"name": "cwd", "label": "Working folder (optional)", "type": "folder", "default": "", "assist": ["input", "data", "file"],
                              "help": "Claude opens in this folder. Type @ to pick one, or use {{data.path}} from the node before. Empty = a private folder. A node with a folder runs in a chat of its own."},
                             {"name": "fresh", "label": "Fresh chat (do not share context with earlier Claude nodes)", "type": "checkbox", "default": False}]},
    "flow.filter": {"title": "Filter (regex)", "kind": "action", "group": "Flow", "desc": "Passes the text on only when it matches; otherwise this branch ends.",
                    "fields": [{"name": "regex", "label": "Regex", "type": "text", "required": True, "default": ""}, {"name": "invert", "label": "Pass when it does NOT match", "type": "checkbox", "default": False}]},
    "flow.template": {"title": "Text template", "kind": "action", "group": "Flow", "desc": "Builds new text from {{input}}, {{trigger}}, {{data.key}}.", "fields": [{"name": "template", "label": "Template", "type": "textarea", "default": "{{input}}"}]},
    "flow.output": {"title": "Output", "kind": "action", "group": "Flow", "desc": "What the run answers with. A chat shows it under your /work: message. Without an Output node the last node's text is the answer.",
                    "fields": [{"name": "title", "label": "Heading (optional)", "type": "text", "default": ""}, {"name": "template", "label": "Answer (markdown)", "type": "textarea", "default": "{{input}}", "help": "{{input}}, {{trigger}}, {{data.key}}"}]},
    "flow.memory": {"title": "Memory", "kind": "action", "group": "Flow", "desc": "Adds remembered context to the text: a chat, this work's earlier runs, or another work's runs.",
                    "fields": [{"name": "source", "label": "Remember from", "type": "select", "options": ["work", "chat", "other"], "default": "work",
                                "help": "work = earlier runs of this work; chat = messages of a chat; other = another work's runs."},
                               {"name": "chat", "label": "Chat id (source = chat; empty = latest chat)", "type": "text", "default": ""},
                               {"name": "work", "label": "Work id (source = other)", "type": "text", "default": ""},
                               {"name": "limit", "label": "How many items", "type": "number", "default": 5, "help": "Runs or chat messages, newest first (1-50)."},
                               {"name": "chars", "label": "Max characters", "type": "number", "default": 6000},
                               {"name": "mode", "label": "Output", "type": "select", "options": ["prepend", "memory only"], "default": "prepend", "help": "prepend = memory, then the incoming text."}]},
    "start.teams": {"title": "Start: Teams message", "kind": "trigger", "group": "Teams", "desc": "Starts when a Power Automate flow forwards a Teams message to this node's own listener (127.0.0.1, secret required).",
                    "run": ["{python}", "{pkg}/nodes/teams_in.py"], "timeout": 60,
                    "guide": TEAMS_IN_GUIDE,
                    "fields": [{"name": "port", "label": "Local port", "type": "number", "default": 8791, "required": True, "help": "Expose only this port through your tunnel."},
                               {"name": "secret", "label": "Shared secret", "type": "password", "required": True, "help": "12+ random characters. Power Automate sends it as header X-Hook-Secret or ?key=."},
                               {"name": "from_regex", "label": "Only from senders matching (regex)", "type": "text", "default": ""},
                               {"name": "chat", "label": "Only in this chat or channel (exact name)", "type": "text", "default": ""},
                               {"name": "strip_html", "label": "Turn Teams HTML into plain text", "type": "checkbox", "default": True}]},
    "send.teams": {"title": "Send to Teams", "kind": "action", "group": "Teams", "desc": "Posts a message to a channel or chat through a Teams Workflows webhook URL.",
                   "run": ["{python}", "{pkg}/nodes/teams_out.py"], "timeout": 60,
                   "guide": TEAMS_OUT_GUIDE,
                   "fields": [{"name": "webhook_url", "label": "Workflow URL", "type": "password", "required": True, "help": "From the Workflows app: 'When a Teams webhook request is received'. Stored encrypted."},
                              {"name": "title", "label": "Title (optional)", "type": "text", "default": ""},
                              {"name": "text", "label": "Message", "type": "textarea", "default": "{{input}}", "required": True},
                              {"name": "payload", "label": "Payload", "type": "select", "options": ["card", "text"], "default": "card", "help": "card = Adaptive Card (the Teams template). text = {\"text\": ...} for your own flow."}]},
    "code.python": {"title": "Python function", "kind": "action", "group": "Code", "desc": "Runs a Python function you write. Type its argument and its return as a pydantic model or a dict: the input is parsed into the argument and the return becomes JSON for the next nodes.",
                    "run": ["{python}", "{pkg}/nodes/python_fn.py"], "timeout": 300, "timeout_key": "timeout", "templated": ["args"], "guide": PYFN_GUIDE,
                    "fields": [{"name": "code", "label": "Python code", "type": "code", "required": True, "default": PYFN_SAMPLE},
                               {"name": "args", "label": "Arguments (optional JSON object)", "type": "text", "default": "", "help": "Overrides the parsed input, e.g. {\"n\": {{data.count}}}. {{input}} and {{data.x}} are filled in."},
                               {"name": "timeout", "label": "Timeout (seconds)", "type": "number", "default": 60}]},
    "io.read_file": {"title": "Read file", "kind": "action", "group": "Data", "desc": "Reads a local text file (or lists a folder) and passes the text on. Key, secret and .env files are refused.",
                     "run": ["{python}", "{pkg}/nodes/read_file.py"], "timeout": 60, "templated": ["path"],
                     "fields": [{"name": "path", "label": "File or folder", "type": "text", "required": True, "default": "", "help": "~ works; a relative path starts in the run's working folder. {{data.x}} is filled in."},
                                {"name": "parse", "label": "Treat as", "type": "select", "options": ["text", "json"], "default": "text"},
                                {"name": "max_chars", "label": "Max characters", "type": "number", "default": 20000},
                                {"name": "read_files", "label": "For a folder: include the text of its first files", "type": "checkbox", "default": False}]},
    "io.request": {"title": "HTTP request", "kind": "action", "group": "Data", "desc": "Calls a URL (GET, POST ...) and passes the response on. Data: status, json.",
                   "run": ["{python}", "{pkg}/nodes/http_request.py"], "timeout": 330, "templated": ["url", "headers", "body", "pick"], "guide": HTTP_GUIDE,
                   "fields": [{"name": "method", "label": "Method", "type": "select", "options": ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"], "default": "GET"},
                              {"name": "url", "label": "URL", "type": "text", "required": True, "default": "", "help": "http:// or https://"},
                              {"name": "authorization", "label": "Authorization value (secret)", "type": "password", "help": "Sent as the Authorization header, e.g. Bearer abc123. Stored encrypted."},
                              {"name": "headers", "label": "Other headers", "type": "textarea", "default": "", "help": "One per line: Name: value. Saved in the work, no secrets here."},
                              {"name": "body_type", "label": "Body type", "type": "select", "options": ["json", "form", "text"], "default": "json"},
                              {"name": "body", "label": "Body", "type": "textarea", "default": ""},
                              {"name": "pick", "label": "Pick from a JSON answer (a.b.0.c)", "type": "text", "default": ""},
                              {"name": "pretty", "label": "Pretty-print JSON", "type": "checkbox", "default": True},
                              {"name": "fail_on_error", "label": "Fail the run on HTTP 4xx / 5xx", "type": "checkbox", "default": True},
                              {"name": "timeout", "label": "Timeout (seconds)", "type": "number", "default": 30}]},
    "git.read": {"title": "Read from git", "kind": "action", "group": "Data", "desc": "Reads a file or folder from a remote git repository with the logins saved in the Vault (the aihub CLI's git credentials).",
                 "run": ["{python}", "{pkg}/nodes/git_read.py"], "timeout": 300, "templated": ["url", "ref", "path"], "guide": GIT_READ_GUIDE,
                 "fields": [{"name": "url", "label": "Repository (https)", "type": "text", "required": True, "default": "", "help": "https://github.com/org/repo.git"},
                            {"name": "ref", "label": "Branch, tag or commit", "type": "text", "default": ""},
                            {"name": "path", "label": "File or folder in the repo", "type": "text", "default": "", "help": "Empty = the root folder"},
                            {"name": "parse", "label": "Treat a file as", "type": "select", "options": ["text", "json"], "default": "text"},
                            {"name": "max_chars", "label": "Max characters", "type": "number", "default": 20000},
                            {"name": "read_files", "label": "For a folder: include the text of its first files", "type": "checkbox", "default": False}]},
    "ai.free": {"title": "Free chat", "kind": "action", "group": "AI", "desc": "Asks a model through the API directly (no Claude Code): cheap, no tools of its own. Tick MCP servers to give it tools; pointers are passed as paths, or read as text when you ask.",
                "fields": [{"name": "prompt", "label": "Prompt", "type": "textarea", "required": True, "default": ""}, {"name": "model", "label": "Model (empty = Settings default)", "type": "model", "default": ""},
                           {"name": "effort", "label": "Effort", "type": "effort", "default": ""},
                           {"name": "mcp", "label": "MCP servers to switch on (names, comma separated)", "type": "text", "default": "", "help": "Names from the MCP page. It then runs the usual tool loop, for example a filesystem MCP server to open the path it is given."},
                           {"name": "read_pointers", "label": "Paste the text of small files named by the input paths (max 30 KB)", "type": "checkbox", "default": False},
                           {"name": "fresh", "label": "Fresh chat (do not share context with earlier Free chat nodes)", "type": "checkbox", "default": False}]},
    "git.pull": {"title": "Read from git (path)", "kind": "action", "group": "Data", "desc": "Like Read from git, but in ONE node it pulls the repository and passes on the PATH of the file or folder you pick, not the content. The next node (Claude, Free chat, Publish) opens it.",
                 "run": ["{python}", "{pkg}/nodes/git_pull.py"], "timeout": 330, "templated": ["url", "ref", "paths"], "guide": GIT_PULL_GUIDE,
                 "fields": [{"name": "url", "label": "Repository (https)", "type": "text", "required": True, "default": "", "help": "https://github.com/org/data.git"},
                            {"name": "ref", "label": "Branch, tag or commit", "type": "text", "default": ""},
                            {"name": "paths", "label": "File or folder inside the repo (one per line for several)", "type": "textarea", "default": "", "help": "Each becomes a pointer. Empty = the whole repository folder."}]},
    "io.path": {"title": "File / folder (path)", "kind": "action", "group": "Data", "desc": "Points at local files or folders and passes on their PATH, not the content. * and ? patterns work.",
                "run": ["{python}", "{pkg}/nodes/path_pointer.py"], "timeout": 60, "templated": ["path"],
                "fields": [{"name": "path", "label": "File, folder or pattern (one per line)", "type": "textarea", "required": True, "default": "", "help": "~ works; a relative path starts in the run's working folder."},
                           {"name": "must_exist", "label": "Fail when nothing is found", "type": "checkbox", "default": True}]},
    "space.publish": {"title": "Publish to Space", "kind": "action", "group": "Hub", "desc": "Publishes a document, slides, report, PDF, sheet, dashboard or HTML page (a file, or a folder with index.html) as a Space on the hub; the same name updates it.",
                      "run": ["{python}", "{pkg}/nodes/space_publish.py"], "timeout": 930, "templated": ["path", "name", "title", "description"], "guide": SPACE_GUIDE,
                      "fields": [{"name": "path", "label": "File or folder (empty = the path from the previous node)", "type": "text", "default": "{{input}}"},
                                 {"name": "name", "label": "Space name (slug; same name = new revision)", "type": "text", "default": ""},
                                 {"name": "kind", "label": "Kind", "type": "select", "options": ["auto", "doc", "slides", "report", "pdf", "sheet", "dashboard", "html"], "default": "auto"},
                                 {"name": "title", "label": "Title", "type": "text", "default": ""},
                                 {"name": "description", "label": "One line description", "type": "text", "default": ""},
                                 {"name": "visibility", "label": "Who can open it", "type": "select", "options": ["private", "public"], "default": "private"}]},
    "send.email": {"title": "Send email (moved)", "kind": "action", "group": "Mail", "legacy": True, "hidden": True,
                   "desc": "Outlook mail moved to the package aihub-nodes-outlook (aihub install aihub-nodes-outlook). With it installed this node keeps working.",
                   "run": ["{python}", "{pkg}/nodes/moved.py"], "timeout": 20, "templated": ["to", "cc", "bcc", "subject", "body", "attachments"], "takes": list(MAIL_KEYS),
                   "fields": [{"name": "to", "label": "To", "type": "text", "default": ""}, {"name": "cc", "label": "Cc", "type": "text", "default": ""}, {"name": "bcc", "label": "Bcc", "type": "text", "default": ""},
                              {"name": "subject", "label": "Subject", "type": "text", "default": ""}, {"name": "body", "label": "Body", "type": "textarea", "default": ""},
                              {"name": "format", "label": "Body format", "type": "select", "options": ["text", "html"], "default": "text"},
                              {"name": "attachments", "label": "Attachments (one path per line)", "type": "textarea", "default": ""},
                              {"name": "action", "label": "Action", "type": "select", "options": ["send", "draft", "display"], "default": "send"}]},
    "flow.loop": {"title": "Loop (for each)", "kind": "action", "group": "Flow", "guide": LOOP_GUIDE,
                  "desc": "Runs everything after it ONCE PER ITEM of a list (senders, files, rows ...), one item after the other, until an End loop node. Each pass sees {{input}} / {{data.item}} = the item, {{data.index}}, {{data.total}}; a JSON object item also puts its keys in {{data.<key>}}.",
                  "fields": [{"name": "items", "label": "The list", "type": "textarea", "default": "{{input}}", "help": "Usually the text of the node before. Lines, a comma list, a regex split or a JSON list (strings or objects)."},
                             {"name": "split", "label": "How the list is written", "type": "select", "options": ["lines", "comma", "json", "regex"], "default": "lines"},
                             {"name": "regex", "label": "Separator regex (split = regex)", "type": "text", "default": ""},
                             {"name": "unique", "label": "Skip repeated items", "type": "checkbox", "default": True},
                             {"name": "limit", "label": "At most this many items", "type": "number", "default": 100, "help": "1-1000. More items than this stops the loop before it starts."},
                             {"name": "delay", "label": "Pause between items (seconds)", "type": "number", "default": 0, "help": "For mail or APIs with rate limits."},
                             {"name": "on_error", "label": "When one item fails", "type": "select", "options": ["stop", "continue"], "default": "stop"},
                             {"name": "fresh_chat", "label": "Claude / Free chat nodes start a fresh chat for every item", "type": "checkbox", "default": True}]},
    "flow.endloop": {"title": "End loop (collect)", "kind": "action", "group": "Flow",
                     "desc": "Closes a Loop. Runs once after the last item with the result of every pass (the last text of each) as {{input}}; {{data.results}} is the JSON list, {{data.count}} / {{data.failed}} the numbers.",
                     "fields": [{"name": "join", "label": "Join the results as", "type": "select", "options": ["json", "lines", "blank line"], "default": "lines"}]},
    "flow.note": {"title": "Note", "kind": "action", "group": "Flow", "desc": "A comment on the canvas. Passes its input through.", "fields": [{"name": "text", "label": "Note", "type": "textarea", "default": ""}]},
}


# ---------- node catalogue (builtin + packages)

_cat = {"t": 0.0, "v": {}}
ASSIST = ("input", "data", "file", "skill")                   # what a field may suggest + highlight: {{input}}/{{trigger}}, {{data.key}}, @file, /skill
BUILTIN_OUTPUTS = {"git.pull": ["path"], "io.path": ["path"], "space.publish": ["url"]}
CLAUDE_OUT = "the reply text. To hand a file or folder on, make Claude end the reply with `OUTPUT: /full/path`"
# what a node hands on (`out`: text|path|json|url, `out_note`) and what it needs from the nodes before it (`needs`, `needs_note`); shown in the editor
BUILTIN_IO = {
    "start.manual": ("text", "the text you type; your inputs are {{data.name}}", "", ""),
    "start.cron": ("text", "the text you set", "", ""),
    "start.webhook": ("text", "the POSTed text (data too, if sent)", "", ""),
    "start.teams": ("text", "the message text", "", ""),
    "ai.claude": ("text", CLAUDE_OUT, "", ""),
    "ai.free": ("text", "the reply text", "", ""),
    "flow.filter": ("text", "the same input, or nothing (the branch stops)", "", ""),
    "flow.note": ("text", "the same input", "", ""),
    "flow.template": ("text", "the template filled in", "", ""),
    "flow.output": ("text", "the run's answer", "", ""),
    "flow.memory": ("text", "remembered context + the input", "", ""),
    "io.read_file": ("text", "the file's text", "", ""),
    "code.python": ("json", "the function's return value as JSON; each key is {{data.key}}", "", ""),
    "io.request": ("json", "the answer text; {{data.status}}", "", ""),
    "git.read": ("text", "the file text or folder listing", "", ""),
    "git.pull": ("path", "the folder or file path, not its content", "", ""),
    "io.path": ("path", "the path(s), not the content", "", ""),
    "space.publish": ("url", "the space reference and link", "path", "a file or folder path: a path node before it, or a Claude task that ends with OUTPUT: /full/path"),
    "send.email": ("text", "what was sent", "", ""),
    "flow.loop": ("text", "the current item, once per pass", "", ""),
    "flow.endloop": ("text", "the results of all passes", "", ""),
    "send.teams": ("text", "what was sent", "", ""),
}


def local_dir(*a):
    return store.ui_dir("nodes", *a)


def _default_assist(typ, spec):
    """Fields that take free text get {{input}}/{{data}} help; the prompt of an AI node also gets @file and /skill."""
    for f in spec["fields"]:
        if f.get("assist"):
            continue
        if typ in ("ai.claude", "ai.free") and f["name"] in ("prompt", "system"):
            f["assist"] = list(ASSIST)
        elif not f.get("secret") and (f["type"] == "textarea" or f["name"] in spec.get("templated", [])):
            f["assist"] = ["input", "data"]


def _names(v):
    return {str(x) for x in v if isinstance(x, str)} if isinstance(v, list) else set()


def python_of(spec):
    """The interpreter a package node runs with: the package's own venv (its [python] requires, e.g. pywin32) when it has one, else the UI's."""
    v = spec.get("venv")
    if v:
        p = os.path.join(v, "Scripts", "python.exe") if os.name == "nt" else os.path.join(v, "bin", "python")
        if os.path.exists(p):
            return p
    return sys.executable


def _argv(spec):
    return [python_of(spec) if x == "{python}" else x.replace("{pkg}", spec["dir"]) for x in spec["run"]]


def _valid_spec(pkg, path, raw):
    try:
        nid, kind = raw["id"], raw["kind"]
        run = raw["run"]
        if not NODE_ID.match(nid) or kind not in KINDS or not isinstance(run, list) or not run or not all(isinstance(x, str) for x in run):
            return None
        fields = []
        for f in raw.get("fields", []):
            if not isinstance(f, dict) or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", str(f.get("name", ""))):
                continue
            t = f.get("type", "text")
            fields.append({"name": f["name"], "label": str(f.get("label", f["name"]))[:100], "type": t if t in FIELD_TYPES else "text", "default": f.get("default", ""),
                           "required": bool(f.get("required")), "secret": bool(f.get("secret")) or t == "password", "help": str(f.get("help", ""))[:300],
                           "options": [str(o) for o in f.get("options", [])][:50],
                           "assist": [a for a in (f.get("assist") if isinstance(f.get("assist"), list) else []) if a in ASSIST][:4]})
        spec = {"type": "pkg:%s/%s" % (pkg["name"], nid), "title": str(raw.get("title", nid))[:60], "kind": kind, "group": str(raw.get("group") or pkg["name"])[:30],
                "desc": str(raw.get("description", ""))[:400], "fields": fields, "package": pkg["name"], "run": run, "dir": pkg["path"],
                "timeout": min(max(int(raw.get("timeout", 120)), 5), 3600), "guide": str(raw.get("guide", ""))[:4000],
                "out": raw.get("out") if raw.get("out") in OUT_KINDS else "text", "out_note": str(raw.get("out_note", ""))[:160],
                "needs": raw.get("needs") if raw.get("needs") in OUT_KINDS else "", "needs_note": str(raw.get("needs_note", ""))[:160],
                "outputs": [str(o)[:40] for o in raw.get("outputs", []) if isinstance(o, str) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", o)][:20] if isinstance(raw.get("outputs"), list) else [],
                "takes": [f["name"] for f in fields if f["name"] in _names(raw.get("takes")) and not f["secret"]][:20],
                "venv": str(pkg.get("venv") or ""), "templated": [x for x in raw.get("templated", []) if isinstance(x, str)][:20] if isinstance(raw.get("templated"), list) else []}
        return spec
    except (KeyError, TypeError, ValueError):
        return None


CAT_TTL = 60
_cat_lock = threading.Lock()


def catalogue_reset():
    """Forget the cached catalogue (the server calls this after an install / uninstall)."""
    _cat.update(t=0.0, v={})


def catalogue(force=False):
    """-> {type: spec}. Builtin nodes plus every `nodes/*.node.json` of an installed, enabled package. Cached 60 s, one rebuild at a time;
    a catalogue built while the CLI failed is returned but not cached."""
    if not force and time.time() - _cat["t"] < CAT_TTL and _cat["v"]:
        return _cat["v"]
    with _cat_lock:
        if not force and time.time() - _cat["t"] < CAT_TTL and _cat["v"]:
            return _cat["v"]
        return _build_catalogue()


def _build_catalogue():
    ok = True
    out = {k: dict(v, type=k, builtin=True, **({"dir": UI_DIR} if v.get("run") else {})) for k, v in BUILTIN.items()}
    try:
        pkgs = A.run(["list"]).get("data") or []
    except A.CliError:
        pkgs, ok = [], False
    for p in pkgs:
        d = os.path.join(p.get("path") or "", "nodes")
        if p.get("enabled", True) is False or not os.path.isdir(d):
            continue
        for f in sorted(os.listdir(d)):
            if f.endswith(".node.json"):
                try:
                    with open(os.path.join(d, f), encoding="utf-8") as fh:
                        spec = _valid_spec(p, d, json.load(fh))
                except (OSError, ValueError):
                    spec = None
                if spec:
                    out[spec["type"]] = spec
    ext = out.get(MAIL_PKG_NODE)
    if ext:                                                              # old works keep their send.email node: it now runs the external package
        out["send.email"] = dict(ext, type="send.email", legacy=True, hidden=True, title="Send email (Outlook)", group="Mail")
    try:
        for nid in sorted(os.listdir(local_dir())):
            d = local_dir(nid)
            try:
                with open(os.path.join(d, "node.json"), encoding="utf-8") as fh:
                    spec = _valid_spec({"name": "local", "path": d}, d, json.load(fh))
            except (OSError, ValueError):
                spec = None
            if spec:
                spec["local"] = True
                out[spec["type"]] = spec
    except OSError:
        pass
    for t, s in out.items():
        if t in BUILTIN_OUTPUTS:
            s["outputs"] = BUILTIN_OUTPUTS[t]
        if t in BUILTIN_IO:
            s["out"], s["out_note"], s["needs"], s["needs_note"] = BUILTIN_IO[t]
    for s in out.values():
        s["fields"] = [dict(f, secret=bool(f.get("secret")) or f.get("type") == "password") for f in s["fields"]]
    for t, s in out.items():
        _default_assist(t, s)
    for s in out.values():
        if s["kind"] == "trigger" and not any(f["name"] == "filter" for f in s["fields"]):
            s["fields"] = s["fields"] + [FILTER]
    if ok:
        _cat.update(t=time.time(), v=out)
    return out


def public(spec):
    return dict({k: v for k, v in spec.items() if k not in ("run", "dir", "venv")}, runnable=runnable(spec))


UI_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------- cron / interval

NAMES = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
         "sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}


def _field(txt, lo, hi, names=False):
    out = set()
    if names:
        txt = re.sub(r"[A-Za-z]{3}", lambda m: str(NAMES[m.group(0).lower()]) if m.group(0).lower() in NAMES else m.group(0), txt)
    for part in txt.split(","):
        step = 1
        if "/" in part:
            part, st = part.split("/", 1)
            step = int(st)
        if part in ("*", ""):
            a, b = lo, hi
        elif "-" in part:
            a, b = [int(x) for x in part.split("-", 1)]
        else:
            a = int(part)
            b = hi if step > 1 else a
        if step < 1 or a < lo or b > hi or a > b:
            raise ValueError("bad cron field %r" % txt)
        out.update(range(a, b + 1, step))
    return out


def parse_schedule(text):
    """-> ("every", seconds) or ("cron", [minute, hour, dom, month, dow] sets). Raises ValueError."""
    t = (text or "").strip()
    m = re.match(r"^(\d+)\s*([smhd]?)$", t)
    if m:
        n = int(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]
        if n < 10:
            raise ValueError("interval must be at least 10 seconds")
        return "every", n
    f = t.split()
    if len(f) != 5:
        raise ValueError("schedule: 5 cron fields (m h dom mon dow) or an interval like 5m")
    mon = _field(f[3], 1, 12, True)
    sets = [_field(f[0], 0, 59), _field(f[1], 0, 23), _field(f[2], 1, 31), mon, {d % 7 for d in _field(f[4], 0, 7, True)}]
    return "cron", sets + [not f[2].startswith("*"), not f[4].startswith("*")]


def cron_due(sets, dt):
    mi, hr, dom, mon, dow, dom_star_not, dow_star_not = sets
    if dt.minute not in mi or dt.hour not in hr or dt.month not in mon:
        return False
    d_ok, w_ok = dt.day in dom, ((dt.weekday() + 1) % 7) in dow
    return (d_ok or w_ok) if (dom_star_not and dow_star_not) else (d_ok and w_ok)


# ---------- secrets of package nodes

def _skey(wid, nid, field):
    return "node:%s:%s:%s" % (wid, nid, field)


SECRETISH = re.compile(r"secret|token|password|passwd|key|auth|webhook", re.I)


def strip_secrets(work, box):
    """Move typed secret values of package nodes into the encrypted box; the work file keeps a marker. Returns the cleaned work."""
    cat = catalogue()
    for n in work["nodes"]:
        spec = cat.get(n.get("type"))
        cfg = n.setdefault("config", {})
        if not spec:                                                   # catalogue unavailable / package removed: never keep a secret-looking value in plain text
            for k, v in list(cfg.items()):
                if SECRETISH.search(k) and isinstance(v, str) and v and v != MARK:
                    box.put(_skey(work["id"], n["id"], k), v)
                    cfg[k] = MARK
            continue
        for f in spec["fields"]:
            if f.get("secret"):
                v = cfg.get(f["name"])
                if isinstance(v, str) and v and v != MARK:
                    box.put(_skey(work["id"], n["id"], f["name"]), v)
                    cfg[f["name"]] = MARK
                elif not v:
                    cfg.pop(f["name"], None)
                    box.remove(_skey(work["id"], n["id"], f["name"]))
    return work


def with_secrets(work_id, node, spec, box):
    cfg = dict(node.get("config") or {})
    allv = None
    for f in spec["fields"]:
        if f.get("secret") and cfg.get(f["name"]) == MARK:
            allv = allv if allv is not None else box.get_all()
            cfg[f["name"]] = allv.get(_skey(work_id, node["id"], f["name"]), "")
    return cfg


# ---------- templating

def render(tpl, ctx):
    def val(m):
        cur = ctx
        for k in m.group(1).split("."):
            cur = cur.get(k) if isinstance(cur, dict) else None
            if cur is None:
                return ""
        return cur if isinstance(cur, str) else json.dumps(cur, ensure_ascii=False)
    return re.sub(r"\{\{\s*([\w.]+)\s*\}\}", val, tpl or "")


def slug(label):
    return re.sub(r"[^a-z0-9]+", "_", str(label or "").lower()).strip("_")[:30] or "node"


def names_of(work, cat):
    """-> {node id: (label, slug)}; the slug is what `{{in.<slug>}}` uses (unique per work: a second 'claude' is 'claude_2')."""
    out, used = {}, set()
    for n in work["nodes"]:
        label = n.get("label") or (cat.get(n.get("type")) or {}).get("title") or n.get("type") or n["id"]
        base = sg = slug(label)
        i = 1
        while sg in used or sg.isdigit():
            i += 1
            sg = "%s_%d" % (base, i)
        used.add(sg)
        out[n["id"]] = (str(label), sg)
    return out


def uses_input(prompt):
    """Does the prompt place the incoming text itself? ({{input}} or one parent's {{in.x}}); else the engine prepends `Input: ...`."""
    return re.search(r"\{\{\s*(input|in\.[\w]+)\s*\}\}", prompt or "") is not None


def text_of(ev):
    return ev.get("text", "") if isinstance(ev, dict) else str(ev or "")


def accepts(node, ev):
    """The trigger's built-in filter: does this event start a run?"""
    rx = ((node.get("config") or {}).get("filter") or "").strip()
    if not rx:
        return True
    try:
        return re.search(rx, text_of(ev)[:20000], re.I | re.M) is not None
    except re.error:
        return False


# ---------- runs

def runs_dir(wid):
    return store.ui_dir("runs", wid)


def keep_of(wid):
    """History limit of a work (runs kept on disk): the work's own `keep`, else KEEP_RUNS."""
    w = store.get_work(wid) or {}
    try:
        return min(500, max(1, int(w.get("keep") or KEEP_RUNS)))
    except (TypeError, ValueError):
        return KEEP_RUNS


def _save_run(run, trim=True):
    d = runs_dir(run["work"])
    os.makedirs(d, exist_ok=True)
    write_private(os.path.join(d, run["id"] + ".json"), json.dumps(run, ensure_ascii=False).encode("utf-8"))
    if trim:
        _trim_runs(d, keep_of(run["work"]))


def _trim_runs(d, keep):
    """Drop the oldest run files beyond KEEP, and the chats those runs created (work chats only)."""
    files = sorted(f for f in os.listdir(d) if f.endswith(".json"))
    for f in files[:-keep] if keep else []:
        chats = []
        try:
            with open(os.path.join(d, f), encoding="utf-8") as fh:
                r = json.load(fh)
            if r.get("status") in ("queued", "running"):
                continue
            chats = r.get("chats") or ([r["chat"]] if r.get("chat") else [])
        except (OSError, ValueError, AttributeError):
            pass
        try:
            os.remove(os.path.join(d, f))
        except OSError:
            continue
        for sid in chats:
            try:
                if isinstance(sid, str) and (store.get_session(sid) or {}).get("work"):
                    store.delete_session(sid)
            except Exception:
                pass


def list_runs(wid):
    d = runs_dir(wid)
    out = []
    for f in sorted(os.listdir(d), reverse=True)[:keep_of(wid)]:
        try:
            with open(os.path.join(d, f), encoding="utf-8") as fh:
                r = json.load(fh)
            out.append({k: r.get(k) for k in ("id", "status", "started", "ended", "via", "text", "chat", "result")})
        except (OSError, ValueError):
            pass
    return out


def get_run(wid, rid):
    if not store.ID.match(str(wid)) or not re.match(r"^[0-9a-z-]{8,40}$", str(rid)):
        return None
    try:
        with open(os.path.join(runs_dir(wid), rid + ".json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _clip(v, n):
    v = " ".join(str(v or "").split())
    return v if len(v) <= n else v[:n - 1] + "..."


def memory_text(cfg, work_id, run_id=""):
    """Text for a Memory node: earlier runs of this/another work, or the latest messages of a chat. Newest LAST, capped."""
    try:
        n = min(50, max(1, int(cfg.get("limit") or 5)))
        cap = min(40000, max(200, int(cfg.get("chars") or 6000)))
    except (TypeError, ValueError):
        n, cap = 5, 6000
    src, lines = cfg.get("source") or "work", []
    if src == "chat":
        sid = str(cfg.get("chat") or "").strip()
        if not sid:                                              # latest chat that is not a work's own
            cand = [x for x in store.sessions()["sessions"] if not x.get("title", "").startswith("Work:")]
            sid = cand[0]["id"] if cand else ""
        for e in store.history(sid)[-400:]:
            if e.get("k") == "user":
                lines.append("User: " + _clip(e.get("text"), 600))
            elif e.get("k") == "msg":
                t = " ".join(b["text"] for b in e.get("blocks", []) if b.get("type") == "text")
                if t.strip():
                    lines.append("Assistant: " + _clip(t, 800))
        lines = lines[-n * 2:]
    else:
        wid = work_id if src == "work" else str(cfg.get("work") or "")
        if not store.ID.match(wid):
            return ""
        runs, d = [], runs_dir(wid)
        for f in sorted(os.listdir(d), reverse=True):
            if len(runs) >= n:
                break
            r = get_run(wid, f[:-5]) if f.endswith(".json") else None
            if r and r["id"] != run_id and r.get("status") in ("done", "failed"):
                runs.append(r)
        for r in reversed(runs):
            outs = [v for v in r.get("nodes", {}).values() if v.get("status") == "ok" and v.get("output")]
            last = max(outs, key=lambda v: v.get("ended") or 0)["output"] if outs else ""
            last = last.rsplit("\n\nCurrent input:\n", 1)[-1]               # drop an earlier Memory block, or memory would snowball
            lines.append("- %s | in: %s | result: %s" % (time.strftime("%Y-%m-%d %H:%M", time.localtime(r.get("started") or 0)), _clip(r.get("text"), 200), _clip(last, 500) or r.get("status")))
    out = "\n".join(lines)
    return out[-cap:]


def topo(work):
    ids = [n["id"] for n in work["nodes"]]
    ins = {i: [e["from"] for e in work["edges"] if e["to"] == i and e["from"] in ids] for i in ids}
    out, seen = [], set()

    def visit(i, stack=()):
        if i in seen or i in stack:
            return
        for u in ins[i]:
            visit(u, stack + (i,))
        seen.add(i)
        out.append(i)
    for i in ids:
        visit(i)
    return out, ins


def _pointers(cdata, inputs):
    """Existing paths named by upstream data (`path`, `paths`) -- what a pointer node passed on."""
    found = []
    for src in [cdata] + [i.get("data") for i in inputs if isinstance(i, dict)]:
        if not isinstance(src, dict):
            continue
        for p in ([src.get("path")] if src.get("path") else []) + (src.get("paths") if isinstance(src.get("paths"), list) else []):
            if isinstance(p, str) and os.path.isabs(p) and os.path.exists(p) and p not in found:
                found.append(p)
    return found[:20]


def pointer_dirs(cdata, inputs):
    """Folders Claude Code is allowed to read for this node (the pointer, or its parent for a file)."""
    out = []
    for p in _pointers(cdata, inputs):
        d = p if os.path.isdir(p) else os.path.dirname(p)
        if d not in out:
            out.append(d)
    return out


def _first_files(root, n):
    out = []
    for r, ds, fs in os.walk(root):
        ds[:] = sorted(x for x in ds if x not in (".git", "node_modules"))
        out.extend(os.path.join(r, f) for f in sorted(fs))
        if len(out) >= n:
            break
    return out[:n]


def pointer_text(cdata, inputs, limit=30000):
    """Opt-in for Free chat: the text of small files behind the pointers (a folder: its first files), capped."""
    out, left = [], limit
    for p in _pointers(cdata, inputs):
        files = [p] if os.path.isfile(p) else _first_files(p, 20)
        for f in files:
            try:
                if sources._blocked(f) or os.path.getsize(f) > left:
                    continue
                with open(f, encoding="utf-8") as fh:
                    t = fh.read(left)
            except (OSError, UnicodeDecodeError):
                continue
            left -= len(t)
            out.append("\n\n--- %s ---\n%s" % (f, t))
    return "".join(out)


def _kill_tree(p):
    """Kill a child and everything it started (its own process group; Windows: taskkill /T)."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True, timeout=10)
        else:
            os.killpg(p.pid, 9)
    except (OSError, subprocess.SubprocessError):
        try:
            p.kill()
        except OSError:
            pass


def run_package_node(spec, cfg, inputs, ctx, wd):
    argv = _argv(spec)
    limit = spec["timeout"]
    if spec.get("timeout_key"):                                     # a node that lets the person set its own limit
        try:
            limit = min(max(int(cfg.get(spec["timeout_key"]) or limit), 1), 3600)
        except (TypeError, ValueError):
            pass
    payload = json.dumps({"config": cfg, "input": "\n\n".join(text_of(i) for i in inputs), "inputs": inputs, "run": ctx, "workdir": wd}, ensure_ascii=False)
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=spec["dir"], start_new_session=True,
                                env=dict(os.environ, AIHUB_NODE=spec["type"], AIHUB_NODE_WORKDIR=wd))
    except OSError as e:
        raise RuntimeError("cannot start: %s" % e)
    try:
        so, se = proc.communicate(payload, timeout=limit)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            proc.communicate(timeout=5)
        except Exception:
            pass
        raise RuntimeError("timed out after %ds" % limit)
    p = subprocess.CompletedProcess(argv, proc.returncode, so or "", se or "")
    last = next((ln for ln in reversed(p.stdout.strip().splitlines()) if ln.strip().startswith("{")), "")
    try:
        res = json.loads(last) if last else {}
    except ValueError:
        res = {}
    if p.returncode != 0 or res.get("ok") is False:
        raise RuntimeError((res.get("error") or p.stderr.strip()[-400:] or "exit %d" % p.returncode))
    out = res.get("output", "")
    ev = out if isinstance(out, dict) else {"text": out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)}
    if "data" in res:
        ev["data"] = res["data"]
    return ev, (res.get("log") or "")


# ---------- a node run on its own, from a chat (`@node:<type>` + a ```node block)

def runnable(spec):
    """A node a chat can run alone: an action with a program (not Claude / flow nodes, which need a work around them)."""
    return bool(spec) and spec.get("kind") == "action" and bool(spec.get("run")) and not spec.get("hidden")


def node_brief(spec):
    """Prompt text for a mentioned node: what it does, its fields, and the protocol (ask for what is missing, else a ```node block)."""
    lines = []
    for f in spec["fields"]:
        extra = [f["type"]] + (["required"] if f.get("required") else []) + (["options: " + ", ".join(f["options"])] if f.get("options") else []) + (["default: %s" % f["default"]] if f.get("default") not in ("", None) else [])
        if f.get("secret"):
            lines.append("- %s (%s): SECRET. Never ask for it and never write it; the card shows a password box to the user." % (f["name"], f["label"]))
        else:
            lines.append("- %s (%s; %s)%s" % (f["name"], f["label"], ", ".join(extra), (": " + f["help"]) if f.get("help") else ""))
    need = [f for f in spec["fields"] if f.get("required") and not f.get("secret")]
    return ("[The user wants the node \"%s\" (@node:%s) run from this chat. %s%s\nFields:\n%s\n"
            "How to run it: work out each field from the user's message and this conversation. If a required field (%s) is still unknown, ask for ONLY the missing ones with a ```form block "
            "(fields named exactly like the node's, same types/options) and stop; do not guess values. Once every required field is known, answer with ONE ```node block and nothing that depends on its result: "
            "{\"type\":\"%s\",\"config\":{\"field\":\"value\"},\"input\":\"optional text for {{input}}\"} (config holds non-secret fields only, values as the field types expect). "
            "The page shows a card where the user checks the values and presses Run; the result comes back as the next message. Do not run the program yourself.]"
            % (spec["title"], spec["type"], spec.get("desc") or "", ("\nGuide: " + spec["guide"]) if spec.get("guide") else "", "\n".join(lines) or "(no fields)",
               ", ".join(f["name"] for f in need) or "none", spec["type"]))


def clean_node_config(spec, cfg):
    """Keep only the node's own fields, coerce types, enforce required. Raises ValueError with what is missing or wrong."""
    cfg = cfg if isinstance(cfg, dict) else {}
    out, missing = {}, []
    for f in spec["fields"]:
        v = cfg.get(f["name"], f.get("default", ""))
        t = f["type"]
        if t == "checkbox":
            v = bool(v)
        elif t == "number":
            if str(v).strip() == "":
                v = ""
            else:
                try:
                    n = float(v)
                except (TypeError, ValueError):
                    raise ValueError("%s must be a number" % f["label"])
                v = int(n) if n == int(n) else n
        else:
            v = str(v if v is not None else "")[:50000]
            if t == "select" and f.get("options") and v and v not in f["options"]:
                raise ValueError("%s: pick one of %s" % (f["label"], ", ".join(f["options"])))
        if f.get("required") and t != "checkbox" and str(v).strip() == "":
            missing.append(f["label"])
        out[f["name"]] = v
    if missing:
        raise ValueError("missing: " + ", ".join(missing))
    return out


def run_single(spec, cfg, text, wd):
    """Run one node alone. -> {"text", "data", "log"}. Templated fields see {{input}}; secrets come in `cfg` for this run only."""
    cfg = clean_node_config(spec, cfg)
    ev = {"text": text}
    ctx = {"input": text, "data": {}, "in": {}}
    tpl = spec.get("templated") or ()
    if tpl:
        cfg = {k: (render(v, ctx) if k in tpl and isinstance(v, str) else v) for k, v in cfg.items()}
    out, log = run_package_node(spec, cfg, [ev] if text else [], {"id": "chat", "work": "", "node": spec["type"]}, wd)
    return {"text": text_of(out)[:20000], "data": out.get("data") if isinstance(out, dict) else None, "log": (log or "")[-1500:]}


LOOP, ENDLOOP = "flow.loop", "flow.endloop"
LOOP_MAX = 1000


def split_items(text, mode, regex=""):
    """The list a Loop walks: lines | comma | regex | json (a JSON list of strings or objects, also inside a ```json fence). -> list."""
    t = (text or "").strip()
    if mode == "json":
        for cand in (t, t[t.find("["):t.rfind("]") + 1] if "[" in t else ""):
            try:
                v = json.loads(cand)
            except ValueError:
                continue
            if isinstance(v, dict):
                v = v["items"] if isinstance(v.get("items"), list) else [v]
            if isinstance(v, list):
                return v
        raise RuntimeError('the list is not JSON: expected ["a@x.com", ...] or [{"to": "a@x.com"}, ...]')
    if mode == "comma":
        parts = re.split(r"[,;\n]+", t)
    elif mode == "regex":
        try:
            parts = re.split(regex or r"\n", t)
        except re.error as e:
            raise RuntimeError("bad separator regex: %s" % e)
    else:
        parts = t.splitlines()
    return [p.strip() for p in parts if p and p.strip()]


def item_parts(item):
    """One list entry -> (text, extra data keys). An object keeps its keys as {{data.<key>}}; its text is the JSON."""
    if isinstance(item, str):
        return item, {}
    if isinstance(item, dict):
        return json.dumps(item, ensure_ascii=False), {k: v for k, v in item.items() if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", str(k))}
    return json.dumps(item, ensure_ascii=False), {}


def loop_bodies(nodes, edges):
    """-> ({loop id: [body ids]}, {end id: loop id}). The body = every node after the loop up to (not including) an End loop node."""
    kids = {}
    for e in edges:
        kids.setdefault(e["from"], []).append(e["to"])
    bodies, ends = {}, {}
    for n in nodes.values():
        if n.get("type") != LOOP:
            continue
        body, todo = [], list(kids.get(n["id"], []))
        while todo:
            i = todo.pop(0)
            if i in body or i == n["id"] or i not in nodes:
                continue
            if nodes[i].get("type") == ENDLOOP:
                ends.setdefault(i, n["id"])
                continue
            body.append(i)
            todo.extend(kids.get(i, []))
        bodies[n["id"]] = body
    return bodies, ends


class Engine:
    """Runs works, fires cron triggers, supervises package trigger processes. One thread per work executes its runs in order."""

    def __init__(self, box):
        self.box = box
        self.queues = {}
        self.running = 0                       # runs executing now (the UI waits for 0 before it restarts for an update)
        self.fired = {}
        self.procs = {}                       # (work, node) -> {"p": Popen, "sig": str}
        self.lock = threading.Lock()
        self.sync_lock = threading.RLock()     # sync() runs from the scheduler and from HTTP handlers
        self.wake = threading.Event()
        self.stop = threading.Event()
        self._interrupted()
        threading.Thread(target=self._loop, daemon=True, name="works-scheduler").start()

    def _interrupted(self):
        """Runs left queued / running by a previous process will never finish: say so."""
        try:
            base = store.ui_dir("runs")
            for wid in os.listdir(base):
                d = os.path.join(base, wid)
                for f in os.listdir(d) if os.path.isdir(d) else ():
                    fp = os.path.join(d, f)
                    try:
                        with open(fp, encoding="utf-8") as fh:
                            r = json.load(fh)
                        if r.get("status") in ("queued", "running"):
                            r.update(status="interrupted", ended=time.time(), error=r.get("error") or "the UI stopped while this run was waiting or running")
                            write_private(fp, json.dumps(r, ensure_ascii=False).encode("utf-8"))
                    except (OSError, ValueError, AttributeError):
                        pass
        except OSError:
            pass

    # -- starting runs
    def fire(self, work, node, event, via):
        """Queue a run for NODE with EVENT (after the trigger's own filter). -> run id or None when filtered out."""
        if not accepts(node, event):
            return None
        run = {"id": time.strftime("%Y%m%d%H%M%S-") + uuid.uuid4().hex[:6], "work": work["id"], "status": "queued", "started": time.time(), "ended": None,
               "via": via, "trigger": node["id"], "text": text_of(event)[:300], "nodes": {}, "chat": None, "result": None}
        _save_run(run, trim=False)
        with self.lock:
            q = self.queues.get(work["id"])
            if not q:
                q = self.queues[work["id"]] = queue.Queue(50)
                threading.Thread(target=self._worker, args=(work["id"], q), daemon=True).start()
        try:
            q.put_nowait((run, event))
        except queue.Full:
            run.update(status="failed", ended=time.time(), error="too many queued runs")
            _save_run(run)
        return run["id"]

    def _worker(self, wid, q):
        while not self.stop.is_set():
            try:
                item = q.get(timeout=5)
            except queue.Empty:
                continue
            if item is None:                                                 # its work was deleted
                return
            run, event = item
            with self.lock:
                self.running += 1
            try:
                self._execute(run, event)
            except Exception as e:                                           # never kill the worker
                run.update(status="failed", ended=time.time(), error=str(e)[:300])
                _save_run(run)
            finally:
                with self.lock:
                    self.running -= 1

    def _execute(self, run, event):
        work = store.get_work(run["work"])
        if not work:
            raise RuntimeError("work was deleted")
        cat = catalogue()
        order, ins = topo(work)
        nodes = {n["id"]: n for n in work["nodes"]}
        run["status"] = "running"
        _save_run(run, trim=False)
        outs = {run["trigger"]: event}
        ctx_base = {"trigger": text_of(event), "data": event.get("data", {}) if isinstance(event, dict) else {}}
        sess, sess_free, failed = None, None, False
        said = {}                                                           # chat id -> input texts already in that chat (never sent twice)
        lock = threading.RLock()                                            # run record, shared chats and `outs` are touched by several nodes at once
        busy = {"claude": False, "free": False}                             # the shared chat is in use: a parallel AI node gets a chat of its own
        names = names_of(work, cat)
        bodies, ends = loop_bodies(nodes, work["edges"])
        inbody = {b for v in bodies.values() for b in v}                  # run by their loop, once per item, not by the scheduler
        ctx_of = {}                                                         # node id -> the template context of its current step
        collected = {}                                                      # End loop id -> {"texts", "results", "failed"} of its loop

        gate = {"t": 0.0, "dirty": False, "timer": None}

        def save(final=False):
            """Write the run file at most once a second while it runs (a timer catches the last change); the final save also trims old runs."""
            with lock:
                now = time.time()
                if final or now - gate["t"] >= 1:
                    if gate["timer"]:
                        gate["timer"].cancel()
                        gate["timer"] = None
                    gate.update(t=now, dirty=False)
                    _save_run(run, trim=final)
                    return
                gate["dirty"] = True
                if not gate["timer"]:
                    def late():
                        with lock:
                            gate["timer"] = None
                            if gate["dirty"]:
                                gate.update(t=time.time(), dirty=False)
                                _save_run(run, trim=False)
                    gate["timer"] = threading.Timer(max(0.05, 1 - (now - gate["t"])), late)
                    gate["timer"].daemon = True
                    gate["timer"].start()

        def upd(rec, **kw):
            with lock:
                rec.update(kw)

        def do_loop(lid, cfg, cdata, rec, outs):
            """Run the loop's body once per item, one pass after the other. -> the loop node's own output (a summary)."""
            body = [b for b in order if b in bodies.get(lid, ())]
            if any(nodes[b].get("type") == LOOP for b in body):
                raise RuntimeError("a loop inside a loop is not supported: end the first one with End loop")
            raw = split_items(render(cfg.get("items") or "{{input}}", ctx_of[lid]), cfg.get("split") or "lines", cfg.get("regex") or "")
            seen, items = set(), []
            for it in raw:
                key = item_parts(it)[0]
                if cfg.get("unique", True) and key in seen:
                    continue
                seen.add(key)
                items.append(it)
            try:
                cap = min(max(int(cfg.get("limit") or 100), 1), LOOP_MAX)
                gap = min(max(float(cfg.get("delay") or 0), 0), 3600)
            except (TypeError, ValueError):
                cap, gap = 100, 0
            if len(items) > cap:
                raise RuntimeError("%d items, more than the limit of %d (raise 'At most this many items', up to %d)" % (len(items), cap, LOOP_MAX))
            end_ids = [e for e, l in ends.items() if l == lid]
            texts, bad, lines, k = [], 0, [], 0
            for k, it in enumerate(items, 1):
                if self.stop.is_set():
                    break
                text, extra = item_parts(it)
                it_outs = dict(outs)
                it_outs[lid] = {"text": text, "data": dict(cdata, **extra, item=text, index=k, total=len(items))}
                if cfg.get("fresh_chat", True):
                    fresh()
                with lock:
                    old = {b: run["nodes"].get(b) for b in body}
                last = None
                for b in body:
                    step(b, it_outs, True)
                    if it_outs.get(b) is not None:
                        last = b
                with lock:
                    recs = [r for b in body if (r := run["nodes"].get(b)) is not None and r is not old[b]]      # records written by THIS pass
                    this_bad = any(r.get("status") == "error" for r in recs)
                    err = next((r.get("error") for r in recs if r.get("status") == "error"), "")
                res = text_of(it_outs[last]) if last else ""
                short = text.replace("\n", " ")[:60]
                if this_bad:
                    bad += 1
                    lines.append("%d. %s: FAILED %s" % (k, short, str(err)[:160]))
                else:
                    texts.append(res)
                    lines.append("%d. %s: ok" % (k, short))
                upd(rec, note="%d/%d done%s" % (k, len(items), ", %d failed" % bad if bad else ""), log="\n".join(lines)[-1500:])
                save()
                if this_bad and cfg.get("on_error") != "continue":
                    raise RuntimeError("item %d (%s) failed: %s" % (k, short, str(err)[:200]))
                if gap and k < len(items) and self.stop.wait(gap):
                    break
            for e in end_ids:
                collected[e] = {"texts": texts, "failed": bad}
            upd(rec, note="%d item(s), %d failed" % (len(items), bad) if items else "the list is empty: nothing to repeat")
            return {"text": "%d item(s) repeated, %d failed" % (len(items), bad), "data": dict(cdata, count=len(items), failed=bad)}

        def step(nid, outs=outs, inloop=False):
            nonlocal sess, sess_free, failed
            n = nodes[nid]
            spec = cat.get(n.get("type"))
            if nid == run["trigger"]:
                with lock:
                    run["nodes"][nid] = {"status": "ok", "output": text_of(event)[:2000]}
                return
            got = [(u, outs[u]) for u in ins[nid] if outs.get(u) is not None]
            if n.get("type") == ENDLOOP and ends.get(nid) in outs:             # the closing node of a loop sees the results of all passes
                got = [(ends[nid], collected[nid])] if nid in collected else []
            inputs = [o for _, o in got]
            if not inputs:
                return
            if not spec:
                with lock:
                    run["nodes"][nid] = {"status": "error", "error": "unknown node type %s (package removed?)" % n.get("type")}
                    failed = failed or not inloop
                return
            if spec["kind"] == "trigger":
                return
            plain = "\n\n".join(text_of(i) for i in inputs)
            text = plain if len(got) == 1 else "\n\n".join("## From %s\n%s" % (names[u][0], text_of(o)) for u, o in got)       # several parents: say which is which
            cdata = dict(ctx_base["data"])
            for i in inputs:                                               # data of upstream nodes (a pointer's {{data.path}}) reaches this one
                if isinstance(i, dict) and isinstance(i.get("data"), dict):
                    cdata.update(i["data"])
            inmap = {}
            for k, (u, o) in enumerate(got, 1):                            # {{in.1}} / {{in.<node>}}: one parent's text
                inmap[str(k)] = inmap[names[u][1]] = text_of(o)
            ctx = dict(ctx_base, input=text, data=cdata, **{"in": inmap})
            ctx_of[nid] = ctx
            with lock:
                rec = run["nodes"][nid] = {"status": "running", "t0": time.time(), "input": text[:2000], "paths": _pointers(cdata, inputs),
                                           "inputs": [{"from": u, "label": names[u][0], "kind": (cat.get((nodes.get(u) or {}).get("type")) or {}).get("out", "text"), "text": text_of(o)[:1200]} for u, o in got]}
            save()
            cfg = with_secrets(work["id"], n, spec, self.box)
            mine = []                                                      # shared chats this node holds (released when it ends)
            try:
                if n["type"] == LOOP:
                    out = do_loop(nid, cfg, cdata, rec, outs)
                elif n["type"] == ENDLOOP:
                    out = inputs[-1]
                    if nid in collected:
                        c = collected[nid]
                        sep = {"json": None, "lines": "\n", "blank line": "\n\n"}.get(cfg.get("join") or "lines", "\n")
                        out = {"text": json.dumps(c["texts"], ensure_ascii=False, indent=1) if sep is None else sep.join(c["texts"]),
                               "data": dict(cdata, results=c["texts"], count=len(c["texts"]), failed=c["failed"])}
                elif n["type"] == "flow.filter":
                    try:
                        hit = re.search(cfg.get("regex") or "", plain[:20000], re.I | re.M) is not None
                    except re.error as e:
                        raise RuntimeError("bad regex: %s" % e)
                    out = inputs[-1] if hit != bool(cfg.get("invert")) else None
                    upd(rec, note="passed" if out is not None else "stopped here")
                elif n["type"] == "flow.template":
                    out = {"text": render(cfg.get("template", "{{input}}"), ctx), "data": cdata}
                elif n["type"] == "flow.note":
                    out = inputs[-1]
                elif n["type"] == "flow.output":
                    body = render(cfg.get("template", "{{input}}"), ctx)
                    run["result"] = {"title": str(cfg.get("title") or "")[:120], "text": body[:20000]}
                    out = {"text": body, "data": cdata}
                elif n["type"] == "flow.memory":
                    mem = memory_text(cfg, work["id"], run["id"])
                    upd(rec, note="%d chars remembered" % len(mem))
                    out = {"text": mem if cfg.get("mode") == "memory only" or not mem else "Remembered context:\n%s\n\nCurrent input:\n%s" % (mem, text), "data": cdata}
                elif n["type"] == "ai.claude":
                    folder = render(str(cfg.get("cwd") or ""), ctx).strip()
                    if folder.startswith("@"):                              # a mention (@/path) picked from the list
                        folder = folder[1:].strip()
                    if folder:
                        try:
                            folder = store.check_cwd(folder)
                        except ValueError as e:
                            raise RuntimeError("working folder: %s" % e)
                    with lock:
                        solo = not folder and not busy["claude"]            # the shared chat is free; else this parallel node talks in a chat of its own
                        if solo:
                            busy["claude"] = True
                            mine.append("claude")
                        if sess is None or cfg.get("fresh") or not solo:
                            s = store.update_session(store.create_session("Work: %s" % work["title"], "", chat.build_system(chat.freechat.WORK_RULES), cwd=folder)["id"], work=True)
                            run.setdefault("chats", []).append(s["id"])
                            sess = s if sess is None and not folder else sess
                            cur = s
                        else:
                            cur = store.get_session(sess["id"])
                        run["chat"] = run["chat"] or cur["id"]
                    prompt = render(cfg.get("prompt", ""), ctx)
                    prompt += sources.resolve(prompt, folder or store.session_dir(cur["id"]))
                    known = said.setdefault(cur["id"], set())
                    if text and not uses_input(cfg.get("prompt")):
                        if text.strip() in known:                           # this chat already holds it (a previous reply or input): do not repeat it
                            prompt = "Using the input above (already in this conversation).\n\nTask: %s" % prompt
                        else:
                            prompt = "Input:\n%s\n\nTask: %s" % (text, prompt)
                    if text:
                        known.add(text.strip())
                    if not prompt.strip():
                        raise RuntimeError("empty prompt")
                    reply, err = "", None
                    for ev in chat.turn(cur, prompt, str(cfg.get("model") or ""), str(cfg.get("effort") or ""), cfg.get("mode") if cfg.get("mode") in chat.MODES else "auto",
                                         extra_env=self.box.get_all(), **({"add_dirs": dirs} if (dirs := pointer_dirs(cdata, inputs)) else {})):
                        if ev["k"] == "msg":
                            reply = "\n".join(b["text"] for b in ev["blocks"] if b["type"] == "text") or reply
                        elif ev["k"] == "error" or (ev["k"] == "result" and ev.get("error")):
                            err = ev.get("text") or "Claude failed"
                    if err:
                        raise RuntimeError(err)
                    if reply:
                        known.add(reply.strip())
                    out = {"text": reply, "data": cdata}
                elif n["type"] == "ai.free":
                    from . import freechat
                    with lock:
                        solo = not busy["free"]
                        if solo:
                            busy["free"] = True
                            mine.append("free")
                        if sess_free is None or cfg.get("fresh") or not solo:
                            made = store.update_session(store.create_session("Work: %s" % work["title"], "", chat.build_system())["id"], engine="free", work=True,
                                                        mcp=[x.strip() for x in str(cfg.get("mcp") or "").split(",") if x.strip()][:20])
                            run.setdefault("chats", []).append(made["id"])
                            sess_free = made if sess_free is None else sess_free
                        else:
                            made = sess_free
                        run["chat"] = run["chat"] or made["id"]
                    prompt = render(cfg.get("prompt", ""), ctx)
                    prompt += sources.resolve(prompt, store.session_dir(made["id"]))
                    if text and not uses_input(cfg.get("prompt")):
                        prompt = "Input:\n%s\n\nTask: %s" % (text, prompt)
                    if cfg.get("read_pointers"):
                        prompt += pointer_text(cdata, inputs)
                    if not prompt.strip():
                        raise RuntimeError("empty prompt")
                    reply, err = "", None
                    for ev in freechat.turn(store.get_session(made["id"]), prompt, str(cfg.get("model") or ""), str(cfg.get("effort") or ""), "auto", extra_env=self.box.get_all()):
                        if ev["k"] == "msg":
                            reply = "\n".join(b["text"] for b in ev["blocks"] if b["type"] == "text") or reply
                        elif ev["k"] == "error" or (ev["k"] == "result" and ev.get("error")):
                            err = ev.get("text") or "Free chat failed"
                    if err:
                        raise RuntimeError(err)
                    out = {"text": reply, "data": cdata}
                else:
                    tpl = spec.get("templated") or ()
                    if tpl:
                        cfg = {k: (render(v, ctx) if k in tpl and isinstance(v, str) else v) for k, v in cfg.items()}
                    for k in spec.get("takes") or ():                       # standard keys: an empty field takes {{data.<same name>}} from the node before
                        v = cdata.get(k)
                        if not str(cfg.get(k) or "").strip() and v not in (None, "", [], {}):
                            cfg[k] = "\n".join(str(x) for x in v) if isinstance(v, list) else v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
                    out, log = run_package_node(spec, cfg, inputs, {"id": run["id"], "work": work["id"], "node": nid}, store.session_dir(run["chat"]) if run["chat"] else os.path.expanduser("~"))
                    if log:
                        upd(rec, log=log[-1500:])
                    if isinstance(out, dict) and out.get("skip"):
                        out = None
                with lock:
                    rec.update(status="ok", ended=time.time(), output=(text_of(out)[:4000] if out is not None else ""))
                    outs[nid] = out
            except Exception as e:
                with lock:
                    rec.update(status="error", ended=time.time(), error=str(e)[:600])
                    failed = failed or not inloop
            with lock:
                for k in mine:
                    busy[k] = False
            save()

        def fresh():
            nonlocal sess, sess_free
            sess = sess_free = None                                          # the next Claude / Free node of a pass opens a chat of its own

        ext = {l: [u for b in v for u in ins[b] if u not in v and u != l] for l, v in bodies.items()}      # parents of a loop body that sit outside it
        done, left, futs = set(), [i for i in order if i not in inbody], {}                           # independent branches run side by side; a node waits for ALL its parents
        with cf.ThreadPoolExecutor(WORKERS, thread_name_prefix="work-node") as ex:
            while left or futs:
                ready = [i for i in left if all(u in done for u in ins[i] + ext.get(i, []))]
                if not ready and not futs:
                    ready = left[:1]                                        # a cycle: go on in order, as before
                for i in ready:
                    left.remove(i)
                    futs[ex.submit(step, i)] = i
                fin, _ = cf.wait(futs, return_when=cf.FIRST_COMPLETED)
                for f in fin:
                    i = futs.pop(f)
                    done.add(i)
                    done.update(bodies.get(i, ()))
                    if f.exception():
                        with lock:
                            run["nodes"][i] = {"status": "error", "error": str(f.exception())[:600]}
                            failed = True
        if not run.get("result") and not failed:                            # no Output node: the last text produced is the answer
            last = [v for v in run["nodes"].values() if v.get("status") == "ok" and v.get("output") and v is not run["nodes"].get(run["trigger"])]
            if last:
                run["result"] = {"title": "", "text": max(last, key=lambda v: v.get("ended") or 0)["output"]}
        run.update(status="failed" if failed else "done", ended=time.time())
        try:
            from . import hubusage
            hubusage.kick()                                       # a finished run is counted on the hub (its AI nodes are chat turns too)
            hubusage.use("work:" + str(work.get("id")), package=work.get("package") or "aihub-ui", session=run.get("id") or "",
                         detail={"title": work.get("title", ""), "status": run["status"], "trigger": str((nodes.get(run["trigger"]) or {}).get("type") or "")})
        except Exception:
            pass
        save(True)

    # -- scheduler + trigger processes
    def _loop(self):
        while not self.stop.is_set():
            self.wake.wait(15)
            self.wake.clear()
            if self.stop.is_set():
                return
            try:
                self.sync()
            except Exception:
                pass

    def kick(self):
        """Ask the scheduler thread to run sync() now (instead of calling it from a request handler)."""
        self.wake.set()

    def sync(self):
        """Fire due cron triggers; start / stop package trigger processes to match the enabled works. Safe to call from any thread."""
        with self.sync_lock:
            self._sync()

    def _sync(self):
        now = datetime.datetime.now()
        wanted, cat = {}, None
        metas = store.works()
        ids = {m["id"] for m in metas}
        with self.lock:                                                     # a deleted work: its queue thread ends, its timers are forgotten
            for wid in [w for w in self.queues if w not in ids]:
                try:
                    self.queues.pop(wid).put_nowait(None)
                except queue.Full:
                    pass
        for k in [k for k in self.fired if k[0] not in ids]:
            self.fired.pop(k, None)
        for meta in metas:
            w = store.get_work(meta["id"])
            if not w or not w.get("enabled"):
                continue
            for n in w["nodes"]:
                t = n.get("type")
                if t == "start.cron":
                    self._cron(w, n, now)
                elif isinstance(t, str) and t.startswith("pkg:"):          # only a package trigger needs the catalogue (it runs the CLI)
                    cat = cat if cat is not None else catalogue()
                    if t in cat and cat[t].get("run") and cat[t]["kind"] == "trigger":
                        wanted[(w["id"], n["id"])] = (w, n, cat[t])
        for key in list(self.procs):
            if key not in wanted:
                self._kill(key)
        for key, (w, n, spec) in wanted.items():
            cfg = with_secrets(w["id"], n, spec, self.box)
            sig = hashlib.sha256(json.dumps([cfg, spec["run"]], sort_keys=True).encode()).hexdigest()
            cur = self.procs.get(key)
            if cur and cur["sig"] != sig:
                self._kill(key)
                cur = None
            if not cur or (cur["p"].poll() is not None and time.time() >= cur["retry"]):
                fails = 0 if not cur else 0 if time.time() - cur["t0"] > 300 else cur["fails"] + 1       # lived 5 min: not a crash loop
                self._spawn(key, w, n, spec, cfg, sig, fails)

    def status(self, wid):
        """{node id: {running, exit, fails, stderr}} of a work's trigger processes (the last 20 stderr lines), for the page."""
        out = {}
        for (w, nid), cur in list(self.procs.items()):
            if w == wid:
                rc = cur["p"].poll()
                out[nid] = {"running": rc is None, "exit": rc, "fails": cur["fails"], "stderr": list(cur["err"])}
        return out

    def _cron(self, w, n, now):
        cfg = n.get("config") or {}
        try:
            kind, spec = parse_schedule(cfg.get("schedule", ""))
        except ValueError:
            return
        key = (w["id"], n["id"])
        if kind == "every":
            last = self.fired.setdefault(key, time.time())
            if time.time() - last >= spec:
                self.fired[key] = time.time()
                self.fire(w, n, {"text": cfg.get("text") or "scheduled run"}, "schedule")
        else:
            minute = now.strftime("%Y%m%d%H%M")
            if self.fired.get(key) != minute and cron_due(spec, now):
                self.fired[key] = minute
                self.fire(w, n, {"text": cfg.get("text") or "scheduled run"}, "schedule")

    def _spawn(self, key, w, n, spec, cfg, sig, fails):
        argv = _argv(spec)
        err = collections.deque(maxlen=20)
        retry = time.time() + min(900, 30 * 2 ** fails)
        try:
            p = subprocess.Popen(argv, cwd=spec["dir"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                                 env=dict(os.environ, AIHUB_NODE=spec["type"]), start_new_session=True)
            p.stdin.write(json.dumps({"config": cfg, "run": {"work": w["id"], "node": n["id"]}}) + "\n")
            p.stdin.flush()
        except OSError as e:
            err.append("cannot start: %s" % e)
            self.procs[key] = {"p": type("Dead", (), {"poll": lambda s: 1})(), "sig": sig, "fails": fails, "retry": retry, "t0": time.time(), "err": err}
            return
        self.procs[key] = {"p": p, "sig": sig, "fails": fails, "retry": retry, "t0": time.time(), "err": err}
        threading.Thread(target=self._drain, args=(p, err), daemon=True).start()
        threading.Thread(target=self._read, args=(w["id"], n["id"], p), daemon=True).start()

    @staticmethod
    def _drain(p, err):
        try:
            for ln in p.stderr:
                err.append(ln.rstrip()[:300])
        except (OSError, ValueError):
            pass

    def _read(self, wid, nid, p):
        """Each stdout line {"event": {"text": ..., "data": ...}} starts a run (after the trigger's filter)."""
        for ln in p.stdout:
            try:
                ev = json.loads(ln).get("event")
            except (ValueError, AttributeError):
                continue
            w = store.get_work(wid)
            node = next((n for n in (w or {}).get("nodes", []) if n["id"] == nid), None)
            if w and w.get("enabled") and node and isinstance(ev, dict):
                self.fire(w, node, {"text": str(ev.get("text", "")), "data": ev.get("data") if isinstance(ev.get("data"), dict) else {}}, "event")

    def _kill(self, key):
        cur = self.procs.pop(key, None)
        if cur and cur["p"].poll() is None:
            _kill_tree(cur["p"]) if os.name == "nt" else self._term(cur["p"])

    @staticmethod
    def _term(p):
        try:
            os.killpg(p.pid, 15)
        except (OSError, AttributeError):
            p.terminate()

    def shutdown(self):
        self.stop.set()
        for k in list(self.procs):
            self._kill(k)


# ---------- generate a work from a description

OUTPUT_ASK = "\n\nWrite the result to a file (a dashboard or site: a folder with index.html) and end your answer with this last line:\nOUTPUT: /full/path"
GEN_EXAMPLE = {"title": "Data dashboard", "description": "Pulls a repo, builds a dashboard with Claude, publishes it as a Space.",
               "nodes": [{"id": "a", "type": "start.manual", "label": "Start", "x": 40, "y": 40, "config": {"inputs": "repo|Repository URL|text||*"}},
                         {"id": "g", "type": "git.pull", "label": "Git data", "x": 340, "y": 40, "config": {"url": "{{data.repo}}"}},
                         {"id": "c", "type": "ai.claude", "label": "Build dashboard", "x": 640, "y": 40,
                          "config": {"prompt": "Open the data in {{input}}, analyse it and build a one-page dashboard as a folder with index.html." + OUTPUT_ASK}},
                         {"id": "p", "type": "space.publish", "label": "Publish", "x": 940, "y": 40, "config": {"name": "data-dashboard"}}],
               "edges": [{"from": "a", "to": "g"}, {"from": "g", "to": "c"}, {"from": "c", "to": "p"}]}
GEN_RULES = """You design a Works flow for AI Hub UI. Reply with ONE ```json block holding the work and nothing else that matters.
Shape: {"title": str, "description": str, "nodes": [{"id": "a", "type": T, "label": str, "x": int, "y": int, "config": {...}}], "edges": [{"from": "a", "to": "b"}]}

STRUCTURE
- Exactly one Start node, first (start.manual unless a schedule / webhook / package trigger fits). It has no incoming edge. Ids are short unique [a-z0-9]; give every node a short, distinct "label" (it is also how {{in.<label>}} names it).
- Every other node is reachable from the Start; edges only join existing ids, never a node to itself, no cycles. Use ONLY the node types listed below and only their field names; fill the required (*) fields, leave password fields empty (the user fills them in), never put a secret in a config.
- Layout is optional (it is tidied when missing or overlapping): x grows by 300 per column, y by 140 per branch (a card is 230 x 100).
- Branches that do not depend on each other run IN PARALLEL; a node with several parents waits for all of them. Use that: independent reads / requests side by side, then ONE node that joins them.

DATA: every node hands on text, plus a small data dict. In any text field (prompt, template, URL, body ...):
- {{input}} = the text of the node(s) before it. With several parents it is their texts one under another, each headed `## From <label>`.
- {{in.1}}, {{in.2}} (parents in the order the edges are listed) or {{in.<label>}} (label in lower case, other characters as _, e.g. label "Git data" -> {{in.git_data}}) = ONE parent only. Use it after a join when the prompt must tell the sources apart. Never use a name that is not a parent's label.
- {{trigger}} = the text that started the run. {{data.key}} = the inputs of start.manual (its "inputs" field: one per line `name|Label|type|options or default|*`, * = required, type text/textarea/number/select/checkbox) and the data keys other nodes list (git.pull / io.path: path).
- A Claude prompt without {{input}} / {{in.x}} still receives the incoming text first ("Input: ..."), so only write {{input}} to place it yourself.
- PATHS, not content: git.pull / io.path output a path. The Claude task after them opens the files itself (give it {{input}} or {{data.path}} and say what to analyse).
- Claude only answers with text. When a later node needs a FILE or FOLDER (space.publish, mail attachments), the Claude prompt must say: write the result to a file (a dashboard or site: a folder with index.html) and end the answer with the last line `OUTPUT: /full/path`. space.publish then needs no File field; set a Space name (lower-case slug).
- The answer of the whole run: add a flow.output node (title + template such as {{input}}); without one the last text is the answer.

- LOOPS: flow.loop repeats everything after it once per item of a list (one mail per sender). Feed it a list (a Claude node that answers one item per line, or a JSON list of objects such as [{"to","subject","body"}]); inside, {{data.item}} / {{input}} is the item, {{data.index}}, and an object's keys are {{data.<key>}} (send mail nodes take to / subject / body from them when their fields are empty). Close it with flow.endloop when something must run once afterwards. No loop inside a loop.

SHAPES
- ai.claude (list the recipients, one per line) -> flow.loop -> ai.claude (write the mail for {{data.item}}) -> a mail node -> flow.endloop -> flow.output.
- git.pull -> ai.claude (analyse, build dashboard/index.html, end with OUTPUT:) -> space.publish.
- start.cron -> io.request / git.read -> ai.claude -> send.teams (or the mail node pkg:aihub-nodes-outlook/outlook-send when it is in the list).
- start.manual -> [git.read, io.request] in parallel -> ai.claude (prompt names {{in.<a>}} and {{in.<b>}}) -> flow.output.
- Keep it as small as the request allows: 3 to 7 nodes.

EXAMPLE (valid):
```json
""" + json.dumps(GEN_EXAMPLE, indent=1) + """
```
Node types (out = what it hands on, needs = what it requires from before):
"""



def _gen_catalogue():
    lines = []
    for t, sp in catalogue().items():
        if sp.get("hidden"):
            continue
        fl = ", ".join("%s%s%s" % (f["name"], "*" if f.get("required") else "", "=" + "/".join(f["options"]) if f.get("options") else "") for f in sp["fields"])
        io = " | out: %s%s%s" % (sp.get("out", "text"), " (%s)" % sp["out_note"] if sp.get("out_note") else "", " | needs: " + sp["needs"] if sp.get("needs") else "")
        lines.append("- %s (%s, %s): %s%s | fields: %s" % (t, sp["kind"], sp["group"], sp["desc"][:160], io, fl or "-"))
    return "\n".join(lines)


NODE_W, NODE_H, GAP_X, GAP_Y = 230, 100, 70, 40       # a node card is 230 wide (NW in app.js) and ~70-100 tall


def _overlap(nodes):
    """True when two cards touch (with a small margin) or a node has no position."""
    for i, a in enumerate(nodes):
        if not a.get("x") and not a.get("y"):
            return True
        for b in nodes[:i]:
            if abs(a["x"] - b["x"]) < NODE_W + 16 and abs(a["y"] - b["y"]) < NODE_H + 12:
                return True
    return False


def tidy(nodes, edges):
    """Layered left-to-right layout: column = longest path from a start, rows ordered by their parents' rows. Sets x/y in place."""
    ids = [n["id"] for n in nodes]
    parents = {i: [e["from"] for e in edges if e["to"] == i and e["from"] in ids and e["from"] != i] for i in ids}
    layer = {i: 0 for i in ids}
    for _ in range(len(ids)):                                   # bounded relaxation: a cycle cannot loop forever
        moved = False
        for i in ids:
            want = max([layer[p] + 1 for p in parents[i]] or [0])
            if want > layer[i] and want < len(ids):
                layer[i], moved = want, True
        if not moved:
            break
    cols, row = {}, {}
    for i in ids:
        cols.setdefault(layer[i], []).append(i)
    for c in sorted(cols):
        cols[c].sort(key=lambda i: (sum(row.get(p, 0) for p in parents[i]) / len(parents[i]) if parents[i] else 0, ids.index(i)))
        for r, i in enumerate(cols[c]):
            row[i] = r
    by = {n["id"]: n for n in nodes}
    for i in ids:
        by[i]["x"] = 40 + layer[i] * (NODE_W + GAP_X)
        by[i]["y"] = 40 + row[i] * (NODE_H + GAP_Y)


def _layout(w):
    """Keep the positions a generator chose only when they are all set and no cards overlap; otherwise tidy the whole graph."""
    nodes = w["nodes"]
    for n in nodes:
        n["x"], n["y"] = int(n.get("x") or 0), int(n.get("y") or 0)
    if _overlap(nodes):
        tidy(nodes, [e for e in w.get("edges", []) if isinstance(e, dict)])


def extract_work(text):
    """The first JSON object with nodes in TEXT (a fenced block, or the whole text). Raises ValueError."""
    cands = re.findall(r"```(?:json|workflow)?\s*\n(.*?)```", text or "", re.S) + [text or ""]
    for c in cands:
        a, b = c.find("{"), c.rfind("}")
        if a >= 0 and b > a:
            try:
                w = json.loads(c[a:b + 1])
            except ValueError:
                continue
            if isinstance(w, dict) and isinstance(w.get("nodes"), list):
                return w
    raise ValueError("the answer holds no workflow JSON")


def repair(w, cat):
    """Quiet fixes for what a model often gets slightly wrong: duplicate ids, edges to nothing / into a Start / repeated, `{{in.x}}` naming
    no parent, and a Publish node whose path nothing can give (the Claude node before it is asked for an OUTPUT: line)."""
    seen, nodes = set(), []
    for n in w["nodes"]:
        base = str(n.get("id") or "n")[:20]
        nid, k = base, 1
        while nid in seen:
            k += 1
            nid = "%s%d" % (base, k)
        n["id"] = nid
        seen.add(nid)
        nodes.append(n)
    by = {n["id"]: n for n in nodes}
    edges, have = [], set()
    for e in w.get("edges") or []:
        if not isinstance(e, dict):
            continue
        a, b = e.get("from"), e.get("to")
        if a in by and b in by and a != b and (a, b) not in have and cat[by[b]["type"]]["kind"] != "trigger":
            have.add((a, b))
            edges.append({"from": a, "to": b})
    w["nodes"], w["edges"] = nodes, edges
    names = names_of(w, cat)
    parents = {i: [e["from"] for e in edges if e["to"] == i] for i in by}
    for n in nodes:                                                     # {{in.x}} must name one of this node's parents
        ok = {str(k) for k in range(1, len(parents[n["id"]]) + 1)} | {names[u][1] for u in parents[n["id"]]}
        cfg = n.get("config") or {}
        for key, v in list(cfg.items()):
            if isinstance(v, str) and "{{" in v:
                cfg[key] = re.sub(r"\{\{\s*in\.(\w+)\s*\}\}", lambda m: m.group(0) if m.group(1) in ok else "{{input}}", v)
    for n in nodes:                                                     # Publish with no way to get a path
        if cat[n["type"]].get("needs") != "path" or (n.get("config") or {}).get("path"):
            continue
        up, todo = set(), list(parents[n["id"]])
        while todo:
            u = todo.pop()
            if u not in up:
                up.add(u)
                todo.extend(parents[u])
        if any(cat[by[u]["type"]].get("out") == "path" for u in up):
            continue
        for u in sorted(up, key=lambda i: i not in parents[n["id"]]):  # the nearest Claude node first
            c = by[u]
            if c["type"] in ("ai.claude", "ai.free"):
                cfg = c.setdefault("config", {})
                if "OUTPUT:" not in str(cfg.get("prompt") or ""):
                    cfg["prompt"] = str(cfg.get("prompt") or "").rstrip() + OUTPUT_ASK
                break
    return w


def checked_work(w, strip_secrets_with=None):
    """A generated / chat-made work, made safe: known node types only, disabled, new id, secrets never taken from it."""
    cat = catalogue()
    w = dict(w)
    w.pop("id", None)
    w["enabled"] = False
    for n in w.get("nodes", []):
        if not isinstance(n, dict) or n.get("type") not in cat:
            raise ValueError("unknown node type %r" % (n.get("type") if isinstance(n, dict) else n))
        cfg = n.setdefault("config", {})
        for f in cat[n["type"]]["fields"]:
            if f.get("secret"):
                cfg[f["name"]] = ""
    if not any(cat[n["type"]]["kind"] == "trigger" for n in w["nodes"]):
        raise ValueError("a work needs a Start node")
    repair(w, cat)
    _layout(w)
    w = store.clean_work(w)
    if not w["nodes"]:
        raise ValueError("the work has no nodes")
    return w


def generate(prompt, model="", box=None):
    """Ask Claude to design a work from PROMPT; -> the saved (disabled) work. Raises RuntimeError.
    An answer that cannot be used (no JSON, unknown node type ...) is sent back once with the reason, in the same chat."""
    prompt = " ".join(str(prompt or "").split())[:4000]
    if not prompt:
        raise RuntimeError("describe the workflow you want")
    s = store.create_session("Workflow generator", "", chat.build_system(GEN_RULES + _gen_catalogue()))

    def ask(text):
        reply, err = "", None
        for ev in chat.turn(store.get_session(s["id"]) or s, text, model, "", "plan", extra_env=box.get_all() if box else None):
            if ev["k"] == "msg":
                reply = "\n".join(b["text"] for b in ev["blocks"] if b["type"] == "text") or reply
            elif ev["k"] == "error" or (ev["k"] == "result" and ev.get("error")):
                err = ev.get("text") or "Claude failed"
        if err:
            raise RuntimeError(err)
        return reply
    try:
        text, last = "Create this workflow:\n" + prompt, None
        for _ in range(2):
            try:
                return store.save_work(checked_work(extract_work(ask(text))))
            except ValueError as e:
                last = e
                text = "That answer cannot be used: %s. Reply again with ONE corrected ```json block (only the listed node types and their fields, one Start node)." % e
        raise RuntimeError("could not build the work: %s" % last)
    finally:
        try:
            store.delete_session(s["id"])
        except Exception:
            pass


# ---------- webhook tokens (stable across restarts)

def hook_token(wid, nid):
    keyf = os.path.join(store.ui_dir(), ".hooks.key")
    if not os.path.exists(keyf):
        write_private(keyf, os.urandom(32))
    with open(keyf, "rb") as f:
        return hmac.new(f.read(), ("hook:%s:%s" % (wid, nid)).encode(), hashlib.sha256).hexdigest()[:32]
