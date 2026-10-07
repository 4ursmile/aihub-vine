import re

_V = re.compile(r"^v?(\d+(?:\.\d+)*)(?:[-.]?(a|b|rc|dev)(\d*))?$")
_ORDER = {"dev": -3, "a": -2, "b": -1, "rc": 0}


def parse(v: str):
    m = _V.match(v.strip().lower())
    if not m:
        raise ValueError("invalid version: %r" % v)
    nums = [int(x) for x in m.group(1).split(".")]
    while len(nums) > 1 and nums[-1] == 0:
        nums.pop()
    pre = (_ORDER[m.group(2)], int(m.group(3) or 0)) if m.group(2) else (1, 0)
    return (tuple(nums), pre)


def compare(a: str, b: str) -> int:
    x, y = parse(a), parse(b)
    return (x > y) - (x < y)


def latest(versions):
    return max(versions, key=parse) if versions else None


def satisfies(v: str, spec: str) -> bool:
    """spec: comma separated, e.g. '>=1.0,<2' ; '' or '*' matches all."""
    spec = (spec or "").strip()
    if spec in ("", "*"):
        return True
    for part in spec.split(","):
        m = re.match(r"^(==|!=|>=|<=|~=|>|<)?\s*(.+)$", part.strip())
        op, ref = m.group(1) or "==", m.group(2)
        c = compare(v, ref)
        if op == "~=":
            base = parse(ref)[0]
            ok = c >= 0 and parse(v)[0][: max(len(base) - 1, 1)] == base[: max(len(base) - 1, 1)]
        else:
            ok = {"==": c == 0, "!=": c != 0, ">=": c >= 0, "<=": c <= 0, ">": c > 0, "<": c < 0}[op]
        if not ok:
            return False
    return True


def bump(v: str, part: str = "patch") -> str:
    nums = list(parse(v)[0]) + [0, 0, 0]
    i = {"major": 0, "minor": 1, "patch": 2}[part]
    nums = nums[:3]
    nums[i] += 1
    for j in range(i + 1, 3):
        nums[j] = 0
    return ".".join(map(str, nums))


def split_spec(dep: str):
    """'name>=1,<2' -> ('name', '>=1,<2'); 'name' -> ('name', '')."""
    for i, ch in enumerate(dep):
        if ch in "<>=!~":
            return dep[:i].strip(), dep[i:].strip()
    return dep.strip(), ""
