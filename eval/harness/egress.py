"""Egress for offline Codex sessions: a CONNECT proxy on loopback that admits only the model's hosts.

An offline Codex session runs inside a Seatbelt profile that denies every outbound connection except to
localhost, so its only way out is this proxy, reached through HTTPS_PROXY. The model's own calls pass; a
tool command's request to any other host gets a 403 here, and a raw socket that skips the proxy is
denied by Seatbelt. Every admitted and refused request is logged, one line each, as the probe's evidence.

Measured with Codex 0.164 on a ChatGPT login: a session completes with only chatgpt.com admitted (it also
asks for ab.chatgpt.com and oaiusercontent.com hosts, and refusing them changes nothing). auth.openai.com
is admitted for token refresh.
"""

import select
import socket
import socketserver
import threading
from pathlib import Path

ALLOWED_TARGETS = frozenset({"chatgpt.com:443", "auth.openai.com:443"})
_HEADER_LIMIT = 65536
_HEADER_SECONDS = 30
_CONNECT_SECONDS = 30


class Proxy:
    """One session's proxy: `with Proxy(log) as proxy:` serves on an ephemeral loopback port until exit.
    Exit owns every connection without waiting on any: from acceptance on, each socket is registered, open
    ones are shut down, and a connection that arrives or completes (a slow DNS lookup) while closing is
    refused, so no tunnel outlives the session."""

    def __init__(self, log_path: Path, allowed: frozenset[str] = ALLOWED_TARGETS):
        self._log_path = log_path
        self._allowed = allowed
        self._lock = threading.Lock()
        self._open: set[socket.socket] = set()
        self._closing = False
        self._server: socketserver.ThreadingTCPServer | None = None

    def __enter__(self) -> "Proxy":
        proxy = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                proxy._handle(self.request)

        self._log = open(self._log_path, "a", encoding="utf-8", buffering=1)
        try:
            self._server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
            self._server.daemon_threads = True
            threading.Thread(target=self._server.serve_forever, daemon=True).start()
        except BaseException:
            if self._server is not None:
                self._server.server_close()
            self._log.close()
            raise
        return self

    def __exit__(self, *exc) -> None:
        with self._lock:
            self._closing = True
            for sock in self._open:
                try:
                    sock.shutdown(socket.SHUT_RDWR)  # unblocks a handler's recv, select or sendall
                except OSError:
                    pass
        self._server.shutdown()
        self._server.server_close()
        with self._lock:
            for sock in self._open:
                sock.close()
            self._open.clear()
            self._log.close()

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def env(self) -> dict[str, str]:
        url = f"http://127.0.0.1:{self.port}"
        return {"HTTPS_PROXY": url, "HTTP_PROXY": url, "https_proxy": url, "http_proxy": url,
                "NO_PROXY": "localhost,127.0.0.1", "no_proxy": "localhost,127.0.0.1"}

    def _handle(self, client: socket.socket) -> None:
        if not self._track(client):
            return
        upstream = None
        try:
            client.settimeout(_HEADER_SECONDS)
            request_line = _request_line(client)
            if request_line is None:
                return
            method, target = request_line
            if method != "CONNECT" or target not in self._allowed:
                self._write(f"DENY {method} {target}")
                client.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                return
            try:
                host, _, port = target.rpartition(":")
                upstream = socket.create_connection((host, int(port)), timeout=_CONNECT_SECONDS)
            except OSError as e:
                self._write(f"FAIL CONNECT {target} {type(e).__name__}")
                client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                return
            if not self._track(upstream):
                return
            self._write(f"ALLOW CONNECT {target}")
            client.settimeout(None)
            upstream.settimeout(None)
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            _relay(client, upstream)
        except (OSError, ValueError):  # ValueError: a socket closed under select
            pass
        finally:
            with self._lock:
                self._open.discard(client)
                self._open.discard(upstream)
            if upstream is not None:
                upstream.close()

    def _track(self, sock: socket.socket) -> bool:
        """Register a socket for exit to shut down; False, and the socket closed, once exit has begun."""
        with self._lock:
            if not self._closing:
                self._open.add(sock)
                return True
        sock.close()
        return False

    def _write(self, line: str) -> None:
        with self._lock:
            if not self._log.closed:
                self._log.write(line + "\n")


def _request_line(client: socket.socket) -> tuple[str, str] | None:
    """The method and target of the request, once its header has arrived; None for a malformed one."""
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = client.recv(4096)
        if not chunk or len(data) > _HEADER_LIMIT:
            return None
        data += chunk
    parts = data.split(b"\r\n", 1)[0].decode("latin-1").split(" ")
    return (parts[0], parts[1]) if len(parts) == 3 else None


def _relay(a: socket.socket, b: socket.socket) -> None:
    """Copy bytes both ways until either side closes."""
    peers = {a: b, b: a}
    while True:
        readable, _, _ = select.select(list(peers), [], [])
        for sock in readable:
            data = sock.recv(65536)
            if not data:
                return
            peers[sock].sendall(data)
