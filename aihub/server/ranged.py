"""HTTP Range support for streamed file downloads, so an interrupted multi-hundred-MiB install can resume instead of restarting."""
import os
import re

from fastapi import HTTPException
from starlette.background import BackgroundTask
from starlette.responses import StreamingResponse

_RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")


def parse_range(header, size):
    """-> (start, end_inclusive) or None for 'no/ignored range'. Raises 416 for an unsatisfiable one. Single ranges only."""
    m = _RANGE.match((header or "").strip())
    if not m or (m.group(1) == "" and m.group(2) == ""):
        return None
    if m.group(1) == "":                                  # suffix: last N bytes
        n = int(m.group(2))
        if n == 0:
            raise HTTPException(416, headers={"Content-Range": "bytes */%d" % size})
        return max(0, size - n), size - 1
    start = int(m.group(1))
    end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
    if start >= size or start > end:
        raise HTTPException(416, headers={"Content-Range": "bytes */%d" % size})
    return start, end


def stream_file(f, size, range_header, headers):
    """`f` is a seekable binary file (local storage). Returns a 200 or 206 StreamingResponse; closes `f` when done."""
    rng = parse_range(range_header, size)
    start, end = rng if rng else (0, size - 1)
    if start:
        f.seek(start)
    left = end - start + 1

    def gen():
        nonlocal left
        while left > 0:
            b = f.read(min(1 << 20, left))
            if not b:
                break
            left -= len(b)
            yield b

    h = dict(headers, **{"Accept-Ranges": "bytes", "Content-Length": str(end - start + 1)})
    if rng:
        h["Content-Range"] = "bytes %d-%d/%d" % (start, end, size)
    return StreamingResponse(gen(), status_code=206 if rng else 200, media_type="application/octet-stream", headers=h,
                             background=BackgroundTask(f.close))
