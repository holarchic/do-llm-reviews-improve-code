import http.server
import json
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from eval.harness import envelope, prices, routing
from eval.harness.envelope import Envelope

KEY = "sk-fake-key"
POLICY = {"paths": ["/v1/messages"], "models": ["m"], "output_limit": {"field": "max_tokens", "cap": 1000}}


class FakeProvider:
    """An upstream with its own billing: each request is charged `charge`, its id streamed in the first event."""

    def __init__(self, charge=0.25, cut_after_first_chunk=False):
        self.charge, self.cut = charge, cut_after_first_chunk
        self.calls, self.billed, self.visible_lag = 0, {}, 0.0
        self.lookup = "billed"  # or "pending" | "404"
        self._lock = threading.Lock()
        provider = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                with provider._lock:
                    provider.calls += 1
                    gen = f"gen-{provider.calls}"
                    provider.billed[gen] = provider.charge
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(f'data: {{"id": "{gen}", "type": "start"}}\n\n'.encode())
                self.wfile.flush()
                if provider.cut:
                    self.connection.shutdown(2)
                    return
                for i in range(3):
                    self.wfile.write(f'data: {{"delta": "{i}"}}\n\n'.encode())
                self.wfile.write(b"data: [DONE]\n\n")

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    @property
    def usage(self) -> float:
        return sum(self.billed.values())


class FakeSettler(routing.Settler):
    def __init__(self, provider: FakeProvider, opening=7.0):
        self.provider, self.opening = provider, opening

    def billing_id(self, data: bytes):
        return routing.OpenRouterSettler._ID.search(data) and routing.OpenRouterSettler._ID.search(data).group(1).decode()

    def settle(self, billing_id):
        if self.provider.lookup == "404":
            raise OSError("HTTP 404 from the billing lookup")
        if self.provider.lookup == "pending":
            return ("pending", None)
        return ("billed", self.provider.billed[billing_id])

    def key_usage(self):
        return self.opening + max(0.0, self.provider.usage - self.provider.visible_lag)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="eval-routing-"))
        self.addCleanup(shutil.rmtree, self.root)
        self.provider = FakeProvider()
        self.addCleanup(self.provider.server.shutdown)
        self.settler = FakeSettler(self.provider)
        owned = Envelope.owned("B", root=self.root, create_usd=150.0)
        self.env = owned.__enter__()
        self.addCleanup(owned.__exit__, None, None, None)
        self.env.fund(credit=10.0, fees=1.0, taxes=1.0)
        self.env.register_scope("or", provider="openrouter", fingerprint=routing.fingerprint(KEY), opening_usage=7.0,
                                allocation=10.0, provider_limit=10.0)
        self.set_route(max_charge=1.0, slots=2)
        self.env.validate_route("r")
        self.env.reconcile("or", 7.0)
        self.env.reserve("job", 1.0)

    def set_route(self, **kw):
        self.env.set_route("r", scope="or", policy=POLICY, bootstrap_build_usd=1.0, **kw)

    def proxy(self, **kw):
        upstream = routing.Upstream(self.provider.base_url, self.settler, "or", routing.fingerprint(KEY))
        proxy = routing.Proxy(self.env, {"r": upstream}, settle_seconds=3600, reconcile_seconds=3600,
                              shutdown_seconds=kw.pop("shutdown_seconds", 0), **kw)
        proxy.__enter__()
        self.addCleanup(proxy.__exit__, None, None, None)
        return proxy

    def post(self, base: str, body=None, key=KEY, path="/v1/messages"):
        body = {"model": "m", "max_tokens": 100, "messages": [{"role": "user", "content": "hi"}]} if body is None else body
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        request = urllib.request.Request(base + path, data=data, headers={"x-api-key": key,
                                                                          "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, exc.read()
        except (urllib.error.URLError, ConnectionError, http.client.IncompleteRead) as exc:
            return None, str(exc).encode()


import http.client  # noqa: E402  (used by the fixture's error handling)


class ForwardingTest(Fixture):
    def test_a_stream_passes_byte_for_byte_and_settles_from_its_billing_id(self):
        proxy = self.proxy()
        status, body = self.post(proxy.session("job", "a1")["r"])
        self.assertEqual(status, 200)
        self.assertEqual(body, b'data: {"id": "gen-1", "type": "start"}\n\n' + b"".join(
            f'data: {{"delta": "{i}"}}\n\n'.encode() for i in range(3)) + b"data: [DONE]\n\n")
        (request,) = self.env.state["requests"].values()
        self.assertEqual((request["billing_id"], request["state"]), ("gen-1", "unresolved"))
        proxy.settle_now()
        self.assertEqual(envelope.summary(self.env.state, 0)["settled"], 0.25)

    def test_requests_outside_policy_or_with_another_key_never_reach_the_provider(self):
        proxy = self.proxy()
        base = proxy.session("job", "a1")["r"]
        ok = {"model": "m", "max_tokens": 100, "messages": []}
        bad = {
            "another model": {**ok, "model": "other"},
            "fallbacks": {**ok, "models": ["m", "other"]},
            "provider override": {**ok, "provider": {"order": ["x"]}},
            "plugins": {**ok, "plugins": [{"id": "web"}]},
            "server tool": {**ok, "tools": [{"type": "web_search_20250305"}]},
            "image": {**ok, "messages": [{"role": "user", "content": [{"type": "image", "source": {}}]}]},
            "no output limit": {k: v for k, v in ok.items() if k != "max_tokens"},
            "output limit too high": {**ok, "max_tokens": 1001},
        }
        for name, body in bad.items():
            with self.subTest(name):
                self.assertEqual(self.post(base, body)[0], 403)
        self.assertEqual(self.post(base, b"{not json")[0], 403)
        self.assertEqual(self.post(base, path="/v1/other")[0], 403)
        self.assertEqual(self.post(base, key="sk-another")[0], 403)
        self.assertEqual(self.provider.calls, 0)
        self.assertEqual(self.env.state["requests"], {})


class AdmissionTest(Fixture):
    def test_slots_hold_until_settlement_and_a_refused_attempt_stays_refused(self):
        self.provider.lookup = "pending"
        proxy = self.proxy()
        base = proxy.session("job", "a1")["r"]
        self.assertEqual([self.post(base)[0] for _ in range(2)], [200, 200])
        self.assertEqual(self.post(base)[0], 429)  # both slots unresolved
        self.provider.lookup = "billed"
        proxy.settle_now()
        self.assertEqual(self.post(base)[0], 429)  # the attempt's refusal sticks
        self.assertEqual(self.post(proxy.session("job", "a2")["r"])[0], 200)
        self.assertLessEqual(self.provider.usage, 10.0)

    def test_a_retrying_client_is_stopped_at_the_cap_while_settlement_is_delayed(self):
        self.provider.lookup = "pending"
        self.set_route(max_charge=1.0, slots=8)
        proxy = self.proxy()
        statuses = [self.post(proxy.session("job", f"a{i}")["r"])[0] for i in range(20)]
        self.assertEqual(statuses.count(200), 8)
        self.assertEqual(self.provider.calls, 8)
        self.assertLessEqual(self.provider.usage, 10.0)

    def test_the_gate_holds_when_charges_show_before_the_providers_own_usage(self):
        self.provider.visible_lag = 100.0  # the key reports nothing yet
        self.set_route(max_charge=2.5, slots=8)
        proxy = self.proxy()
        statuses = [self.post(proxy.session("job", f"a{i}")["r"])[0] for i in range(6)]
        proxy.settle_now()
        self.assertEqual(statuses.count(200), 4)  # 4 × $2.50 fills the $10 allocation
        proxy.reconcile_now()
        self.assertEqual(self.env.state["scopes"]["or"]["excess"], 0.0)  # lag, not excess
        self.assertLessEqual(self.provider.usage, 10.0)

    def test_concurrent_requests_never_exceed_the_slots(self):
        self.provider.lookup = "pending"
        proxy = self.proxy()
        results = []
        threads = [threading.Thread(target=lambda i=i: results.append(self.post(proxy.session("job", f"a{i}")["r"])[0]))
                   for i in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual((results.count(200), results.count(429)), (2, 10))
        self.assertEqual(self.provider.calls, 2)


class SettlementTest(Fixture):
    def test_an_error_inside_a_started_stream_keeps_the_obligation(self):
        self.provider.cut = True
        proxy = self.proxy()
        self.post(proxy.session("job", "a1")["r"])
        (request,) = self.env.state["requests"].values()
        self.assertEqual((request["billing_id"], request["state"]), ("gen-1", "unresolved"))
        proxy.settle_now()
        self.assertEqual(envelope.summary(self.env.state, 0)["settled"], 0.25)

    def test_a_failed_billing_lookup_keeps_the_request_pending(self):
        self.provider.lookup = "404"
        proxy = self.proxy()
        self.post(proxy.session("job", "a1")["r"])
        proxy.settle_now()
        (request,) = self.env.state["requests"].values()
        self.assertEqual(request["state"], "unresolved")

    def test_shutdown_keeps_what_is_still_unresolved(self):
        self.provider.lookup = "pending"
        proxy = self.proxy()
        self.post(proxy.session("job", "a1")["r"])
        proxy.__exit__(None, None, None)
        self.assertEqual(envelope.load(self.env.path)["requests"].popitem()[1]["state"], "unresolved")

    def test_a_failed_usage_reading_lets_the_snapshot_expire(self):
        clock = [1e6]
        self.env._now = lambda: clock[0]
        self.env.reconcile("or", 7.0)

        def broken():
            raise OSError("down")
        self.settler.key_usage = broken
        proxy = self.proxy()
        clock[0] += envelope.SNAPSHOT_SECONDS + 1
        proxy.reconcile_now()
        self.assertEqual(self.post(proxy.session("job", "a1")["r"])[0], 429)
        self.assertEqual(self.provider.calls, 0)


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
        self.settler = routing.OpenRouterSettler(f"http://127.0.0.1:{self.server.server_address[1]}/api/v1", KEY)

    def test_a_billed_generation_settles_and_a_missing_or_failed_one_stays_pending(self):
        self.responses["/api/v1/generation?id=gen-1"] = (200, {"data": {"total_cost": 0.0123}})
        self.responses["/api/v1/generation?id=gen-3"] = (500, {"error": "down"})
        self.responses["/api/v1/generation?id=gen-4"] = (200, {"data": {"total_cost": None}})
        self.assertEqual(self.settler.settle("gen-1"), ("billed", 0.0123))
        for missing in ("gen-2", "gen-3", "gen-4"):
            self.assertEqual(self.settler.settle(missing), ("pending", None))

    def test_key_usage_reads_or_raises(self):
        self.responses["/api/v1/key"] = (200, {"data": {"usage": 4.5}})
        self.assertEqual(self.settler.key_usage(), 4.5)
        self.responses["/api/v1/key"] = (200, {"data": {"usage": "x"}})
        with self.assertRaises(ValueError):
            self.settler.key_usage()

    def test_the_billing_id_is_found_in_a_stream(self):
        self.assertEqual(self.settler.billing_id(b'data: {"id": "gen-abc-1", "x": 1}'), "gen-abc-1")
        self.assertIsNone(self.settler.billing_id(b'data: {"id": "msg_1"}'))


class MaxChargeTest(unittest.TestCase):
    def test_the_bound_covers_a_maximal_request_at_every_permitted_rate(self):
        sol = prices.lookup(prices.OPENAI, "gpt-6.1-sol")
        bound = routing.max_charge(sol.rates, sol.tier, max_input_tokens=1_050_000, max_output_tokens=128_000)
        # Independently: every input token as a long-context 12h cache write, every output at the long rate.
        self.assertGreaterEqual(bound, (1_050_000 * 10.0 + 128_000 * 15.0) / 1e6)
        for prompt_rate in (2.0, 2.50, 5.0, 4.0, 5.0, 10.0):
            self.assertGreaterEqual(bound, (1_050_000 * prompt_rate + 128_000 * 10.0) / 1e6)
        opus = prices.lookup(prices.ANTHROPIC, "claude-opus-5-5")
        self.assertAlmostEqual(routing.max_charge(opus.rates, opus.tier, max_input_tokens=1_000_000,
                                                  max_output_tokens=128_000, per_request_usd=0.01),
                               (1_000_000 * 8.0 + 128_000 * 20.0) / 1e6 + 0.01, places=9)


if __name__ == "__main__":
    unittest.main()
