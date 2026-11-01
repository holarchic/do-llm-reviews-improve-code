import http.client
import http.server
import json
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from eval.harness import envelope, prices, proxy, settlers
from eval.harness.envelope import Envelope

KEY = "sk-fake-key"
FIELDS = ["model", "max_tokens", "messages", "stream", "tools", "system"]
POLICY = {"paths": ["/v1/messages"], "models": ["m"], "fields": FIELDS, "tool_types": ["", "custom"],
          "output_limit": {"field": "max_tokens", "cap": 1000}}
RATE_IN, RATE_OUT = 4.0, 20.0  # the fake's own prices, $/M tokens; a body byte counts as an input token


class FakeProvider:
    """An upstream with its own billing: a request costs its body bytes and max_tokens at RATE_IN/RATE_OUT."""

    def __init__(self):
        self.calls, self.billed, self.visible_lag = 0, {}, 0.0
        self.encodings, self.cut, self.events = [], False, None
        self.lookup = "billed"  # or "pending" | "error"
        self._lock = threading.Lock()
        provider = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                request = json.loads(body)
                with provider._lock:
                    provider.calls += 1
                    provider.encodings.append(self.headers.get("Accept-Encoding"))
                    gen = f"gen-{provider.calls}"
                    provider.billed[gen] = (len(body) * RATE_IN + request.get("max_tokens", 0) * RATE_OUT) / 1e6
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

    def settle(self, billing_id):
        if self.blocker is not None:
            self.blocker.wait()
        if self.provider.lookup == "error":
            raise OSError("HTTP 404 from the billing lookup")
        if self.provider.lookup == "pending":
            return settlers.Pending("not yet")
        return settlers.Billed(self.provider.billed[billing_id])

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
        self.env.register_scope("or", provider="openrouter", fingerprint=proxy.fingerprint(KEY), opening_usage=7.0,
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
        served = proxy.Proxy(self.env, settler_for=lambda upstream: self.settler, settle_seconds=3600,
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
        """The fake bills each request at its own prices; the route's bound comes from the price
        table arithmetic; provider usage stays within funded credit, fees and taxes within the envelope."""
        window, cap = 4000, 100_000  # the largest body the route admits, and the output cap: about $2 a request
        fake_price = prices.Price("fake", "m", prices.Rates(RATE_IN, RATE_IN, RATE_OUT), "test")
        bound = prices.request_charge_bound(fake_price, max_input_tokens=window, max_output_tokens=cap)
        self.set_route(policy={**POLICY, "output_limit": {"field": "max_tokens", "cap": cap}}, max_charge=bound, slots=3)
        self.env.reconcile("or", 7.0)
        served = self.start()
        filler = "x" * (window - 200)
        statuses = []
        for i in range(40):
            statuses.append(self.post(served.session("job", f"a{i}")["r"],
                                      {"model": "m", "max_tokens": cap, "messages": [{"role": "user", "content": filler}]})[0])
            served.settle_now()
            self.env.reconcile("or", self.settler.key_usage())
        state = self.env.state
        self.assertGreater(statuses.count(200), 0)
        self.assertLessEqual(self.provider.usage, 10.0 + 1e-9)  # within the scope allocation and funded credit
        self.assertLessEqual(self.provider.usage + 1.0 + 1.0, state["envelope_usd"])  # with fees and taxes
        self.assertTrue(all(b <= bound for b in self.provider.billed.values()))
        self.assertIn(429, statuses)


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
        served = proxy.Proxy(self.env, settler_for=lambda upstream: (seen.append(upstream["base_url"]), self.settler)[1],
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
            raise OSError("down")
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
        self.assertEqual(self.settler.settle("gen-1"), settlers.Billed(0.0123))
        for missing in ("gen-2", "gen-3", "gen-4"):
            self.assertIsInstance(self.settler.settle(missing), settlers.Pending)

    def test_key_usage_reads_or_raises(self):
        self.responses["/api/v1/key"] = (200, {"data": {"usage": 4.5}})
        self.assertEqual(self.settler.key_usage(), 4.5)
        self.responses["/api/v1/key"] = (200, {"data": {"usage": "x"}})
        with self.assertRaises(ValueError):
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


if __name__ == "__main__":
    unittest.main()
