"""Hidden feature tests: `jobscan run` emails each published digest through the user's mail account."""
import base64
import contextlib
import io
import shutil
import smtplib
import socketserver
import ssl
import tempfile
import threading
import unittest
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr
from pathlib import Path
from unittest import mock

from jobscan import cli, notify
from tests.test_run import RunFixture

# A real SMTP server on loopback, so any use of smtplib works against it. The tests replace smtplib.SMTP and
# smtplib.SMTP_SSL with subclasses that record the requested server and the TLS context, then connect to this
# server instead, trusting its certificate. The certificate is for 127.0.0.1 and valid until 2126.
CERT = """-----BEGIN CERTIFICATE-----
MIIBejCCASCgAwIBAgIUGoM2sqCfkGu2j35/i7SMKQcGi4wwCgYIKoZIzj0EAwIw
FDESMBAGA1UEAwwJbG9jYWxob3N0MCAXDTI2MTAyNzE3NDIyNloYDzIxMjYxMDAz
MTc0MjI2WjAUMRIwEAYDVQQDDAlsb2NhbGhvc3QwWTATBgcqhkjOPQIBBggqhkjO
PQMBBwNCAASbiYqTh4tYNgIASoHRhj7AIPig8PlrtP956j5l/gYE7R1wM4f8NASz
XUNxu2IUVZXEzcran5Iqdwrdvi9YSygso04wTDAdBgNVHQ4EFgQUXln40m/9E/jz
td7HqyLXHxOD9UAwDwYDVR0TAQH/BAUwAwEB/zAaBgNVHREEEzARgglsb2NhbGhv
c3SHBH8AAAEwCgYIKoZIzj0EAwIDSAAwRQIhAIwTsfnOwt4D01uw71M5WEoawHYN
r4/KjJCTYal369InAiAPBdhCgi6S/GwKGTIec/TJyLont31kRAsdXssfDPWYQQ==
-----END CERTIFICATE-----
"""
KEY = """-----BEGIN PRIVATE KEY-----
MIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQgl59D7SXspNvwfKHO
6S72Mt4HWTtKDB+90fMDiL0nxHGhRANCAASbiYqTh4tYNgIASoHRhj7AIPig8Plr
tP956j5l/gYE7R1wM4f8NASzXUNxu2IUVZXEzcran5Iqdwrdvi9YSygs
-----END PRIVATE KEY-----
"""
# The real classes, captured before any test replaces them.
REAL_SMTP, REAL_SMTP_SSL = smtplib.SMTP, smtplib.SMTP_SSL
PASSWORD = "Zq7 xK\\9mT"  # as typed: a space and a backslash


class SMTPServer:
    """One conversation at a time. `starttls=False` never offers STARTTLS; `tls=True` speaks TLS from the first
    byte. `replies` overrides the reply to AUTH, MAIL, RCPT, DATA, END (after the message) or QUIT with one raw line (\"\" for QUIT drops the connection unanswered);
    an AUTH reply starting 334 is sent again for every further line the client sends."""

    def __init__(self, *, tls=False, starttls=True, replies=None):
        self.tls, self.offers_starttls, self.replies = tls, starttls, dict(replies or {})
        self.logins, self.recipients, self.messages, self.lines = [], [], [], []
        directory = tempfile.mkdtemp(prefix="smtp-cert-")
        cert, key = Path(directory) / "cert.pem", Path(directory) / "key.pem"
        cert.write_text(CERT)
        key.write_text(KEY)
        self.server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.server_context.load_cert_chain(cert, key)
        self.client_context = ssl.create_default_context(cafile=str(cert))
        shutil.rmtree(directory)
        server = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                server._converse(self)

        self._server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self):
        self._server.shutdown()
        self._server.server_close()

    def _converse(self, handler):
        sock, secure = handler.request, self.tls
        if self.tls:
            sock = self.server_context.wrap_socket(sock, server_side=True)
        rfile, wfile = sock.makefile("rb"), sock.makefile("wb")

        def say(line):
            wfile.write(line.encode() + b"\r\n")
            wfile.flush()

        def read():
            line = rfile.readline(10000).decode("utf-8", "replace").rstrip("\r\n")
            self.lines.append(line)
            return line

        say("220 fake ESMTP")
        while True:
            line = read()
            verb = line.split(" ", 1)[0].upper()
            if not line and verb == "":
                return
            if verb == "EHLO":
                features = ["AUTH PLAIN LOGIN"] + (["STARTTLS"] if self.offers_starttls and not secure else [])
                for feature in ["fake"] + features[:-1]:
                    say(f"250-{feature}")
                say(f"250 {features[-1]}")
            elif verb in ("HELO", "RSET", "NOOP"):
                say("250 ok")
            elif verb == "STARTTLS" and self.offers_starttls and not secure:
                say("220 go ahead")
                sock = self.server_context.wrap_socket(sock, server_side=True)
                rfile, wfile, secure = sock.makefile("rb"), sock.makefile("wb"), True
            elif verb == "AUTH":
                self._auth(line, read, say, secure)
            elif verb in ("MAIL", "RCPT"):
                if verb == "RCPT":
                    self.recipients.append(line.split(":", 1)[1].strip().strip("<>"))
                say(self.replies.get(verb, "250 ok"))
            elif verb == "DATA":
                if "DATA" in self.replies:
                    say(self.replies["DATA"])
                    continue
                say("354 go ahead")
                body = []
                while (data := rfile.readline()) not in (b".\r\n", b""):
                    body.append(data[1:] if data.startswith(b"..") else data)
                self.messages.append(BytesParser(policy=policy.default).parsebytes(b"".join(body)))
                say(self.replies.get("END", "250 queued"))
            elif verb == "QUIT":
                if self.replies.get("QUIT") != "":
                    say(self.replies.get("QUIT", "221 bye"))
                return
            else:
                say("500 unknown")

    def _auth(self, line, read, say, secure):
        parts = line.split()
        mechanism = parts[1].upper() if len(parts) > 1 else ""
        if mechanism == "PLAIN":
            encoded = parts[2] if len(parts) > 2 else (say("334 ") or read())
            _, user, password = base64.b64decode(encoded).decode("utf-8", "replace").split("\0")
        elif mechanism == "LOGIN":
            user = base64.b64decode(parts[2] if len(parts) > 2 else (say("334 VXNlcm5hbWU6") or read())).decode()
            say("334 UGFzc3dvcmQ6")
            password = base64.b64decode(read()).decode("utf-8", "replace")
        else:
            say("504 unrecognized")
            return
        self.logins.append({"user": user, "password": password, "secure": secure})
        reply = self.replies.get("AUTH", "235 ok")
        while reply.startswith("334"):
            say(reply)
            if not read():
                return
        say(reply)


def redirect_smtp(test, server):
    """Replace smtplib's two connection classes for the test; returns the list of (class, host, port, verifying)."""
    opened = []

    def verifying(context):
        return isinstance(context, ssl.SSLContext) and context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED

    class Plain(REAL_SMTP):
        def connect(self, host="localhost", port=0, source_address=None):
            opened.append({"class": "SMTP", "host": host, "port": port})
            return super().connect("127.0.0.1", server.port, source_address)

        def starttls(self, *args, **kwargs):
            opened[-1]["starttls_verifying"] = verifying(kwargs.get("context", args[2] if len(args) > 2 else None))
            return super().starttls(context=server.client_context)

    class Implicit(REAL_SMTP_SSL):
        def __init__(self, *args, context=None, **kwargs):
            self._verifying = verifying(context)
            super().__init__(*args, context=server.client_context, **kwargs)

        def connect(self, host="localhost", port=0, source_address=None):
            opened.append({"class": "SMTP_SSL", "host": host, "port": port, "verifying": self._verifying})
            return super().connect("127.0.0.1", server.port, source_address)

    for name, replacement in (("SMTP", Plain), ("SMTP_SSL", Implicit)):
        patcher = mock.patch.object(smtplib, name, replacement)
        patcher.start()
        test.addCleanup(patcher.stop)
    return opened


class EmailFixture(RunFixture):
    """`jobscan run` with email.toml in place and smtplib redirected to a local server."""

    def setUp(self):
        super().setUp()
        self.server = None
        self.serve()

    def serve(self, *, port=465, starttls=True, replies=None):
        if self.server is not None:
            self.server.close()
        self.server = SMTPServer(tls=port == 465, starttls=starttls, replies=replies)
        self.addCleanup(self.server.close)
        self.opened = redirect_smtp(self, self.server)
        self.write_email(port=port)

    def write_email(self, *, port=465, mode=0o600, password=PASSWORD, to="you@example.com", extra=""):
        path = self.home / "email.toml"
        escaped = None if password is None else password.replace("\\", "\\\\").replace('"', '\\"')
        path.write_text(f'to = "{to}"\nfrom = "me@example.com"\nhost = "smtp.example.com"\nport = {port}\n'
                        + (f'password = "{escaped}"\n' if escaped is not None else "") + extra)
        path.chmod(mode)

    def run_notified(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(notify, "notify") as notified, contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            code = cli.main(["run", "--notify", *args])
        self.stderr = err.getvalue()
        return code, out.getvalue().strip().split("\t"), [c.args[0] for c in notified.call_args_list]

    def log(self):
        path = self.home / "runs.log"
        return path.read_text() if path.exists() else ""


class EmailRunTest(EmailFixture):
    def test_the_digest_is_emailed_and_a_quiet_run_sends_nothing(self):
        code, (_, new, path), _ = self.run_notified()
        self.assertEqual((code, new), (0, "2"))
        (message,) = self.server.messages
        self.assertEqual((message["Subject"], parseaddr(message["From"])[1], parseaddr(message["To"])[1]),
                         ("jobscan: 2 new for ae", "me@example.com", "you@example.com"))
        self.assertEqual(message.get_content().replace("\r\n", "\n"), Path(path).read_text(encoding="utf-8"))
        self.assertEqual(self.server.recipients, ["you@example.com"])
        self.assertIn("email_sent", self.log())
        self.assertEqual(self.run_notified()[:2], (0, ["ae", "0", "-"]))
        self.assertEqual(len(self.server.messages), 1)

    def test_a_digest_of_untagged_postings_is_emailed_too(self):
        self.model.routes["/chat/completions"] = (503, {"error": "down"})
        self.assertEqual(self.run_notified()[0], 1)
        (message,) = self.server.messages
        self.assertEqual(message["Subject"], "jobscan: 0 new for ae · 2 untagged")

    def test_a_failed_send_is_reported_and_never_resent(self):
        self.serve(replies={"AUTH": "535 5.7.8 Username and Password not accepted"})
        code, (_, new, path), notified = self.run_notified()
        self.assertEqual((code, new), (1, "2"))
        self.assertEqual(notified, [f"2 new postings in {self.home / 'digests' / 'ae'}; "
                                    "the digest couldn't be emailed; see runs.log"])
        self.assertTrue(Path(path).exists())
        self.assertIn("email_failed", self.log())
        self.serve()
        self.assertEqual(self.run_notified(), (0, ["ae", "0", "-"], []))
        self.assertEqual(self.server.messages, [])
        self.assertEqual(len(self.digests()), 1)

    def test_a_broken_email_setup_stops_the_run_before_any_work(self):
        for mode, password in [(0o644, PASSWORD), (0o600, None)]:
            self.write_email(mode=mode, password=password)
            code, _, notified = self.run_notified()
            self.assertEqual(code, 2, (mode, password))
            self.assertIn("email.toml", notified[0])
            self.assertEqual((self.ats.requests, self.opened), ([], []))

    def test_a_bad_setting_is_refused_without_echoing_the_password(self):
        self.write_email(extra='color = "blue"\n')
        code, out, notified = self.run_notified()
        self.assertEqual(code, 2)
        self.assertNotIn(PASSWORD, self.stderr + "\n".join(out + notified))


class EncryptionTest(EmailFixture):
    def test_port_465_is_tls_from_the_start_with_a_verified_server(self):
        self.assertEqual(self.run_notified()[0], 0)
        self.assertEqual(self.opened, [{"class": "SMTP_SSL", "host": "smtp.example.com", "port": 465,
                                        "verifying": True}])
        self.assertEqual(self.server.logins, [{"user": "me@example.com", "password": PASSWORD, "secure": True}])

    def test_other_ports_start_tls_before_the_login(self):
        self.serve(port=587)
        self.assertEqual(self.run_notified()[0], 0)
        self.assertEqual(self.opened, [{"class": "SMTP", "host": "smtp.example.com", "port": 587,
                                        "starttls_verifying": True}])
        self.assertEqual(self.server.logins, [{"user": "me@example.com", "password": PASSWORD, "secure": True}])
        self.assertEqual(len(self.server.messages), 1)

    def test_a_server_without_starttls_never_sees_the_password(self):
        self.serve(port=587, starttls=False)
        code, _, notified = self.run_notified()
        self.assertEqual(code, 1)
        self.assertIn("the digest couldn't be emailed", notified[0])
        self.assertEqual(self.server.logins, [])
        self.assertFalse(any(line.upper().startswith(("AUTH", "MAIL")) for line in self.server.lines))


class EmailOffTest(EmailFixture):
    def test_off_removes_even_a_broken_file_and_is_idempotent(self):
        self.write_email(mode=0o644, password=None)
        for _ in range(2):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(["email", "--off"]), 0)
            self.assertFalse((self.home / "email.toml").exists())


if __name__ == "__main__":
    unittest.main()
