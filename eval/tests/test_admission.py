"""The admission checks against fakes: each passes, fails and turns inconclusive exactly on its rule."""

import http.server
import json
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from eval.harness import admission, envelope, prices, proxy, settlers
from eval.harness.admission import FAIL, INCONCLUSIVE, PASS
from eval.harness.envelope import Envelope

UTC = timezone.utc
PRO = admission.RouteRef("deepseek--deepseek-v4-pro", "deepseek", "deepseek-v4-pro")
GLM = admission.RouteRef("openrouter--z-ai__glm-5.3", "openrouter", "z-ai/glm-5.3")
PRO_PRICE = prices.lookup("deepseek", "deepseek-v4-pro")
GLM_PRICE = prices.lookup("openrouter", "z-ai/glm-5.3")
MONDAY_PEAK = datetime(2026, 11, 2, 6, 0, tzinfo=UTC)


def at(day, hour, minute=0):
    return datetime(2026, 11, day, hour, minute, tzinfo=UTC)


class PeakWindowTest(unittest.TestCase):
    def test_boundaries_weekends_and_coverage(self):
        for t, expected in ((at(2, 0, 59), False), (at(2, 1), True), (at(2, 3, 59), True), (at(2, 4), False),
                            (at(2, 5, 59), False), (at(2, 6), True), (at(2, 9, 59), True), (at(2, 10), False),
                            (at(7, 7), False), (at(8, 7), False)):  # Saturday, Sunday
            with self.subTest(t=t):
                self.assertIs(admission.peak_window(t), expected)
        self.assertIsNone(admission.peak_window(datetime(2027, 1, 4, 7, tzinfo=UTC)))  # past coverage

    def test_a_listed_holiday_is_off_peak(self):
        original = admission.HOLIDAYS
        admission.HOLIDAYS = frozenset({at(3, 0).date()})
        try:
            self.assertFalse(admission.peak_window(at(3, 7)))
        finally:
            admission.HOLIDAYS = original

    def test_a_check_needs_thirty_minutes_of_window_left(self):
        admission._require_window(at(2, 9, 30))
        with self.assertRaises(admission.Refused):
            admission._require_window(at(2, 9, 31))
        with self.assertRaises(admission.Refused):
            admission._require_window(datetime(2027, 1, 4, 7, tzinfo=UTC))

    def test_tolerance_follows_the_balances_decimals_with_a_cent_floor(self):
        self.assertAlmostEqual(admission.tolerance("4.25"), 0.02)
        self.assertAlmostEqual(admission.tolerance("4.2500"), 0.01)
        self.assertAlmostEqual(admission.tolerance("4.2"), 0.2)


def request(rid, *, billed=None, metered=None, cached=0, state="billed", served=None, tokens=None, at_time=None):
    return {"request": rid, "state": state, "billed": billed if billed is not None else metered,
            "metered": metered, "metered_tokens": {"fresh": 1000, "cached": cached, "output": 100} if metered is not None
            else None, "billed_tokens": tokens, "served": served or [], "admitted_at": (at_time or MONDAY_PEAK).timestamp()}


class FakeAccount:
    """Sessions, requests, a clock and a DeepSeek-like balance: each session spends `metered` (as metered by
    the proxy) and the provider bills `rate` × that, visible `lag` seconds later."""

    def __init__(self, *, start=MONDAY_PEAK, rate=1.0, lag=0, metered=0.30, cached=500_000, reasoning=6000,
                 unresolved=False, outside=False, funded=50.0, decimals=2, noise=None):
        self.clock, self.rate, self.lag, self.metered = start, rate, lag, metered
        self.cached, self.reasoning, self.unresolved, self.outside = cached, reasoning, unresolved, outside
        self.funded, self.decimals, self.noise = funded, decimals, noise
        self.charges, self.by_attempt, self.sessions = [], {}, 0

    def now(self):
        return self.clock

    def sleep(self, seconds):
        self.clock += timedelta(seconds=seconds)

    def balance(self):
        visible = sum(c for t, c in self.charges if (self.clock - t).total_seconds() >= self.lag)
        extra = self.noise(self.clock) if self.noise else 0.0
        balance = self.funded - visible - extra
        return -balance, f"{balance:.{self.decimals}f}"

    def session(self, attempt_dir):
        self.sessions += 1
        key = str(Path(attempt_dir).resolve())
        when = self.clock - timedelta(hours=12) if self.outside else self.clock
        self.by_attempt[key] = [request(f"q{self.sessions}", metered=self.metered, cached=self.cached,
                                        state="unresolved" if self.unresolved else "billed", at_time=when,
                                        served=["deepseek-v4-pro"])]
        self.charges.append((self.clock, self.rate * self.metered))
        self.clock += timedelta(minutes=2)
        return SimpleNamespace(terminal="completed", reason="", usage={"reasoning_output_tokens": self.reasoning},
                               transcript=Path("/nonexistent"))

    def deps(self):
        return admission.Deps(session=self.session, requests=lambda key: self.by_attempt.get(key, []),
                              settle=lambda: None, balance=self.balance, scope_unresolved=lambda: False,
                              now=self.now, sleep=self.sleep)


class MeteringTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="eval-admission-"))
        self.addCleanup(shutil.rmtree, self.root)

    def check(self, account, cli="codex"):
        return admission.metering(PRO, cli, PRO_PRICE, "a1", self.root, account.deps())

    def test_agreement_within_tolerance_passes(self):
        result = self.check(FakeAccount())
        self.assertEqual(result.verdict, PASS, result.reason)
        self.assertEqual(result.reason, "agreement for the observed workload within tolerance")
        self.assertGreaterEqual(result.evidence["metered"], 50 * 0.02)

    def test_billed_beyond_the_metered_total_fails(self):
        self.assertEqual(self.check(FakeAccount(rate=1.2)).verdict, FAIL)

    def test_billed_short_of_metered_is_inconclusive(self):
        result = self.check(FakeAccount(rate=0.5))  # debits that plateau below the metered total
        self.assertEqual(result.verdict, INCONCLUSIVE)
        self.assertIn("still be landing", result.reason)

    def test_a_balance_still_moving_after_the_run_is_inconclusive(self):
        account = FakeAccount()
        start = account.clock
        account.noise = lambda t: 0.01 * int((t - start).total_seconds() // 300) if t > start + timedelta(minutes=60) else 0
        self.assertEqual(self.check(account).verdict, INCONCLUSIVE)

    def test_a_balance_that_never_settles_before_the_run_is_inconclusive(self):
        account = FakeAccount()
        account.noise = lambda t: 0.01 * int((t - MONDAY_PEAK).total_seconds() // 300)
        result = self.check(account)
        self.assertEqual((result.verdict, account.sessions), (INCONCLUSIVE, 0))

    def test_an_earlier_debit_landing_late_delays_the_baseline_and_never_enters_the_fall(self):
        account = FakeAccount()
        account.noise = lambda t: 0.50 if t >= MONDAY_PEAK + timedelta(minutes=3) else 0.0
        result = self.check(account)
        self.assertEqual(result.verdict, PASS, result.reason)
        self.assertAlmostEqual(result.evidence["fall"], result.evidence["metered"], places=6)

    def test_requests_outside_the_window_or_unresolved_are_inconclusive(self):
        self.assertIn("outside the peak window", self.check(FakeAccount(outside=True)).reason)
        self.assertIn("unresolved", self.check(FakeAccount(unresolved=True)).reason)

    def test_a_workload_without_reasoning_or_cache_never_qualifies(self):
        for account in (FakeAccount(reasoning=0), FakeAccount(cached=0)):
            result = self.check(account)
            self.assertEqual((result.verdict, account.sessions), (INCONCLUSIVE, admission.MAX_SESSIONS))

    def test_a_component_worth_exactly_the_tolerance_does_not_qualify(self):
        """Reasoning summing to one tolerance at the session cap: omitting it entirely could hide inside
        the comparison, so the rule asks for three."""
        per_session = int(0.02 * 1e6 / PRO_PRICE.rates.output / admission.MAX_SESSIONS) + 1
        result = self.check(FakeAccount(reasoning=per_session))
        self.assertEqual(result.verdict, INCONCLUSIVE)
        self.assertLess(result.evidence["reasoning_usd"], 3 * 0.02)
        self.assertGreaterEqual(result.evidence["reasoning_usd"], 0.02)

    def test_it_refuses_before_spending_off_peak_or_with_unresolved_requests(self):
        saturday = FakeAccount(start=at(7, 7))
        with self.assertRaises(admission.Refused):
            self.check(saturday)
        self.assertEqual(saturday.sessions, 0)
        account = FakeAccount()
        deps = replace(account.deps(), scope_unresolved=lambda: True)
        with self.assertRaises(admission.Refused):
            admission.metering(PRO, "codex", PRO_PRICE, "a1", self.root, deps)

    def test_the_window_closing_mid_workload_is_inconclusive(self):
        result = self.check(FakeAccount(start=at(2, 9, 25), metered=0.05))
        self.assertEqual(result.verdict, INCONCLUSIVE)

    def test_claude_reasoning_is_read_from_the_transcripts_thinking_text(self):
        transcript = self.root / "t.jsonl"
        transcript.write_text(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "thinking", "thinking": "x" * 600}, {"type": "text", "text": "y" * 600}]}}) + "\n")
        self.assertEqual(admission.reasoning_tokens("claude", SimpleNamespace(transcript=transcript)), 100)
        self.assertEqual(admission.reasoning_tokens("codex", SimpleNamespace(usage={"reasoning_output_tokens": 7})), 7)
        self.assertEqual(admission.reasoning_tokens("codex", SimpleNamespace(usage={"reasoning_output_tokens": True})), 0)


class CacheAndServedTest(unittest.TestCase):
    def run_check(self, requests, terminal="completed", route=GLM, price=GLM_PRICE):
        deps = admission.Deps(session=lambda d: SimpleNamespace(terminal=terminal, reason="x"),
                              requests=lambda key: requests, settle=lambda: None, sleep=lambda s: None)
        return {r.check: r for r in admission.cache_and_served(route, "claude", price, "a1", Path("/tmp/x"), deps)}

    def tokens(self, prompt=100_000, cached=80_000, completion=1_000):
        uncached = (prompt * 1.40 + completion * 4.40) / 1e6
        saving = cached * (1.40 - 0.26) / 1e6
        return {"prompt": prompt, "completion": completion, "cached": cached}, uncached, saving

    def test_a_saving_at_the_cache_rate_passes_and_zai_served_it(self):
        counts, uncached, saving = self.tokens()
        results = self.run_check([request("q1", billed=uncached - saving, tokens=counts, served=["Z.AI"])])
        self.assertEqual((results["cache"].verdict, results["served"].verdict), (PASS, PASS))

    def test_a_small_saving_or_none_fails(self):
        counts, uncached, saving = self.tokens()
        self.assertEqual(self.run_check([request("q1", billed=uncached - 0.1 * saving, tokens=counts,
                                                 served=["Z.AI"])])["cache"].verdict, FAIL)
        none, uncached, _ = self.tokens(cached=0)
        self.assertEqual(self.run_check([request("q1", billed=uncached, tokens=none, served=["Z.AI"])])["cache"].verdict,
                         FAIL)

    def test_another_provider_or_none_fails_served(self):
        counts, uncached, saving = self.tokens()
        for served in (["Chutes"], [], ["Z.AI", "Chutes"]):
            with self.subTest(served=served):
                results = self.run_check([request("q1", billed=uncached - saving, tokens=counts, served=served)])
                self.assertEqual(results["served"].verdict, FAIL)

    def test_deepseek_must_report_the_routes_model(self):
        results = self.run_check([request("q1", metered=0.01, cached=500, served=["deepseek-v4-pro"])], route=PRO,
                                 price=PRO_PRICE)
        self.assertEqual(results["served"].verdict, PASS)

    def test_incomplete_evidence_is_inconclusive(self):
        counts, uncached, saving = self.tokens()
        cases = {"empty": ([], "completed"), "unresolved": ([request("q1", state="unresolved")], "completed"),
                 "session failed": ([request("q1", billed=1.0, tokens=counts)], "infra"),
                 "no counts": ([request("q1", billed=1.0)], "completed")}
        for name, (requests, terminal) in cases.items():
            with self.subTest(name):
                results = self.run_check(requests, terminal)
                self.assertEqual(results["cache"].verdict, INCONCLUSIVE)


class FakeDrain:
    """A provider account drained by concurrent requests: each costs `cost`; a batch is admitted
    against the balance at its start, so it can overdraw; refusals per `refuse`."""

    def __init__(self, *, funded=2.0, cost=0.04, overspend=0.0, size_refusal_at=None, clears=False):
        self.balance, self.cost, self.overspend = funded, cost, overspend
        self.size_refusal_at, self.clears, self.refusals = size_refusal_at, clears, 0
        self.batches = 0

    def send(self, count):
        self.batches += 1
        replies, start = [], self.balance
        for _ in range(count):
            if self.size_refusal_at is not None and start <= self.size_refusal_at:
                replies.append(admission.Reply(402, {"error": {"message": "x", "metadata": {"reason": "weight_exceeds_budget"}}}))
            elif start <= -self.overspend and not (self.clears and self.refusals >= 1):
                self.refusals += 1
                replies.append(admission.Reply(402, {"error": {"message": "Insufficient Balance"}}))
            else:
                self.balance -= self.cost
                replies.append(admission.Reply(200, {"id": "x"}))
        return replies

    def deps(self):
        return admission.Deps(send=self.send, remaining=lambda: self.balance, requests=lambda a: [],
                              settle=lambda: None, sleep=lambda s: None)


class ExhaustionTest(unittest.TestCase):
    def run_check(self, drain, bound=0.8, max_requests=300):
        return admission.exhaustion(GLM, "x1", funding=2.0, request_cost=0.05, overdraw_bound=bound,
                                    deps=drain.deps(), max_requests=max_requests)

    def test_the_provider_stopping_at_its_balance_within_the_reserve_passes(self):
        result = self.run_check(FakeDrain())
        self.assertEqual(result.verdict, PASS, result.reason)
        self.assertLessEqual(result.evidence["overdraw"], 0.8)
        self.assertIn("Insufficient Balance", json.dumps(result.evidence["refusals"]))

    def test_serving_past_the_limit_beyond_the_reserve_fails(self):
        self.assertEqual(self.run_check(FakeDrain(overspend=2.0)).verdict, FAIL)

    def test_refusals_that_dont_show_exhaustion_are_inconclusive(self):
        cases = {"a refusal that clears": FakeDrain(clears=True),
                 "a request-size refusal with credit left": FakeDrain(size_refusal_at=0.5)}
        for name, drain in cases.items():
            with self.subTest(name):
                self.assertEqual(self.run_check(drain).verdict, INCONCLUSIVE)

    def test_the_envelope_refusing_first_or_the_cap_is_inconclusive(self):
        envelope_first = FakeDrain()
        envelope_first.send = lambda count: [admission.Reply(429, {"error": {"type": "proxy", "message": "spend refused"}})]
        self.assertIn("envelope refused", self.run_check(envelope_first).reason)
        self.assertIn("without the provider refusing", self.run_check(FakeDrain(funded=100.0), max_requests=40).reason)


class RecordsTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="eval-admission-records-"))
        self.addCleanup(shutil.rmtree, self.root)
        self.binding = admission.Binding(PRO.name, "v1", "fp1", "https://api.deepseek.com/responses", "codex",
                                         "codex 0.164", prices.TABLE_SHA256, "deepseek-main")

    def write(self, check="metering", attempt="20261102T060000-aaaaaa", verdict=PASS, state="done", binding=None,
              route=PRO.name, cli="codex", evidence=None):
        result = admission.Result(check, route, cli, attempt, verdict, "", evidence or {})
        return admission.write(self.root, result, binding or self.binding, state)

    def test_attempts_are_kept_and_an_interrupted_one_stays_running(self):
        first = self.write(attempt="20261102T060000-aaaaaa")
        second = self.write(attempt="20261102T070000-bbbbbb", state="running")
        self.assertNotEqual(first, second)
        (row,) = [r for r in admission.status(self.root, [self.binding]) if r["check"] == "metering"]
        self.assertEqual((row["attempt"], row["status"]), ("20261102T070000-bbbbbb", "running"))

    def test_a_changed_cli_version_or_price_table_makes_an_attempt_stale(self):
        self.write()
        for change in ({"cli_version": "codex 0.170"}, {"prices_sha": "other"}, {"route_version": "v2"}):
            with self.subTest(change=change):
                (row,) = [r for r in admission.status(self.root, [replace(self.binding, **change)])
                          if r["check"] == "metering"]
                self.assertTrue(row["stale"])
        (row,) = [r for r in admission.status(self.root, [self.binding]) if r["check"] == "metering"]
        self.assertFalse(row["stale"])

    def test_exhaustion_applies_to_every_route_of_the_same_account_after_the_key_rollover(self):
        exhaust = replace(self.binding, route="deepseek--deepseek-flash", key_fingerprint="old-key", cli="-")
        self.write(check="exhaustion", route=exhaust.route, cli="-", binding=exhaust)
        b_routes = [replace(self.binding, key_fingerprint="new-key"),
                    replace(self.binding, route="deepseek--deepseek-flash", key_fingerprint="new-key")]
        rows = [r for r in admission.status(self.root, b_routes) if r["check"] == "exhaustion"]
        self.assertEqual([r["verdict"] for r in rows], [PASS, PASS])
        other = replace(self.binding, account="another-account")
        (row,) = [r for r in admission.status(self.root, [other]) if r["check"] == "exhaustion"]
        self.assertIsNone(row["verdict"])

    def test_the_budget_subtracts_the_exhaustion_runs(self):
        self.write(check="exhaustion", route="openrouter--z-ai__glm-5.3-flash", cli="-",
                   evidence={"funding": 5.0, "fees": 0.8, "taxes": 0.0})
        self.write(check="exhaustion", route="deepseek--deepseek-flash", cli="-", attempt="20261102T070000-cccccc",
                   evidence={"funding": 2.0, "fees": 0.0, "taxes": 0.0})
        self.assertAlmostEqual(admission.budget(self.root), 150.0 - 7.8)


class AdmitCommandTest(unittest.TestCase):
    def admit(self, *argv, root):
        from unittest import mock
        from eval.__main__ import main
        from eval.harness import admit_commands
        with mock.patch.object(envelope, "DEFAULT_ROOT", root), mock.patch.object(admit_commands, "ROOT", root / "adm"), \
                mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            return main(["admit", *argv])

    def test_it_refuses_a_bad_route_and_a_missing_envelope_before_any_spend(self):
        root = Path(tempfile.mkdtemp(prefix="eval-admit-"))
        self.addCleanup(shutil.rmtree, root)
        self.assertEqual(self.admit("cache", "--route", "nowhere", "--cli", "codex", root=root), 2)
        self.assertEqual(self.admit("cache", "--route", "fireworks--glm", "--cli", "codex", root=root), 2)
        self.assertEqual(self.admit("cache", "--route", "deepseek--deepseek-flash", root=root), 2)  # no --cli
        self.assertEqual(self.admit("metering", "--route", "openrouter--z-ai__glm-5.3", "--cli", "codex",
                                    root=root), 2)  # not metered
        self.assertEqual(self.admit("cache", "--route", "deepseek--deepseek-flash", "--cli", "codex", root=root), 3)
        self.assertEqual(self.admit("exhaustion", "--route", "deepseek--deepseek-flash", root=root), 2)  # no --funding
        self.assertFalse((root / "adm").exists())  # nothing recorded, nothing spent
        self.assertEqual(self.admit("budget", root=root), 0)


class ExhaustionThroughTheProxyTest(unittest.TestCase):
    """The real proxy and envelope against a fake DeepSeek that refuses at a zero balance."""

    def setUp(self):
        from eval.tests import test_metered
        self.root = Path(tempfile.mkdtemp(prefix="eval-admission-proxy-"))
        self.addCleanup(shutil.rmtree, self.root)
        self.fake = test_metered.FakeDeepSeek(funded=2.0)
        self.fake.output, self.fake.refuse_at_zero = 16, True
        self.addCleanup(self.fake.server.server_close)
        self.addCleanup(self.fake.server.shutdown)
        self.flash = prices.lookup("deepseek", "deepseek-flash")
        self.settler = settlers.DeepSeekSettler(self.fake.base_url, test_metered.KEY, self.flash)
        self.key = test_metered.KEY

    def drain(self, rate):
        from eval.tests import test_metered
        self.fake.rate = rate
        policy = {**test_metered.POLICY, "models": ["deepseek-flash"]}
        bound = prices.request_charge_bound(self.flash, max_input_tokens=test_metered.CONTEXT,
                                            max_output_tokens=test_metered.MAX_OUT)
        slots, funding = 4, 2.0
        credit = round((funding + (slots + 1) * bound) * 1.25, 4)
        with Envelope.owned("X", root=self.root, create_usd=credit + slots * bound + 1) as env:
            env.fund(credit=credit)
            env.fund_probe(credit)
            opening = self.settler.key_usage()
            env.register_scope("ds", provider="deepseek", fingerprint=envelope.fingerprint(self.key),
                               opening_usage=opening, allocation=credit, provider_limit=None, account="ds-main")
            env.set_route("r", scope="ds", max_charge=bound, slots=slots, policy=policy, bootstrap_build_usd=1.0,
                          upstream={"base_url": self.fake.base_url, "settler": "deepseek", "model": "deepseek-flash"})
            env.reconcile("ds", opening)
            with proxy.Proxy(env, settler_for=lambda u, f: self.settler, settle_seconds=3600, reconcile_seconds=3600,
                             shutdown_seconds=0) as served:
                body = json.dumps({"model": "deepseek-flash", "max_tokens": 16, "stream": True, "messages": [
                    {"role": "user", "content": [{"type": "text", "text": "x" * 30_000}]}]}).encode()

                def send(count):
                    out, threads = [None] * count, []

                    def one(i):
                        base = served.session(envelope.PROBE, "x1")["r"]
                        request = urllib.request.Request(base + "/anthropic/v1/messages", data=body,
                                                         headers={"x-api-key": self.key})
                        try:
                            with urllib.request.urlopen(request, timeout=20) as response:
                                response.read()
                                out[i] = admission.Reply(response.status, {})
                        except urllib.error.HTTPError as exc:
                            with exc:
                                try:
                                    out[i] = admission.Reply(exc.code, json.loads(exc.read()))
                                except ValueError:
                                    out[i] = admission.Reply(exc.code, None)
                    for i in range(count):
                        threads.append(threading.Thread(target=one, args=(i,)))
                        threads[-1].start()
                    for t in threads:
                        t.join()
                    return out

                deps = admission.Deps(send=send, remaining=lambda: -self.settler.key_usage(), requests=env.requests_of,
                                      settle=served.settle_now, sleep=lambda s: None)
                result = admission.exhaustion(admission.RouteRef("r", "deepseek", "deepseek-flash"), "x1", funding=funding,
                                              request_cost=0.01, overdraw_bound=slots * bound, deps=deps)
                refused = [r for r in env.requests_of("x1") if r["refused_by_provider"]]
                return result, refused, env.state

    def test_at_peak_the_provider_stops_first_and_refusals_free_their_slots(self):
        result, refused, state = self.drain(rate=1.0)
        self.assertEqual(result.verdict, PASS, result.reason)
        self.assertTrue(refused)
        self.assertTrue(all(r["state"] == "not_billed" for r in refused))
        self.assertEqual(envelope.summary(state, 0)["unresolved_requests"], 0)

    def test_off_peak_billing_still_lets_the_provider_stop_first(self):
        result, _, _ = self.drain(rate=0.5)  # the envelope counts twice what is billed
        self.assertEqual(result.verdict, PASS, result.reason)

    def test_bs_scope_on_a_fresh_key_after_the_drain_starts_clean(self):
        self.drain(rate=1.0)
        self.fake.funded += 10.0  # the operator's top-up for B
        with Envelope.owned("B", root=self.root, create_usd=150.0) as env:
            env.fund(credit=10.0)
            fresh_key = "sk-ds-SYNTHETIC-B"
            reading = self.settler.key_usage()  # the same account's balance, read with B's key
            env.register_scope("ds", provider="deepseek", fingerprint=envelope.fingerprint(fresh_key),
                               opening_usage=reading, allocation=10.0, provider_limit=None, account="ds-main")
            env.reconcile("ds", self.settler.key_usage())
            scope = env.state["scopes"]["ds"]
            self.assertEqual((scope["excess"], scope["fault"]), (0.0, None))


if __name__ == "__main__":
    unittest.main()
