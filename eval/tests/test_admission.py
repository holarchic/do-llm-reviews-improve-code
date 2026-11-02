"""The admission checks against fakes: each passes, fails and turns inconclusive exactly on its rule."""

import json
import shutil
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from eval.harness import admission, envelope, prices, providers, settlers
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
        with mock.patch.object(admission, "HOLIDAYS", frozenset({at(3, 0).date()})):
            self.assertFalse(admission.peak_window(at(3, 7)))

    def test_a_check_needs_thirty_minutes_of_window_left(self):
        admission.require_window(at(2, 9, 30))
        for refused in (at(2, 9, 31), datetime(2027, 1, 4, 7, tzinfo=UTC)):
            with self.assertRaises(admission.Refused):
                admission.require_window(refused)

    def test_tolerance_follows_the_balances_decimals_with_a_cent_floor(self):
        self.assertAlmostEqual(admission.tolerance("4.25"), 0.02)
        self.assertAlmostEqual(admission.tolerance("4.2500"), 0.01)
        self.assertAlmostEqual(admission.tolerance("4.2"), 0.2)


def request(rid, *, billed=None, metered=None, cached=0, state="billed", served=(), tokens=None, at_time=None):
    return admission.Request(rid, state, billed if billed is not None else metered, metered,
                             {"fresh": 1000, "cached": cached, "output": 100} if metered is not None else None,
                             tokens, tuple(served), (at_time or MONDAY_PEAK).timestamp(), None)


class FakeAccount:
    """Sessions, requests, a clock and a DeepSeek-like balance: each session spends `metered` (as metered by
    the proxy) and the provider bills `rate` × that, visible `lag` seconds later. `noise(t)` is spend
    outside this attempt; `accounted` is what the ledger settled on the scope before it."""

    def __init__(self, *, start=MONDAY_PEAK, rate=1.0, lag=0, metered=0.30, cached=500_000, reasoning=6000,
                 unresolved=False, outside=False, funded=50.0, decimals=2, noise=None, accounted=0.0):
        self.clock, self.rate, self.lag, self.metered = start, rate, lag, metered
        self.cached, self.reasoning, self.unresolved, self.outside = cached, reasoning, unresolved, outside
        self.funded, self.decimals, self.noise, self.accounted = funded, decimals, noise, accounted
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
        key = admission.attempt_key(Path(attempt_dir))
        when = self.clock - timedelta(hours=12) if self.outside else self.clock
        self.by_attempt[key] = [request(f"q{self.sessions}", metered=self.metered, cached=self.cached,
                                        state="unresolved" if self.unresolved else "billed", at_time=when,
                                        served=["deepseek-v4-pro"])]
        self.charges.append((self.clock, self.rate * self.metered))
        self.clock += timedelta(minutes=2)
        return SimpleNamespace(terminal="completed", reason="", usage={"reasoning_output_tokens": self.reasoning},
                               transcript=Path("/nonexistent"))

    def deps(self, **change):
        deps = admission.MeteringDeps(session=self.session, requests=lambda key: self.by_attempt.get(key, []),
                                      settle=lambda: None, sleep=self.sleep, balance=self.balance,
                                      scope_unresolved=lambda: False,
                                      scope_accounted=lambda: (-self.funded, self.accounted), now=self.now)
        return replace(deps, **change)


class MeteringTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="eval-admission-"))
        self.addCleanup(shutil.rmtree, self.root)

    def check(self, account, cli="codex", **change):
        return admission.metering(PRO, cli, PRO_PRICE, "a1", self.root, account.deps(**change))

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

    def test_an_earlier_debit_the_ledger_settled_may_land_late_without_entering_the_fall(self):
        account = FakeAccount(accounted=0.50)
        account.noise = lambda t: 0.50 if t >= MONDAY_PEAK + timedelta(minutes=3) else 0.0
        result = self.check(account)
        self.assertEqual(result.verdict, PASS, result.reason)
        self.assertAlmostEqual(result.evidence["fall"], result.evidence["metered"], places=6)

    def test_earlier_billing_the_ledger_doesnt_account_for_makes_the_baseline_untrusted(self):
        """The review's case: a $0.12 earlier debit landing after the quiet period would mask a workload
        billed at 0.9×; the baseline must match the ledger first."""
        account = FakeAccount(rate=0.9, accounted=0.12)  # settled locally, its debit not yet on the account
        account.noise = lambda t: 0.12 if t >= MONDAY_PEAK + timedelta(minutes=35) else 0.0
        result = self.check(account)
        self.assertEqual((result.verdict, account.sessions), (INCONCLUSIVE, 0))
        landed = FakeAccount()
        landed.noise = lambda t: 0.12  # already landed, but never settled by the ledger
        result = self.check(landed)
        self.assertEqual((result.verdict, landed.sessions), (INCONCLUSIVE, 0))
        self.assertIn("earlier billing", result.reason)

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
        with self.assertRaises(admission.Refused):
            self.check(FakeAccount(), scope_unresolved=lambda: True)

    def test_the_window_closing_mid_workload_is_inconclusive(self):
        result = self.check(FakeAccount(start=at(2, 8, 50), metered=0.05))
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
        deps = admission.SessionDeps(session=lambda d: SimpleNamespace(terminal=terminal, reason="x"),
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
                self.assertEqual(self.run_check(requests, terminal)["cache"].verdict, INCONCLUSIVE)


INSUFFICIENT = {"error": {"message": "Insufficient Balance"}}


class FakeDrain:
    """A provider account drained by concurrent requests: each costs `cost`; a batch is admitted against
    the balance at its start, so it can overdraw. `refusal` is the provider's answer once the balance is
    spent (or from the start, with `always`)."""

    def __init__(self, *, funded=2.0, cost=0.04, overspend=0.0, refusal=(402, INSUFFICIENT), always=False,
                 clears=False, unresolved=False):
        self.balance, self.cost, self.overspend, self.refusal = funded, cost, overspend, refusal
        self.always, self.clears, self.unresolved, self.refusals = always, clears, unresolved, 0
        self.clock, self.requests = MONDAY_PEAK, []

    def send(self, count):
        replies, start = [], self.balance
        for _ in range(count):
            if (self.always or start <= -self.overspend) and not (self.clears and self.refusals >= 1):
                self.refusals += 1
                replies.append(admission.Reply(*self.refusal))
            else:
                self.balance -= self.cost
                self.requests.append(request(f"q{len(self.requests)}", billed=self.cost,
                                             state="unresolved" if self.unresolved else "billed"))
                replies.append(admission.Reply(200, {"id": "x"}))
        return replies

    def sleep(self, seconds):
        self.clock += timedelta(seconds=seconds)

    def deps(self, **change):
        deps = admission.ExhaustionDeps(send=self.send, remaining=lambda: self.balance, requests=lambda a: self.requests,
                                        settle=lambda: None, sleep=self.sleep, now=lambda: self.clock,
                                        may_continue=lambda t: True)
        return replace(deps, **change)


class ExhaustionTest(unittest.TestCase):
    def run_check(self, drain, bound=0.8, max_requests=300, **change):
        return admission.exhaustion(GLM, "x1", funding=2.0, overdraw_bound=bound, deps=drain.deps(**change),
                                    max_requests=max_requests, deadline=MONDAY_PEAK + timedelta(hours=2))

    def test_the_provider_stopping_at_its_balance_within_the_reserve_passes(self):
        result = self.run_check(FakeDrain())
        self.assertEqual(result.verdict, PASS, result.reason)
        self.assertLessEqual(result.evidence["overdraw"], 0.8)
        self.assertIn("Insufficient Balance", json.dumps(result.evidence["refusals"]))

    def test_serving_past_the_limit_beyond_the_reserve_fails(self):
        self.assertEqual(self.run_check(FakeDrain(overspend=2.0)).verdict, FAIL)

    def test_refusals_that_dont_show_an_empty_account_are_inconclusive(self):
        """The review's cases: persistent 429s, and a request-size refusal with credit left, never pass."""
        cases = {
            "a refusal that clears": FakeDrain(clears=True),
            "a persistent rate limit": FakeDrain(refusal=(429, {"error": {"message": "rate limited"}})),
            "a request-size refusal with credit left": FakeDrain(
                refusal=(402, {"error": {"message": "x", "metadata": {"reason": "weight_exceeds_budget"}}})),
            "a 402 without a JSON error": FakeDrain(refusal=(402, None)),
        }
        for name, drain in cases.items():
            with self.subTest(name):
                self.assertEqual(self.run_check(drain, max_requests=120).verdict, INCONCLUSIVE)

    def test_a_request_still_unresolved_at_the_end_is_inconclusive(self):
        drain = FakeDrain()
        stuck = request("stuck", state="unresolved")
        result = self.run_check(drain, requests=lambda a: drain.requests + ([stuck] if drain.refusals else []))
        self.assertEqual(result.verdict, INCONCLUSIVE)
        self.assertIn("unresolved", result.reason)

    def test_credit_left_for_a_request_is_inconclusive(self):
        result = self.run_check(FakeDrain(cost=0.04), remaining=lambda: 0.05)  # above the cheapest billed charge
        self.assertEqual(result.verdict, INCONCLUSIVE)

    def test_the_envelope_refusing_first_the_cap_the_deadline_or_the_window_is_inconclusive(self):
        envelope_first = FakeDrain()
        envelope_first.send = lambda count: [admission.Reply(429, {"error": {"type": "proxy", "message": "refused"}})]
        self.assertIn("envelope refused", self.run_check(envelope_first).reason)
        self.assertIn("without the provider", self.run_check(FakeDrain(funded=100.0), max_requests=40).reason)
        late = FakeDrain(funded=100.0)
        result = self.run_check(late, max_requests=100_000, now=lambda: MONDAY_PEAK + timedelta(hours=3))
        self.assertEqual((result.verdict, late.refusals), (INCONCLUSIVE, 0))  # the deadline, not the cap
        self.assertIn("window closed", self.run_check(FakeDrain(), may_continue=lambda t: False).reason)

    def test_a_balance_that_never_holds_still_is_inconclusive(self):
        readings = iter(range(10_000))
        self.assertEqual(self.run_check(FakeDrain(), remaining=lambda: -0.001 * next(readings)).verdict,
                         INCONCLUSIVE)


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

    def rows(self, bindings, check="metering"):
        return [r for r in admission.status(self.root, bindings) if r["check"] == check]

    def test_attempts_are_kept_and_an_interrupted_one_stays_running(self):
        first = self.write(attempt="20261102T060000-aaaaaa")
        second = self.write(attempt="20261102T070000-bbbbbb", state="running", verdict="running")
        self.assertNotEqual(first, second)
        (row,) = self.rows([self.binding])
        self.assertEqual((row["attempt"], row["status"]), ("20261102T070000-bbbbbb", "running"))

    def test_a_damaged_latest_record_never_falls_back_to_an_earlier_pass(self):
        self.write(attempt="20261102T060000-aaaaaa")
        damaged = self.write(attempt="20261102T070000-bbbbbb")
        damaged.write_text("{not json")
        (row,) = self.rows([self.binding])
        self.assertEqual((row["verdict"], row["stale"]), ("invalid", True))

    def test_a_changed_cli_version_or_price_table_makes_an_attempt_stale(self):
        self.write()
        for change in ({"cli_version": "codex 0.170"}, {"prices_sha": "other"}, {"route_version": "v2"}):
            with self.subTest(change=change):
                (row,) = self.rows([replace(self.binding, **change)])
                self.assertTrue(row["stale"])
        (row,) = self.rows([self.binding])
        self.assertFalse(row["stale"])

    def test_exhaustion_applies_to_every_route_of_the_same_account_after_the_key_rollover(self):
        exhaust = replace(self.binding, route="deepseek--deepseek-flash", key_fingerprint="old-key", cli="any")
        self.write(check="exhaustion", route=exhaust.route, cli="any", binding=exhaust,
                   evidence={"funding": 2.0, "fees": 0.0, "taxes": 0.0})
        b_routes = [replace(self.binding, key_fingerprint="new-key"),
                    replace(self.binding, route="deepseek--deepseek-flash", key_fingerprint="new-key")]
        self.assertEqual([r["verdict"] for r in self.rows(b_routes, "exhaustion")], [PASS, PASS])
        (row,) = self.rows([replace(self.binding, account="another-account")], "exhaustion")
        self.assertIsNone(row["verdict"])

    def test_the_budget_counts_every_exhaustion_run_and_refuses_one_without_purchases(self):
        self.write(check="exhaustion", route="openrouter--z-ai__glm-5.3-flash", cli="any",
                   evidence={"funding": 5.0, "fees": 0.8, "taxes": 0.0})
        self.write(check="exhaustion", route="deepseek--deepseek-flash", cli="any", attempt="20261102T070000-cccccc",
                   state="running", verdict="running", evidence={"funding": 2.0, "fees": 0.0, "taxes": 0.0})
        self.assertAlmostEqual(admission.budget(self.root), 150.0 - 7.8)  # the interrupted run counts too
        self.write(check="exhaustion", route="deepseek--deepseek-flash", cli="any", attempt="20261102T080000-dddddd",
                   state="running", verdict="running", evidence={})
        with self.assertRaises(admission.Refused):
            admission.budget(self.root)

    def test_a_damaged_exhaustion_record_refuses_the_budget(self):
        damaged = self.write(check="exhaustion", route="deepseek--deepseek-flash", cli="any",
                             evidence={"funding": 2.0, "fees": 0.0, "taxes": 0.0})
        damaged.write_text("{")
        with self.assertRaises(admission.Refused):
            admission.budget(self.root)

    def test_purchases_must_be_finite_non_negative_amounts(self):
        for bad in ((0.0, 0.0, 0.0), (-1.0, 0.0, 0.0), (2.0, float("nan"), 0.0), (2.0, 0.0, True)):
            with self.subTest(bad=bad), self.assertRaises(admission.Refused):
                admission.purchases(*bad)


class AdmitCommandTest(unittest.TestCase):
    """The real `admit` command, its wiring and the proxy, against a fake DeepSeek."""

    def setUp(self):
        from eval.tests import test_metered
        self.metered = test_metered
        self.root = Path(tempfile.mkdtemp(prefix="eval-admit-"))
        self.addCleanup(shutil.rmtree, self.root)
        (self.root / "key").write_text(test_metered.KEY + "\n")
        self.clock = [MONDAY_PEAK]

    def admit(self, *argv):
        from eval.__main__ import main
        from eval.harness import admit_commands

        def sleep(seconds):
            self.clock[0] += timedelta(seconds=seconds)
        with mock.patch.object(envelope, "DEFAULT_ROOT", self.root), \
                mock.patch.object(admit_commands, "ROOT", self.root / "adm"), \
                mock.patch.object(admit_commands, "_now", lambda: self.clock[0]), \
                mock.patch.object(admit_commands, "_sleep", sleep), \
                mock.patch.dict(providers.KEY_PATHS, {"deepseek": self.root / "key"}), \
                mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            return main(["admit", *argv])

    def test_it_refuses_bad_flags_and_a_missing_envelope_before_any_spend(self):
        self.assertEqual(self.admit("cache", "--route", "nowhere", "--cli", "codex"), 2)
        self.assertEqual(self.admit("cache", "--route", "fireworks--glm", "--cli", "codex"), 2)
        self.assertEqual(self.admit("cache", "--route", "deepseek--deepseek-flash"), 2)  # no --cli
        self.assertEqual(self.admit("metering", "--route", "openrouter--z-ai__glm-5.3", "--cli", "codex"), 2)
        self.assertEqual(self.admit("cache", "--route", "deepseek--deepseek-flash", "--cli", "codex"), 3)
        self.assertEqual(self.admit("exhaustion", "--route", "deepseek--deepseek-flash"), 2)  # no --funding
        self.assertEqual(self.admit("exhaustion", "--route", "deepseek--deepseek-flash", "--funding", "-1"), 3)
        self.assertFalse((self.root / "adm").exists())  # nothing recorded, nothing spent
        self.assertEqual(self.admit("budget"), 0)

    def exhaustion_envelope(self, fake, slots=4):
        flash = prices.lookup("deepseek", "deepseek-flash")
        bound = prices.request_charge_bound(flash, max_input_tokens=self.metered.CONTEXT,
                                            max_output_tokens=self.metered.MAX_OUT)
        credit = round((2.0 + (slots + 1) * bound) * 1.25, 4)
        policy = {**self.metered.POLICY, "models": ["deepseek-flash"]}
        reader = settlers.DeepSeekSettler(fake.base_url, self.metered.KEY)
        with Envelope.owned("X", root=self.root, create_usd=credit + slots * bound + 1) as env:
            env.fund(credit=credit)
            env.fund_probe(credit)
            env.register_scope("ds", provider="deepseek", fingerprint=envelope.fingerprint(self.metered.KEY),
                               opening_usage=reader.key_usage(), allocation=credit, provider_limit=None,
                               account="ds-main")
            env.set_route("deepseek--deepseek-flash", scope="ds", max_charge=bound, slots=slots, policy=policy,
                          bootstrap_build_usd=1.0,
                          upstream={"base_url": fake.base_url, "settler": "deepseek", "model": "deepseek-flash"})

    def fake(self, **kw):
        fake = self.metered.FakeDeepSeek(funded=2.0)
        fake.output, fake.refuse_at_zero = 16, True
        for name, value in kw.items():
            setattr(fake, name, value)
        self.addCleanup(fake.server.server_close)
        self.addCleanup(fake.server.shutdown)
        self.exhaustion_envelope(fake)
        return fake

    def drain(self, rate):
        fake = self.fake(rate=rate)
        code = self.admit("exhaustion", "--route", "deepseek--deepseek-flash", "--funding", "2", "--fees", "0.1",
                          "--envelope", "X")
        (record,) = (self.root / "adm" / "deepseek--deepseek-flash").glob("exhaustion-any-*.json")
        return code, json.loads(record.read_text()), fake

    def test_the_command_drains_a_fake_account_at_peak_and_records_its_purchases(self):
        code, record, fake = self.drain(rate=1.0)
        self.assertEqual(code, 0)
        result = record["result"]
        self.assertEqual((record["status"], result["verdict"]), ("done", PASS), result["reason"])
        self.assertEqual((result["evidence"]["funding"], result["evidence"]["fees"]), (2.0, 0.1))
        self.assertTrue(record["binding"]["endpoint"].endswith("/anthropic/v1/messages"))
        self.assertLessEqual(result["evidence"]["overdraw"], result["evidence"]["overdraw_bound"])
        self.assertLessEqual(result["evidence"]["statuses"].count(200), result["evidence"]["max_requests"])
        with Envelope.owned("X", root=self.root) as env:
            self.assertEqual(envelope.summary(env.state, 0)["unresolved_requests"], 0)
            self.assertTrue(any(r["refused_by_provider"] for r in env.state["requests"].values()))
        self.assertAlmostEqual(admission.budget(self.root / "adm"), 150.0 - 2.1)

    def test_off_peak_billing_still_lets_the_provider_stop_first(self):
        _, record, _ = self.drain(rate=0.5)  # the envelope counts twice what is billed
        self.assertEqual(record["result"]["verdict"], PASS, record["result"]["reason"])

    def test_the_drain_refuses_outside_a_deepseek_peak_window_before_any_spend(self):
        fake = self.fake()
        self.clock[0] = at(7, 7)  # a Saturday
        self.assertEqual(self.admit("exhaustion", "--route", "deepseek--deepseek-flash", "--funding", "2",
                                    "--envelope", "X"), 3)
        self.assertEqual(fake.charges, [])

    def test_bs_scope_on_a_fresh_key_after_the_drain_starts_clean(self):
        _, _, fake = self.drain(rate=1.0)
        fake.funded += 10.0  # the operator's top-up for B
        reader = settlers.DeepSeekSettler(fake.base_url, "sk-ds-B")
        with Envelope.owned("B", root=self.root, create_usd=150.0) as env:
            env.fund(credit=10.0)
            env.register_scope("ds", provider="deepseek", fingerprint=envelope.fingerprint("sk-ds-B"),
                               opening_usage=reader.key_usage(), allocation=10.0, provider_limit=None,
                               account="ds-main")
            env.reconcile("ds", reader.key_usage())
            scope = env.state["scopes"]["ds"]
            self.assertEqual((scope["excess"], scope["fault"]), (0.0, None))

    def test_status_reads_a_snapshot_while_another_process_owns_the_envelope(self):
        with Envelope.owned("B", root=self.root, create_usd=150.0):
            self.assertEqual(self.admit("status"), 0)


class AdmissionExperimentTest(unittest.TestCase):
    def test_only_probe_runs_on_an_experiment_without_tasks(self):
        from eval.__main__ import main
        with mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            for command in (["run"], ["calibrate"], ["freeze"], ["report"], ["cost"]):
                with self.subTest(command=command):
                    self.assertEqual(main([*command, "--experiment", "admission"]), 2)


if __name__ == "__main__":
    unittest.main()
