import shutil
import socket
import socketserver
import tempfile
import threading
import unittest
from pathlib import Path

from eval.harness import egress


class Echo(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.sendall(b"ECHO " + self.request.recv(1024))


def connect(port: int, request: bytes) -> tuple[bytes, socket.socket]:
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    sock.sendall(request)
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = sock.recv(1)
        if not chunk:
            break
        head += chunk
    return head, sock


class ProxyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="eval-test-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        self.upstream = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Echo)
        self.upstream.daemon_threads = True
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        self.addCleanup(self.upstream.server_close)
        self.addCleanup(self.upstream.shutdown)
        self.allowed = f"127.0.0.1:{self.upstream.server_address[1]}"
        self.log = self.tmp / "egress.log"

    def test_an_allowed_target_is_relayed_and_logged(self):
        with egress.Proxy(self.log, frozenset({self.allowed})) as proxy:
            head, sock = connect(proxy.port, f"CONNECT {self.allowed} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
            with sock:
                self.assertTrue(head.startswith(b"HTTP/1.1 200"))
                sock.sendall(b"hello")
                self.assertEqual(sock.recv(1024), b"ECHO hello")
        self.assertEqual(self.log.read_text(), f"ALLOW CONNECT {self.allowed}\n")

    def test_any_other_target_or_method_is_refused_with_403_and_logged(self):
        with egress.Proxy(self.log, frozenset({self.allowed})) as proxy:
            for request in (b"CONNECT github.com:443 HTTP/1.1\r\n\r\n",
                            b"CONNECT 127.0.0.1:1 HTTP/1.1\r\n\r\n",
                            b"GET http://github.com/ HTTP/1.1\r\nHost: github.com\r\n\r\n"):
                head, sock = connect(proxy.port, request)
                sock.close()
                self.assertTrue(head.startswith(b"HTTP/1.1 403"), head)
        self.assertEqual(self.log.read_text().splitlines(), [
            "DENY CONNECT github.com:443", "DENY CONNECT 127.0.0.1:1", "DENY GET http://github.com/"])

    def test_exit_closes_open_tunnels(self):
        with egress.Proxy(self.log, frozenset({self.allowed})) as proxy:
            head, sock = connect(proxy.port, f"CONNECT {self.allowed} HTTP/1.1\r\n\r\n".encode())
            self.assertTrue(head.startswith(b"HTTP/1.1 200"))
        with sock:
            self.assertEqual(sock.recv(1024), b"")

    def test_the_default_admits_only_the_model_hosts(self):
        self.assertEqual(egress.ALLOWED_TARGETS, frozenset({"chatgpt.com:443", "auth.openai.com:443"}))

    def test_env_points_every_proxy_variable_at_the_proxy_and_exempts_localhost(self):
        with egress.Proxy(self.log) as proxy:
            env = proxy.env()
        url = f"http://127.0.0.1:{proxy.port}"
        self.assertEqual({env[k] for k in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy")}, {url})
        self.assertEqual(env["NO_PROXY"], "localhost,127.0.0.1")


if __name__ == "__main__":
    unittest.main()
