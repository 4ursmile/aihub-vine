"""Model API (Anthropic Messages with tool use). Replace `complete` to use another provider; the agent loop only needs its return shape."""
import json
import os
import urllib.error
import urllib.request

from . import config

URL = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/") + "/v1/messages"


def complete(system, messages, tools):
    """-> {"stop": "end_turn"|"tool_use", "content": [blocks]}"""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set. Put it in %s" % config.ENV_FILE)
    body = {"model": config.MODEL, "max_tokens": 4096, "system": system, "messages": messages}
    if tools:
        body["tools"] = tools
    rq = urllib.request.Request(URL, json.dumps(body).encode(), {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    try:
        with urllib.request.urlopen(rq, timeout=120) as r:
            j = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError("model API %s: %s" % (e.code, e.read()[:300].decode("utf-8", "replace")))
    return {"stop": j.get("stop_reason"), "content": j.get("content", [])}
