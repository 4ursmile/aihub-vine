"""Fixed category taxonomy. Derived from a package's tags and type, so no schema change is needed."""

CATEGORIES = [
    ("development", "Development", "Code review, git and everyday engineering helpers.",
     {"review", "quality", "git", "code", "lint", "test", "testing", "refactor", "debug", "dev", "ci"}),
    ("data", "Data and SQL", "Query, explain and explore databases.",
     {"sql", "database", "db", "data", "analytics", "postgres", "mysql", "sqlite"}),
    ("docs", "Docs and Writing", "Summarize, draft and organize documents.",
     {"docs", "pdf", "writing", "markdown", "summary", "notes", "wiki"}),
    ("productivity", "Productivity", "Small habits that save a few minutes every day.",
     {"productivity", "workflow", "automation", "commit", "shortcut", "template"}),
    ("ops", "Ops and Runbooks", "Team procedures, infrastructure and incident help.",
     {"ops", "runbook", "devops", "infra", "deploy", "incident", "security", "team", "onboarding"}),
    ("starters", "Starters", "Demos and first packages to learn the format.",
     {"demo", "example", "starter", "hello", "tutorial"}),
]

# when no tag matches, fall back on the package type
TYPE_FALLBACK = {"agent": "development", "mcp": "data", "tool": "productivity", "setup": "ops", "skill": "productivity"}

BY_ID = {c[0]: {"id": c[0], "label": c[1], "desc": c[2], "tags": c[3]} for c in CATEGORIES}


def category_of(tags, type=None):
    """-> category id. The category whose tag set overlaps the package tags the most wins (earlier wins ties)."""
    ts = {str(t).lower() for t in (tags or [])}
    best, score = None, 0
    for cid, _, _, cts in CATEGORIES:
        n = len(ts & cts)
        if n > score:
            best, score = cid, n
    return best or TYPE_FALLBACK.get(type or "", "productivity")


def listing():
    return [{"id": c["id"], "label": c["label"], "desc": c["desc"]} for c in BY_ID.values()]
