# CLI guide

The `aihub` command is a Python zipapp distributed by the server. It uses the Python standard library and supports Python 3.9 or later.

## Install and configure

Install from your AI Hub server. The web UI's **Get started** page detects your OS and suggests the matching command.

**macOS** (Terminal):

```sh
curl -fsSL https://hub.example.com/install.sh | sh
```

**Linux** (bash):

```sh
curl -fsSL https://hub.example.com/install.sh | sh
```

On macOS and Linux the installer adds `~/.aihub/bin` to your PATH by writing one marked block (`# >>> aihub` … `# <<< aihub`) to your shell profile (`~/.zshrc`, `~/.bash_profile` on macOS or `~/.bashrc` on Linux, `~/.config/fish/config.fish`, otherwise `~/.profile`). In a terminal it asks first (`[Y/n]`, default yes); run non-interactively it adds the block directly. Set `AIHUB_NO_MODIFY_PATH=1` to skip the change, in which case it prints the line to add yourself (`curl -fsSL https://hub.example.com/install.sh | AIHUB_NO_MODIFY_PATH=1 sh`). Re-running never adds a second block. Open a new terminal afterwards.

**Windows** (PowerShell):

```powershell
irm https://hub.example.com/install.ps1 | iex
```

The Windows script creates `%USERPROFILE%\.aihub\bin\aihub.cmd` and adds that folder to your user `PATH`; open a new terminal afterwards. On Windows the script finds a working Python 3.9+ (ignoring the Microsoft Store stub) and installs Python 3.12 via winget or python.org if none is found. On macOS and Linux, Python 3.9+ must already be installed.

The install scripts download `aihub.pyz`, creates an `aihub` wrapper in `~/.aihub/bin`, sets the hub URL, and attempts to install usage hooks for detected tools. 

To install the zipapp manually, use the server URL in place of `https://hub.example.com`:

```sh
mkdir -p "$HOME/.aihub/bin"
curl -fsSL https://hub.example.com/cli/aihub.pyz -o "$HOME/.aihub/bin/aihub.pyz"
printf '#!/bin/sh\nexec python3 "$HOME/.aihub/bin/aihub.pyz" "$@"\n' > "$HOME/.aihub/bin/aihub"
chmod +x "$HOME/.aihub/bin/aihub"
```

Configure a hub URL, then register or log in:

```sh
aihub config set hub https://hub.example.com
aihub register
aihub login
```

`register` prompts for username and for the password twice (nothing is created when they differ). `login [username]` prompts for a username if omitted and asks for the password without echoing it; when the admin allows sign-in email domains (Settings > Access), `aihub login abc@gmail.com` signs in to the account `abc` and that account name is saved. Registration is subject to the server's signup policy. The first account on an empty server becomes admin. `password` changes the password of the signed-in account (prompts for the current one and the new one twice, no echo; minimum 6 characters, must differ from the current one); the hub then signs your other devices out and this machine stays signed in. A forgotten password is reset by an administrator (People > reset password).

`AIHUB_URL` supplies the default hub URL when no saved `hub` value exists. `AIHUB_HOME` changes the CLI state directory from `~/.aihub` to the specified path.

Packages are found in a git-backed package index, not on the hub. The index is a folder in that repository (default path `index`, holding `root.json`, `catalog/` and `packages/`); an older single `index.json` is still read. The CLI only reads the index; the server is the only writer. When a hub is configured the CLI reads the index from it first (`/index/catalog`, `/index/packages/<name>`, cached under `~/.aihub/index-hub` with the `ETag` and revalidated with `If-None-Match`, so an unchanged index costs one `304`; the copy is trusted for `client_index_ttl` seconds, `AIHUB_INDEX_TTL` overrides) and falls back to git when the hub cannot serve it. `AIHUB_INDEX_VIA=git` (or `aihub config set index_via git`) skips the hub, and so does a pinned `AIHUB_INDEX_URL`. `aihub setup` asks the hub for the index location and the telemetry inbox. Without a hub, run `aihub setup --manual`, set the index with `aihub config set index_url <git url>`, or export `AIHUB_INDEX_URL` (and `AIHUB_TELEMETRY_URL`/`AIHUB_TELEMETRY_INBOX_BRANCH` to send usage somewhere other than the bundled default). Git logins are remembered by a small built-in helper, `aihub credential`, which `aihub setup` registers (global git config `credential.<scheme>://<host>.helper`) for your index and telemetry hosts, and for any other host a package repo lives on, the first time it is fetched (a login is asked once per host, in a terminal, only if git has none); other hosts are untouched, and for these hosts every helper you already had (osxkeychain, manager, store, ...) is kept after ours, so a login you saved before keeps working. It stores one entry per host, so a new token replaces the old one. Storage, strongest first: macOS Keychain, Windows DPAPI (encrypted for your user), Linux Secret Service (`secret-tool`, when a keyring is running), otherwise `~/.aihub/git-credentials.json` with mode 0600 (not encrypted, like ssh keys). If a host is already configured, `aihub setup` says so and changes nothing. `aihub self-uninstall` removes our entries and restores what each host key held before. Check with `git config --global --get-all credential.https://<host>.helper`.

`aihub vault` manages those remotes by hand: `vault` (menu on a terminal, list otherwise), `vault add URL --user NAME`, `vault edit HOST`, `vault remove HOST [--yes]`, `vault list`, `vault test [URL|HOST]` (does the stored login reach the host? exit 1 if not; it uses the same lookup as the hub server, so run it as the account that runs the server). Missing values are prompted for (token hidden); scripts pass the token with `--token-stdin`. Logins go to the credential helper above; `~/.aihub/vault.json` (0600) lists only host and user name.

Saved logins are never asked for again after an upgrade: a login stored by an older version under `user@host` (or a differently cased host) is found, moved to the plain `host[:port]` key (secret unchanged) and the old key is removed; the old `credential.https://user@host.helper` git config entries that pointed at our helper are removed too (other helpers are never touched). Git failures end with ONE `[aihub hint]` line that names the fix, for example `[aihub hint] git login for git.example.com was refused or is missing. Run: aihub vault add https://git.example.com --user NAME  (2FA accounts: use a personal access token, not the password). Check it: aihub vault test git.example.com`; an unreachable host says `cannot reach HOST (network, proxy or TLS). Check: git ls-remote URL; behind a proxy set AIHUB_GIT_PROXY; private CA: AIHUB_CA_BUNDLE=<ca.pem>`; a commit or branch that is gone says to re-publish or install another version. On the hub server the login hint names `AIHUB_GIT_TOKEN` / `AIHUB_GIT_HOSTS` instead.

To remove aihub from a machine, run `aihub self-uninstall` (add `--yes` to skip the question). It removes installed packages, usage hooks, the PATH block and the aihub home folder, and leaves published packages and git repositories alone. For a pip install, also run `pip uninstall aihub-cli`.

## Common commands

```sh
aihub search "code review"
aihub search "code review" --plain      # "name version" per line, no table (for scripts)
aihub search --name-only                # names only
aihub info package-name
aihub install package-name
aihub install package-name --yes
aihub install pkg-a pkg-b "pkg-c>=1.0"
aihub install -r requirements.txt
aihub install package-name --from archive|repo   # where the package comes from: archive = download it from the hub (same as -i), repo = clone its git repository. Default: a package published to the hub is an archive, any other comes from git. --from repo on an archive package fails (it has no repository); -i with --from repo is refused
aihub install package-name -u        # --upgrade: install only if a newer allowed version exists (up to date = skipped, not installed = installed)
aihub install package-name -i        # --indirect: the hub fetches from git and sends it (needs the install_indirect permission; no git fallback: a hub failure is an error). Without `-i`, a missing or refused git login falls back to the hub automatically (not with `--from repo`). Git LFS files are downloaded in both modes (needs git-lfs on the machine that runs git: the hub for -i, else you); the CLI says whether it used a cache or fetched
aihub install package-name --tool claude --tool opencode
aihub list
aihub list --plain                      # "name version" per line; --name-only for names only
aihub update
aihub update package-name
aihub uninstall package-name
aihub disable package-name
aihub enable package-name
aihub tree
aihub download my-skill@1.0.0 -o ./pkgs
aihub lock
aihub sync
aihub doctor
aihub version
aihub upgrade
```

- `search [term]` prints up to 30 matching packages.
- `info <name>` prints package metadata and versions (as JSON when output is not a terminal).
- `install <name>...` takes several packages and/or `-r FILE` (pip-style requirements file: one `name[<spec>]` per line, `#` comments, nested `-r`); a failing package is reported and the rest still install, exit 1 if any failed. `install <name>` resolves the package from the git index, clones its repository at the version's ref, installs dependencies, and can register components with detected tools. Version constraints such as `"name>=1.2"` or `"name==1.2.0"` are accepted. Interactive mode asks for confirmation. `--yes` skips prompts; `--tool` can be repeated to select tools explicitly.
- `download <name>[@version] [-o DIR] [--archive|--folder] [--from archive|repo]` gets a package without installing it. A package published to the hub (`dev publish -i`) is saved as `<name>-<version>.tar.gz`; a git package as the unpacked folder `<name>-<version>/`. `--archive` / `--folder` pick the other form (for a git package `--archive` asks the hub to build the tar, which needs `install_indirect`). The archive comes down in parallel 4 MiB Range chunks (4 at a time), an interrupted download continues from `<file>.part` + `<file>.part.json`, every chunk must belong to the same version of the file, and the SHA-256 the hub announces is checked before the final file appears. A small archive is one request.
- `unpublish <name>[@version] [--yes]` removes the package, or one version, from the hub (database and index). Only someone who manages the package (site admin, owner, maintainer) may; others get 403. Git repos and installed copies are untouched. Asks first unless `--yes`.
- `list` shows installed package names, versions and enabled status.
- `update [name]` updates all installed packages or the named one.
- `uninstall <name>` removes package files and reverts registered integration and setup changes where recorded. It also removes the package's Python command shims and virtual environment.
- `logout` deletes the saved token; later usage events are anonymous but the hub still merges them with your account when the OS user name equals your hub username. Token usage uploads (`usage-sync`) work the same way: not signed in, the hub attributes them to the account whose name matches your OS user (admin permission `view_usage`, on by default); with no matching account nothing is stored and the upload is retried later.
- `self-uninstall [--yes]` removes aihub from this machine: every installed package, the usage hooks in your AI tools, the `# >>> aihub` ... `# <<< aihub` PATH block that the installer added to your shell config, and the aihub home folder (`~/.aihub`, or `$AIHUB_HOME`). It asks for confirmation unless `--yes` is given. The CLI reports its own install (first command after install) and uninstall as `install`/`uninstall` events for package `aihub-cli` (`detail` = `origin=web` for the install script, `origin=git` for pip/git). It does not touch packages you published or your git repositories. A pip install is removed with `pip uninstall aihub-cli`.
- `disable <name>...` switches a package off without deleting it: its skills, agents and MCP servers are removed from your AI tools, setup steps are reverted, and its commands leave PATH. `enable <name>...` (or `aihub install <name>` on a disabled package) puts it back from the files already on disk, so it works offline. See [Enable, disable, tree, lock and sync](#enable-disable-tree-lock-and-sync).
- `tree [name]` prints the dependency tree. `lock` and `sync` write and apply a lock file.
- `doctor` checks that the package index and the telemetry inbox are reachable (`git ls-remote`), whether you are signed in, which detected tools have usage hooks, and whether `python3` is available. The hub is optional and only reported.
- `welcome` runs first-time setup for detected tools and prints a one-line note that an account is optional (`aihub login`); `setup` pulls index and telemetry settings (see [Install and configure](#install-and-configure)). The policy guard is ticked by default in the welcome menu (and ON unless `aihub policy off`). In every multi-choice prompt `a` selects all and `n` deselects all (numbered fallback: `a`/`n`).
- `version` shows the CLI version and the one the hub offers; `upgrade` installs it (see [Versions and self-update](#versions-and-self-update)).

During installation, missing required commands are reported with any manifest hint. Package dependencies are recursively installed. Python dependencies in `[python]` are installed into a package-specific virtual environment. With `[python] install = true` the package also installs itself (`pip install` of its folder, see [Requirements and Python dependencies](manifest.md#requirements-and-python-dependencies)), and its console scripts are linked into `bin/`. Installation asks before running the platform install script and before registering components; `--yes` skips these prompts. When stdin is closed (non-interactive), those prompts count as "no".

## JSON output (for UIs, scripts and agents)

Every command takes `--json` (or put `--json` before the command, or set `AIHUB_JSON=1`) and then prints ONE JSON document on stdout: `{schema, ok, command, exit, error, hint, data, output, messages, changes, context}`. Child processes write to stderr, nothing prompts (pass `--yes` yourself; it is never implied), a bad command line is a document with `exit: 2`, and no token or password is ever in it (secret-looking config keys read `"set"`, the vault says `stored`). Secrets go in on stdin: `login USER --password-stdin`, `register USER --password-stdin`, `password --password-stdin` (current and new on two lines), `vault add URL --user U --token-stdin`. A tool that calls the CLI shares its sign-in, vault and installed packages with the terminal. Without `--json` all output is unchanged (`aihub usage --json` keeps its own shape). The full contract, `data` per command and how to build a UI or web terminal on top: [Building on aihub](extending.md).

```sh
aihub --json search sql
aihub install my-skill --yes --json
printf '%s\n' "$PW" | aihub login sam --password-stdin --json
```

## Spaces (`aihub space`)

A **Space** is a document, slide deck, report, PDF, sheet, dashboard or HTML page published to the hub and opened from a link, addressed `author@space-name`
(`<hub>/#/s/alice@q3-review`). Spaces live outside packages and are not installed anywhere.

```sh
aihub space publish deck.pptx q3-review --view fit            # .pptx -> slides with transitions
aihub space publish report.html --kind dashboard --private    # one HTML file (sandboxed)
aihub space publish ./site team-site                          # folder with index.html (packed as .tar.gz)
aihub space publish new.pptx alice@q3-review                  # new version of a space you may edit
aihub space share alice@q3-review --group data-team [--edit]  # private spaces: view or edit, like a package
aihub space set alice@q3-review --transition fade --autoplay 8 --loop --view full
aihub space url alice@q3-review --view full                   # link that opens full screen (--bare: no hub header)
aihub space list [--mine] [--kind slides] | top | search WORDS | info REF | get REF -o file | rm REF -y
```

Accepted: `.pptx`, `.html` deck (`<section class="slide">` parts, optional `data-transition`, `<aside class="notes">`, `<meta name="aihub-slide-size" content="1280x720">`),
`.docx`, `.md`, `.txt`, `.xlsx`/`.csv`/`.tsv`, `.pdf`, `.html`, `.zip`/`.tar.gz` with `index.html`. Kinds: doc, slides, report, pdf, sheet, dashboard, html.
The same form is on the hub: Spaces > New space (drop a file). `--view canvas|fit|full` is the default viewer size; readers switch it on the page.
Publishing the same name again replaces the content (rev + 1) and keeps the metadata you do not send. Needs the `publish` permission.

## Hub administration (`aihub admin`)

Needs `aihub login` as an account with the `admin` permission (group actions also work for the owner and the managers of a group; deleting a group only for an admin or the owner). Every action is one call, or a loop of calls, to an existing `/api/v1` route; the hub does the permission checks and audit logging. Each item is tried; failures are listed at the end and the exit status is 1. `--dry-run` shows what would change, `--json` prints list/show output as JSON.

```sh
aihub admin users list [QUERY]                       # username, name, role, status
aihub admin users role ROLE USER... [-f FILE]        # bulk role change
aihub admin users status active|pending|disabled USER... [-f FILE]
aihub admin users reset-password USER...             # new random password, printed once; that person is signed out everywhere
aihub admin users delete USER... [-f FILE] [--yes]   # asks first (--yes in scripts)
aihub admin users create FILE [--role user] [--out new-accounts.csv]
aihub admin users batch FILE [--mode add|replace] [--dry-run]
aihub admin groups list | show G | create G... [--description T] | describe G --description T | delete G... [--yes]
aihub admin groups add G USER... [-f FILE] [--manager] | remove G USER... [-f FILE]
aihub admin groups manager G USER... [-f FILE] | unmanager G USER... [-f FILE]   # grant / take back group manager
aihub admin roles list | perms | show R | create R [--perm a,b] [--description T] | delete R [--yes]
aihub admin roles set R [--perm a,b | --add-perm a --remove-perm b] [--description T]
```

- **Name lists**: `-f FILE` (`-` = stdin) adds usernames from a file, one per line or the first column of a CSV/TSV (a `username` header row is skipped). Names given on the command line are used too.
- **`users batch FILE`** updates existing accounts from a sheet with columns `username, role, groups` (header row optional; without one the order is `username, role, groups`; several groups in one cell separated by `;` or `|`). A blank role or groups cell leaves that person as is. `--mode add` (default) only adds people to the listed groups; `--mode replace` makes the listed groups their whole membership. Unknown accounts, roles and groups skip that row (create a group first with `groups create`); roles and memberships that already match are not touched. Use `--dry-run` first.
- **`users create FILE`** is the hub's bulk import (CSV or xlsx; columns username, display_name, title, role, groups; missing groups are created). The generated passwords come back once and are written to `--out` (mode 0600, refuses to overwrite an existing file).
- **Group managers**: an admin, the owner or a manager can make a member a manager (`groups manager G USER...`, or `groups add G USER... --manager`; someone who is not in the group yet is added) and take it back (`unmanager`). Managers add and remove members and pass the flag on, like the owner; they cannot delete the group. `groups show` lists each member's role.
- You cannot change your own role or status from the CLI. `groups create` makes the creating account a member (hub behavior).

## Enable, disable, tree, lock and sync

**Dependency resolution** happens on your machine against the package index. `install` and `sync` walk the dependency graph and produce every package to install, dependencies first. The index checkout in `~/.aihub/repos/_index/` is fetched at most every five minutes; if the fetch fails, the last checkout is used. Constraints from every dependent are combined, so a package shared by two others gets one version that satisfies both (for example `left` needs `base>=1,<2` and `right` needs `base>=1.2,<3`: `base` 1.5.0 is chosen). If nothing satisfies all constraints the error names the package and who requires it. Packages already installed at a satisfying version are skipped. Each package is then cloned from its repository at the version's ref.

**Enable and disable.** `aihub disable my-skill` keeps the files but removes everything visible to your tools. `aihub disable` refuses while an enabled package depends on it (`--force` overrides; `--all` disables everything). `aihub enable my-skill` enables disabled dependencies first. `aihub install my-skill` on a disabled package enables it, unless the index has a newer version, in which case it updates. `aihub list` shows the status, `aihub update` skips disabled packages (and runs unattended: each package is downloaded through the hub first when a hub is configured, git is used only if the hub fails, and it never asks for a git login), and usage hooks ignore them. Enable and disable are local only and send no telemetry.

**Tree.** `aihub tree` shows installed packages and what they need; `aihub tree my-skill` shows one package, and which packages need it. `aihub tree "my-skill>=1.2" --remote` resolves against the index and marks each package `(new)`, `(installed)` or `(installed 1.0.0)` without installing. A package that appears more than once is expanded once and marked `(*)` afterwards.

**Lock and sync.** `aihub lock` writes `aihub.lock` (JSON, `-f` for another path). For each installed package it records the exact version, the git commit it was installed from, its dependencies, whether you installed it directly (`requested`), and whether it is enabled. Commit it. On another machine or in CI, `aihub sync` resolves the locked versions from the index, installs exactly those commits, and restores the requested and enabled flags. It stops if the index can no longer provide a locked version. `sync --check` only reports differences (exit 1 if the machine would change), `--prune` removes packages not in the lock, and `--yes` skips the confirmation. `lock --check` fails if the file is out of date. `update` never moves a package outside the range its enabled dependents allow.

## Supported tools and install locations

The CLI detects Claude Code, Codex, and OpenCode when their configuration directory exists or their executable is on `PATH`. With interactive installation, it asks which detected tools should receive package components. With `--yes`, all detected tools are selected unless `--tool` restricts them.

| Tool | Skills | Agents | MCP server registration |
| --- | --- | --- | --- |
| Claude Code | `~/.claude/skills/<name>/` | `~/.claude/agents/<name>.md` | `mcpServers` in `~/.claude.json` |
| Codex | `${CODEX_HOME:-~/.codex}/skills/<name>/` | Custom prompt in `${CODEX_HOME:-~/.codex}/prompts/<name>.md` | Managed `[mcp_servers.<name>]` section in `${CODEX_HOME:-~/.codex}/config.toml` |
| OpenCode | `~/.config/opencode/skill/<name>/` | `~/.config/opencode/agent/<name>.md` | `mcp` entry in `~/.config/opencode/opencode.json` |

Codex has no native agent format, so agent files are installed as custom prompts. Codex has no tool-call hook, so `aihub hooks install` reports "no tool-call hook available" for it and only install and uninstall events are sent; per-use counting is not available for Codex.

## Usage hooks and telemetry

Usage hooks record when an installed package's component is used (a skill, an agent, or one of its MCP tools). Calls to tools that do not belong to an installed package are not recorded. Each record also carries the working directory, the machine's host name, and the OS user name of whoever ran it. Install, update and uninstall events are recorded as well; install and update events include the package version.

Parameters are scrubbed on your machine before anything is queued: values under keys such as `password`, `token`, `secret`, `api_key` or `authorization`, `Bearer ...` headers, `password=...` style text, passwords inside `scheme://user:pass@host` URLs, and common key formats (`sk-...`, `ghp_...`, AWS, Slack, JWT, private keys) are replaced with `[redacted]`. Scrubbing is pattern based, so it cannot catch every secret.

Events go to the hub (`POST /api/v1/telemetry`) in batches, and to the telemetry inbox (a git repo) only when the hub cannot be reached, under your account name when you are signed in, or `~<OS user>` when you are not. That name comes from the client and is not verified. Hook handling is intended to be fast, non-blocking, and non-fatal to the coding tool.

- Claude Code uses `PreToolUse` and `UserPromptSubmit` hooks in `~/.claude/settings.json` (the latter catches a typed `/skill-name`). It matches tool calls, identifies installed skills, agent tasks, and MCP tools, then invokes the CLI hook handler.
- OpenCode uses `~/.config/opencode/plugin/aihub-usage.js`, listening for `tool.execute.before` and launching the CLI in the background.
- Codex does not expose a general tool-call hook, so there is no equivalent hook integration.

The hook writes events to `~/.aihub/queue/events.jsonl` (or `$AIHUB_HOME/queue/events.jsonl`). Each event gets a unique `id`. A background flusher waits one telemetry window (the hub sets it, 60 s by default; 15 s without a hub, between 5 and 3600; `AIHUB_TELEMETRY_INTERVAL`, `aihub config set telemetry_interval <seconds>`, or `telemetry.interval` in the bundled `defaults.json` changes it, in that order of priority), so a burst of events becomes one batch, then adds it as one file, `inbox/<client_id>-<unix>-<rand>.jsonl`, to the single `telemetry-inbox` branch of the inbox repo. Nothing is pushed when the queue is empty. `aihub flush` pushes the queue immediately, and returns without waiting if the background flusher already holds the lock. Events wait in the queue while the inbox is unreachable or not writable. After a failed send the CLI records `queue/backoff.json` and starts no flusher for 30 seconds, doubling after each further failure up to 1 hour, each wait randomly 50-100% of that, never shorter than the hub's `Retry-After`; a busy hub (`429`/`503`) is waited for and never replaced by git, and git is tried only after 3 failures in a row to reach the hub (`AIHUB_TELEMETRY_VIA=git` or `aihub config set telemetry_via git` = git only); `aihub flush` ignores this delay and retries at once. A flusher that finds events arriving during its pass sends them in another pass (at most 20 per process), so nothing is stranded. The inbox repo is `AIHUB_TELEMETRY_URL` > `telemetry_url` in `config.json` > the bundled default (`aihub/core/defaults.json`) > the package index repo, and the branch is `AIHUB_TELEMETRY_INBOX_BRANCH` / `telemetry_inbox_branch` (default `telemetry-inbox`). Users need write access to that one branch, not `main`. Every push attempt writes `queue/status.json`; `aihub flush -v` prints the last error and the inbox URL, and `aihub doctor` warns when the last push failed. The queue is capped at 5 MiB; if it exceeds that size, additional events are dropped until it shrinks. Hook failures are suppressed to avoid blocking the coding tool.

Install or remove hooks explicitly:

```sh
aihub hooks install
aihub hooks install --tool claude
aihub hooks remove
```

The `--tool` option can be repeated. Package installation can also install hooks for tools where it registered package components.

## Local usage report (`aihub usage`)

`aihub usage [--tool claude|codex|aihub-ui] [--days N] [--model REGEX] [--sessions | --session ID | --tips] [--find TEXT] [--sort recent|cost|ctx|requests] [--top N] [--by-day] [--plain] [--json]` reads the session logs the AI tools keep on this machine (Claude Code `~/.claude/projects/**/*.jsonl`, Codex `~/.codex/sessions/**/*.jsonl`, and the Free chat turns of the aihub-ui service in `~/.aihub/ui/usage.jsonl` as tool `aihub-ui`) and prints, for today (local midnight), **this month** (since the 1st), the last N days (default 30) and all time: sessions, requests, input / output / cache-write / cache-read tokens, the cache hit rate (cache read share of all input context) and an **estimated** cost, by tool and by model. `--by-day` shows one row per local day (newest first, up to 90) instead of by tool and model. In a terminal the command opens a **full screen browser** (alternate screen, restored on exit) with four views, opening on **Summary** (by tool and by model): **Summary**, **Sessions** (one row per session: last used, tool, project folder, requests, peak context, cache hit, cost, title), **Days** and **Tips**. Move with the arrow keys (or `j`/`k`), PgUp/PgDn, Home/End or the mouse wheel; left/right (or `1`-`4`) switch period (today / this month / last N days / all time); **`n` types another number of days** for the "last N days" tab (also `--days N`); `Tab` or `m` `s` `d` `t` switch view; **click** a period tab, a view tab or a row. `Enter` (or a click) on a session opens its detail: models, folder, time span, a context sparkline and every request with input, output, cache write/read, context size and cost, with flags for a **cold cache rebuild** (cache written after more than 5 idle minutes), **compacted** context and **very large context** (> 150k tokens); left/right then step to the previous/next session. `Enter` on a summary or day row lists that row's sessions; `Enter` on a tip lists the sessions behind it. `o` cycles the session sort (recent, cost, peak context, requests), `f` finds text in session id / title / folder / model, `/` sets the model regex, `c` clears filters, `Esc` goes back, `q` quits. Without a raw terminal (a pipe, `TERM=dumb`) the old line prompt is used. Costs and cache hit rates are coloured. **Tips** are computed from your own logs: cold cache rebuilds after idle gaps (with the extra cost), requests with a very large context, sessions with a low cache hit rate, an Opus-class model used for short replies, many one-shot sessions, one session dominating the cost, and models with no price. Non-interactive forms: `--sessions` (table, `--top N` rows, `--sort`, `--find`), `--session ID_OR_TEXT` (the detail of one session, matches the end of the id, title or folder), `--tips`; they use the `--days` window (`--days 0` = all time) and take `--json`. A spinner shows progress while logs are read (none for `--plain`, `--json` or a pipe); parsed files, including each session's title and folder, are cached in `~/.aihub/usage-cache.json` by mtime and size, so later runs only read new or changed logs, and the browser memoizes every list it builds, so key presses never re-read or recompute. `--model` is a case-insensitive regex; `--plain`, `--by-day`, `--json` or a pipe print and exit. The bare `aihub` menu has the same view under "Usage, cost and cache per tool", and "Policy guard: on/off and rules" to turn the guard on or off and tick which rules are active (same as `aihub policy on|off|enable|disable`). It is purely local: nothing is sent to the hub and no hub is needed. Claude messages streamed as several log lines count once (last line wins); Codex totals are cumulative, so each increase is one request. Prices are list prices per 1M tokens kept in the package `aihub-prices` (`prices.json`, bundled and refreshed from the hub by `policy.auto`; its patterns are regexes, tried against the raw and the normalized model name; `PRICES` in `usage.py` is only a last-resort fallback); a model with no price shows `*` and costs $0. Add or override prices in `~/.aihub/prices.json`: `{"model-substring": {"input": 3, "output": 15, "cache_write": 3.75, "cache_read": 0.3}}`. A key matches a model name as a substring, ignoring case and the separators space, `_`, `.`, `/` and `:` (all treated as `-`); the longest matching key wins, so `deepseek-v4-flash` prices `DeepSeek-V4-Flash-Vision-Exp` unless a more specific key such as `deepseek-v4-flash-vision` exists. Each model is priced once per report, so large logs stay fast. Subscription or enterprise billing can differ, so treat the cost as a rough guide. OpenCode is not read yet (no stable local session log).

## Local files

The default CLI home is `~/.aihub`; set `AIHUB_HOME` to override it. The CLI uses these files and directories:

```text
~/.aihub/
  config.json          Hub URL, index and telemetry settings, client ID
  credentials.json     Hub login token and username
  index-cache.json     Cached copy of an http(s) .json index, used when it cannot be fetched
  state.json           Installed package state (version, rev, dependencies, enabled flag)
  packages/<name>/     Installed package files
  repos/               Cached git checkouts of the index and packages
  venvs/<name>/        Package virtual environments for [python] dependencies
  bin/                 CLI wrapper and package executable shims
  backups/<name>/      Setup-step backups
  queue/               Usage event spool and flusher state
```

Settings resolve in this order: environment variables, then `config.json`, then the built-in `aihub/core/defaults.json` (keys such as `index_url`, `index_branch`, `index_path`, `telemetry_url`, `telemetry_inbox_branch` and `telemetry_interval`). `aihub setup` does not copy the bundled telemetry values into `config.json`; they are read from `defaults.json` each time, unless the hub sends its own.

The `credentials.json` file is written with restrictive permissions. Keep the directory private.

## Package development

`aihub dev` creates and validates package projects, builds a source archive, publishes it through git, and reads package statistics. Run commands from the project directory or pass its path. Publishing needs no hub account: it uses your own git access.

```sh
aihub dev init ./my-package --type skill --name my-package --description "A short package description"
aihub dev validate ./my-package
aihub dev build ./my-package
aihub dev publish ./my-package
aihub dev publish ./my-package --bump patch
aihub dev stats ./my-package
```

`aihub dev init -w [path]` (`--wizard`) is an interactive setup wizard (needs a terminal) that asks what the folder should become: one package (this folder), a monorepo (one git repository; you pick which existing sub folders become packages, optionally name new ones, then set the remote and branch), or one more package in a workspace (new sub folder). It runs the same code as `--mono`, `--sub` and `--name`, so scripts and assistants use those flags instead.

`aihub dev init [path] --type skill|agent|mcp|tool|setup --name NAME --description DESCRIPTION --force` creates a starter project for the selected type and fills in the `[git]` table from the project's repository when it can. If `aihub.toml` already exists, init keeps it; `--force` overwrites the starter files it generates, and unrelated files are left in place. `aihub dev validate [path]` parses and normalizes the manifest, then lints the files it references, reporting errors and warnings. See [Publishing to AI Hub](publishing.md) for project trees, manifest examples, install scripts, visibility, and release steps. `dev build` writes `dist/<name>-<version>.tar.gz`. `dev publish` commits the project, pushes it to the `[git]` remote and branch in `aihub.toml`, and pushes an `aihub.publish` event to the telemetry inbox right away (queued if the inbox is unreachable). It stops if the repository's remote or branch does not match `[git]`, and it tracks files over 50 MB with git-lfs. It does not edit any index: the publish event carries the `aihub.toml` and README (`bundle`), so the hub builds the index entry from them without fetching the repository. A publish the hub cannot index yet is retried, and the reason shows in Admin > Sync. Published versions cannot be reused, so bump the version for each release. `--bump` supports `major`, `minor`, and `patch`. **`aihub dev publish -i`** (`--indirect`) publishes straight to the hub instead of git: the package is validated, packed (the same file list as `dev build`) and uploaded to `POST /api/v1/index/direct`; the hub keeps the archive in `<data>/direct/<name>/<version>.tar.gz`, lists it (repository `hub://direct`) and records the `publish` event itself. No git, remote, `[git]` table or git account is needed, so people without git can publish. It needs `aihub login` with a role that has `publish`. A new package belongs to the publisher; a new version of an existing one needs a maintainer or an admin. A version is immutable: publishing the same files again is accepted ("already on the hub, nothing to do"; the hub compares what the archive CONTAINS, so a different tar/zlib or timestamps do not matter, and the CLI packs reproducibly anyway), different files under the same version are refused with 409. Installs of such a package download the archive from the hub automatically, with or without `install -i`, and need no `install_indirect` permission. **Workspace / monorepo**: `aihub dev publish -i WORKSPACE` (or `--only a,b`) sends every selected package as its own archive containing only that package's folder (a package needs no git, and its `[git]` table is not used). Which files ship: git's own rules inside a git repo (root `.gitignore` included), otherwise the `.gitignore` emulation; the ignore files of the folders between a package and the workspace folder apply too (a workspace-level `.aihubignore`, and a workspace-level `.gitignore` when there is no git), and `.git`, `dist`, `node_modules`, `.venv` never ship. The working files are packed, not the last commit. Dependencies between packages (`[requires] packages`) install from the hub like any other. `--bump` raises the version of each selected package (`version = "x"` in `[package]`, any spacing or quotes; it fails loudly instead of doing nothing). A package the hub refuses (409, 403...) does not stop the others and the command exits non-zero; running it again is safe, packages already on the hub report "already on the hub". Several packages: each is sent on its own and failures are reported at the end. Limit: admin number `direct_max_mb` (default 100 MB; a reverse proxy may cap lower). Archives up to 8 MB go in one request; bigger ones use the **chunked upload**: the CLI opens a session (`POST /index/direct/uploads {size, sha256}`, keyed by person + size + digest, so starting the same upload again resumes it), sends 4 MiB chunks four at a time in any order (`PUT .../chunks/N`, each with `X-Chunk-Sha256`, a repeated chunk is harmless), then `.../complete` joins them, checks size and sha256 and publishes. Unfinished sessions are removed after a day; at most 8 are open per person. A failure shows the percent uploaded; running the same command again continues. The web package page shows a **Download** button (parallel chunks too) instead of the git Source link for such versions.

`aihub dev` commands also accept a folder holding several packages and `--only NAME` to pick some; `aihub dev list` shows them. `--only` matches the folder name, the relative path, or the package name, and can be repeated or comma-separated. It works with `validate`, `build`, `publish`, `index` and `stats`. `aihub dev init PATH --sub --name NAME` creates the package in `PATH/NAME`, which is how to add a package to a workspace folder. For a monorepo, `aihub dev init PATH --mono [--packages "a,b:mcp"] [--remote URL] [--branch B]` makes PATH one git repository with one package per sub folder. `aihub dev tree [PATH]` shows the packages with their repository and `[git]` status. `aihub dev doctor [PATH] [--check] [--remote URL]` sets `[git]` (url, branch, subdir) from the working repository and rewrites `version` and `description` in a sibling `pyproject.toml` (`[project]` or `[tool.poetry]`; `name` is left alone, so the Python name may differ from the hub name, e.g. `my_pkg` vs `my-pkg`) to match `aihub.toml` (`--check` only reports); `validate` fails when they differ and `publish` (also after `--bump`) syncs it first, and `aihub dev tree-solve [PATH] [--nested merge|keep] [--remote URL]` resolves a package folder that has its own `.git` inside another repository. `aihub dev publish` treats packages independently: a failing package is reported and the rest still publish, with one push per repository, and the exit code is non-zero if any failed. See [several packages in one repository](publishing.md#several-packages-in-one-repository).

See [Publishing to AI Hub](publishing.md) for end-to-end package authoring and the [manifest reference](manifest.md) for package fields and setup step behavior.

## Built-in skills

The two built-in skills are ordinary hub packages, not code inside the CLI: `aihub-guide` (tells the assistant to fetch the latest Markdown docs from `GET /api/v1/docs/{slug}/raw` before using AI Hub) and `aihub-package` (turns a new or existing project into a valid package). Neither contains the instructions themselves for packaging: `aihub-package` fetches the *Assistant packaging workflow* section of [publishing.md](publishing.md#assistant-packaging-workflow) from the hub, so editing that document changes the behaviour without a new release. Their sources live in `aihub/builtin_packages/<name>/`. On startup the server publishes any version the registry does not have yet (bump `version` in the package's `aihub.toml` to roll out a change), and they can be resolved and downloaded without signing in, so a brand-new CLI can fetch them. The hub installer and `aihub welcome` install them automatically with the normal installer, so `aihub list`, `aihub update`, `aihub disable` and `aihub uninstall` manage them like any other package.

```sh
aihub skill install                      # (re)install both, registered with every detected tool
aihub skill install --tool claude        # only Claude Code
aihub skill remove                       # same as: aihub uninstall aihub-guide aihub-package
```

On Windows the installer writes both `aihub.cmd` (PowerShell, cmd) and an extensionless `aihub` script (Git Bash, which Claude Code uses to run commands) into `%USERPROFILE%\.aihub\bin`. The skills also tell the assistant how to fall back to the full path if `aihub` is not on the shell's PATH yet.

Then ask the assistant, e.g. "package this project for AI Hub".

## Versions and self-update

The release number lives in `aihub/core/release.py` and is shared by the server, the CLI zipapp and the web UI (shown in the footer and in Admin > Settings).

- `aihub version` shows the installed CLI version and the version the hub offers.
- `aihub upgrade` downloads the CLI from the hub, verifies its SHA-256, and swaps it in. The old copy is kept as `aihub.pyz.prev`. `--check` only reports, `--force` reinstalls, `--rollback` restores the previous copy.
- Auto-update checks after every command and never makes the command wait, nor slows Claude Code (hooks never check):
  - The command itself only reads `~/.aihub/update.json`; when a check is due it starts ONE detached, low-priority worker (`aihub auto-update --background`, lock `~/.aihub/queue/update.lock`, so a burst of commands starts one). The worker asks the hub (`/cli/version`, 10 s timeout), downloads and verifies the new zipapp and swaps it in (old copy kept as `aihub.pyz.prev`). The NEXT command runs the new version and prints `updated aihub A -> B` once.
  - `aihub config set auto_update on|notify|off` (default `on`; `true/1/yes` and `false/0/no` work, any case). `on` installs a newer hub version; `notify` only prints `aihub X available: run aihub upgrade` (at most once a day per version); `off` does nothing. Env `AIHUB_AUTO_UPDATE` overrides the config; `AIHUB_NO_UPDATE=1` always means off.
  - `aihub config set auto_update_minutes N` = least time between two checks (default 1, clamped to 1..43200; env `AIHUB_AUTO_UPDATE_MINUTES`). The older `auto_update_hours` / `AIHUB_AUTO_UPDATE_HOURS` still work when no minutes value is set. A failed check (hub unreachable, bad download, checksum mismatch) backs off 1, 2, 4 ... minutes up to 1 hour.
  - `aihub auto-update` runs the same check in the foreground now.
  - Without a terminal (scripts, assistants) the message goes to stderr, nothing to stdout. No check is started for `hook`, `flush`, `usage-sync`, `credential`, `upgrade`, `version`, `uninstall`, `self-uninstall`, `auto-update`, nor when the command was given `--json`, `--plain` or `--name-only`.
  - State is in `~/.aihub/update.json` (`checked`, `ok`, `fails`, `last_error`, `available`, `announce`, `notified`, `skip_version`, plus the `pyz_version`/`pyz` keys of a pip install). `aihub version` shows the mode, gap, last check and last error.
  - `aihub upgrade --rollback` records the version you left as `skip_version`: auto-update will not reinstall it (a newer one is fine). A manual `aihub upgrade` ignores and clears the skip.

To ship a new CLI, bump `VERSION` in `aihub/core/release.py` and restart the server.

## Site identity (admin)

Admin > Settings > Site sets the site name, logo (PNG/JPEG/WebP, max 256 KB), and contact name/email/support link/phone. They appear in the nav and footer. API: `PUT /api/v1/admin/settings`, `POST|DELETE /api/v1/admin/logo`; public read via `GET /api/v1/meta`.


## HTTPS with a private or self-signed certificate

Certificate checks are on by default. If your hub or git host uses an internal CA, tell the CLI once:

| setting | environment | effect |
| --- | --- | --- |
| `aihub config set ca_bundle /path/ca.pem` | `AIHUB_CA_BUNDLE` | trust this CA file (preferred; verification stays on) |
| `aihub config set insecure_tls false` | `AIHUB_INSECURE=1` | certificate checks are skipped by default (`defaults.json` `tls.insecure_tls`), with a warning on every command; set `false` to verify again, or use `ca_bundle` to keep verification with a private CA |

They apply to every CLI call: hub, an https index, and git (`GIT_SSL_CAINFO` / `GIT_SSL_NO_VERIFY`). The installers read the same
variables, e.g. `curl -kfsSL https://hub/install.sh | AIHUB_INSECURE=1 sh` (PowerShell: set `$env:AIHUB_INSECURE="1"` first).

## Policy guard (`aihub policy`)

`aihub-policy` is a built-in guard on the Claude Code `PreToolUse` hook (the same hook that counts usage). Before an agent runs a tool, the rules are checked and a match is answered with `deny` (blocked, the agent sees the reason) or `ask` (the tool asks you). Defaults cover dropping/truncating tables, `DELETE`/`UPDATE` without `WHERE`, `dropdb`/`flushall`, `aws s3 rm/rb/sync --delete` (and gsutil, mc, azcopy), overwriting a bucket (`ask`), `rm -rf /` or `~`, writes to disks, `/etc`, `~/.ssh`, `~/.aws`, `~/.aihub`, force-push to main (`ask`), destructive MCP tools (`ask`), and an agent changing its own guard. Codex has no tool-call hook, so it is not covered.

- `aihub policy` lists rules; `on` / `off` switch the whole guard; `disable|enable ID` switch one rule; `add ID --pattern REGEX [--reason T] [--mode deny|ask] [--tool RE] [--field command|file_path|name|*]` adds your own; `remove ID` deletes your rule (bundled/hub rules are switched off instead); `test 'COMMAND'` shows the verdict; `log` shows recent blocks; `update [--force]` pulls the hub package now.
- Layers (later wins by id): rules bundled in the CLI (`aihub/builtin_packages/aihub-policy/rules/*.json`, work offline) < hub package `aihub-policy` (`policy.json` with `revision`/`retire`/`rules`, plus `rules/*.json`) < `~/.aihub/policy-user.json`. The package is the one place to maintain rules: edit `aihub/builtin_packages/aihub-policy/rules/*.json` (the zipapp and wheel builds copy them into the CLI) and bump `version` in `aihub/builtin_packages/aihub-policy/aihub.toml`; the server publishes it, and every CLI checks the hub after a command at most every `policy_check_hours` (config, default 6) and installs a newer version by itself. Blocked calls are logged to `~/.aihub/policy.log`.
- The agent cannot turn the guard off: rules refuse `aihub policy off|disable|...` and edits under `~/.aihub`. This stops accidents, not a determined local user.

**AI judge.** Rules only see text, so a call like `python -c "shutil.rmtree('/var/lib/postgresql')"` passes them. When no rule matched and the call looks risky (any shell command that is not a known read-only one, a file write outside the project, an MCP tool with a mutating verb), the hook asks a model for a second opinion: it runs headless `claude -p --model haiku` with no tools and a fixed system prompt (criteria in the `judge` key of `aihub-policy`, so the hub can edit them), the call is passed as quoted data, and the answer is `allow`/`ask`/`deny` as JSON. About 4 s per judged call; answers are cached 24 h by hash (`policy-judge-cache.json`). A rule `deny` is final and never reaches the model; the judge can only add `ask`/`deny`. **If the judge fails or times out (15 s), the call is allowed** (unknown commands are not blocked). It is skipped silently when `claude` is not installed. `aihub policy judge on|off|status`, or `AIHUB_POLICY_JUDGE=on|off`. The nested session runs with `AIHUB_JUDGE=1`, which makes the hook a no-op so it cannot recurse. The PreToolUse hook timeout is 25 s (re-applied automatically). The call text (3000 characters at most, not redacted) goes to Anthropic through your own Claude login; turn the judge off if commands may contain secrets.

## Bots and knowledge

`aihub onboard [BOT]` runs a bot's optional guided credential setup (`[bot] onboard`); `aihub install` offers it in a terminal. The Teams bot uses the Teams Developer CLI (`teams login`, `teams app create`) and writes the credentials into its `.env`.

```sh
aihub install aihub-bot          # prints: source folder, settings file (.env), how to run
aihub dev init . --type node --name NAME       # Works nodes for aihub-ui: nodes/NAME.node.json + nodes/NAME.py (see ui-nodes)
aihub dev init . --type workflow --name NAME   # a Works graph for aihub-ui: works/NAME.work.json, no code (see ui-nodes)
aihub run BOT [-- ARGS]          # start an installed bot or service (type = "bot" | "service") IN THE BACKGROUND; with one the name is optional; a service restarts always by default ([service] restart/schedule); `unique = true` = one run of it at a time
aihub run BOT --fg               # in this terminal instead (needed for an interactive chat); Ctrl-C stops it
aihub run BOT --name N --schedule '0 8 * * 1-5' --restart no -- --once "digest"
aihub run list | status N        # every run on this machine (--json)
aihub run logs N [-f] [-n 40]    # its output; `listen N` = logs -f
aihub run stop N | --all         # end it (and its supervisor);  pause N = stop the bot, keep the supervisor
aihub run start N | restart N    # start a saved run again with its saved settings
aihub run set N --schedule 2h --restart on-failure [-- NEW ARGS] | --clear-args
aihub run rm N [--force]
aihub knowledge list|pull|push [NOTE]|edit NOTE|rm NOTE|path
aihub knowledge get NOTE         # print the note body to stdout (hub first, local copy as fallback); OWNER/NOTE for a shared one
aihub soul                       # shortcut for: aihub knowledge get soul
aihub knowledge share NOTE (--user U | --group G) [--write]   # read-only unless --write; soul is never shared
aihub knowledge unshare NOTE (--user U | --group G)
aihub knowledge shares NOTE      # who has it
aihub knowledge shared           # notes others shared with me
aihub knowledge push-shared OWNER/NOTE   # save my edit of a write-shared note
```

`aihub run` loads `~/.aihub/bots/NAME/.env`, refuses to start when a required `[[bot.env]]` value is empty (and prints the file to edit) and gives the bot its knowledge folder, skill folders and own MCP list. Notes live on the hub (web page **Knowledge**, where you can also share a note with a user or group and open notes shared with you; `GET/PUT/DELETE /api/v1/me/knowledge/{name}`) and in `~/.aihub/knowledge`. Shared notes (`GET /knowledge/shared`, `PUT/GET /knowledge/shared/{owner}/{name}`, `PUT/DELETE/GET /me/knowledge/{name}/shares` with `{"user"|"group", "access"}`) are copied by `pull` to `~/.aihub/knowledge/shared/<owner>/`. The built-in skill package `aihub-knowledge` teaches assistants to use them. Every `aihub install` ends with `source:` (package folder) and, for bots, `settings:`.

**Background runs.** Every start goes through a small supervisor (`aihub run _supervise ID`, detached; `--fg` runs it in the terminal) that keeps `~/.aihub/runs/ID.json` (state + heartbeat), writes all output to `ID.log` (rotated at 5 MB) and reads requests from `ID.ctl`. `--restart`: `on-failure` (default; 2 s backoff doubling to 5 min, reset after a minute of uptime), `always`, `no` (default with `--schedule` or `--fg`). `--schedule`: seconds, `30s`/`5m`/`2h`/`1d` (10 s to 7 days) or a 5-field cron expression in the machine's local time; an interval runs at once and then every N, cron waits for its first tick, a tick while the bot still runs is skipped. `.env` and the bot command are read again at every start. A bot that ends within a second of `aihub run` prints its last log lines (a chat bot in the background: use `--fg`). Signed in to a hub (`aihub login`), each run reports to `POST /api/v1/runs/report` every 15 s (2 s while someone has it open on the hub) and shows on the web page **Running** (Workspace menu), where it can be stopped (supervisor stays, so it can be started again), started, restarted, ended, and its schedule, restart policy and (owner only) arguments changed; the log tail is visible there too. Turn it off with `aihub config set run_report off`, `AIHUB_RUN_REPORT=off` or `--no-hub`. A finished run whose supervisor reports to a hub waits 10 minutes for a restart from the web before it exits. `self-uninstall` stops every run.

Bot flags go after `--`: `-- --info` (what the bot sees), `-- --once "question"`, `-- --memory` (what it remembers), `-- --cron` (run scheduled prompts). `BOT_HARNESS` in `.env` picks the engine: `builtin` (default), `claude`, `codex`, `command` or an extension harness. Full guide: `docs/bots.md`.

### Usage on the hub (per person and per group)

With a hub and `aihub login`, the CLI also uploads your **daily token totals** so the hub's **Usage** page (`#/usage`) can show you and your groups. Only numbers leave the machine: per UTC day, tool and model the requests, input / output / cache-write / cache-read tokens, the estimated cost and the count of unpriced requests. No prompts, titles, folders or file names.

- **When:** Claude Code's `Stop` hook (end of every answer turn, not the end of the session) calls `aihub hook`, which only marks "something changed" and starts one detached worker (`aihub usage-sync --background`). The worker waits about 8 s so quick consecutive turns are ONE upload, reads the usage logs through the per-file cache (only changed files are parsed), and sends only the daily rows that differ from the last successful upload (`~/.aihub/usage-sync.json`). The agent never waits for it. Failures back off 1 min doubling to 1 h (the next turn retries).
- **No double counting, no history loss:** a row is the whole day, the hub overwrites it, so a resend or a lost answer is harmless; two devices of one person add up. Reinstalling is not a second device: a new install derives its `client_id` from the machine (an id already in `config.json` is kept), and the hub folds a client that reports a busy day (5+ requests) with exactly the same numbers as another client of the same person into one, so re-uploaded history is counted once (rows doubled by earlier hub versions are repaired once at start). A day **never shrinks**: Claude Code deletes old session logs, so if the local total of a day is smaller than what was already sent, the CLI does not resend it and the hub ignores a smaller row. The first upload after login sends all the history that is still in your logs.
- **What is sent besides the daily totals:** one numbers-only summary per session (short hash id, day, tool, main model, requests, tokens, cost, **peak context**, and the counts of cold cache rebuilds, compactions, over-150k requests and premium-model short replies) so the Usage page can show sessions and hints. No title, prompt, folder or file name.
- **Cheap:** routine uploads only total the last 2 UTC days (a full pass runs on the first upload, after a login, weekly, and for `aihub usage-sync`), send only rows that changed, gzip them, the hub writes uploads of all users in one transaction per few seconds, and answers a usage query with four SQL statements. A hook process no longer loads `ssl`/git (about 10 ms less per tool call).
- **Hooks stay in place:** the hooks re-check `~/.claude/settings.json` themselves at most every 5 minutes, and every `aihub` command, the first run, `aihub login`, `aihub usage` and each upload run the same check, so a hand edit that removed one of our entries is undone. (If all three entries are deleted, Claude Code no longer calls us; the next `aihub` command restores them.)
- `aihub usage-sync` uploads now. Turn it off with `aihub config set usage_sync off` or `AIHUB_USAGE_SYNC=off`. Codex has no turn hook: its usage is uploaded together with the next Claude turn or by `aihub usage-sync`.
