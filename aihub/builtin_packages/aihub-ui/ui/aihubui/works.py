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
import shutil
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

SLACK_GUIDE = """Send a message to Slack with an Incoming Webhook:
1. Open https://api.slack.com/apps > Create New App > From scratch, pick the workspace.
2. Incoming Webhooks > turn it on > Add New Webhook to Workspace > choose the channel.
3. Copy the Webhook URL (https://hooks.slack.com/services/...). Treat it like a password: it is stored encrypted here.
Test: Start: manual > Send to Slack, set Message, press Run."""

DISCORD_GUIDE = """Send a message to Discord with a channel webhook:
1. In Discord: channel settings (gear) > Integrations > Webhooks > New Webhook > Copy Webhook URL.
2. Paste it into "Webhook URL". It is stored encrypted here. @everyone / @here in the text never ping anyone.
Plain messages are cut at 2000 characters; with a Title the text goes into an embed (4096).
Test: Start: manual > Send to Discord, set Message, press Run."""

WEBHOOK_GUIDE = """Send data to any URL (Zapier, n8n, Make, your own service):
1. Paste the https URL. If the receiver wants a key, put it in "Authorization value" (stored encrypted).
2. Body: JSON is sent as it is, anything else is wrapped as {"text": ...}. Format "text" sends it raw.
The answer of the receiver is passed on. For other methods or reading data use the HTTP request node.
Test: Start: manual > Send to webhook, set the URL and body, press Run."""

SLACK_IN_GUIDE = """Start a work from a Slack message or slash command. Slack cannot call your laptop, so point a tunnel at this node's port.
1. Pick a Local port (8792) and turn the work on; the node listens on 127.0.0.1:PORT. Start a tunnel for that port (cloudflared / ngrok), note its https address.
2. https://api.slack.com/apps > your app > Basic Information > copy the Signing Secret into this node (stored encrypted). Requests without a valid signature are refused.
3. Event Subscriptions: Request URL = https://<tunnel>/slack/events (Slack confirms it at once, so the work must be on). Subscribe to bot events message.channels / message.groups / message.im / app_mention. Invite the bot to the channel.
4. Optional slash command: Request URL = https://<tunnel>/slack/command; the text after the command starts the run.
Reply with Send to Slack (or the thread via Data channel, thread_ts). Bot messages are ignored unless you tick Include bots."""

TELEGRAM_IN_GUIDE = """Start a work from a Telegram message. No tunnel or open port: the node asks Telegram for new messages (long polling).
1. In Telegram talk to @BotFather: /newbot, copy the bot token. Stored encrypted here.
2. Write to your bot (or add it to a group; in groups turn off privacy with /setprivacy to read every message).
3. Turn the work on. Messages sent while it was off are skipped. Only one program may poll a bot at a time.
Use Chat IDs to accept only your own chat (send a message, the run's data shows chat_id). Reply with Send to Telegram: its Chat ID is filled from the message."""

TELEGRAM_OUT_GUIDE = """Send a message to Telegram with a bot (@BotFather: /newbot, copy the token).
Chat ID: your user id, a group id (negative), or @channelname. After a Start: Telegram node it is filled from the message.
Test: Start: manual > Send to Telegram, set Chat ID and Message, press Run."""

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

SMTP_GUIDE = """Sends mail through any mail server (SMTP), on any system; no Outlook needed (for the Outlook desktop app use the aihub-nodes-outlook package).
1. Server: smtp.gmail.com / smtp.office365.com / your company's server. Security starttls = port 587, ssl = 465.
2. Username + Password: the password is stored encrypted. An account with 2-step sign-in needs an app password.
3. Action: send; draft = saved into the account's Drafts folder over IMAP (imap.<same domain>:993 unless you set it; Gmail's folder is [Gmail]/Drafts), so you check and send it from your mail app; eml = only writes a .eml file into the run's folder (drafts/), no server at all.
4. Leave To / Subject / Body empty to take {{data.to}}, {{data.subject}} ... from the node before (a Loop over [{to, subject, body}] sends one mail per item). A blank body uses the text coming in.
5. Attachments: one file path per line ({{input}} or an earlier node's OUTPUT path). Key and secret files are refused."""

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
    "start.slack": {"title": "Start: Slack message", "kind": "trigger", "group": "Chat", "desc": "Starts when Slack posts a message, mention or slash command to this node's own listener (127.0.0.1, Slack signature required).",
                    "run": ["{python}", "{pkg}/nodes/slack_in.py"], "timeout": 60, "guide": SLACK_IN_GUIDE,
                    "fields": [{"name": "port", "label": "Local port", "type": "number", "default": 8792, "required": True, "help": "Expose only this port through your tunnel."},
                               {"name": "signing_secret", "label": "Signing secret", "type": "password", "required": True, "help": "Slack app > Basic Information. Stored encrypted."},
                               {"name": "channel", "label": "Only these channel IDs (space or comma separated)", "type": "text", "default": ""},
                               {"name": "from_regex", "label": "Only from user IDs matching (regex)", "type": "text", "default": ""},
                               {"name": "include_bots", "label": "Include bot messages", "type": "checkbox", "default": False}]},
    "start.telegram": {"title": "Start: Telegram message", "kind": "trigger", "group": "Chat", "desc": "Starts when your Telegram bot receives a message. Polls Telegram: no tunnel, no open port.",
                       "run": ["{python}", "{pkg}/nodes/telegram_in.py"], "timeout": 60, "guide": TELEGRAM_IN_GUIDE,
                       "fields": [{"name": "bot_token", "label": "Bot token", "type": "password", "required": True, "help": "From @BotFather. Stored encrypted."},
                                  {"name": "chat_ids", "label": "Only these chat IDs (space or comma separated)", "type": "text", "default": ""},
                                  {"name": "from_regex", "label": "Only from names matching (regex)", "type": "text", "default": ""},
                                  {"name": "include_bots", "label": "Include messages from bots", "type": "checkbox", "default": False}]},
    "send.telegram": {"title": "Send to Telegram", "kind": "action", "group": "Chat", "desc": "Sends a message through a Telegram bot.",
                      "run": ["{python}", "{pkg}/nodes/telegram_send.py"], "timeout": 60, "templated": ["chat_id", "reply_to"], "takes": ["chat_id"], "guide": TELEGRAM_OUT_GUIDE,
                      "fields": [{"name": "bot_token", "label": "Bot token", "type": "password", "required": True, "help": "From @BotFather. Stored encrypted."},
                                 {"name": "chat_id", "label": "Chat ID", "type": "text", "default": "{{data.chat_id}}", "required": True, "help": "Filled from a Start: Telegram node; or a user id, group id, @channel."},
                                 {"name": "text", "label": "Message", "type": "textarea", "default": "{{input}}", "required": True},
                                 {"name": "format", "label": "Format", "type": "select", "options": ["plain", "HTML", "MarkdownV2"], "default": "plain"},
                                 {"name": "reply_to", "label": "Reply to message ID (optional)", "type": "text", "default": "{{data.message_id}}"}]},
    "send.slack": {"title": "Send to Slack", "kind": "action", "group": "Chat", "desc": "Posts a message to a Slack channel through an Incoming Webhook URL.",
                   "run": ["{python}", "{pkg}/nodes/slack_send.py"], "timeout": 60, "guide": SLACK_GUIDE,
                   "fields": [{"name": "webhook_url", "label": "Webhook URL", "type": "password", "required": True, "help": "https://hooks.slack.com/services/... Stored encrypted."},
                              {"name": "title", "label": "Title (optional)", "type": "text", "default": ""},
                              {"name": "text", "label": "Message", "type": "textarea", "default": "{{input}}", "required": True},
                              {"name": "username", "label": "Sender name (optional)", "type": "text", "default": ""},
                              {"name": "icon_emoji", "label": "Icon, e.g. :robot_face: (optional)", "type": "text", "default": ""}]},
    "send.discord": {"title": "Send to Discord", "kind": "action", "group": "Chat", "desc": "Posts a message to a Discord channel through a webhook URL.",
                     "run": ["{python}", "{pkg}/nodes/discord_send.py"], "timeout": 60, "guide": DISCORD_GUIDE,
                     "fields": [{"name": "webhook_url", "label": "Webhook URL", "type": "password", "required": True, "help": "https://discord.com/api/webhooks/... Stored encrypted."},
                                {"name": "title", "label": "Title (optional, makes it an embed)", "type": "text", "default": ""},
                                {"name": "text", "label": "Message", "type": "textarea", "default": "{{input}}", "required": True},
                                {"name": "username", "label": "Sender name (optional)", "type": "text", "default": ""}]},
    "send.webhook": {"title": "Send to webhook", "kind": "action", "group": "Chat", "desc": "POSTs JSON (or text) to any https URL: Zapier, n8n, Make, your own service. Data: status.",
                     "run": ["{python}", "{pkg}/nodes/webhook_send.py"], "timeout": 60, "templated": ["url"], "guide": WEBHOOK_GUIDE,
                     "fields": [{"name": "url", "label": "URL", "type": "text", "required": True, "help": "https only; may use {{data.x}}"},
                                {"name": "method", "label": "Method", "type": "select", "options": ["POST", "PUT", "PATCH"], "default": "POST"},
                                {"name": "body", "label": "Body", "type": "textarea", "default": "{{input}}"},
                                {"name": "format", "label": "Format", "type": "select", "options": ["json", "text"], "default": "json"},
                                {"name": "headers", "label": "Headers (Name: value per line)", "type": "textarea", "default": ""},
                                {"name": "secret", "label": "Authorization value (optional)", "type": "password", "default": "", "help": "e.g. Bearer abc123. Stored encrypted."}]},
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
    "send.smtp": {"title": "Send email (SMTP)", "kind": "action", "group": "Mail", "desc": "Sends mail through any SMTP server (Gmail, Microsoft 365, your company's), or saves it as a draft (IMAP Drafts folder, or a .eml file). Not Outlook: that is the aihub-nodes-outlook package.",
                  "run": ["{python}", "{pkg}/nodes/smtp_send.py"], "timeout": 180, "templated": ["from", "to", "cc", "bcc", "reply_to", "subject", "body", "attachments"], "takes": list(MAIL_KEYS),
                  "guide": SMTP_GUIDE,
                  "fields": [{"name": "action", "label": "Action", "type": "select", "options": ["send", "draft", "eml"], "default": "draft",
                              "help": "draft = saved in the account's Drafts folder (IMAP), you send it yourself; eml = a .eml file in the run's folder, no server."},
                             {"name": "host", "label": "SMTP server", "type": "text", "default": "", "help": "smtp.gmail.com, smtp.office365.com ... Not needed for eml."},
                             {"name": "security", "label": "Security", "type": "select", "options": ["starttls", "ssl", "none"], "default": "starttls"},
                             {"name": "port", "label": "Port (0 = 587 / 465 / 25 by security)", "type": "number", "default": 0},
                             {"name": "username", "label": "Username", "type": "text", "default": ""},
                             {"name": "password", "label": "Password / app password", "type": "password", "help": "Stored encrypted. Empty = no login."},
                             {"name": "from", "label": "From (empty = the username)", "type": "text", "default": ""},
                             {"name": "to", "label": "To (; or , between)", "type": "text", "default": "", "help": "Empty = {{data.to}} from the node before."},
                             {"name": "cc", "label": "Cc", "type": "text", "default": ""},
                             {"name": "bcc", "label": "Bcc", "type": "text", "default": ""},
                             {"name": "reply_to", "label": "Reply-To", "type": "text", "default": ""},
                             {"name": "subject", "label": "Subject", "type": "text", "default": "", "help": "Empty = {{data.subject}} from the node before."},
                             {"name": "body", "label": "Body", "type": "textarea", "default": "", "help": "Empty = {{data.body}}, else the text coming in."},
                             {"name": "format", "label": "Body format", "type": "select", "options": ["text", "html"], "default": "text"},
                             {"name": "attachments", "label": "Attachments (one path per line)", "type": "textarea", "default": ""},
                             {"name": "imap_host", "label": "IMAP server for drafts (empty = imap.<same domain>)", "type": "text", "default": ""},
                             {"name": "imap_port", "label": "IMAP port (993 SSL, 143 STARTTLS)", "type": "number", "default": 993},
                             {"name": "drafts_folder", "label": "Drafts folder", "type": "text", "default": "Drafts", "help": "Gmail: [Gmail]/Drafts"},
                             {"name": "insecure", "label": "Do not check the server's TLS certificate", "type": "checkbox", "default": False}]},
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
for _t in ("git.read", "git.pull"):                                   # a login for this call only: wins over the Vault / credential helper (https only, never argv or disk)
    BUILTIN[_t]["fields"] = BUILTIN[_t]["fields"] + [
        {"name": "username", "label": "Login username (overrides the Vault)", "type": "text", "default": "", "help": "Empty = x-access-token. Only used together with a token."},
        {"name": "token", "label": "Access token (overrides the Vault)", "type": "password", "help": "Stored encrypted. Empty = the Vault / credential helper answers as usual."}]
# which credential kind each builtin node takes, and which node field gets which set field ({set field: node field})
BUILTIN_CRED = {"send.smtp": ("smtp", {}), "io.request": ("http", {}), "git.read": ("git", {"username": "username", "token": "token"}), "git.pull": ("git", {"username": "username", "token": "token"}),
                "send.teams": ("teams", {"webhook_url": "webhook_url"}), "start.teams": ("teams", {"hook_secret": "secret"}),
                "send.slack": ("slack", {"webhook_url": "webhook_url"}), "send.discord": ("discord", {"webhook_url": "webhook_url"}),
                "start.slack": ("slack", {"signing_secret": "signing_secret"}), "start.telegram": ("telegram", {"bot_token": "bot_token"}), "send.telegram": ("telegram", {"bot_token": "bot_token"})}
for _t, (_k, _m) in BUILTIN_CRED.items():
    BUILTIN[_t]["credential"] = {"kind": _k, "map": _m}

CLAUDE_OUT = "the reply text. To hand a file or folder on, make Claude end the reply with `OUTPUT: /full/path`"


# ONE explicit input/output schema per node. inputs: [{name, type text|path|json|url, desc, required}]  (name `input` = the text of the parent node(s), {{input}};
# any other name = a standard data key taken from the node before, {{data.name}}, or a settings field of that name)
# outputs: [{name, type, desc, example}]  (name `text` = what is handed on as {{input}}; any other name = {{data.name}} for the nodes after).
def _i(name, type="text", desc="", required=False):
    return {"name": name, "type": type, "desc": desc, "required": required}


def _o(name, type="text", desc="", example=""):
    return {"name": name, "type": type, "desc": desc, "example": example}


_IN_TEXT = _i("input", "text", "the text of the node before (optional)")
_MAIL_KEYS = [_i(k, "text", "taken from the node before when the field is empty") for k in ("to", "subject", "body")]
BUILTIN_SCHEMA = {
    "start.manual": ([], [_o("text", "text", "the text you type for the run", "manual run"), _o("<input name>", "text", "each input you declare is {{data.name}}", "")]),
    "start.cron": ([], [_o("text", "text", "the text you set", "nightly check")]),
    "start.webhook": ([], [_o("text", "text", "the POSTed text", "deploy done"), _o("<key>", "json", "keys of the POSTed `data` object", "")]),
    "start.teams": ([], [_o("text", "text", "the message text", "deploy now"), _o("from", "text", "sender", "alice"), _o("chat", "text", "channel or chat name", "Ops")]),
    "ai.claude": ([_IN_TEXT, _i("path", "path", "folders/files named by a pointer node are opened by Claude")], [_o("text", "text", CLAUDE_OUT, "Done. OUTPUT: /Users/me/report/index.html")]),
    "ai.free": ([_IN_TEXT], [_o("text", "text", "the reply text", "A short summary...")]),
    "flow.filter": ([_i("input", "text", "the text tested against the regex", True)], [_o("text", "text", "the same input, or nothing (the branch stops)", "")]),
    "flow.note": ([_IN_TEXT], [_o("text", "text", "the same input", "")]),
    "flow.template": ([_IN_TEXT], [_o("text", "text", "the template filled in", "Hello ada")]),
    "flow.output": ([_IN_TEXT], [_o("text", "text", "the run's answer", "Report ready")]),
    "flow.memory": ([_IN_TEXT], [_o("text", "text", "remembered context + the input", "Remembered context: ...")]),
    "io.read_file": ([_i("path", "path", "a file (or folder) path; relative = the run folder (set in the node)", False)], [_o("text", "text", "the file's text", "col1,col2\n1,2"), _o("json", "json", "the parsed JSON when 'Parse' is JSON", "")]),
    "code.python": ([_IN_TEXT], [_o("text", "json", "the function's return value as JSON", '{"total": 3}'), _o("<key>", "text", "each key of a returned object is {{data.key}}", "")]),
    "io.request": ([_i("url", "url", "set in the node, may use {{data.x}}", False)], [_o("text", "json", "the answer text", '{"items": []}'), _o("status", "text", "HTTP status code", "200")]),
    "git.read": ([_i("url", "url", "repository URL", True)], [_o("text", "text", "the file text or folder listing", "# README ...")]),
    "git.pull": ([_i("url", "url", "repository URL", True)], [_o("text", "path", "the folder or file path, not its content", "/Users/me/.aihub/ui/git/repo"), _o("path", "path", "the same path as {{data.path}}", "/Users/me/.aihub/ui/git/repo")]),
    "io.path": ([_IN_TEXT], [_o("text", "path", "the path(s), not the content", "/Users/me/data"), _o("path", "path", "the same path as {{data.path}}", "/Users/me/data")]),
    "space.publish": ([_i("path", "path", "a file or folder (folder: index.html at its top): a path node before it, or a Claude task that ends with OUTPUT: /full/path", True)],
                      [_o("text", "url", "the space reference and link", "Published space me@dash http://hub/#/s/me@dash"), _o("url", "url", "the link as {{data.url}}", "http://hub/#/s/me@dash")]),
    "send.email": (_MAIL_KEYS, [_o("text", "text", "what was sent", "Sent to a@x.com")]),
    "send.smtp": (_MAIL_KEYS + [_i("attachments", "path", "one path per line, from the node before")],
                  [_o("text", "text", "what was sent or saved", "Sent to a@x.com"), _o("to", "text", "recipients", "a@x.com"), _o("subject", "text", "", "Report"), _o("attachments", "path", "", ""),
                   _o("action", "text", "send | draft | eml", "send"), _o("path", "path", "the .eml file for an eml draft", "")]),
    "send.teams": ([_IN_TEXT], [_o("text", "text", "what was posted", "posted")]),
    "start.slack": ([], [_o("text", "text", "the message text", "deploy now"), _o("from", "text", "Slack user ID", "U024BE7LH"), _o("channel", "text", "channel ID", "C024BE91L"),
                         _o("thread_ts", "text", "thread to reply in", "1700000000.0001"), _o("ts", "text", "message time", ""), _o("command", "text", "slash command, if any", "/ask"), _o("response_url", "text", "slash command reply URL", "")]),
    "start.telegram": ([], [_o("text", "text", "the message text", "hello"), _o("from", "text", "sender name", "alice"), _o("chat_id", "text", "chat to reply to", "123456"),
                            _o("chat", "text", "chat name", "Ops"), _o("message_id", "text", "message id", "42")]),
    "send.telegram": ([_IN_TEXT, _i("chat_id", "text", "set in the node, or from Start: Telegram", False)], [_o("text", "text", "what was sent", "sent"), _o("message_id", "text", "id of the sent message", "43")]),
    "send.slack": ([_IN_TEXT], [_o("text", "text", "what was posted", "posted")]),
    "send.discord": ([_IN_TEXT], [_o("text", "text", "what was posted", "posted")]),
    "send.webhook": ([_IN_TEXT], [_o("text", "text", "the receiver's answer", '{"ok": true}'), _o("status", "text", "HTTP status code", "200")]),
    "flow.loop": ([_i("input", "text", "the list: lines, comma separated, or a JSON list", True)],
                  [_o("text", "text", "the current item, once per pass", "a@x.com"), _o("item", "text", "the item as {{data.item}}", "a@x.com"), _o("index", "text", "1-based position", "1"), _o("total", "text", "how many items", "5")]),
    "flow.endloop": ([_i("input", "text", "the results of all passes", False)], [_o("text", "text", "the results of all passes, joined", "ok\nok"), _o("count", "text", "how many passes worked", "5"), _o("failed", "text", "how many failed", "0")]),
}


def _io_item(raw, out):
    """One schema entry from a dict (or just a name). None when unusable. Never raises."""
    if isinstance(raw, str):
        raw = {"name": raw}
    if not isinstance(raw, dict):
        return None
    nm = str(raw.get("name", ""))
    if not re.match(r"^(?:[A-Za-z_][A-Za-z0-9_]{0,39}|<[A-Za-z0-9 _-]{1,30}>)$", nm):
        return None
    it = {"name": nm, "type": raw.get("type") if raw.get("type") in OUT_KINDS else "text", "desc": str(raw.get("desc") or raw.get("description") or "")[:200]}
    if out:
        it["example"] = str(raw.get("example") or "")[:120]
    else:
        it["required"] = bool(raw.get("required"))
    return it


def norm_io(raw):
    """-> (inputs, outputs) of a node.json: the new `inputs`/`outputs` objects, or the old keys as a fallback
    (`out`/`out_note` = the `text` output, `outputs` as names = data keys, `needs`/`needs_note` = a required input)."""
    raw = raw if isinstance(raw, dict) else {}
    outs = [x for x in (_io_item(o, True) for o in (raw.get("outputs") if isinstance(raw.get("outputs"), list) else [])[:30]) if x]
    ins = [x for x in (_io_item(i, False) for i in (raw.get("inputs") if isinstance(raw.get("inputs"), list) else [])[:30]) if x]
    if not any(o["name"] == "text" for o in outs):
        outs.insert(0, {"name": "text", "type": raw.get("out") if raw.get("out") in OUT_KINDS else "text", "desc": str(raw.get("out_note", ""))[:160], "example": ""})
    if not ins:
        if raw.get("needs") in OUT_KINDS:
            ins = [{"name": "path" if raw.get("needs") == "path" else "input", "type": raw["needs"], "desc": str(raw.get("needs_note", ""))[:160], "required": True}]
        elif raw.get("kind") != "trigger":
            ins = [dict(_IN_TEXT)]
    return ins, outs


def legacy_of(ins, outs):
    """The old flat keys the editor and repair still read, derived from the schema."""
    text = next((o for o in outs if o["name"] == "text"), None) or {"type": "text", "desc": ""}
    need = next((i for i in ins if i.get("required") and i["type"] == "path"), None)
    return {"out": text["type"], "out_note": text["desc"], "needs": need["type"] if need else "", "needs_note": need["desc"] if need else "",
            "outputs": [o["name"] for o in outs if o["name"] != "text" and not o["name"].startswith("<")][:20]}


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


def _spec_cred(raw, fields):
    """A node declares the credential kind it takes: `credential: {kind, map: {set field: node field}}`."""
    cr = raw.get("credential")
    if not isinstance(cr, dict) or not KIND_ID.match(str(cr.get("kind", ""))):
        return {}
    names = {f["name"] for f in fields}
    mp = cr.get("map") if isinstance(cr.get("map"), dict) else {}
    return {"credential": {"kind": cr["kind"], "map": {str(k): str(v) for k, v in mp.items() if str(v) in names and FIELD_NAME.match(str(k))}}}


def _spec_io(raw):
    ins, outs = norm_io(raw)
    return dict(legacy_of(ins, outs), io={"inputs": ins, "outputs": outs})


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
                **_spec_io(raw),
                "takes": [f["name"] for f in fields if f["name"] in _names(raw.get("takes")) and not f["secret"]][:20],
                **_spec_cred(raw, fields),
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
    found = {}
    for p in pkgs:
        d = os.path.join(p.get("path") or "", "nodes")
        if p.get("enabled", True) is False or not os.path.isdir(d):
            continue
        for f in sorted(os.listdir(d)):
            if f.endswith(".cred.json"):                                 # a credential kind the package defines (fields, labels, secrets, hint)
                try:
                    with open(os.path.join(d, f), encoding="utf-8") as fh:
                        kd = _valid_kind(p["name"], json.load(fh))
                except (OSError, ValueError):
                    kd = None
                if kd:
                    found[kd["kind"]] = kd
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
        if t in BUILTIN_SCHEMA:
            ins, outs = BUILTIN_SCHEMA[t]
            s.update(legacy_of(ins, outs), io={"inputs": ins, "outputs": outs})
        elif "io" not in s:
            s.update(_spec_io(s))
    for s in out.values():
        s["fields"] = [dict(f, secret=bool(f.get("secret")) or f.get("type") == "password") for f in s["fields"]]
    for t, s in out.items():
        _default_assist(t, s)
    for s in out.values():
        if s["kind"] == "trigger" and not any(f["name"] == "filter" for f in s["fields"]):
            s["fields"] = s["fields"] + [FILTER]
    _cat["kinds"] = found
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
    """The config a node runs with: its own fields (stored secrets filled in) over the saved node it uses (`preset`), if any."""
    cfg = dict(node.get("config") or {})
    allv = None
    for f in spec["fields"]:
        if f.get("secret") and cfg.get(f["name"]) == MARK:
            allv = allv if allv is not None else box.get_all()
            cfg[f["name"]] = allv.get(_skey(work_id, node["id"], f["name"]), "")
    p = preset(node.get("preset"))
    base = None
    if p and p["type"] == node.get("type"):
        base = dict(p["config"])
        for f in spec["fields"]:
            if f.get("secret") and base.get(f["name"]) == MARK:
                allv = allv if allv is not None else box.get_all()
                base[f["name"]] = allv.get(_pkey(p["id"], f["name"]), "")
    cv = cred_values(node, spec, box)                                      # order: node override > credential set > saved node > defaults
    if cv:
        base = dict(base or {}, **cv)
    if base is not None:
        base.update({k: v for k, v in cfg.items() if v not in (None, "")})   # what this work typed wins; an empty field inherits
        cfg = base
    return cfg


# ---------- saved nodes: a node set up once (credentials too) that any work uses as it is, or with some fields changed

PRESET_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")


def _presets_file():
    return os.path.join(store.ui_dir(), "presets.json")


def _pkey(pid, field):
    return "preset:%s:%s" % (pid, field)


def presets():
    """-> {id: {id, name, type, description, config, updated}}. Secret fields hold MARK; the values are in the box."""
    d = store._read(_presets_file(), {})
    return {k: v for k, v in d.items() if PRESET_ID.match(k) and isinstance(v, dict) and isinstance(v.get("config"), dict)} if isinstance(d, dict) else {}


def preset(pid):
    return presets().get(pid) if isinstance(pid, str) and PRESET_ID.match(pid) else None


def preset_usage():
    """-> {preset id: [{id, title}] of the works that use it}."""
    out = {}
    for row in store.works():
        w = store.get_work(row["id"]) or {}
        for n in w.get("nodes", []):
            if n.get("preset"):
                lst = out.setdefault(n["preset"], [])
                if not any(x["id"] == w["id"] for x in lst):
                    lst.append({"id": w["id"], "title": w.get("title") or "Untitled"})
    return out


def presets_view():
    use, cat = preset_usage(), catalogue()
    return sorted(({**p, "used_in": use.get(p["id"], []), "missing": p["type"] not in cat} for p in presets().values()), key=lambda p: p["name"].lower())


def save_preset(p, box, copy=None):
    """Create or update a saved node. Secret fields: a typed value is moved into the box, MARK keeps the stored one, empty removes it.
    COPY = {"preset": id} or {"work": id, "node": id}: a new saved node takes the stored secrets of that one (Duplicate, Save as saved node)."""
    spec = catalogue().get(str(p.get("type") or ""))
    if not spec:
        raise ValueError("unknown node type %r" % p.get("type"))
    name = " ".join(str(p.get("name") or "").split())[:60]
    if not name:
        raise ValueError("give the saved node a name")
    have = presets()
    pid = str(p.get("id") or "")
    if pid and (not PRESET_ID.match(pid) or pid not in have):
        raise ValueError("no such saved node")
    if not pid:
        base = slug(name).replace("_", "-")[:32] or "node"
        pid, i = base, 1
        while pid in have:
            i += 1
            pid = "%s-%d" % (base, i)
    src = dict(p.get("config") or {})
    cfg, allv = {}, None
    for f in spec["fields"]:
        k, v = f["name"], src.get(f["name"])
        if not f.get("secret"):
            if v not in (None, ""):
                cfg[k] = v
            continue
        if isinstance(v, str) and v and v != MARK:
            box.put(_pkey(pid, k), v)
            cfg[k] = MARK
        elif v == MARK:
            old = None
            if copy and pid not in have:
                allv = allv if allv is not None else box.get_all()
                old = allv.get(_pkey(copy["preset"], k)) if copy.get("preset") else allv.get(_skey(copy.get("work"), copy.get("node"), k))
                if old:
                    box.put(_pkey(pid, k), old)
            if old or (pid in have and have[pid]["config"].get(k) == MARK):
                cfg[k] = MARK
        else:
            box.remove(_pkey(pid, k))
    rec = {"id": pid, "name": name, "type": spec["type"], "description": " ".join(str(p.get("description") or "").split())[:200], "config": cfg, "updated": time.time()}
    have[pid] = rec
    store._write(_presets_file(), have)
    return rec


def delete_preset(pid, box):
    have = presets()
    rec = have.pop(pid, None)
    if rec:
        for k, v in rec["config"].items():
            if v == MARK:
                box.remove(_pkey(pid, k))
        store._write(_presets_file(), have)
    return bool(rec)


# ---------- credentials: named sets (teams, smtp, git, http ...) several nodes share; a node can still override any single field

KIND_ID = re.compile(r"^[a-z][a-z0-9_-]{0,30}$")
FIELD_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,40}$")
CRED_KINDS = {                                     # builtin kinds; a package adds its own with nodes/*.cred.json
    "teams": {"title": "Microsoft Teams", "hint": "Tenant and app registration, plus the Workflows URL and the listener secret the Teams nodes use.",
              "fields": [{"name": "tenant_id", "label": "Tenant ID"}, {"name": "client_id", "label": "Client ID"}, {"name": "client_secret", "label": "Client secret", "secret": True},
                         {"name": "webhook_url", "label": "Workflow URL (send)", "secret": True}, {"name": "hook_secret", "label": "Listener secret (receive)", "secret": True}]},
    "smtp": {"title": "Mail server (SMTP)", "hint": "Login for send.smtp. A node can still set its own host, port, username or password.",
             "fields": [{"name": "host", "label": "SMTP server"}, {"name": "security", "label": "Security (starttls, ssl, none)"}, {"name": "port", "label": "Port"},
                        {"name": "username", "label": "Username"}, {"name": "password", "label": "Password / app password", "secret": True}]},
    "slack": {"title": "Slack", "hint": "Incoming Webhook URL (send) and signing secret (receive) for the Slack nodes.", "fields": [{"name": "webhook_url", "label": "Webhook URL (send)", "secret": True}, {"name": "signing_secret", "label": "Signing secret (receive)", "secret": True}]},
    "telegram": {"title": "Telegram bot", "hint": "Bot token from @BotFather, used by the Telegram nodes.", "fields": [{"name": "bot_token", "label": "Bot token", "secret": True}]},
    "discord": {"title": "Discord", "hint": "Channel webhook URL for the Discord node.", "fields": [{"name": "webhook_url", "label": "Webhook URL", "secret": True}]},
    "git": {"title": "Git login", "hint": "Used by the git nodes for that call only, instead of the Vault / credential helper. https only.",
            "fields": [{"name": "username", "label": "Username (empty = x-access-token)"}, {"name": "token", "label": "Access token", "secret": True}]},
    "http": {"title": "HTTP API", "hint": "Authorization header (and extra headers) for the HTTP request node.",
             "fields": [{"name": "authorization", "label": "Authorization value", "secret": True, "help": "e.g. Bearer abc123"}, {"name": "headers", "label": "Other headers (Name: value per line)"}]},
    "generic": {"title": "Generic key / values", "hint": "Any names; fields whose name looks like a secret (token, key, password ...) are stored encrypted. A node takes the ones named like its own fields.",
                "free": True, "fields": []},
}


def _valid_kind(pkg, raw):
    try:
        kid = str(raw["kind"])
        if not KIND_ID.match(kid):
            return None
        fields = [{"name": f["name"], "label": str(f.get("label", f["name"]))[:100], "secret": bool(f.get("secret")), "help": str(f.get("help", ""))[:300]}
                  for f in raw.get("fields", []) if isinstance(f, dict) and FIELD_NAME.match(str(f.get("name", "")))][:30]
        return {"kind": kid, "title": str(raw.get("title", kid))[:60], "hint": str(raw.get("hint", ""))[:400], "fields": fields, "free": bool(raw.get("free")), "package": pkg}
    except (KeyError, TypeError):
        return None


def kinds():
    """-> {kind: {kind, title, hint, fields, free, package}}: builtin kinds plus those shipped by installed packages (the same kind in a package replaces the builtin)."""
    catalogue()
    out = {k: dict(v, kind=k, package="", free=bool(v.get("free")), fields=[dict({"secret": False, "help": ""}, **f) for f in v["fields"]]) for k, v in CRED_KINDS.items()}
    out.update(_cat.get("kinds") or {})
    return out


def _ckey(cid, field):
    return "cred:%s:%s" % (cid, field)


def _creds_file():
    return os.path.join(store.ui_dir(), "credentials.json")


def credentials():
    """-> {id: {id, name, kind, fields, updated}}. Secret fields hold MARK; the values are in the box under cred:<id>:<field>."""
    d = store._read(_creds_file(), {})
    return {k: v for k, v in d.items() if PRESET_ID.match(k) and isinstance(v, dict) and isinstance(v.get("fields"), dict)} if isinstance(d, dict) else {}


def credential(cid):
    return credentials().get(cid) if isinstance(cid, str) and PRESET_ID.match(cid) else None


def _is_secret_field(kd, name):
    f = next((x for x in kd["fields"] if x["name"] == name), None)
    return bool(f["secret"]) if f else bool(kd.get("free")) and bool(SECRETISH.search(name))


def cred_map(spec, kd=None):
    """{set field: node field} a node takes from its credential set (spec `credential.map`, else the same names)."""
    cs = spec.get("credential") or {}
    if cs.get("map"):
        return dict(cs["map"])
    names = {f["name"] for f in spec.get("fields", [])}
    if kd and not kd.get("free"):
        return {f["name"]: f["name"] for f in kd["fields"] if f["name"] in names}
    return {n: n for n in names}


def cred_values(node, spec, box):
    """{node field: value} the node's credential set provides (secrets filled in from the box); {} when none / missing / the wrong kind."""
    cs = spec.get("credential")
    c = credential(node.get("credential")) if cs else None
    if not c or c["kind"] != cs["kind"]:
        return {}
    kd = kinds().get(c["kind"]) or {"fields": [], "free": True}
    names, allv, out = {f["name"] for f in spec["fields"]}, None, {}
    for sf, nf in cred_map(spec, kd).items():
        v = c["fields"].get(sf)
        if v == MARK:
            allv = allv if allv is not None else box.get_all()
            v = allv.get(_ckey(c["id"], sf), "")
        if nf in names and v not in (None, ""):
            out[nf] = v
    return out


def credential_problem(node, spec):
    """Why this node cannot get its credentials ('' = fine or none wanted)."""
    cs = (spec or {}).get("credential")
    cid = node.get("credential")
    if cid:
        c = credential(cid)
        if not c:
            return "the credential set '%s' this node uses was deleted: pick another set or fill in the fields, then remove the link" % cid
        if cs and c["kind"] != cs["kind"]:
            return "the credential set '%s' is a '%s' set but this node needs a '%s' one" % (c["name"], c["kind"], cs["kind"])
    elif node.get("credential_kind") and cs:
        return "this node needs a '%s' credential set (the workflow was shared without logins): pick or create one in the node's settings (Settings > Credentials)" % node["credential_kind"]
    return ""


def credential_usage():
    """-> {set id: [{id, title, nodes: [labels]}] of the works that use it}."""
    out = {}
    for row in store.works():
        w = store.get_work(row["id"]) or {}
        for n in w.get("nodes", []):
            if n.get("credential"):
                lst = out.setdefault(n["credential"], [])
                hit = next((x for x in lst if x["id"] == w["id"]), None)
                if not hit:
                    hit = {"id": w["id"], "title": w.get("title") or "Untitled", "nodes": []}
                    lst.append(hit)
                hit["nodes"].append(n.get("label") or n.get("type") or n["id"])
    return out


def credentials_view():
    use, ks = credential_usage(), kinds()
    return sorted(({**c, "used_in": use.get(c["id"], []), "missing": c["kind"] not in ks} for c in credentials().values()), key=lambda c: c["name"].lower())


def save_credential(p, box):
    """Create or update a set. A typed secret goes into the box and the file keeps MARK; MARK keeps the stored one; empty removes it."""
    ks = kinds()
    kd = ks.get(str(p.get("kind") or ""))
    if not kd:
        raise ValueError("unknown credential kind %r" % p.get("kind"))
    name = " ".join(str(p.get("name") or "").split())[:60]
    if not name:
        raise ValueError("give the credential set a name")
    have = credentials()
    cid = str(p.get("id") or "")
    if cid and (not PRESET_ID.match(cid) or cid not in have):
        raise ValueError("no such credential set")
    if cid and have[cid]["kind"] != kd["kind"]:
        raise ValueError("the kind of a set cannot change")
    if not cid:
        base = slug(name).replace("_", "-")[:32] or "credentials"
        cid, i = base, 1
        while cid in have:
            i += 1
            cid = "%s-%d" % (base, i)
    src = p.get("fields") if isinstance(p.get("fields"), dict) else {}
    names = [f["name"] for f in kd["fields"]] + ([k for k in src if FIELD_NAME.match(str(k)) and k not in {f["name"] for f in kd["fields"]}][:30] if kd.get("free") else [])
    fields = {}
    for k in names:
        v, sec = src.get(k), _is_secret_field(kd, k)
        if not sec:
            if v not in (None, ""):
                fields[k] = str(v)[:4000]
        elif isinstance(v, str) and v and v != MARK:
            box.put(_ckey(cid, k), v)
            fields[k] = MARK
        elif v == MARK and cid in have and have[cid]["fields"].get(k) == MARK:
            fields[k] = MARK
        else:
            box.remove(_ckey(cid, k))
    for k, v in have.get(cid, {}).get("fields", {}).items():       # a field dropped from a free set loses its stored secret
        if v == MARK and k not in fields:
            box.remove(_ckey(cid, k))
    rec = {"id": cid, "name": name, "kind": kd["kind"], "fields": fields, "updated": time.time()}
    have[cid] = rec
    store._write(_creds_file(), have)
    return rec


def delete_credential(cid, box):
    have = credentials()
    rec = have.pop(cid, None)
    if rec:
        for k, v in rec["fields"].items():
            if v == MARK:
                box.remove(_ckey(cid, k))
        store._write(_creds_file(), have)
    return bool(rec)


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
        shutil.rmtree(os.path.join(d, f[:-5]), ignore_errors=True)         # its node logs
        for sid in chats:
            try:
                if isinstance(sid, str) and (store.get_session(sid) or {}).get("work"):
                    store.delete_session(sid)
            except Exception:
                pass


def drop_work(wid):
    """Delete a work for good: stop its queued/running runs, delete the chats its runs created, then the work file and ALL its run records, logs
    and run folders (~/.aihub/ui/runs/<work>). -> number of runs stopped. Triggers end when the caller runs Engine.sync()."""
    stopped = 0
    with CTL_LOCK:
        live = [rid for rid, c in CTL.items() if c.kind == "work" and c.work == wid]
    for rid in live:
        stopped += bool(stop_run(rid))
    d = runs_dir(wid) if store.ID.match(str(wid)) else None
    if d and os.path.isdir(d):
        for f in os.listdir(d):
            if not f.endswith(".json"):
                continue
            try:
                with open(os.path.join(d, f), encoding="utf-8") as fh:
                    r = json.load(fh)
                for sid in r.get("chats") or ([r["chat"]] if r.get("chat") else []):
                    if isinstance(sid, str) and (store.get_session(sid) or {}).get("work"):
                        store.delete_session(sid)
            except Exception:
                pass
    store.delete_work(wid)
    return stopped


def list_runs(wid):
    d = runs_dir(wid)
    out = []
    for f in sorted((x for x in os.listdir(d) if x.endswith(".json")), reverse=True)[:keep_of(wid)]:
        try:
            with open(os.path.join(d, f), encoding="utf-8") as fh:
                r = json.load(fh)
            out.append({k: r.get(k) for k in ("id", "status", "started", "ended", "via", "text", "chat", "result", "origin", "card", "trigger", "error")})
        except (OSError, ValueError):
            pass
    return out


CHAT_RUNS = "chat"                                                     # runs/chat/: nodes run alone from a chat card (workflow runs stay with their work)
CHAT_KEEP = 200


def save_chat_run(rec):
    """Write the record of a node run started from a chat (secret fields never in it)."""
    d = runs_dir(CHAT_RUNS)
    os.makedirs(d, exist_ok=True)
    write_private(os.path.join(d, rec["id"] + ".json"), json.dumps(rec, ensure_ascii=False).encode("utf-8"))
    files = sorted(f for f in os.listdir(d) if f.endswith(".json"))
    for f in files[:-CHAT_KEEP]:
        try:
            os.remove(os.path.join(d, f))
            shutil.rmtree(os.path.join(d, f[:-5]), ignore_errors=True)
        except OSError:
            pass


def new_chat_run(spec, origin, card, cfg, text, rid=None):
    """A run record for one node started from a chat card. Secret field values are left out. RID: an id the card chose (so it can follow the log
    while the run is going); ignored unless it looks like an id and is not used yet."""
    secret = {f["name"] for f in spec.get("fields", []) if f.get("secret")}
    if not (isinstance(rid, str) and re.match(r"^[0-9a-z-]{8,40}$", rid) and not os.path.exists(os.path.join(runs_dir(CHAT_RUNS), rid + ".json")) and rid not in CTL):
        rid = time.strftime("%Y%m%d%H%M%S-") + uuid.uuid4().hex[:6]
    return {"id": rid, "kind": "node", "type": spec["type"], "title": spec.get("title") or spec["type"],
            "status": "running", "started": time.time(), "ended": None, "via": "chat", "origin": origin, "card": str(card or "")[:40],
            "config": {k: v for k, v in (cfg if isinstance(cfg, dict) else {}).items() if k not in secret and isinstance(k, str)}, "text": (text or "")[:300], "result": None}


def chat_runs(origin=None, limit=60):
    """Runs started from chats, newest first: node runs (runs/chat/) and workflow runs whose `origin` is a chat."""
    out, d = [], runs_dir(CHAT_RUNS)
    for f in sorted(os.listdir(d), reverse=True)[:CHAT_KEEP] if os.path.isdir(d) else ():
        try:
            with open(os.path.join(d, f), encoding="utf-8") as fh:
                r = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(r, dict) and (not origin or r.get("origin") == origin):
            out.append({k: r.get(k) for k in ("id", "kind", "type", "title", "status", "started", "ended", "origin", "card", "text", "result", "error")})
    for w in store.works():
        for r in list_runs(w["id"]):
            if r.get("origin") and (not origin or r["origin"] == origin):
                out.append(dict(r, kind="work", work=w["id"], title=w.get("title") or "Workflow"))
    out.sort(key=lambda r: -(r.get("started") or 0))
    return out[:limit]


def get_chat_run(rid):
    if not re.match(r"^[0-9a-z-]{8,40}$", str(rid)):
        return None
    try:
        with open(os.path.join(runs_dir(CHAT_RUNS), rid + ".json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


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


class Stopped(RuntimeError):
    """The person pressed Stop: not a failure."""


LOG_MAX = 1_000_000                                                    # bytes kept per node log (the tail stays)
NODE_FILE = re.compile(r"[^A-Za-z0-9_-]")


def run_dir(wid, rid, nid=None, make=True):
    """The ONE working folder of a run: ~/.aihub/ui/runs/<work>/<run>/work/, and per node <that>/<node id>/. Claude / Free chat nodes without a
    `cwd` work in it, package programs get it as `workdir` and in `run.dir` / `run.node_dir` of their stdin; templates see {{run.dir}} / {{node.dir}}."""
    d = os.path.join(runs_dir(wid), rid, "work")
    if nid:
        d = os.path.join(d, NODE_FILE.sub("_", nid)[:60])
    if make:
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass
    return d


def run_files(wid, rid, limit=300):
    """-> [{path (relative to the run folder), node, size, mtime}] of the files written in the run folder, newest first."""
    root, out = run_dir(wid, rid, make=False), []
    for dp, dn, names in os.walk(root):
        dn[:] = [x for x in dn if x != ".git" and x != "node_modules"]
        for n in names:
            fp = os.path.join(dp, n)
            try:
                st = os.stat(fp)
            except OSError:
                continue
            rel = os.path.relpath(fp, root).replace(os.sep, "/")
            out.append({"path": rel, "node": rel.split("/")[0] if "/" in rel else "", "size": st.st_size, "mtime": st.st_mtime})
            if len(out) > 5000:
                break
    out.sort(key=lambda x: -x["mtime"])
    return out[:limit]


def log_path(wid, rid, nid):
    """~/.aihub/ui/runs/<work>/<run>/<node>.log (node runs from a chat: runs/chat/<run>/node.log)."""
    return os.path.join(runs_dir(wid), rid, NODE_FILE.sub("_", nid or "node")[:60] + ".log")


class NodeLog:
    """Append-only log of one node execution: command, stdout/stderr lines, steps, exit code. Secret values are replaced by ***,
    the file is capped at LOG_MAX (the newest half is kept) and `<file>.base` remembers how many bytes were dropped, so a reader's
    `after` offset stays valid while the node runs."""

    def __init__(self, path, secrets=()):
        self.path = path
        self.secrets = sorted({s for s in secrets if isinstance(s, str) and len(s) >= 4}, key=len, reverse=True)
        self.lock = threading.Lock()
        self.base = 0
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb"):
            pass
        self.size = 0
        try:
            os.remove(path + ".base")
        except OSError:
            pass

    def clean(self, text):
        for s in self.secrets:
            text = text.replace(s, "***")
        return text

    def write(self, text, tag=""):
        t = self.clean(str(text if text is not None else "")).rstrip("\n")
        stamp = time.strftime("%H:%M:%S ")
        data = "".join("%s%s%s\n" % (stamp, tag, ln) for ln in (t.split("\n") if t else [""])).encode("utf-8", "replace")
        with self.lock:
            try:
                with open(self.path, "ab") as f:
                    f.write(data)
                self.size += len(data)
                if self.size > LOG_MAX:
                    self._cut()
            except OSError:
                pass

    def _cut(self):
        with open(self.path, "rb") as f:
            old = f.read()
        k = len(old) - LOG_MAX // 2
        nl = old.find(b"\n", k)
        k = nl + 1 if nl >= 0 else k
        mark = b"... [earlier output cut]\n"
        new = mark + old[k:]
        with open(self.path, "wb") as f:
            f.write(new)
        self.base += k - len(mark)
        self.size = len(new)
        write_private(self.path + ".base", str(self.base).encode())


def read_log(wid, rid, nid, after=0, limit=200_000):
    """-> {"text", "next", "size"} from byte offset AFTER of the logical stream (offsets stay valid after a cut). None if there is no log."""
    if not (store.ID.match(str(wid)) or wid == CHAT_RUNS) or not re.match(r"^[0-9a-z-]{8,40}$", str(rid)):
        return None
    p = log_path(wid, rid, nid)
    try:
        base = int(open(p + ".base").read() or 0)
    except (OSError, ValueError):
        base = 0
    try:
        with open(p, "rb") as f:
            f.seek(max(0, int(after or 0) - base))
            raw = f.read(limit)
            pos = f.tell()
            end = os.fstat(f.fileno()).st_size
    except OSError:
        return None
    return {"text": raw.decode("utf-8", "replace"), "next": base + pos, "size": base + end}


class Ctl:
    """Stop switch of one run (work run or node run from a chat): the processes and AI chats it started."""

    def __init__(self, kind, run, work=None, total=0):
        self.kind, self.run, self.work, self.total = kind, run, work, total
        self.cancel = threading.Event()
        self.procs = set()
        self.chats = set()
        self.lock = threading.Lock()

    def add(self, p):
        with self.lock:
            self.procs.add(p)
        if self.cancel.is_set():
            _kill_tree(p)

    def drop(self, p):
        with self.lock:
            self.procs.discard(p)

    def stop(self):
        self.cancel.set()
        with self.lock:
            procs, chats = list(self.procs), list(self.chats)
        for p in procs:
            _kill_tree(p)
        for sid in chats:
            try:
                chat.stop(sid)
            except Exception:
                pass

    def check(self):
        if self.cancel.is_set():
            raise Stopped("stopped")


CTL = {}                                                               # run id -> Ctl, while the run is queued or running
CTL_LOCK = threading.Lock()


def ctl_for(rid, *a, **k):
    with CTL_LOCK:
        c = CTL.get(rid)
        if c is None:
            c = CTL[rid] = Ctl(*a, **k)
        return c


def ctl_drop(rid):
    with CTL_LOCK:
        CTL.pop(rid, None)


def stop_run(rid):
    """Stop a queued or running run. -> True when there was one."""
    with CTL_LOCK:
        c = CTL.get(rid)
    if not c:
        return False
    c.stop()
    return True


def active():
    """Everything queued or running now: work runs and node runs from chats. Cheap (memory only)."""
    with CTL_LOCK:
        items = list(CTL.items())
    out, now = [], time.time()
    for rid, c in items:
        r = c.run
        nodes = r.get("nodes") or {}
        recs = list(nodes.values())
        cur = [v.get("label") or k for k, v in list(nodes.items()) if v.get("status") == "running"]
        cur_ids = [k for k, v in list(nodes.items()) if v.get("status") == "running"]
        done = sum(1 for v in recs if v.get("status") in ("ok", "error", "stopped", "skipped"))
        out.append({"id": rid, "kind": c.kind, "work": c.work if c.kind == "work" else CHAT_RUNS, "title": r.get("title") or "", "status": r.get("status") or "queued",
                    "via": r.get("via") or "", "origin": r.get("origin") or "", "started": r.get("started"), "elapsed": round(now - (r.get("started") or now), 1),
                    "current": cur[:3], "current_ids": cur_ids[:3], "done": done, "total": c.total, "stopping": c.cancel.is_set(), "chat": r.get("chat") or ""})
    out.sort(key=lambda x: x.get("started") or 0)
    return out


def log_ai(ev, nlog, steps):
    """Record one chat event of an AI node: the answer text and every tool call (a short step list for the UI, the full lines in the log)."""
    if ev.get("k") == "msg":
        for b in ev.get("blocks") or ():
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text" and str(b.get("text") or "").strip():
                nlog.write("answer: " + str(b["text"]).strip()[:600])
            elif b.get("type") == "tool_use":
                arg = b.get("input")
                brief = ""
                if isinstance(arg, dict):
                    brief = next((str(v) for k in ("command", "file_path", "path", "pattern", "url", "query", "description") if (v := arg.get(k))), "")
                nlog.write("tool %s %s" % (b.get("name") or "?", brief[:200]))
                if len(steps) < 40:
                    steps.append(_clip("%s %s" % (b.get("name") or "tool", brief), 90))
    elif ev.get("k") in ("error", "note") or (ev.get("k") == "result" and ev.get("error")):
        nlog.write("%s: %s" % (ev.get("k"), ev.get("text") or ""))


def _pump(stream, sink, log, tag, cap=2_000_000):
    """Read LINES from a pipe until EOF: keep them (capped) and write them to the log at once."""
    n = 0
    try:
        for line in iter(stream.readline, ""):
            if n < cap:
                sink.append(line)
                n += len(line)
            if log:
                log.write(line, tag)
    except (OSError, ValueError):
        pass


def run_package_node(spec, cfg, inputs, ctx, wd, log=None, ctl=None, info=None):
    """Run a node program: JSON on stdin, last JSON line on stdout. stdout/stderr stream into LOG while it runs; the call ends when the
    PROGRAM ends (a detached grandchild that keeps the pipes open, such as the CLI's background updater, never holds it up).
    INFO gets cmd and exit; CTL lets Stop kill the process group."""
    argv = _argv(spec)
    limit = spec["timeout"]
    if spec.get("timeout_key"):                                     # a node that lets the person set its own limit
        try:
            limit = min(max(int(cfg.get(spec["timeout_key"]) or limit), 1), 3600)
        except (TypeError, ValueError):
            pass
    payload = json.dumps({"config": cfg, "input": "\n\n".join(text_of(i) for i in inputs), "inputs": inputs, "run": ctx, "workdir": wd}, ensure_ascii=False)
    if ctl:
        ctl.check()
    cmd = " ".join(argv)
    if info is not None:
        info["cmd"] = log.clean(cmd) if log else cmd
    if log:
        log.write("$ " + cmd + "   (timeout %ds, cwd %s)" % (limit, spec["dir"]))
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                                cwd=spec["dir"], start_new_session=True,
                                env=dict(os.environ, AIHUB_NODE=spec["type"], AIHUB_NODE_WORKDIR=wd, AIHUB_NO_UPDATE="1", AIHUB_RUN_DIR=str((ctx or {}).get("dir") or wd), AIHUB_NODE_DIR=str((ctx or {}).get("node_dir") or "")))
    except OSError as e:
        if log:
            log.write("cannot start: %s" % e)
        raise RuntimeError("cannot start: %s" % e)
    if ctl:
        ctl.add(proc)
    so, se = [], []
    readers = [threading.Thread(target=_pump, args=(proc.stdout, so, log, ""), daemon=True),
               threading.Thread(target=_pump, args=(proc.stderr, se, log, "[err] "), daemon=True)]

    def feed():
        try:
            proc.stdin.write(payload)
            proc.stdin.close()
        except (OSError, ValueError):
            pass
    readers.append(threading.Thread(target=feed, daemon=True))
    for t in readers:
        t.start()
    try:
        try:
            proc.wait(timeout=limit)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            if log:
                log.write("timed out after %ds: killed" % limit)
            raise RuntimeError("timed out after %ds" % limit)
        end = time.time() + 3                                       # the program is done; give the pipes a moment, then go on without them
        for t in readers[:2]:
            t.join(max(0.05, end - time.time()))
    finally:
        if ctl:
            ctl.drop(proc)
        if proc.poll() is None:
            _kill_tree(proc)
        for t, st in zip(readers[:2], (proc.stdout, proc.stderr)):
            if not t.is_alive():
                try:
                    st.close()
                except OSError:
                    pass
    if info is not None:
        info["exit"] = proc.returncode
    if log:
        log.write("exit %s" % proc.returncode)
    if ctl and ctl.cancel.is_set():
        raise Stopped("stopped")
    stdout, stderr = "".join(so), "".join(se)
    last = next((ln for ln in reversed(stdout.strip().splitlines()) if ln.strip().startswith("{")), "")
    try:
        res = json.loads(last) if last else {}
    except ValueError:
        res = {}
    if proc.returncode != 0 or res.get("ok") is False:
        raise RuntimeError((res.get("error") or stderr.strip()[-400:] or "exit %s" % proc.returncode))
    out = res.get("output", "")
    ev = out if isinstance(out, dict) else {"text": out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)}
    if "data" in res:
        ev["data"] = res["data"]
    return ev, (res.get("log") or "")


# ---------- a node run on its own, from a chat (`@node:<type>` + a ```node block)

def runnable(spec):
    """A node a chat can run alone: an action with a program (not Claude / flow nodes, which need a work around them)."""
    return bool(spec) and spec.get("kind") == "action" and bool(spec.get("run")) and not spec.get("hidden")


def cred_fields(spec):
    """Names of the node fields a credential set of the node's kind can fill (what the model must not ask for)."""
    cs = spec.get("credential")
    if not cs:
        return []
    kd = kinds().get(cs["kind"]) or {"fields": [], "free": True}
    return sorted(set(cred_map(spec, kd).values()))


def node_brief(spec, sets=None):
    """Prompt text for a mentioned node: what it does, its fields, and the protocol (ask for what is missing, else a ```node block)."""
    lines = []
    for f in spec["fields"]:
        extra = [f["type"]] + (["required"] if f.get("required") else []) + (["options: " + ", ".join(f["options"])] if f.get("options") else []) + (["default: %s" % f["default"]] if f.get("default") not in ("", None) else [])
        if f.get("secret"):
            lines.append("- %s (%s): SECRET. Never ask for it and never write it; the card shows a password box to the user." % (f["name"], f["label"]))
        else:
            lines.append("- %s (%s; %s)%s" % (f["name"], f["label"], ", ".join(extra), (": " + f["help"]) if f.get("help") else ""))
    cf = cred_fields(spec)
    cred = ""
    if cf:
        names = [str(c.get("name"))[:40] for c in (sets or [])]
        cred = ("\nCredentials: this node takes a saved credential set of kind \"%s\" (fields it fills: %s). %s The card has a picker for it. Do NOT ask the user for those fields and never write their values."
                % (spec["credential"]["kind"], ", ".join(cf), ("Saved sets: " + ", ".join(names) + ".") if names else "No set is saved yet: the user can fill the fields in the card."))
    need = [f for f in spec["fields"] if f.get("required") and not f.get("secret") and f["name"] not in cf]
    return ("[The user wants the node \"%s\" (@node:%s) run from this chat. %s%s\nFields:\n%s\n"
            "How to run it: work out each field from the user's message and this conversation. If a required field (%s) is still unknown, ask for ONLY the missing ones with a ```form block "
            "(fields named exactly like the node's, same types/options) and stop; do not guess values. Once every required field is known, answer with ONE ```node block and nothing that depends on its result: "
            "{\"type\":\"%s\",\"config\":{\"field\":\"value\"},\"input\":\"optional text for {{input}}\"} (config holds non-secret fields only, values as the field types expect). "
            "The page shows a card where the user checks the values and presses Run; the result comes back as the next message. Do not run the program yourself.]"
            % (spec["title"], spec["type"], spec.get("desc") or "", ("\nGuide: " + spec["guide"]) if spec.get("guide") else "", ("\n".join(lines) or "(no fields)") + cred,
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


def run_single(spec, cfg, text, wd, rid=None, rec=None, box=None):
    """Run one node alone. -> {"text", "data", "log", "exit", "cmd"}. Templated fields see {{input}}; secrets come in `cfg` for this run only.
    With RID the run has a log (runs/chat/<rid>/node.log) and can be stopped (stop_run); REC is the chat run record (kept up to date)."""
    cfg = clean_node_config(spec, cfg)
    ev = {"text": text}
    ctx = {"input": text, "data": {}, "in": {}}
    tpl = spec.get("templated") or ()
    if tpl:
        cfg = {k: (render(v, ctx) if k in tpl and isinstance(v, str) else v) for k, v in cfg.items()}
    ctl = log = None
    info = {}
    if rid:
        t0 = time.time()
        secrets = [cfg.get(f["name"]) for f in spec["fields"] if f.get("secret")] + list((box.get_all() if box else {}).values())
        log = NodeLog(log_path(CHAT_RUNS, rid, "node"), [x for x in secrets if isinstance(x, str)])
        ctl = ctl_for(rid, "node", rec if rec is not None else {"status": "running", "started": t0}, total=1)
        nrec = {"status": "running", "label": spec.get("title") or spec["type"], "started": t0, "t0": t0, "logfile": True}
        if rec is not None:
            rec["nodes"] = {"node": nrec}
    try:
        out, lg = run_package_node(spec, cfg, [ev] if text else [], {"id": "chat", "work": "", "node": spec["type"], "dir": wd, "node_dir": wd}, wd, log=log, ctl=ctl, info=info)
    except BaseException as e:
        if rid and rec is not None:
            nrec.update(status="stopped" if isinstance(e, Stopped) else "error", ended=time.time(), error=str(e)[:300], **info)
        raise
    finally:
        if rid:
            ctl_drop(rid)
    if rid and rec is not None:
        nrec.update(status="ok", ended=time.time(), **info)
    return {"text": text_of(out)[:20000], "data": out.get("data") if isinstance(out, dict) else None, "log": (lg or "")[-1500:], **info}


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
    def fire(self, work, node, event, via, extra=None):
        """Queue a run for NODE with EVENT (after the trigger's own filter). -> run id or None when filtered out. EXTRA: more record fields (origin chat, card)."""
        if not accepts(node, event):
            return None
        run = {"id": time.strftime("%Y%m%d%H%M%S-") + uuid.uuid4().hex[:6], "work": work["id"], "status": "queued", "started": time.time(), "ended": None,
               "via": via, "trigger": node["id"], "text": text_of(event)[:300], "nodes": {}, "chat": None, "result": None}
        run.update({k: v for k, v in (extra or {}).items() if k in ("origin", "card")})
        try:                                                              # what started it, so the run can be started again with the same inputs
            ev = {"text": text_of(event)[:20000], "data": event.get("data") if isinstance(event, dict) else {}}
            if len(json.dumps(ev, ensure_ascii=False, default=str)) < 60000:
                run["event"] = ev
        except (TypeError, ValueError):
            pass
        run["title"] = work.get("title") or "Workflow"
        ctl_for(run["id"], "work", run, work=work["id"], total=len(work.get("nodes") or ()))
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
            ctl_drop(run["id"])
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
            ctl = ctl_for(run["id"], "work", run, work=run["work"])
            if ctl.cancel.is_set():                                          # stopped while it waited in the queue
                run.update(status="stopped", ended=time.time())
                _save_run(run)
                ctl_drop(run["id"])
                continue
            with self.lock:
                self.running += 1
            try:
                self._execute(run, event)
            except Exception as e:                                           # never kill the worker
                run.update(status="stopped" if ctl.cancel.is_set() else "failed", ended=time.time(), error="" if ctl.cancel.is_set() else str(e)[:300])
                _save_run(run)
            finally:
                ctl_drop(run["id"])
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
        run["title"] = work.get("title") or "Workflow"
        ctl = ctl_for(run["id"], "work", run, work=work["id"], total=len(nodes))
        ctl.total = len(nodes)
        rdir = run["dir"] = run_dir(work["id"], run["id"])
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
                if self.stop.is_set() or ctl.cancel.is_set():
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
                    run["nodes"][nid] = {"status": "ok", "output": text_of(event)[:2000], "label": n.get("label") or "", "started": run.get("started"), "ended": run.get("started")}
                return
            if ctl.cancel.is_set():                                         # Stop was pressed: the rest of the run is skipped
                with lock:
                    run["nodes"][nid] = {"status": "skipped", "label": n.get("label") or names[nid][0]}
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
            ndir = run_dir(work["id"], run["id"], nid, make=bool(spec.get("run")) or n.get("type", "").startswith("ai."))
            ctx = dict(ctx_base, input=text, data=cdata, run={"id": run["id"], "dir": rdir}, node={"id": nid, "dir": ndir}, **{"in": inmap})
            ctx_of[nid] = ctx
            with lock:
                rec = run["nodes"][nid] = {"status": "running", "t0": time.time(), "started": time.time(), "label": n.get("label") or names[nid][0], "logfile": True, "input": text[:2000], "paths": _pointers(cdata, inputs),
                                           "inputs": [{"from": u, "label": names[u][0], "kind": (cat.get((nodes.get(u) or {}).get("type")) or {}).get("out", "text"), "text": text_of(o)[:1200]} for u, o in got]}
            save()
            cfg = with_secrets(work["id"], n, spec, self.box)
            nlog = NodeLog(log_path(work["id"], run["id"], nid), [cfg.get(f["name"]) for f in spec["fields"] if f.get("secret")] + list(self.box.get_all().values()))
            nlog.write("node %s (%s) started" % (n.get("label") or nid, n.get("type")))
            steps = rec["steps"] = []
            mine = []                                                      # shared chats this node holds (released when it ends)
            try:
                bad = credential_problem(n, spec)
                if bad:
                    raise RuntimeError(bad)
                if n.get("preset") and not preset(n["preset"]):
                    raise RuntimeError("the saved node '%s' this node uses was deleted: pick another one or fill in the fields, then remove the link" % n["preset"])
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
                            s = store.update_session(store.create_session("Work: %s" % work["title"], "", chat.build_system(chat.freechat.WORK_RULES), cwd=folder or rdir)["id"], work=True)
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
                    ctl.chats.add(cur["id"])
                    upd(rec, chat=cur["id"], cmd="claude %s (mode %s)" % (cfg.get("model") or "default model", cfg.get("mode") or "auto"))
                    nlog.write("Claude Code chat %s: %d characters of prompt" % (cur["id"], len(prompt)))
                    ctl.check()
                    for ev in chat.turn(cur, prompt, str(cfg.get("model") or ""), str(cfg.get("effort") or ""), cfg.get("mode") if cfg.get("mode") in chat.MODES else "auto",
                                         extra_env=self.box.get_all(), **({"add_dirs": dirs} if (dirs := pointer_dirs(cdata, inputs)) else {})):
                        log_ai(ev, nlog, steps)
                        if ev["k"] == "msg":
                            reply = "\n".join(b["text"] for b in ev["blocks"] if b["type"] == "text") or reply
                        elif ev["k"] == "error" or (ev["k"] == "result" and ev.get("error")):
                            err = ev.get("text") or "Claude failed"
                    ctl.check()
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
                            made = store.update_session(store.create_session("Work: %s" % work["title"], "", chat.build_system(), cwd=rdir)["id"], engine="free", work=True,
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
                    ctl.chats.add(made["id"])
                    upd(rec, chat=made["id"], cmd="free chat %s" % (cfg.get("model") or "default model"))
                    nlog.write("Free chat %s: %d characters of prompt" % (made["id"], len(prompt)))
                    ctl.check()
                    for ev in freechat.turn(store.get_session(made["id"]), prompt, str(cfg.get("model") or ""), str(cfg.get("effort") or ""), "auto", extra_env=self.box.get_all()):
                        log_ai(ev, nlog, steps)
                        if ev["k"] == "msg":
                            reply = "\n".join(b["text"] for b in ev["blocks"] if b["type"] == "text") or reply
                        elif ev["k"] == "error" or (ev["k"] == "result" and ev.get("error")):
                            err = ev.get("text") or "Free chat failed"
                    ctl.check()
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
                    out, log = run_package_node(spec, cfg, inputs, {"id": run["id"], "work": work["id"], "node": nid, "dir": rdir, "node_dir": ndir}, rdir,
                                               log=nlog, ctl=ctl, info=rec)
                    if log:
                        upd(rec, log=log[-1500:])
                    if isinstance(out, dict) and out.get("skip"):
                        out = None
                with lock:
                    rec.update(status="ok", ended=time.time(), output=(text_of(out)[:4000] if out is not None else ""))
                    outs[nid] = out
                nlog.write("finished ok in %.1fs" % (rec["ended"] - rec["t0"]))
            except Exception as e:
                if ctl.cancel.is_set() or isinstance(e, Stopped):
                    with lock:
                        rec.update(status="stopped", ended=time.time())
                    nlog.write("stopped")
                else:
                    with lock:
                        rec.update(status="error", ended=time.time(), error=str(e)[:600])
                        failed = failed or not inloop
                    nlog.write("failed: %s" % e)
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
        if not run.get("result") and not failed and not ctl.cancel.is_set():                            # no Output node: the last text produced is the answer
            last = [v for v in run["nodes"].values() if v.get("status") == "ok" and v.get("output") and v is not run["nodes"].get(run["trigger"])]
            if last:
                run["result"] = {"title": "", "text": max(last, key=lambda v: v.get("ended") or 0)["output"]}
        run.update(status="stopped" if ctl.cancel.is_set() else "failed" if failed else "done", ended=time.time())
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
            if credential_problem(n, spec):                                # no set to start it with: not started (the editor shows why)
                self._kill(key)
                continue
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
- start.cron -> io.request / git.read -> ai.claude -> send.teams / send.slack / send.discord / send.webhook or send.smtp (mail through any server; action draft saves it for review), or pkg:aihub-nodes-outlook/outlook-send for the Outlook app when it is in the list.
- start.manual -> [git.read, io.request] in parallel -> ai.claude (prompt names {{in.<a>}} and {{in.<b>}}) -> flow.output.
- Keep it as small as the request allows: 3 to 7 nodes.

EXAMPLE (valid):
```json
""" + json.dumps(GEN_EXAMPLE, indent=1) + """
```
Each node type below lists `in:` (name:type, * = required) and `out:` (name:type (what it is)). An output called `text` is {{input}} for the next node; every other output is {{data.name}}. Only write {{data.x}} when x is declared in `out:` of a node before it (or is an input of the Start node). Connect an `out` to an `in` of the same type (path -> path, url -> url); a `text` output can feed any `input`.
Node types:
"""



def _gen_catalogue():
    lines = []
    for t, sp in catalogue().items():
        if sp.get("hidden"):
            continue
        fl = ", ".join("%s%s%s" % (f["name"], "*" if f.get("required") else "", "=" + "/".join(f["options"]) if f.get("options") else "") for f in sp["fields"])
        sio = sp.get("io") or {}
        io = " | in: %s | out: %s" % (", ".join("%s:%s%s" % (i["name"], i["type"], "*" if i.get("required") else "") for i in sio.get("inputs", ())) or "-",
                                      "; ".join("%s:%s%s" % (o["name"], o["type"], " (%s)" % o["desc"][:110] if o.get("desc") else "") for o in sio.get("outputs", ())) or "text")
        ck = (sp.get("credential") or {}).get("kind")
        lines.append("- %s (%s, %s): %s%s | fields: %s%s" % (t, sp["kind"], sp["group"], sp["desc"][:160], io, fl or "-",
                                                         " | credential kind: %s (a saved login set is linked automatically: leave its secret fields out of config)" % ck if ck else ""))
    saved = [p for p in presets().values() if p["type"] in catalogue()]
    if saved:
        lines.append("\nSaved nodes (set up by the user, logins included). When one fits, use it: {\"id\",\"type\": <its type>, \"preset\": <its id>, \"config\": {}} and put in config only the fields this work changes (never logins):")
        for p in saved[:40]:
            lines.append("- preset %s (type %s): %s%s | set: %s" % (p["id"], p["type"], p["name"], " - " + p["description"] if p["description"] else "", ", ".join(sorted(p["config"])) or "-"))
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


def _ancestors(by, parents, nid):
    seen, todo = set(), list(parents.get(nid, ()))
    while todo:
        u = todo.pop()
        if u not in seen and u in by:
            seen.add(u)
            todo.extend(parents.get(u, ()))
    return seen


def ref_warnings(w, cat):
    return [t for _, t in ref_issues(w, cat)]


def ref_issues(w, cat):
    """Strings that say which `{{data.x}}` a node uses that no node before it declares as an output (typo, wrong order, missing edge).
    Dynamic outputs (a `<key>` entry, e.g. code.python or the inputs of a manual Start) are respected: start.manual declares its own inputs."""
    by = {n["id"]: n for n in w.get("nodes", [])}
    parents = {i: [e["from"] for e in w.get("edges", []) if e["to"] == i] for i in by}
    out = []
    for n in by.values():
        used = set()
        for v in (n.get("config") or {}).values():
            if isinstance(v, str) and "{{" in v:
                used.update(m.group(1) for m in re.finditer(r"\{\{\s*data\.(\w+)", v))
        if not used:
            continue
        have, dynamic = set(), False
        for u in _ancestors(by, parents, n["id"]):
            sp = cat.get(by[u].get("type")) or {}
            for o in (sp.get("io") or {}).get("outputs", ()):
                if o["name"].startswith("<"):
                    dynamic = dynamic or by[u]["type"] != "start.manual"
                elif o["name"] != "text":
                    have.add(o["name"])
            if by[u].get("type") == "start.manual":
                have.update(f["name"] for f in store.parse_inputs((by[u].get("config") or {}).get("inputs")))
            for k in sp.get("outputs") or ():
                have.add(k)
        for k in sorted(used - have):
            if not dynamic:
                out.append((n["id"], '"%s" uses {{data.%s}}, but no node before it declares an output called %s%s' % (n.get("label") or n["id"], k, k, " (declared: %s)" % ", ".join(sorted(have)) if have else "")))
    return out


def checked_work(w, strip_secrets_with=None, warnings=None):
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
        p = preset(n.get("preset"))
        if not p or p["type"] != n["type"]:
            n.pop("preset", None)                                            # only a saved node of this type that really exists
        n.pop("credential", None)                                            # the generator names no sets: we pick below
        n.pop("credential_kind", None)
        n.pop("credential_name", None)
        ck = (cat[n["type"]].get("credential") or {}).get("kind")
        if ck:
            fit = [c for c in credentials().values() if c["kind"] == ck]
            if len(fit) == 1:
                n["credential"] = fit[0]["id"]                               # exactly one set of that kind: use it
            else:
                n["credential_kind"] = ck                                    # none or several: the user is asked to pick
    if not any(cat[n["type"]]["kind"] == "trigger" for n in w["nodes"]):
        raise ValueError("a work needs a Start node")
    repair(w, cat)
    if warnings is not None:
        warnings.extend(ref_warnings(w, cat))
    _layout(w)
    w = store.clean_work(w)
    if not w["nodes"]:
        raise ValueError("the work has no nodes")
    return w


def generate(prompt, model="", box=None, warnings=None):
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
                return store.save_work(checked_work(extract_work(ask(text)), warnings=warnings))
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
