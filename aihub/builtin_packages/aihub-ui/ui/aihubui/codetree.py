"""Code-tree analysis for completions: what the notebook's earlier cells and the cell being edited DEFINE, taken from the syntax tree (not regexes), reduced to the
few lines a model needs: imports with their aliases, function signatures, classes, variables with an inferred type, and the function the cursor is inside
(its arguments and locals). Names the code before the cursor mentions come first; the rest is cut to a budget. Stdlib only, never raises."""
import ast
import re

_IDENT = re.compile(r"[A-Za-z_]\w*")
_READ = re.compile(r"^(?:read_\w+|load\w*|open|connect)$")
_DF = {"DataFrame", "read_csv", "read_excel", "read_parquet", "read_json", "read_sql", "read_sql_query", "read_table", "merge", "concat", "pivot_table"}


def parse(src):
    """A tree for SRC even when its end is unfinished (the cursor is usually in the middle of a statement): drop trailing lines until it parses, close an open block."""
    lines = src.split("\n")
    for cut in range(0, min(len(lines), 40)):
        part = "\n".join(lines[: len(lines) - cut])
        for tail in ("", "\n    pass", "\n        pass"):
            try:
                return ast.parse(part + tail), len(lines) - cut
            except (SyntaxError, ValueError, RecursionError):
                continue
    return None, 0


def _u(node, limit=60):
    try:
        return ast.unparse(node)[:limit]
    except Exception:
        return ""


def _dotted(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return ""


def infer(v):
    """A short type hint for the value assigned to a name."""
    if isinstance(v, ast.Constant):
        return type(v.value).__name__ + (("=" + repr(v.value)[:24]) if isinstance(v.value, (int, float, bool)) else "")
    if isinstance(v, (ast.List, ast.ListComp)):
        return "list"
    if isinstance(v, (ast.Dict, ast.DictComp)):
        return "dict" + ("{" + ",".join(_u(k, 14) for k in v.keys[:5] if k is not None) + "}" if isinstance(v, ast.Dict) and v.keys else "")
    if isinstance(v, (ast.Set, ast.SetComp)):
        return "set"
    if isinstance(v, ast.Tuple):
        return "tuple"
    if isinstance(v, ast.Call):
        d = _dotted(v.func)
        last = d.rsplit(".", 1)[-1]
        if last in _DF:
            return "DataFrame"
        if isinstance(v.func, ast.Attribute):
            base = _dotted(v.func.value)
            return (base + "." + last + "()") if base and len(base) < 20 else last + "()"
        return (d or "call") + "()"
    if isinstance(v, ast.Name):
        return "= " + v.id
    if isinstance(v, ast.Subscript):
        return "item of " + (_dotted(v.value) or "?")
    return ""


def _sig(fn):
    try:
        a = ast.unparse(fn.args)
    except Exception:
        a = "..."
    r = (" -> " + _u(fn.returns, 30)) if getattr(fn, "returns", None) else ""
    doc = (ast.get_docstring(fn) or "").strip().split("\n")[0][:70]
    return "%s%s(%s)%s" % ("async " if isinstance(fn, ast.AsyncFunctionDef) else "", fn.name, a[:90], r), doc


class Symbols:
    def __init__(self):
        self.imports, self.funcs, self.classes, self.vars = {}, {}, {}, {}

    def add(self, tree):
        for n in tree.body:
            if isinstance(n, ast.Import):
                for a in n.names:
                    self.imports[a.asname or a.name.split(".")[0]] = "import %s%s" % (a.name, " as " + a.asname if a.asname else "")
            elif isinstance(n, ast.ImportFrom):
                for a in n.names:
                    self.imports[a.asname or a.name] = "from %s%s import %s%s" % ("." * (n.level or 0), n.module or "", a.name, " as " + a.asname if a.asname else "")
            elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.funcs[n.name] = _sig(n)
            elif isinstance(n, ast.ClassDef):
                methods = [m.name for m in n.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and not m.name.startswith("__")]
                init = next((m for m in n.body if isinstance(m, ast.FunctionDef) and m.name == "__init__"), None)
                self.classes[n.name] = (", ".join(_u(b, 20) for b in n.bases[:3]), methods[:8], _u(init.args, 70) if init else "", (ast.get_docstring(n) or "").strip().split("\n")[0][:60])
            elif isinstance(n, ast.Assign):
                for t in n.targets:
                    for nm in ([t] if isinstance(t, ast.Name) else list(getattr(t, "elts", []))):
                        if isinstance(nm, ast.Name):
                            self.vars[nm.id] = infer(n.value)
            elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
                self.vars[n.target.id] = _u(n.annotation, 30)
            elif isinstance(n, (ast.For, ast.AsyncFor)) and isinstance(n.target, ast.Name):
                self.vars[n.target.id] = "loop item"
            elif isinstance(n, (ast.With, ast.AsyncWith)):
                for it in n.items:
                    if isinstance(it.optional_vars, ast.Name):
                        self.vars[it.optional_vars.id] = infer(it.context_expr)


def scope_at(tree, nlines):
    """The innermost function or class the end of the text is inside: (header, [arguments], [locals]) or None."""
    best = None
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and getattr(n, "end_lineno", 0) >= nlines - 1:
            if best is None or n.lineno >= best.lineno:
                best = n
    if best is None:
        return None
    if isinstance(best, ast.ClassDef):
        return "class " + best.name, [], []
    args = [a.arg for a in best.args.posonlyargs + best.args.args + best.args.kwonlyargs] + ([best.args.vararg.arg] if best.args.vararg else []) + ([best.args.kwarg.arg] if best.args.kwarg else [])
    loc = []
    for n in ast.walk(best):
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store) and n.id not in args and n.id not in loc:
            loc.append(n.id)
    ret = ""
    return _sig(best)[0], args, loc[:12]


def context(cells, prefix, suffix="", budget=1700):
    """-> the compact text for the model. CELLS = earlier cells [{type, src, out}], PREFIX/SUFFIX = the cell being edited around the cursor."""
    sym, order = Symbols(), []
    for c in cells or []:
        if (c.get("type") or "code") != "code":
            continue
        tree, _ = parse(str(c.get("src") or "")[:6000])
        if tree is not None:
            sym.add(tree)
    cur, nl = parse(prefix + ("\n" + suffix[:400] if suffix.strip() else ""))
    cur_sym = Symbols()
    pre_tree, pre_n = parse(prefix)
    if pre_tree is not None:
        cur_sym.add(pre_tree)
    scope = scope_at(pre_tree, len(prefix.split("\n"))) if pre_tree is not None else None
    for d in ("imports", "funcs", "classes", "vars"):
        getattr(sym, d).update(getattr(cur_sym, d))
    used = set(_IDENT.findall(prefix[-800:])) | set(_IDENT.findall(suffix[:200]))
    first = lambda items: sorted(items, key=lambda kv: (kv[0] not in used,))        # names the code is talking about go first (stable)
    out = []
    if sym.imports:
        out.append("imports: " + "; ".join(v for k, v in first(sym.imports.items())[:14]))
    if sym.funcs:
        out.append("functions:\n" + "\n".join("  %s%s" % (s, " # " + d if d else "") for k, (s, d) in first(sym.funcs.items())[:10]))
    if sym.classes:
        out.append("classes:\n" + "\n".join("  %s(%s)%s%s%s" % (k, b, " init(" + i + ")" if i else "", " methods: " + ", ".join(m) if m else "", " # " + d if d else "") for k, (b, m, i, d) in first(sym.classes.items())[:6]))
    if sym.vars:
        out.append("variables: " + "; ".join("%s: %s" % (k, v) if v else k for k, v in first(sym.vars.items())[:20]))
    if scope:
        out.append("inside: %s%s" % (scope[0], (" | locals: " + ", ".join(scope[2])) if scope[2] else ""))
    txt = "\n".join(out)
    if len(txt) > budget:
        txt = txt[:budget].rsplit("\n", 1)[0]
    return txt
