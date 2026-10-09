"""Where the server may reach out to on behalf of data it did not write (publish events, package repos)."""
import ipaddress
import re
import socket
from urllib.parse import urlsplit


def host_of(url):
    """Host name of an http(s)/ssh/scp-style git URL, or ''."""
    u = str(url or "").strip()
    m = re.match(r"^[\w.-]+@([^:/]+)[:/]", u)
    return (m.group(1) if m else urlsplit(u).hostname or "").lower()


def public_host(url):
    """False for hosts that resolve to loopback/private/link-local space: the server must not be a way into the network."""
    try:
        h = host_of(url)
        return bool(h) and all(ipaddress.ip_address(a[4][0]).is_global for a in socket.getaddrinfo(h, 443, proto=socket.IPPROTO_TCP))
    except (OSError, ValueError, TypeError):
        return False


def allowed(url, trusted_url=""):
    """A publish event may name a repo on a public host, or on the same host as the configured index (a private GitLab, say)."""
    h = host_of(url)
    return bool(h) and (h == host_of(trusted_url) or public_host(url))
