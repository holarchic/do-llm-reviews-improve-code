"""Metered settlement (DeepSeek's own API) through the proxy, against a fake DeepSeek that bills by its
own rates and reports usage the way DeepSeek documents it, on both wire protocols."""

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
from eval.harness.envelope import Envelope, Refused

KEY = "sk-ds-SYNTHETIC-metered"
MODEL = "deepseek-v4-pro"
PRICE = prices.lookup(prices.DEEPSEEK, MODEL)
CONTEXT, MAX_OUT, OVERHEAD = 1_048_576, 393_216, 300
POLICY = {"paths": ["/anthropic/v1/messages", "/responses"], "models": [MODEL],
          "fields": ["model", "max_tokens", "messages", "stream", "system", "tools", "input", "reasoning"],
          "tool_types": ["", "function"], "max_body_bytes": 3 * CONTEXT}
BOUND = prices.request_charge_bound(PRICE, max_input_tokens=CONTEXT, max_output_tokens=MAX_OUT)


def peak(fresh, cached, output, model=MODEL):
    r = prices.lookup(prices.DEEPSEEK, model).rates
    return (fresh * r.input + cached * r.cache_read + output * r.output) / 1e6


class FakeDeepSeek:
    """Bills each request at `rate` × peak (0.5 = off-peak) for the tokens it served, of which it reports
    all but `hidden` output tokens; its balance shows a charge only after `lag` further requests."""

    def __init__(self, funded=10.0):
        self.funded, self.charges, self.rate, self.hidden, self.lag = funded, [], 1.0, 0, 0
        self.cached_share, self.output, self.cut, self.events_override = 0.5, 1000, None, None
        self.refuse_at_zero = False  # refuse (402, JSON, before generating) once the balance is spent
        self._lock = threading.Lock()
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path != "/user/balance":
                    return self.send_error(404)
                with fake._lock:
                    shown = fake.charges[:len(fake.charges) - fake.lag] if fake.lag else fake.charges
                    balance = fake.funded - sum(shown)
                data = json.dumps({"is_available": balance > 0, "balance_infos": [
                    {"currency": "USD", "total_balance": f"{balance:.6f}", "granted_balance": "0.00",
                     "topped_up_balance": f"{balance:.6f}"}]}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                model = json.loads(body).get("model", MODEL)
                prompt = len(body) + OVERHEAD
                if prompt > CONTEXT:
                    self.send_response(400)
                    self.end_headers()
                    return
                if fake.refuse_at_zero and fake.funded - fake.billed <= 0:
                    data = json.dumps({"error": {"message": "Insufficient Balance", "type": "invalid_request_error"}}).encode()
                    self.send_response(402)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                cached = int(prompt * fake.cached_share)
                output = min(fake.output, MAX_OUT)
                with fake._lock:
                    n = len(fake.charges) + 1
                    fake.charges.append(fake.rate * peak(prompt - cached, cached, output, model))
                reported = max(0, output - fake.hidden)
                anthropic = self.path.split("?", 1)[0] == "/anthropic/v1/messages"  # Claude adds ?beta=true
                events = (fake.events_override or (fake.anthropic if anthropic else fake.responses))(
                    n, prompt, cached, reported)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for i, event in enumerate(events):
                    if fake.cut is not None and i == fake.cut:
                        self.connection.shutdown(2)
                        return
                    self.wfile.write(f"event: {event.get('type', '')}\ndata: {json.dumps(event)}\n\n".encode())
                    self.wfile.flush()

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    @staticmethod
    def anthropic(n, prompt, cached, output):
        return [{"type": "message_start", "message": {"id": f"msg{n}", "model": MODEL,
                                                      "usage": {"input_tokens": 1, "output_tokens": 1}}},  # provisional
                {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "ok"}},
                {"type": "message_delta", "usage": {"input_tokens": prompt - cached, "cache_read_input_tokens": cached,
                                                    "cache_creation_input_tokens": 0, "output_tokens": output}},
                {"type": "message_stop"}]

    @staticmethod
    def responses(n, prompt, cached, output):
        return [{"type": "response.created", "response": {"id": f"resp{n}", "model": MODEL}},
                {"type": "response.output_text.delta", "item_id": "x", "delta": "ok"},
                {"type": "response.completed", "response": {
                    "id": f"resp{n}", "model": MODEL,
                    "usage": {"input_tokens": prompt, "input_tokens_details": {"cached_tokens": cached},
                              "output_tokens": output, "output_tokens_details": {"reasoning_tokens": output // 2}}}}]

    @property
    def billed(self) -> float:
        return sum(self.charges)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="eval-metered-"))
        self.addCleanup(shutil.rmtree, self.root)
        self.fake = FakeDeepSeek()
        self.addCleanup(self.fake.server.server_close)
        self.addCleanup(self.fake.server.shutdown)
        self.settler = settlers.DeepSeekSettler(self.fake.base_url, KEY, PRICE)
        self.open_owner()
        self.env.fund(credit=10.0)
        opening = self.settler.key_usage()  # −10: a funded balance reads negative
        self.env.register_scope("ds", provider="deepseek", fingerprint=envelope.fingerprint(KEY),
                                opening_usage=opening, allocation=10.0, provider_limit=None)
        self.env.set_route("r", scope="ds", max_charge=BOUND, slots=3, policy=POLICY, bootstrap_build_usd=1.0,
                           upstream={"base_url": self.fake.base_url, "settler": "deepseek", "model": MODEL})
        self.env.validate_route("r")
        self.env.reconcile("ds", opening)
        self.env.reserve("job", 1.0)

    def open_owner(self):
        self.owned = Envelope.owned("B", root=self.root, create_usd=150.0)
        self.env = self.owned.__enter__()
        self.addCleanup(self.close_owner)

    def close_owner(self):
        if self.owned is not None:
            self.owned.__exit__(None, None, None)
            self.owned = None

    def start(self):
        served = proxy.Proxy(self.env, settler_for=lambda upstream, fingerprint: self.settler, settle_seconds=3600,
                             reconcile_seconds=3600, shutdown_seconds=0)
        served.__enter__()
        self.addCleanup(served.__exit__, None, None, None)
        return served

    def post(self, base, path="/anthropic/v1/messages", size=200):
        if path == "/responses":
            body = {"model": MODEL, "stream": True, "input": [{"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": ""}]}]}
            text = body["input"][0]["content"][0]
        else:
            body = {"model": MODEL, "max_tokens": 100, "stream": True, "messages": [{"role": "user", "content": [
                {"type": "text", "text": ""}]}]}
            text = body["messages"][0]["content"][0]
        text["text"] = "x" * max(0, size - len(json.dumps(body)))
        request = urllib.request.Request(base + path, data=json.dumps(body).encode(),
                                         headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                response.read()
                return response.status
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code
        except (urllib.error.URLError, ConnectionError, OSError):
            return None

    def requests(self):
        return list(self.env.state["requests"].values())


class MeteredSettlementTest(Fixture):
    def test_an_anthropic_stream_settles_at_its_reported_usage_priced_at_peak(self):
        served = self.start()
        self.assertEqual(self.post(served.session("job", "a1")["r"]), 200)
        (request,) = self.requests()
        self.assertEqual((request["billing_id"], request["served"], request["protocol_complete"]),
                         ("msg1", [MODEL], True))
        served.settle_now()
        (request,) = self.requests()
        prompt = 200 + OVERHEAD
        self.assertAlmostEqual(request["billed"], peak(prompt - prompt // 2, prompt // 2, 1000), places=12)
        self.assertAlmostEqual(request["billed"], self.fake.billed, places=12)  # at peak: exactly the charge
        self.assertEqual((self.env.answered("a1"), self.env.answered("other")), (1, 0))  # the network probe's control

    def test_a_responses_stream_counts_cached_input_inside_input_and_reasoning_inside_output(self):
        served = self.start()
        self.assertEqual(self.post(served.session("job", "a1")["r"], path="/responses"), 200)
        served.settle_now()
        (request,) = self.requests()
        self.assertEqual((request["billing_id"], request["served"]), ("resp1", [MODEL]))
        self.assertAlmostEqual(request["billed"], self.fake.billed, places=12)

    def test_off_peak_billing_is_below_the_metered_figure(self):
        self.fake.rate = 0.5
        served = self.start()
        self.post(served.session("job", "a1")["r"])
        served.settle_now()
        self.assertAlmostEqual(self.requests()[0]["billed"], 2 * self.fake.billed, places=12)

    def test_a_stream_cut_before_message_stop_stays_unresolved(self):
        self.fake.cut = 3  # after message_delta's usage, before message_stop
        served = self.start()
        self.post(served.session("job", "a1")["r"])
        served.settle_now()
        (request,) = self.requests()
        self.assertIsNotNone(request["metered"])
        self.assertEqual((request["protocol_complete"], request["state"]), (False, "unresolved"))

    def test_malformed_or_conflicting_usage_never_settles_automatically(self):
        # Each case stays unresolved by design: give every one its own slot, and the credit to hold it.
        self.env.fund(credit=11.0)
        self.env.raise_allocation("ds", 21.0)
        self.env.set_route("r", scope="ds", max_charge=BOUND, slots=6, policy=POLICY, bootstrap_build_usd=1.0,
                           upstream={"base_url": self.fake.base_url, "settler": "deepseek", "model": MODEL})
        self.env.validate_route("r")
        served = self.start()
        good = {"type": "message_delta", "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 0,
                                                   "cache_creation_input_tokens": 0}}
        for name, events in (
                ("malformed", [{"type": "message_start", "message": {"id": "m1"}},
                               {"type": "message_delta", "usage": {"input_tokens": "ten", "output_tokens": 5}},
                               {"type": "message_stop"}]),
                ("valid then malformed", [{"type": "message_start", "message": {"id": "m2"}}, good,
                                          {"type": "message_delta", "usage": {"output_tokens": 5}},
                                          {"type": "message_stop"}]),
                ("two figures", [{"type": "message_start", "message": {"id": "m3"}}, good,
                                 {"type": "message_delta", "usage": {**good["usage"], "input_tokens": 99}},
                                 {"type": "message_stop"}]),
                ("server tool", [{"type": "message_start", "message": {"id": "m4"}},
                                 {"type": "message_delta", "usage": {
                                     "input_tokens": 1, "output_tokens": 1, "cache_read_input_tokens": 0,
                                     "cache_creation_input_tokens": 0, "server_tool_use": {"web_search_requests": 1}}},
                                 {"type": "message_stop"}]),
                ("null server tools", [{"type": "message_start", "message": {"id": "m6"}},
                                       {"type": "message_delta", "usage": {
                                           "input_tokens": 1, "output_tokens": 1, "cache_read_input_tokens": 0,
                                           "cache_creation_input_tokens": 0, "server_tool_use": None}},
                                       {"type": "message_stop"}]),
                ("no cache counter", [{"type": "message_start", "message": {"id": "m5"}},
                                      {"type": "message_delta", "usage": {"input_tokens": 0, "output_tokens": 1}},
                                      {"type": "message_stop"}])):
            with self.subTest(name):
                self.fake.events_override = lambda *a, events=events: events
                self.post(served.session("job", name)["r"])
        served.settle_now()
        self.assertEqual(len(self.requests()), 6)  # every case was admitted and ran
        for request in self.requests():
            self.assertIsNotNone(request["metered_invalid"], request["attempt"])
            self.assertEqual(request["state"], "unresolved", request["attempt"])

    def test_captured_figures_survive_a_restart_before_settlement(self):
        self.fake.cut = 3
        served = self.start()
        self.post(served.session("job", "cut")["r"])
        self.fake.cut = None
        self.post(served.session("job", "whole")["r"])
        served.__exit__(None, None, None)
        self.close_owner()
        self.open_owner()  # a new owner process
        self.env.reconcile("ds", self.settler.key_usage())
        served = self.start()
        served.settle_now()
        states = {r["attempt"]: (r["state"], r["protocol_complete"], r["metered"] is not None) for r in self.requests()}
        whole = next(r for r in self.requests() if r["attempt"] == "whole")
        self.assertEqual(set(whole["metered_tokens"]), {"fresh", "cached", "output"})  # the breakdown survives
        self.assertEqual(states, {"cut": ("unresolved", False, True), "whole": ("billed", True, True)})


class MeteredBoundTest(Fixture):
    def test_maximal_requests_with_late_balance_and_slot_reuse_stay_within_funded_credit(self):
        """While the reported usage is complete, settled ≥ billed, so reusing freed slots before the
        balance shows the charges can't take spend past funded credit."""
        self.fake.output, self.fake.lag, self.fake.cached_share = MAX_OUT, 2, 0.0
        served = self.start()
        size = CONTEXT - OVERHEAD
        statuses = []
        for i in range(12):
            statuses.append(self.post(served.session("job", f"a{i}")["r"], size=size))
            served.settle_now()
            self.env.reconcile("ds", self.settler.key_usage())
        self.assertIn(200, statuses)
        self.assertIn(429, statuses)
        self.assertTrue(all(c <= BOUND + 1e-9 for c in self.fake.charges))
        self.assertAlmostEqual(max(self.fake.charges), BOUND, places=6)  # the bound is tight
        self.assertLessEqual(self.fake.billed, 10.0 + 1e-9)
        self.assertEqual(envelope.summary(self.env.state, 0)["scopes"]["ds"]["excess"], 0.0)

    def test_usage_reported_short_of_the_charge_is_caught_once_the_balance_shows_it(self):
        self.fake.hidden = 900  # billed for 1000 output tokens, reports 100
        served = self.start()
        for i in range(3):
            self.post(served.session("job", f"a{i}")["r"])
            served.settle_now()
        self.assertGreater(self.env.reconcile("ds", self.settler.key_usage()), 0)
        self.assertEqual(self.post(served.session("job", "after")["r"]), 429)


class SignedReadingTest(Fixture):
    def test_a_funded_balance_reads_negative_and_rises_through_zero(self):
        scope = self.env.state["scopes"]["ds"]
        self.assertAlmostEqual(scope["opening_usage"], -10.0)
        self.env.reconcile("ds", -4.0)  # spent 6, but nothing settled: excess, not a fault
        self.assertIsNone(self.env.state["scopes"]["ds"]["fault"])
        self.env.reconcile("ds", 0.5)  # rose past zero
        self.assertIsNone(self.env.state["scopes"]["ds"]["fault"])

    def test_a_falling_reading_faults_the_scope_across_restarts_until_cleared(self):
        self.env.register_scope("or", provider="openrouter", fingerprint="fp-or", opening_usage=1.0,
                                allocation=0.0, provider_limit=None)
        self.env.reconcile("or", 1.0)
        self.env.reconcile("ds", -10.5)  # a top-up: the balance rose
        self.assertIn("fell", self.env.state["scopes"]["ds"]["fault"])
        with self.assertRaises(Refused):
            self.env.reserve("other", 0.1)
        with self.assertRaises(Refused):
            self.env.admit("q1", "r", job="job", attempt="a1")
        self.close_owner()
        self.open_owner()
        self.assertIsNotNone(self.env.state["scopes"]["ds"]["fault"])
        self.env.reconcile("ds", -10.5)
        self.env.reconcile("or", 1.0)
        with self.assertRaises(Refused):
            self.env.admit("q2", "r", job="job", attempt="a2")
        self.env.clear_fault("ds")
        self.env.admit("q3", "r", job="job", attempt="a3")

    def test_a_first_reading_below_the_opening_usage_faults_the_scope(self):
        self.env.register_scope("ds2", provider="deepseek", fingerprint="fp-2", opening_usage=-3.0,
                                allocation=0.0, provider_limit=None)
        self.env.reconcile("ds2", -3.5)
        self.assertIsNotNone(self.env.state["scopes"]["ds2"]["fault"])


class EventReadingTest(unittest.TestCase):
    """DeepSeekSettler.read and billing_id on single events, both protocols."""

    def setUp(self):
        self.settler = settlers.DeepSeekSettler("http://unused", KEY, PRICE)

    def test_cache_writes_are_priced_as_input(self):
        reading = self.settler.read({"type": "message_delta", "usage": {
            "input_tokens": 100, "cache_creation_input_tokens": 900, "cache_read_input_tokens": 50, "output_tokens": 10}})
        self.assertAlmostEqual(reading.metered.usd, peak(1000, 50, 10), places=15)

    def test_a_server_tool_call_makes_the_usage_unpriceable(self):
        for event in ({"type": "message_delta", "usage": {"input_tokens": 1, "output_tokens": 1,
                                                          "server_tool_use": {"web_search_requests": 2}}},
                      {"type": "response.completed", "response": {"usage": {
                          "input_tokens": 1, "output_tokens": 1, "server_tool_use": {"tool_search": 1}}}}):
            with self.subTest(event=event["type"]):
                self.assertIsInstance(self.settler.read(event).metered, settlers.Invalid)
        full = {"input_tokens": 1, "output_tokens": 1, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
        zero = {"type": "message_delta", "usage": {**full, "server_tool_use": {"web_search_requests": 0}}}
        self.assertIsInstance(self.settler.read(zero).metered, settlers.Usage)
        for malformed in ({"tool_search": True}, [1], {"web_search_requests": -1}, {"web_search_requests": "1"},
                          {"web_search_requests": False}, {"tool_search": ""}, {"tool_fetch": 0.0},  # falsy, still malformed
                          None):  # present but null: not the same as absent
            with self.subTest(malformed=malformed):
                event = {"type": "message_delta", "usage": {**full, "server_tool_use": malformed}}
                self.assertIsInstance(self.settler.read(event).metered, settlers.Invalid)
        responses_null = {"type": "response.completed", "response": {"usage": {
            "input_tokens": 1, "output_tokens": 1, "server_tool_use": None}}}
        self.assertIsInstance(self.settler.read(responses_null).metered, settlers.Invalid)

    def test_an_absurd_counter_is_unusable_not_a_crash(self):
        huge = 10 ** 400
        for event in ({"type": "message_delta", "usage": {"input_tokens": huge, "output_tokens": 1,
                                                          "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}},
                      {"type": "response.completed", "response": {"usage": {"input_tokens": huge, "output_tokens": 1}}}):
            with self.subTest(event=event["type"]):
                self.assertIsInstance(self.settler.read(event).metered, settlers.Invalid)

    def test_every_anthropic_counter_is_required_since_input_excludes_cache_hits(self):
        full = {"input_tokens": 10, "output_tokens": 1, "cache_read_input_tokens": 500, "cache_creation_input_tokens": 0}
        self.assertIsInstance(self.settler.read({"type": "message_delta", "usage": full}).metered, settlers.Usage)
        for name in full:
            for bad in (None, "absent"):
                usage = {k: v for k, v in full.items() if k != name} if bad == "absent" else {**full, name: None}
                with self.subTest(name=name, bad=bad):
                    reading = self.settler.read({"type": "message_delta", "usage": usage})
                    self.assertIsInstance(reading.metered, settlers.Invalid)

    def test_the_billing_id_comes_only_from_the_events_that_carry_the_response(self):
        get = self.settler.billing_id
        self.assertEqual(get({"type": "message_start", "message": {"id": "msg_1"}}), "msg_1")
        self.assertEqual(get({"type": "response.created", "response": {"id": "resp_1"}}), "resp_1")
        self.assertEqual(get({"type": "message", "id": "msg_2"}), "msg_2")
        for stray in ({"type": "message_delta", "id": "x1"}, {"type": "content_block_start", "id": "x2"},
                      {"type": "response.output_item.added", "item": {"id": "x3"}, "id": "x4"},
                      {"type": "message_start", "message": {"id": "has space"}}):
            with self.subTest(stray=stray):
                self.assertIsNone(get(stray))


class BalanceReadingTest(unittest.TestCase):
    def reading(self, payload, raw=False):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                data = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        settler = settlers.DeepSeekSettler(f"http://127.0.0.1:{server.server_address[1]}", KEY)
        return settler.balance_reading() if raw else settler.key_usage()

    def test_the_usd_balance_is_read_negated_and_anything_else_is_unavailable(self):
        self.assertEqual(self.reading({"balance_infos": [{"currency": "USD", "total_balance": "4.25"}]}), -4.25)
        self.assertEqual(self.reading({"balance_infos": [{"currency": "USD", "total_balance": "4.250"}]}, raw=True),
                         (-4.25, "4.250"))  # the raw string keeps its precision
        self.assertEqual(self.reading({"balance_infos": [{"currency": "USD", "total_balance": "1.00"},
                                                         {"currency": "CNY", "total_balance": "0.00"}]}), -1.0)
        for bad in ({"balance_infos": [{"currency": "CNY", "total_balance": "3.00"}]},
                    {"balance_infos": [{"currency": "USD", "total_balance": "nan"}]},
                    {"balance_infos": [{"currency": "USD"}]}, {"nothing": 1},
                    {"balance_infos": [{"currency": "USD", "total_balance": True}]},
                    {"balance_infos": []},  # no USD row: unavailable, never a zero balance
                    {"balance_infos": [{"currency": "USD", "total_balance": "10"}, {"currency": "CNY", "total_balance": "25"},
                                       {"currency": "CNY", "total_balance": "0"}]},  # a later row can't hide one
                    {"balance_infos": "USD 4"}, {"balance_infos": [{"currency": "USD", "total_balance": 10 ** 400}]}):
            with self.subTest(bad=bad), self.assertRaises(settlers.UsageUnavailable):
                self.reading(bad)


if __name__ == "__main__":
    unittest.main()
