from fastapi import Depends, HTTPException, Request

from .security import hash_token


def ctx(request: Request):
    return request.app.state


def current_user(request: Request):
    h = request.headers.get("authorization", "")
    if not h.lower().startswith("bearer "):
        return None
    u = request.app.state.repos.token_user(hash_token(h[7:].strip()))
    return u if u and u["status"] == "active" else None


def require_user(u=Depends(current_user)):
    if not u:
        raise HTTPException(401, "authentication required")
    return u


def require_perm(perm):
    def dep(request: Request, u=Depends(require_user)):
        if perm not in request.app.state.repos.perms(u["role"]):
            raise HTTPException(403, "missing permission: " + perm)
        return u
    return dep


def require_any(*perms):
    def dep(request: Request, u=Depends(require_user)):
        if not set(perms) & request.app.state.repos.perms(u["role"]):
            raise HTTPException(403, "missing permission: " + " or ".join(perms))
        return u
    return dep


def viewer_of(repos, u):
    """The identity used for visibility checks. `bypass` = may see every package (site admin / manage_all)."""
    if not u:
        return None
    perms = repos.perms(u["role"])
    return {"id": u["id"], "username": u["username"], "bypass": bool({"admin", "manage_all"} & perms)}


def can_manage(repos, u, pkg):
    """Repo admin: site admin, or an owner/maintainer of the package."""
    return repos.access_level(pkg["id"], viewer_of(repos, u)) == "admin"


def can_develop(repos, u, pkg):
    return repos.access_level(pkg["id"], viewer_of(repos, u)) in ("admin", "develop")
