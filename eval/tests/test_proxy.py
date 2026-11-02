import http.client
import http.server
import json
import shutil
import tempfile
import threading
import unittest
import unittest.mock
import urllib.error
import urllib.request
from pathlib import Path

from eval.harness import envelope, prices, providers, proxy, route_config, settlers
from eval.harness.envelope import Envelope

KEY = "sk-fake-key"
FIELDS = ["model", "max_tokens", "messages", "stream", "tools", "system"]
POLICY = {"paths": ["/v1/messages"], "models": ["m"], "fields": FIELDS, "tool_types": ["", "custom"],
          "output_limit": {"field": "max_tokens", "cap": 1000}, "max_body_bytes": 20_000}
# The fake's own billing, $/M tokens: a request's input is its body bytes plus OVERHEAD tokens the
# provider adds itself (a tool prompt, message framing), billed as a 1-hour cache write (the dearest
# input-side rate); input past its CONTEXT window is refused, unbilled.
RATE_IN, RATE_WRITE, RATE_OUT = 4.0, 8.0, 20.0
CONTEXT, OVERHEAD = 20_000, 300


class FakeProvider:
    """An upstream with its own billing: a request costs its input tokens at RATE_WRITE and its
    max_tokens at RATE_OUT."""

    def __init__(self):
        self.calls, self.billed, self.visible_lag, self.credentials = 0, {}, 0.0, []
        self.encodings, self.cut, self.events = [], False, None
        self.lookup = "billed"  # or "pending" | "error"
        self.refuse = None  # (status, body bytes, content type): answer every request with it
        self._lock = threading.Lock()
        provider = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                request = json.loads(body)
                if provider.refuse is not None:
                    status, payload, kind = provider.refuse
                    self.send_response(status)
                    self.send_header("Content-Type", kind)
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                provider.credentials.append((self.headers.get_all("Authorization"), self.headers.get_all("x-api-key")))
                tokens = len(body) + OVERHEAD
                if tokens > CONTEXT:  # past the context window: refused, not billed
                    self.send_response(400)
                    self.end_headers()
                    return
                with provider._lock:
                    provider.calls += 1
                    provider.encodings.append(self.headers.get("Accept-Encoding"))
                    gen = f"gen-{provider.calls}"
                    provider.billed[gen] = (tokens * RATE_WRITE + request.get("max_tokens", 0) * RATE_OUT) / 1e6
                events = provider.events(gen) if provider.events else [{"id": gen, "type": "start"},
                                                                        {"delta": "a"}, {"delta": "b"}]
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(f"data: {json.dumps(events[0])}\n\n".encode())
                self.wfile.flush()
                if provider.cut:
                    self.connection.shutdown(2)
                    return
                for event in events[1:]:
                    self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    @property
    def usage(self) -> float:
        return sum(self.billed.values())


class FakeSettler(settlers.Settler):
    def __init__(self, provider: FakeProvider, opening=7.0):
        self.provider, self.opening, self.blocker = provider, opening, None

    def billing_id(self, event):
        return settlers.OpenRouterSettler("x", "k").billing_id(event)

    def settle(self, pending):
        billing_id = pending.billing_id
        if self.blocker is not None:
            self.blocker.wait()
        if self.provider.lookup == "error":
            raise OSError("HTTP 404 from the billing lookup")
        if self.provider.lookup == "pending":
            return settlers.Pending("not yet")
        return settlers.Billed(self.provider.billed[billing_id], getattr(self, "served", None),
                               getattr(self, "tokens", None))

    def key_usage(self):
        return self.opening + max(0.0, self.provider.usage - self.provider.visible_lag)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="eval-proxy-"))
        self.addCleanup(shutil.rmtree, self.root)
        self.provider = FakeProvider()
        self.addCleanup(self.provider.server.server_close)
        self.addCleanup(self.provider.server.shutdown)
        self.settler = FakeSettler(self.provider)
        self.owned = Envelope.owned("B", root=self.root, create_usd=150.0)
        self.env = self.owned.__enter__()
        self.addCleanup(self.close_owner)
        self.env.fund(credit=10.0, fees=1.0, taxes=1.0)
        self.env.register_scope("or", provider="openrouter", fingerprint=envelope.fingerprint(KEY), opening_usage=7.0,
                                allocation=10.0, provider_limit=10.0)
        self.set_route(max_charge=1.0, slots=2)
        self.env.reconcile("or", 7.0)
        self.env.reserve("job", 1.0)

    def close_owner(self):
        if self.owned is not None:
            self.owned.__exit__(None, None, None)
            self.owned = None

    def set_route(self, policy=POLICY, **kw):
        self.env.set_route("r", scope="or", policy=policy, bootstrap_build_usd=1.0,
                           upstream={"base_url": self.provider.base_url, "settler": "fake"}, **kw)
        self.env.validate_route("r")

    def start(self, **kw):
        served = proxy.Proxy(self.env, settler_for=lambda upstream, fingerprint: self.settler, settle_seconds=3600,
                             reconcile_seconds=3600, shutdown_seconds=kw.pop("shutdown_seconds", 0), **kw)
        served.__enter__()
        self.addCleanup(served.__exit__, None, None, None)
        return served

    def post(self, base: str, body=None, key=KEY, path="/v1/messages"):
        body = {"model": "m", "max_tokens": 100, "messages": [{"role": "user", "content": "hi"}]} if body is None else body
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        request = urllib.request.Request(base + path, data=data, headers={"x-api-key": key, "Accept-Encoding": "gzip",
                                                                          "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, exc.read()
        except (urllib.error.URLError, ConnectionError, http.client.IncompleteRead) as exc:
            return None, str(exc).encode()

    def requests(self):
        return list(self.env.state["requests"].values())


class ForwardingTest(Fixture):
    def test_a_stream_passes_byte_for_byte_unencoded_and_settles_from_its_billing_id(self):
        served = self.start()
        status, body = self.post(served.session("job", "a1")["r"])
        self.assertEqual(status, 200)
        self.assertEqual(body, b'data: {"id": "gen-1", "type": "start"}\n\ndata: {"delta": "a"}\n\n'
                               b'data: {"delta": "b"}\n\ndata: [DONE]\n\n')
        self.assertEqual(self.provider.encodings, ["identity"])  # the client asked for gzip; upstream gets identity
        (request,) = self.requests()
        self.assertEqual((request["billing_id"], request["state"]), ("gen-1", "unresolved"))
        served.settle_now()
        self.assertAlmostEqual(envelope.summary(self.env.state, 0)["settled"], self.provider.billed["gen-1"])

    def test_the_billing_id_includes_a_messages_errors_top_level_request_id(self):
        get = settlers.OpenRouterSettler("x", "k").billing_id
        self.assertEqual(get({"type": "error", "error": {"type": "overloaded_error"}, "request_id": "gen-77"}), "gen-77")
        self.assertIsNone(get({"type": "error", "error": {}, "request_id": None}))
        self.assertIsNone(get({"type": "message_delta", "request_id": "gen-1"}))  # only on an error envelope

    def test_the_provider_that_served_a_billed_request_is_recorded(self):
        served = self.start()
        self.post(served.session("job", "a1")["r"])
        self.settler.served, self.settler.tokens = "Z.AI", {"prompt": 10, "completion": 2, "cached": 4}
        served.settle_now()
        self.assertEqual(self.requests()[0]["served"], ["Z.AI"])
        self.assertEqual(self.requests()[0]["billed_tokens"], {"prompt": 10, "completion": 2, "cached": 4})
        self.assertEqual(envelope.summary(self.env.state, 0)["served"], {"r": ["Z.AI"]})

    def test_the_billing_id_is_read_at_fixed_paths_and_two_ids_make_it_ambiguous(self):
        served = self.start()
        self.provider.events = lambda gen: [{"type": "message_start", "nested": {"id": "gen-old"},
                                             "message": {"id": gen}}, {"delta": "x"}]
        self.post(served.session("job", "a1")["r"])
        self.assertEqual(self.requests()[0]["billing_id"], "gen-1")  # message.id, never the nested one
        self.provider.events = lambda gen: [{"id": gen}, {"id": "gen-other"}]
        self.post(served.session("job", "a2")["r"])
        second = [r for r in self.requests() if r["attempt"] == "a2"][0]
        self.assertEqual((second["billing_id"], second["ambiguous"]), (None, True))
        served.settle_now()
        self.assertEqual(second["state"], "unresolved")
        self.assertEqual([r["state"] for r in self.requests() if r["attempt"] == "a2"], ["unresolved"])

    def test_a_malformed_length_or_policy_never_reaches_the_provider(self):
        served = self.start()
        base = served.session("job", "a1")["r"]
        for length in ("-1", "abc", str(POLICY["max_body_bytes"] + 1)):
            with self.subTest(length=length):
                connection = http.client.HTTPConnection("127.0.0.1", served.port, timeout=5)
                connection.putrequest("POST", base.split(str(served.port))[1] + "/v1/messages")
                connection.putheader("Content-Length", length)
                connection.putheader("x-api-key", KEY)
                connection.endheaders()
                self.assertIn(connection.getresponse().status, (411, 413))
                connection.close()
        self.env.set_route("r", scope="or", policy={"paths": "not a list"}, bootstrap_build_usd=1.0, max_charge=1.0,
                           slots=2, upstream={"base_url": self.provider.base_url, "settler": "fake"})
        self.env.validate_route("r")
        self.assertEqual(self.post(served.session("job", "a2")["r"])[0], 403)
        self.assertEqual(self.provider.calls, 0)

    def test_requests_outside_the_allowlist_or_with_another_key_never_reach_the_provider(self):
        served = self.start()
        base = served.session("job", "a1")["r"]
        ok = {"model": "m", "max_tokens": 100, "messages": []}
        bad = {
            "another model": {**ok, "model": "other"},
            "a field not allowed (fallbacks)": {**ok, "models": ["m", "other"]},
            "a field not allowed (provider)": {**ok, "provider": {"order": ["x"]}},
            "a tool type not allowed": {**ok, "tools": [{"type": "image_generation"}]},
            "a server tool": {**ok, "tools": [{"type": "web_search_20250305"}]},
            "image": {**ok, "messages": [{"role": "user", "content": [{"type": "image", "source": {}}]}]},
            "a remote document": {**ok, "messages": [{"role": "user", "content": [
                {"type": "document", "source": {"type": "url", "url": "https://example.com/a.pdf"}}]}]},
            "a file in a tool result": {**ok, "messages": [{"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t", "content": [{"type": "input_file", "file_url": "u"}]}]}]},
            "a file in the system prompt": {**ok, "system": [{"type": "file", "file_id": "f"}]},
            "audio": {**ok, "messages": [{"role": "user", "content": [{"type": "input_audio", "data": ""}]}]},
            "a content type that is not a string": {**ok, "messages": [{"role": "user", "content": [{"type": []}]}]},
            "a model that is not a string": {**ok, "model": []},
            "tools that are not a list": {**ok, "tools": 42},
            "a tool that is not a table": {**ok, "tools": [42]},
            "a tool type that is not a string": {**ok, "tools": [{"type": ["custom"]}]},
            "no output limit": {k: v for k, v in ok.items() if k != "max_tokens"},
            "output limit too high": {**ok, "max_tokens": 1001},
        }
        for name, body in bad.items():
            with self.subTest(name):
                self.assertEqual(self.post(base, body)[0], 403)
        self.assertEqual(self.post(base, b"{not json")[0], 403)
        self.assertEqual(self.post(base, path="/v1/other")[0], 403)
        self.assertEqual(self.post(base, path="/unapproved/v1/messages")[0], 403)  # paths match exactly
        self.assertEqual(self.post(base, key="sk-another")[0], 403)
        self.assertEqual(self.provider.calls, 0)
        self.assertEqual(self.requests(), [])
        self.assertEqual(self.post(base, {**ok, "tools": [{"name": "Bash", "input_schema": {}}]})[0], 200)
        # A tool call's arguments are resent as text (input tokens, within the window), never content
        # the provider dereferences: they are not walked.
        call = {"role": "assistant", "content": [{"type": "tool_use", "id": "t", "name": "Write",
                                                  "input": {"type": "document", "schema": {"type": "object"}}}]}
        self.assertEqual(self.post(base, {**ok, "messages": [call]})[0], 200)

    def test_every_content_type_both_clis_send_is_admitted(self):
        """The shapes Claude Code and Codex sent through the proxy in the live check, plus the tool
        traffic of a working session."""
        claude = {"model": "m", "max_tokens": 100, "system": [{"type": "text", "text": "s",
                                                               "cache_control": {"type": "ephemeral"}}],
                  "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]},
                               {"role": "assistant", "content": [{"type": "thinking", "thinking": "t", "signature": "x"},
                                                                 {"type": "tool_use", "id": "u", "name": "Bash",
                                                                  "input": {"command": "ls"}}]},
                               {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "u",
                                                             "content": [{"type": "text", "text": "a.py"}]}]}]}
        self.assertIsNone(route_config.request_problem(route_config.parse_policy(POLICY), "/v1/messages",
                                                       json.dumps(claude).encode()))
        codex = {"model": "m", "input": [
            {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "d"}]},
            {"type": "reasoning", "summary": [{"type": "summary_text", "text": "r"}], "encrypted_content": "e"},
            {"type": "function_call", "name": "shell", "arguments": "{}", "call_id": "c"},
            {"type": "function_call_output", "call_id": "c", "output": "ok"},
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "done"}]}]}
        responses = route_config.parse_policy({"paths": ["/v1/responses"], "models": ["m"], "fields": ["model", "input"],
                                               "tool_types": [], "max_body_bytes": 20_000})
        self.assertIsNone(route_config.request_problem(responses, "/v1/responses", json.dumps(codex).encode()))
        codex["input"].append({"type": "message", "role": "user", "content": [{"type": "input_file", "file_id": "f"}]})
        self.assertIn("input_file", route_config.request_problem(responses, "/v1/responses", json.dumps(codex).encode()))


class CredentialTest(Fixture):
    def raw_post(self, served, headers):
        base = served.session("job", "a1")["r"]
        body = json.dumps({"model": "m", "max_tokens": 100, "messages": []}).encode()
        connection = http.client.HTTPConnection("127.0.0.1", served.port, timeout=10)
        connection.putrequest("POST", base.split(str(served.port))[1] + "/v1/messages")
        connection.putheader("Content-Length", str(len(body)))
        for name, value in headers:
            connection.putheader(name, value)
        connection.endheaders(body)
        status = connection.getresponse().status
        connection.close()
        return status

    def test_every_credential_header_must_carry_the_same_key(self):
        served = self.start()
        for headers in ([("Authorization", f"Bearer {KEY}"), ("Authorization", "Bearer sk-other")],
                        [("Authorization", "Bearer sk-other"), ("Authorization", f"Bearer {KEY}")],
                        [("x-api-key", KEY), ("Authorization", "Bearer sk-other")],
                        [("x-api-key", KEY), ("x-api-key", "sk-other")],
                        [("Authorization", f"Basic {KEY}")], [("Authorization", f"Basic {KEY}"), ("x-api-key", KEY)],
                        [("Authorization", "Bearer ")], []):
            with self.subTest(headers=headers):
                self.assertEqual(self.raw_post(served, headers), 403)
        self.assertEqual((self.provider.calls, self.requests()), (0, []))

    def test_the_checked_key_is_exactly_what_is_forwarded(self):
        served = self.start()
        self.assertEqual(self.raw_post(served, [("Authorization", f"Bearer {KEY}"), ("x-api-key", KEY)]), 200)
        self.assertEqual(self.raw_post(served, [("authorization", f"Bearer  {KEY} ")]), 200)
        self.assertEqual(self.provider.credentials, [([f"Bearer {KEY}"], [KEY]), ([f"Bearer {KEY}"], None)])


class SettlerScopeTest(Fixture):
    def test_each_scope_gets_its_own_settler_with_its_own_key(self):
        built = []
        served = self.start()
        served._settler_for = lambda upstream, fingerprint: (built.append(fingerprint), FakeSettler(self.provider))[1]
        upstream = {"base_url": self.provider.base_url, "settler": "fake"}
        first = served._settler(upstream, "fp-a")
        self.assertIs(served._settler(upstream, "fp-a"), first)
        self.assertIsNot(served._settler(upstream, "fp-b"), first)
        self.assertEqual(built, ["fp-a", "fp-b"])

    def test_the_openrouter_settler_refuses_a_key_file_holding_another_key(self):
        key_file = self.root / "key"
        key_file.write_text("sk-or-current\n")
        upstream = route_config.Upstream("https://openrouter.ai/api", "openrouter")
        with unittest.mock.patch.dict(providers.KEY_PATHS, {"openrouter": key_file}):
            settler = settlers.from_upstream(upstream, envelope.fingerprint("sk-or-current"))
            self.assertEqual(settler.api_base, "https://openrouter.ai/api/v1")
            with self.assertRaises(settlers.NoSettler):
                settlers.from_upstream(upstream, envelope.fingerprint("sk-or-registered"))
        with self.assertRaises(settlers.NoSettler):
            settlers.from_upstream(route_config.Upstream("https://x", "other"), "fp")

    def test_a_route_that_cannot_be_settled_is_not_admitted(self):
        def no_settler(upstream, fingerprint):
            raise settlers.NoSettler("the key file holds a different key from the scope's")
        served = proxy.Proxy(self.env, settler_for=no_settler, settle_seconds=3600, reconcile_seconds=3600,
                             shutdown_seconds=0, warn=lambda message: None)
        served.__enter__()
        self.addCleanup(served.__exit__, None, None, None)
        self.assertEqual(self.post(served.session("job", "a1")["r"])[0], 503)
        self.assertEqual((self.provider.calls, self.requests()), (0, []))


class StartupTest(Fixture):
    def test_a_failed_start_releases_its_listener(self):
        def broken(upstream, fingerprint):
            raise RuntimeError("an adapter bug")
        served = proxy.Proxy(self.env, settler_for=broken, settle_seconds=3600, reconcile_seconds=3600, shutdown_seconds=0)
        with self.assertRaises(RuntimeError):
            served.__enter__()
        port = served.port
        with self.assertRaises(OSError):
            http.client.HTTPConnection("127.0.0.1", port, timeout=2).connect()


class RefusalTest(Fixture):
    """A provider's refusal before generating frees its slot; anything that may have been billed doesn't."""

    def refuse(self, status, body, kind="application/json"):
        self.provider.refuse = (status, body if isinstance(body, bytes) else json.dumps(body).encode(), kind)

    def test_a_json_payment_or_rate_refusal_settles_as_not_billed_with_its_evidence(self):
        served = self.start()
        for status in (402, 429):
            self.refuse(status, {"error": {"code": status, "message": "Insufficient credits", "metadata": {"x": 1}}})
            self.assertEqual(self.post(served.session("job", f"a{status}")["r"])[0], status)
        for request in self.requests():
            self.assertEqual((request["state"], request["billed"]), ("not_billed", 0.0))
            self.assertIn("Insufficient credits", request["refused_by_provider"]["error"])
        self.assertEqual(sorted(r["refused_by_provider"]["status"] for r in self.requests()), [402, 429])

    def test_a_refusal_carrying_openrouters_generation_id_is_looked_up_not_freed(self):
        self.provider.lookup = "pending"
        served = self.start()
        self.refuse(429, {"type": "error", "error": {"type": "rate_limit_error", "message": "slow"}, "request_id": "gen-9"})
        self.post(served.session("job", "a1")["r"])
        served.settle_now()
        (request,) = self.requests()
        self.assertEqual((request["billing_id"], request["state"], request["refused_by_provider"]),
                         ("gen-9", "unresolved", None))
        served.__exit__(None, None, None)
        self.close_owner()
        self.owned = Envelope.owned("B", root=self.root)
        self.env = self.owned.__enter__()
        self.env.reconcile("or", 7.0)
        self.provider.lookup = "billed"
        self.provider.billed["gen-9"] = 0.002
        served = self.start()
        served.settle_now()
        self.assertEqual((self.requests()[0]["state"], self.requests()[0]["billed"]), ("billed", 0.002))

    def test_a_null_request_id_takes_the_refusal_path(self):
        served = self.start()
        self.refuse(429, {"type": "error", "error": {"type": "rate_limit_error"}, "request_id": None})
        self.post(served.session("job", "a1")["r"])
        self.assertEqual(self.requests()[0]["state"], "not_billed")

    def test_other_failures_stay_unresolved(self):
        self.set_route(max_charge=1.0, slots=4)  # each case stays unresolved: give each its own slot
        self.env.reconcile("or", 7.0)
        served = self.start()
        for name, (status, body, kind) in {"server error": (500, {"error": {"message": "down"}}, "application/json"),
                                           "a 402 without JSON": (402, b"payment required", "text/plain"),
                                           "a 402 without an error": (402, {"detail": "x"}, "application/json")}.items():
            with self.subTest(name):
                self.refuse(status, body, kind)
                self.post(served.session("job", name)["r"])
        self.assertEqual([r["state"] for r in self.requests()], ["unresolved"] * 3)  # all three reached it


class AdmissionTest(Fixture):
    def test_slots_hold_until_settlement_and_a_refused_attempt_stays_refused(self):
        self.provider.lookup = "pending"
        served = self.start()
        base = served.session("job", "a1")["r"]
        self.assertEqual([self.post(base)[0] for _ in range(2)], [200, 200])
        self.assertEqual(self.post(base)[0], 429)
        self.provider.lookup = "billed"
        served.settle_now()
        self.assertEqual(self.post(base)[0], 429)  # the attempt's refusal sticks
        self.assertEqual(self.post(served.session("job", "a2")["r"])[0], 200)

    def test_a_retrying_client_is_stopped_at_the_cap_while_settlement_is_delayed(self):
        self.provider.lookup = "pending"
        self.set_route(max_charge=1.0, slots=8)
        self.env.reconcile("or", 7.0)
        served = self.start()
        statuses = [self.post(served.session("job", f"a{i}")["r"])[0] for i in range(20)]
        self.assertEqual((statuses.count(200), self.provider.calls), (8, 8))

    def test_concurrent_requests_never_exceed_the_slots(self):
        self.provider.lookup = "pending"
        served = self.start()
        results = []
        threads = [threading.Thread(target=lambda i=i: results.append(self.post(served.session("job", f"a{i}")["r"])[0]))
                   for i in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual((results.count(200), results.count(429), self.provider.calls), (2, 10, 2))


class BoundEndToEndTest(Fixture):
    def test_maximal_requests_at_a_tight_allocation_never_bill_past_funded_credit(self):
        """The fake bills each request at its own prices (input as cache writes, the dearest rate, with
        input it adds itself); the route's bound comes from the price arithmetic over the enforced
        limits, the context window and the output cap, never from the body's size."""
        cap = 100_000  # the output cap: about $2 a maximal request
        fake_price = prices.Price("fake", "m", prices.Rates(RATE_IN, RATE_IN, RATE_OUT, (("1h", RATE_WRITE),)), "test")
        bound = prices.request_charge_bound(fake_price, max_input_tokens=CONTEXT, max_output_tokens=cap)
        policy = {**POLICY, "output_limit": {"field": "max_tokens", "cap": cap}, "max_body_bytes": 2 * CONTEXT}
        self.set_route(policy=policy, max_charge=bound, slots=3)
        self.env.reconcile("or", 7.0)
        served = self.start()

        def body_of(size):
            shell = {"model": "m", "max_tokens": cap, "messages": [{"role": "user", "content": ""}]}
            shell["messages"][0]["content"] = "x" * (size - len(json.dumps(shell)))
            return json.dumps(shell).encode()

        self.assertEqual(self.post(served.session("job", "big")["r"], body_of(2 * CONTEXT + 1))[0], 413)
        # Within the body limit but past the window once the provider adds its own input: refused, unbilled.
        self.assertEqual(self.post(served.session("job", "wide")["r"], body_of(CONTEXT))[0], 400)
        statuses = []
        for i in range(40):
            statuses.append(self.post(served.session("job", f"a{i}")["r"], body_of(CONTEXT - OVERHEAD))[0])
            served.settle_now()
            self.env.reconcile("or", self.settler.key_usage())
        self.assertGreater(statuses.count(200), 0)
        self.assertIn(429, statuses)
        self.assertTrue(all(b <= bound + 1e-12 for b in self.provider.billed.values()))
        self.assertAlmostEqual(max(self.provider.billed.values()), bound, places=9)  # the bound is tight
        self.assertLessEqual(self.provider.usage, 10.0 + 1e-9)  # within the allocation and funded credit
        self.assertLessEqual(self.provider.usage + 1.0 + 1.0, self.env.state["envelope_usd"])  # with fees and taxes


class CaptureTest(Fixture):
    def test_a_request_settles_only_after_its_capture_ends_and_with_the_id_it_kept(self):
        self.env.admit("q1", "r", job="job", attempt="a1")
        self.env.record_billing_id("q1", "gen-1")
        self.assertEqual(self.env.pending_settlements(), [])  # the stream may still bring a second id
        self.env.capture_complete("q1")
        (pending,) = self.env.pending_settlements()
        self.assertFalse(self.env.settle_looked_up("q1", "gen-other", 0.1))  # a lookup of another id
        self.assertTrue(self.env.settle_looked_up("q1", "gen-1", 0.1))

    def test_a_lookup_in_flight_when_the_id_turns_ambiguous_commits_nothing(self):
        self.env.admit("q1", "r", job="job", attempt="a1")
        self.env.record_billing_id("q1", "gen-1")
        self.env.record_billing_id("q1", "gen-2")  # a second id: ambiguous
        self.env.capture_complete("q1")
        self.assertFalse(self.env.settle_looked_up("q1", "gen-1", 0.01))
        self.assertEqual(self.requests()[0]["state"], "unresolved")

    def test_admission_records_exactly_the_route_the_proxy_checked(self):
        checked = self.env.route("r")
        self.set_route(max_charge=0.01, slots=2)  # the route changes between check and admission
        with self.assertRaises(envelope.Refused):
            self.env.admit("q1", "r", job="job", attempt="a1", version=checked.version)
        self.assertEqual(self.requests(), [])


class SettlementTest(Fixture):
    def test_an_error_inside_a_started_stream_keeps_the_obligation(self):
        self.provider.cut = True
        served = self.start()
        self.post(served.session("job", "a1")["r"])
        (request,) = self.requests()
        self.assertEqual((request["billing_id"], request["state"]), ("gen-1", "unresolved"))
        served.settle_now()
        self.assertEqual(self.requests()[0]["state"], "billed")

    def test_a_failed_billing_lookup_keeps_the_request_pending(self):
        self.provider.lookup = "error"
        served = self.start()
        self.post(served.session("job", "a1")["r"])
        try:
            served.settle_now()
        except OSError:
            pass
        self.assertEqual(self.requests()[0]["state"], "unresolved")

    def test_settlement_uses_the_upstream_a_request_was_admitted_under(self):
        seen = []
        served = proxy.Proxy(self.env, settler_for=lambda upstream, fingerprint: (seen.append(upstream.base_url), self.settler)[1],
                             settle_seconds=3600, reconcile_seconds=3600, shutdown_seconds=0)
        served.__enter__()
        self.addCleanup(served.__exit__, None, None, None)
        self.post(served.session("job", "a1")["r"])
        self.env.set_route("r", scope="or", policy=POLICY, bootstrap_build_usd=1.0, max_charge=1.0, slots=2,
                           upstream={"base_url": "http://elsewhere", "settler": "fake"})
        self.assertEqual(self.env.route("r").stage, "probe")  # a changed route needs validating again
        served.settle_now()
        self.assertEqual(seen[-1], self.provider.base_url)

    def test_a_failed_usage_reading_lets_the_snapshot_expire(self):
        clock = [1e6]
        self.env._now = lambda: clock[0]
        self.env.reconcile("or", 7.0)

        def broken():
            raise settlers.UsageUnavailable("down")
        self.settler.key_usage = broken
        served = self.start()
        clock[0] += envelope.SNAPSHOT_SECONDS + 1
        served.reconcile_now()
        self.assertEqual(self.post(served.session("job", "a1")["r"])[0], 429)
        self.assertEqual(self.provider.calls, 0)


class ShutdownTest(Fixture):
    def test_a_late_worker_can_never_write_over_the_next_owners_ledger(self):
        served = self.start(shutdown_seconds=0.5)
        self.post(served.session("job", "a1")["r"])
        self.settler.blocker = threading.Event()  # the settlement lookup hangs
        late = threading.Thread(target=lambda: _swallow(served.settle_now))
        late.start()
        served.__exit__(None, None, None)
        self.close_owner()  # the owner closes: the flock is released
        with Envelope.owned("B", root=self.root) as successor:
            successor.reconcile("or", 7.0 + self.provider.usage)
            successor.reserve("job-2", 1.0)
            successor.admit("q-new", "r", job="job-2", attempt="b1")
            self.settler.blocker.set()  # the stale worker wakes and tries to settle
            late.join(5)
            self.assertIn("q-new", envelope.load(successor.path)["requests"])
            old = [r for r in envelope.load(successor.path)["requests"].values() if r["attempt"] == "a1"][0]
            self.assertEqual(old["state"], "unresolved")  # the stale write was refused, not applied

    def test_shutdown_keeps_what_is_still_unresolved_and_refuses_new_requests(self):
        self.provider.lookup = "pending"
        served = self.start()
        base = served.session("job", "a1")["r"]
        self.post(base)
        served.__exit__(None, None, None)
        self.assertEqual(envelope.load(self.env.path)["requests"].popitem()[1]["state"], "unresolved")


def _swallow(work):
    try:
        work()
    except envelope.EnvelopeError:
        pass


def pending(billing_id, **kw):
    return envelope.PendingSettlement("q", billing_id, None, "or", "fp", **kw)


class OpenRouterSettlerTest(unittest.TestCase):
    def setUp(self):
        self.responses = {}
        test = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                status, payload = test.responses.get(self.path, (404, {"error": "no"}))
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.settler = settlers.OpenRouterSettler(f"http://127.0.0.1:{self.server.server_address[1]}/api/v1", KEY)

    def test_a_billed_generation_settles_and_a_missing_or_failed_one_stays_pending(self):
        self.responses["/api/v1/generation?id=gen-1"] = (200, {"data": {"total_cost": 0.0123}})
        self.responses["/api/v1/generation?id=gen-3"] = (500, {"error": "down"})
        self.responses["/api/v1/generation?id=gen-4"] = (200, {"data": {"total_cost": None}})
        self.responses["/api/v1/generation?id=gen-5"] = (200, {"data": {"total_cost": 0.5, "provider_name": "Z.AI"}})
        self.assertEqual(self.settler.settle(pending("gen-1")), settlers.Billed(0.0123))  # served unknown, not guessed
        self.assertEqual(self.settler.settle(pending("gen-5")), settlers.Billed(0.5, "Z.AI"))
        self.responses["/api/v1/generation?id=gen-6"] = (200, {"data": {
            "total_cost": 0.1, "provider_name": "Z.AI", "native_tokens_prompt": 900, "native_tokens_completion": 50,
            "native_tokens_cached": 600}})
        self.assertEqual(self.settler.settle(pending("gen-6")).tokens, {"prompt": 900, "completion": 50, "cached": 600})
        for missing in ("gen-2", "gen-3", "gen-4"):
            self.assertIsInstance(self.settler.settle(pending(missing)), settlers.Pending)

    def test_key_usage_reads_or_raises(self):
        self.responses["/api/v1/key"] = (200, {"data": {"usage": 4.5}})
        self.assertEqual(self.settler.key_usage(), 4.5)
        for bad in ((200, {"data": {"usage": "x"}}), (500, {"error": "down"}), (200, {"nothing": 1}),
                    (200, {"data": {"usage": True}}), (200, {"data": {"usage": -1.0}}),
                    (200, {"data": {"usage": float("inf")}}), (200, {"data": {"usage": float("nan")}})):
            self.responses["/api/v1/key"] = bad
            with self.subTest(bad=bad), self.assertRaises(settlers.UsageUnavailable):
                self.settler.key_usage()

    def test_billing_ids_come_from_fixed_paths_only(self):
        get = self.settler.billing_id
        self.assertEqual(get({"id": "gen-a"}), "gen-a")
        self.assertEqual(get({"type": "message_start", "message": {"id": "gen-b"}}), "gen-b")
        self.assertEqual(get({"type": "response.created", "response": {"id": "gen-c"}}), "gen-c")
        self.assertIsNone(get({"nested": {"id": "gen-old"}}))
        self.assertIsNone(get({"id": "msg_1"}))

    def test_a_billed_outcome_must_be_a_charge(self):
        for bad in (None, -1.0, float("nan"), True):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                settlers.Billed(bad)


class ChargeBoundTest(unittest.TestCase):
    def test_the_bound_covers_a_maximal_request_at_every_permitted_rate(self):
        sol = prices.lookup(prices.OPENAI, "gpt-6.1-sol")
        bound = prices.request_charge_bound(sol, max_input_tokens=1_050_000, max_output_tokens=128_000)
        self.assertGreaterEqual(bound, (1_050_000 * 10.0 + 128_000 * 15.0) / 1e6)  # long-context 12h writes
        opus = prices.lookup(prices.ANTHROPIC, "claude-opus-5-5")
        self.assertAlmostEqual(prices.request_charge_bound(opus, max_input_tokens=1_000_000, max_output_tokens=128_000,
                                                           per_request_usd=0.01),
                               (1_000_000 * 8.0 + 128_000 * 20.0) / 1e6 + 0.01, places=9)
        dear_reads = prices.Price("fake", "m", prices.Rates(1.0, 3.0, 2.0, (("5m", 2.0),)), "test")
        self.assertAlmostEqual(prices.request_charge_bound(dear_reads, max_input_tokens=1_000_000, max_output_tokens=0),
                               3.0)  # a cached read dearer than everything else still bounds it


if __name__ == "__main__":
    unittest.main()
