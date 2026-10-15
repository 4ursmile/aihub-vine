---
name: aihub-knowledge
description: Use when the user mentions their notes, knowledge, handbook, runbook or "soul", asks to remember or write something down for later or for a team, wants to look something up in notes shared by colleagues, or wants to share a note with a person or group on AI Hub.
---

# AI Hub knowledge notes

Notes are markdown files on the hub, mirrored to `~/.aihub/knowledge/<note>.md`. Notes other people shared with the user
are mirrored read-only-by-default to `~/.aihub/knowledge/shared/<owner>/<note>.md`. `soul` is the user's persona note: it is
private and never shared.

Find the CLI first: `aihub version` (if not found, try `~/.aihub/bin/aihub`).

## Read before you answer

1. `aihub knowledge pull` (refreshes own and shared notes; revoked shares disappear).
2. `aihub knowledge list` (own) and `aihub knowledge shared` (owner, note, access).
3. Read the relevant files directly from `~/.aihub/knowledge/` and `~/.aihub/knowledge/shared/*/`. Prefer a shared note over
   guessing a team convention; name the owner when you rely on it ("per alice/handbook ...").
4. If a note and the code disagree, say so; do not silently pick one.

## Write

- One topic per note, lowercase name (`a-z 0-9 . _ -`, max 64), a `# Title` line first, short sections, facts and decisions
  rather than chat logs. Max 200 000 characters per note, 200 notes.
- Edit the file under `~/.aihub/knowledge/`, then `aihub knowledge push [NOTE]`. `aihub knowledge edit NOTE` opens `$EDITOR`.
- Append to an existing note instead of creating a near-duplicate; `aihub knowledge list` first.
- Never put secrets, tokens or personal data in a note: sharing makes them readable by others.

## Share (ask the user first)

Sharing exposes content to other people, so confirm the note, the recipient and the access before running it.

```sh
aihub knowledge share NOTE --user alice            # read-only
aihub knowledge share NOTE --group backend --write # everyone in the group may edit
aihub knowledge shares NOTE                        # who has it
aihub knowledge unshare NOTE --user alice
```

Check the note for secrets before sharing it. `soul` cannot be shared.

## Edit a note someone shared

Only when the share says `write`: edit `~/.aihub/knowledge/shared/<owner>/<note>.md`, then
`aihub knowledge push-shared <owner>/<note>`. On `read` shares the hub answers 403; ask the owner for write access instead of
copying the text into a note of your own and letting it drift.

## Errors

- `not signed in` / 401: `aihub login`.
- `no such user` / `no such group`: names are hub names, see `aihub knowledge shares NOTE` or ask the user.
- 404 on a shared note: the owner removed the note or the share.
