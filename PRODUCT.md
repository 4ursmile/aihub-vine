# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users
An org-wide mixed audience: engineers who find, install and publish AI-tool packages, plus non-technical staff and managers who browse the catalog and read the usage dashboard. Admins run the hub (people, roles, sync, backup, settings). Engineers also use the `aihub` CLI; the web UI is the shared window onto the same data.

## Product Purpose
AI Hub (agentforce) is a package hub for AI coding tools. Packages (skills, agents, MCP servers, tools, setup profiles, bots) live in git; a FastAPI server indexes them and serves a web UI and a usage dashboard; a stdlib-only CLI installs and publishes them. Success: people in the org can discover and install the right package quickly, and the org can see how packages are actually used.

## Positioning
Git-native, hub-served: packages stay in the org's own git repos, while the hub provides the index, installs, permissions and usage analytics, with git as the fallback path. A plain git host has no catalog or telemetry; a generic registry would own the packages.

## Operating Context
- Web UI: Preact + htm SPA, no build step (`aihub/server/static/app.js`, `style.css`). Pages include package list and detail, rankings, dashboard, audit, knowledge notes, and Admin tabs (People, Roles, Sync, Backup, Settings).
- CLI: `aihub` for install, search, publish, usage and policy; the web UI and CLI share one set of docs (`docs/`).
- Viewers range from anonymous (permissions can grant them the dashboard) to admins, so visibility varies per role.

## Capabilities and Constraints
- Package types: skills, agents, MCP servers, tools, setup profiles, bots.
- Installs target several AI tools (Claude Code, Codex and others).
- Frontend constraint: no build step, and `node` is unavailable on the dev machine, so JS cannot be syntax-checked locally.
- Server DB rows and all SQL go through `Repos`; visibility is enforced by one function.
- Undecided: any visual identity work; none has been confirmed.

## Brand Commitments
Product name is "AI Hub" (internal codename agentforce). No logo, palette or voice has been declared binding.

## Evidence on Hand
Real running product with live data (packages, events, rankings). Docs in `docs/` (cli, manifest, publishing, api, architecture, bots). No testimonials, customer logos or benchmarks exist; do not fabricate them.

## Product Principles
- Packages belong to their owners' git repos; the hub indexes and serves, never captures them.
- Make discovery and install fast for anyone, technical or not.
- Usage is shown honestly and per role; visibility rules are never bypassed by the UI.
- The UI must work without a build step and degrade gracefully when the hub is busy or git is slow.

## Accessibility & Inclusion
WCAG AA is required: contrast, keyboard operation, visible focus, and no information carried by color alone.
