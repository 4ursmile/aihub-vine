"""A minimal WebSocket client (RFC 6455, text frames) on the standard library: enough to talk to a Jupyter kernel or terminal channel.
No extensions, no subprotocols; ping is answered, fragments are joined."""
import base64
import hashlib
import os
import socket
import ssl
import struct
import threading
import urllib.parse

GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class WSError(OSError):
    pass


class WS:
    def __init__(self, url, headers=None, ctx=None, timeout=20):
        u = urllib.parse.urlparse(url)
        secure = u.scheme in ("wss", "https")
        host, port = u.hostname, u.port or (443 if secure else 80)
        s = socket.create_connection((host, port), timeout=timeout)
        try:
            if secure:
                s = (ctx or ssl.create_default_context()).wrap_socket(s, server_hostname=host)
            key = base64.b64encode(os.urandom(16))
            path = (u.path or "/") + ("?" + u.query if u.query else "")
            lines = ["GET %s HTTP/1.1" % path, "Host: %s" % u.netloc, "Upgrade: websocket", "Connection: Upgrade",
                     "Sec-WebSocket-Key: " + key.decode(), "Sec-WebSocket-Version: 13"]
            lines += ["%s: %s" % kv for kv in (headers or {}).items()]
            s.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = s.recv(4096)
                if not chunk:
                    raise WSError("the server closed the connection during the handshake")
                buf += chunk
                if len(buf) > 65536:
                    raise WSError("handshake answer too long")
            head, _, rest = buf.partition(b"\r\n\r\n")
            status = head.split(b"\r\n", 1)[0].decode("latin-1")
            if " 101 " not in status + " ":
                raise WSError("WebSocket refused: " + status[:120])
            want = base64.b64encode(hashlib.sha1(key + GUID).digest()).decode().lower()
            if "sec-websocket-accept: " + want not in head.decode("latin-1").lower():
                raise WSError("bad WebSocket handshake answer")
        except BaseException:
            s.close()
            raise
        s.settimeout(None)
        self.s, self.buf, self.lock, self.dead = s, rest, threading.Lock(), False

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.s.recv(65536)
            if not chunk:
                raise WSError("connection closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _frame(self, op, payload=b""):
        n, head = len(payload), bytearray([0x80 | op])
        if n < 126:
            head.append(0x80 | n)
        elif n < 65536:
            head += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        head += mask
        # mask in one big-int xor: fast enough for megabytes
        if n:
            m = (mask * (n // 4 + 1))[:n]
            payload = (int.from_bytes(payload, "big") ^ int.from_bytes(m, "big")).to_bytes(n, "big")
        return bytes(head) + payload

    def send(self, text):
        with self.lock:
            if self.dead:
                raise WSError("connection closed")
            self.s.sendall(self._frame(1, text.encode("utf-8")))

    def recv(self):
        """-> the next text message, or None when the peer closed."""
        parts = []
        while True:
            b1, b2 = self._read(2)
            op, n = b1 & 15, b2 & 127
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            data = self._read(n) if n else b""            # server frames are not masked
            if op == 9:
                with self.lock:
                    self.s.sendall(self._frame(10, data))
            elif op == 8:
                self.close()
                return None
            elif op in (0, 1, 2):
                parts.append(data)
                if b1 & 0x80:
                    return b"".join(parts).decode("utf-8", "replace")

    def close(self):
        with self.lock:
            if self.dead:
                return
            self.dead = True
            try:
                self.s.sendall(self._frame(8, b""))
            except OSError:
                pass
            try:
                self.s.close()
            except OSError:
                pass
