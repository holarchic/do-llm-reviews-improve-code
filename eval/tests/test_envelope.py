import json
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from eval.harness import envelope
from eval.harness.envelope import Envelope, EnvelopeError, Refused

REPO = Path(__file__).resolve().parents[2]


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


class Fixture(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="eval-envelope-"))
        self.addCleanup(shutil.rmtree, self.root)
        self.clock = Clock()

    def owned(self, name="B", usd=150.0):
        return Envelope.owned(name, root=self.root, create_usd=usd, now=self.clock)

    def ready(self, env, *, credit=100.0, fees=2.0, taxes=3.0, allocation=100.0, max_charge=1.0, slots=4,
              validated=True, fingerprint="fp-1", scope="or"):
        """A funded envelope with one reconciled scope and one route."""
        env.fund(credit=credit, fees=fees, taxes=taxes)
        env.register_scope(scope, provider="openrouter", fingerprint=fingerprint, opening_usage=7.0,
                           allocation=allocation, provider_limit=allocation)
        env.set_route("r", scope=scope, max_charge=max_charge, slots=slots, policy={}, bootstrap_build_usd=2.0)
        if validated:
            env.validate_route("r")
        env.reconcile(scope, 7.0)


class BoundTest(Fixture):
    def test_funding_or_a_route_that_would_break_the_bound_is_refused(self):
        with self.owned(usd=150.0) as env:
            env.fund(credit=140.0, fees=4.0, taxes=4.0)  # 148
            with self.assertRaises(Refused):
                env.fund(credit=3.0)  # 151
            env.register_scope("or", provider="openrouter", fingerprint="fp", opening_usage=0.0, allocation=140.0,
                               provider_limit=140.0)
            env.set_route("r", scope="or", max_charge=0.5, slots=4, policy={}, bootstrap_build_usd=1.0)  # +2 → 150
            with self.assertRaises(Refused):
                env.set_route("r2", scope="or", max_charge=0.5, slots=1, policy={}, bootstrap_build_usd=1.0)
            self.assertEqual(set(env.state["routes"]), {"r"})
            self.assertEqual(len(env.state["funding"]), 1)

    def test_allocations_never_exceed_funded_credit(self):
        with self.owned() as env:
            env.fund(credit=50.0)
            with self.assertRaises(Refused):
                env.register_scope("or", provider="openrouter", fingerprint="fp", opening_usage=0.0,
                                   allocation=60.0, provider_limit=None)


class GateTest(Fixture):
    def test_unequal_bounds_count_each_request_at_its_own(self):
        """$10 funded, $1 settled, one $9 request unresolved: a $1 candidate is refused."""
        with self.owned() as env:
            env.fund(credit=10.0)
            env.register_scope("or", provider="openrouter", fingerprint="fp", opening_usage=0.0, allocation=10.0,
                               provider_limit=10.0)
            env.set_route("big", scope="or", max_charge=9.0, slots=4, policy={}, bootstrap_build_usd=1.0)
            env.set_route("small", scope="or", max_charge=1.0, slots=4, policy={}, bootstrap_build_usd=1.0)
            for route in ("big", "small"):
                env.validate_route(route)
            env.reconcile("or", 0.0)
            env.reserve("job", 1.0)
            env.admit("q1", "small", job="job", attempt="a1")
            env.settle("q1", 1.0)
            env.admit("q2", "big", job="job", attempt="a1")
            with self.assertRaises(Refused):
                env.admit("q3", "small", job="job", attempt="a2")

    def test_a_lowered_bound_never_shrinks_an_old_obligation(self):
        with self.owned() as env:
            self.ready(env, credit=10.0, fees=0.0, taxes=0.0, allocation=10.0, max_charge=6.0, slots=2)
            env.reserve("job", 1.0)
            env.admit("q1", "r", job="job", attempt="a1")
            env.set_route("r", scope="or", max_charge=1.0, slots=2, policy={}, bootstrap_build_usd=1.0)
            env.admit("q2", "r", job="job", attempt="a1")  # 6 + 1 = 7
            with self.assertRaises(Refused):
                env.admit("q3", "r", job="job", attempt="a1")  # the slots are full
            env.set_route("r", scope="or", max_charge=4.0, slots=4, policy={}, bootstrap_build_usd=1.0)
            with self.assertRaises(Refused):  # 6 + 1 + 4 = 11 > 10
                env.admit("q4", "r", job="job", attempt="a2")

    def test_slots_cap_concurrent_admission_on_many_threads(self):
        with self.owned() as env:
            self.ready(env, max_charge=1.0, slots=3)
            env.reserve("job", 1.0)
            admitted, refused = [], []

            def one(i):
                try:
                    env.admit(f"q{i}", "r", job="job", attempt=f"a{i}")
                    admitted.append(i)
                except Refused:
                    refused.append(i)
            threads = [threading.Thread(target=one, args=(i,)) for i in range(20)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual((len(admitted), len(refused)), (3, 17))
            on_disk = json.loads(env.path.read_text())
            self.assertEqual(sum(r["state"] == "unresolved" for r in on_disk["requests"].values()), 3)

    def test_two_scopes_are_each_bounded_by_their_allocation(self):
        with self.owned() as env:
            env.fund(credit=10.0)
            for scope, fp in (("a", "fp-a"), ("b", "fp-b")):
                env.register_scope(scope, provider="p", fingerprint=fp, opening_usage=0.0, allocation=5.0,
                                   provider_limit=5.0)
                env.set_route(scope, scope=scope, max_charge=3.0, slots=4, policy={}, bootstrap_build_usd=1.0)
                env.validate_route(scope)
                env.reconcile(scope, 0.0)
            env.reserve("job", 1.0)
            env.admit("a1", "a", job="job", attempt="x")
            with self.assertRaises(Refused):
                env.admit("a2", "a", job="job", attempt="y")  # scope a: 3 + 3 > 5
            env.admit("b1", "b", job="job", attempt="z")  # scope b has its own room

    def test_a_refusal_sticks_to_its_attempt(self):
        with self.owned() as env:
            self.ready(env, max_charge=1.0, slots=1)
            env.reserve("job", 1.0)
            env.admit("q1", "r", job="job", attempt="a1")
            with self.assertRaises(Refused):
                env.admit("q2", "r", job="job", attempt="a2")
            env.settle("q1", 0.5)  # a slot frees, but a2 stays refused
            with self.assertRaises(Refused):
                env.admit("q3", "r", job="job", attempt="a2")
            self.assertIn("slots", env.refusal("a2"))
            env.admit("q4", "r", job="job", attempt="a3")

    def test_a_request_id_is_admitted_once(self):
        with self.owned() as env:
            self.ready(env)
            env.reserve("job", 1.0)
            env.admit("q1", "r", job="job", attempt="a1")
            with self.assertRaises(Refused):
                env.admit("q1", "r", job="job", attempt="a1")


class SettlementTest(Fixture):
    def test_settle_and_resolve_are_idempotent(self):
        with self.owned() as env:
            self.ready(env)
            env.reserve("job", 1.0)
            env.admit("q1", "r", job="job", attempt="a1")
            self.assertTrue(env.settle("q1", 0.4))
            self.assertFalse(env.settle("q1", 0.4))
            self.assertFalse(env.settle("q1", None))
            self.assertEqual(envelope.summary(env.state, self.clock())["settled"], 0.4)

    def test_a_failed_write_leaves_the_last_persisted_state(self):
        with self.owned() as env:
            self.ready(env)
            env.reserve("job", 1.0)
            env.admit("q1", "r", job="job", attempt="a1")
            with mock.patch.object(envelope.os, "replace", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    env.settle("q1", 0.4)
            self.assertEqual(env.state["requests"]["q1"]["state"], "unresolved")
            self.assertEqual(envelope.load(env.path)["requests"]["q1"]["state"], "unresolved")
            self.assertTrue(env.settle("q1", 0.4))  # settled again after the crash: once

    def test_a_restart_keeps_unresolved_obligations_after_a_route_is_changed(self):
        with self.owned() as env:
            self.ready(env, max_charge=5.0)
            env.reserve("job", 1.0)
            env.admit("q1", "r", job="job", attempt="a1")
        with self.owned() as env:  # a new owner process
            env.set_route("r", scope="or", max_charge=0.1, slots=4, policy={}, bootstrap_build_usd=1.0)
            self.assertEqual(env.state["requests"]["q1"]["bound"], 5.0)
            self.assertEqual(envelope.summary(env.state, self.clock())["unresolved"], 5.0)
            self.assertFalse(env.release("job"))  # still unresolved: the reservation holds


class OwnershipTest(Fixture):
    def test_a_second_owner_is_refused_and_status_reads_without_the_lock(self):
        with self.owned() as env:
            self.ready(env)
            code = ("import sys; from pathlib import Path; from eval.harness.envelope import Envelope, EnvelopeError\n"
                    "try:\n    Envelope.owned('B', root=Path(sys.argv[1])).__enter__()\nexcept EnvelopeError:\n"
                    "    sys.exit(3)\nsys.exit(0)")
            other = subprocess.run([sys.executable, "-c", code, str(self.root)], cwd=REPO)
            self.assertEqual(other.returncode, 3)
            self.assertEqual(envelope.load(env.path)["name"], "B")

    def test_unreadable_or_inconsistent_state_refuses_everything(self):
        with self.owned():
            pass
        path = self.root / "B.json"
        for text in ("{not json", json.dumps({"name": "B"}), json.dumps({**json.loads(path.read_text()),
                                                                        "envelope_usd": -1})):
            with self.subTest(text=text[:30]):
                path.write_text(text)
                with self.assertRaises(EnvelopeError):
                    with self.owned():
                        pass

    def test_a_key_belongs_to_one_envelope_even_when_two_processes_race(self):
        code = ("import sys, time; from pathlib import Path; from eval.harness.envelope import Envelope, Refused\n"
                "with Envelope.owned(sys.argv[2], root=Path(sys.argv[1]), create_usd=150.0) as env:\n"
                "    env.fund(credit=10.0)\n"
                "    while time.time() < float(sys.argv[3]): pass\n"
                "    try:\n        env.register_scope('s', provider='p', fingerprint='shared', opening_usage=0.0,"
                " allocation=5.0, provider_limit=None)\n    except Refused:\n        sys.exit(4)\nsys.exit(0)")
        import time
        start = str(time.time() + 1.0)
        procs = [subprocess.Popen([sys.executable, "-c", code, str(self.root), name, start], cwd=REPO)
                 for name in ("B", "C")]
        codes = sorted(p.wait() for p in procs)
        self.assertEqual(codes, [0, 4])
        with self.owned("D") as env:  # and still after both have exited
            env.fund(credit=10.0)
            with self.assertRaises(Refused):
                env.register_scope("s", provider="p", fingerprint="shared", opening_usage=0.0, allocation=5.0,
                                   provider_limit=None)


class ReconciliationTest(Fixture):
    def test_lag_is_allowed_and_excess_refuses_everything(self):
        with self.owned() as env:
            self.ready(env, max_charge=1.0)
            env.reserve("job", 1.0)
            env.admit("q1", "r", job="job", attempt="a1")
            env.settle("q1", 0.8)
            self.assertEqual(env.reconcile("or", 7.0), 0.0)  # settled before the key shows it: lag
            env.admit("q2", "r", job="job", attempt="a1")  # admission during the lag
            self.assertEqual(env.reconcile("or", 7.8), 0.0)  # caught up
            self.assertAlmostEqual(env.reconcile("or", 7.8 + 1.0 + 0.5), 0.5)  # beyond settled + unresolved
            with self.assertRaises(Refused):
                env.admit("q3", "r", job="job", attempt="a2")
            with self.assertRaises(Refused):
                env.reserve("job-2", 1.0)

    def test_an_expired_or_missing_snapshot_refuses(self):
        with self.owned() as env:
            self.ready(env)
            env.reserve("job", 1.0)
            self.clock.t += envelope.SNAPSHOT_SECONDS + 1
            with self.assertRaises(Refused):
                env.admit("q1", "r", job="job", attempt="a1")
            with self.assertRaises(Refused):
                env.reserve("job-2", 1.0)
            env.reconcile("or", 7.0)
            with self.assertRaises(Refused):  # a1's refusal sticks: its step ends INFRA
                env.admit("q1", "r", job="job", attempt="a1")
            env.admit("q2", "r", job="job", attempt="a2")

    def test_two_routes_on_one_key_reconcile_once(self):
        with self.owned() as env:
            self.ready(env)
            env.set_route("r2", scope="or", max_charge=1.0, slots=2, policy={}, bootstrap_build_usd=1.0)
            env.validate_route("r2")
            env.reserve("job", 1.0)
            for request, route in (("q1", "r"), ("q2", "r2")):
                env.admit(request, route, job="job", attempt="a")
                env.settle(request, 0.5)
            self.assertEqual(env.reconcile("or", 7.0 + 1.0), 0.0)  # one key's usage covers both routes

    def test_an_account_balance_above_the_allocation_still_bounds_admission(self):
        with self.owned() as env:
            self.ready(env, credit=100.0, allocation=2.0, max_charge=1.0, slots=8)
            env.reserve("job", 1.0)
            env.admit("q1", "r", job="job", attempt="a")
            env.admit("q2", "r", job="job", attempt="a")
            with self.assertRaises(Refused):
                env.admit("q3", "r", job="job", attempt="b")


class StageAndReservationTest(Fixture):
    def test_a_probe_draws_on_the_probe_reservation_and_stops_when_it_is_spent(self):
        with self.owned() as env:
            self.ready(env, max_charge=1.0, validated=False)
            with self.assertRaises(Refused):
                env.admit("q0", "r", job=envelope.PROBE, attempt="p0")  # no probe reservation yet
            env.fund_probe(2.0)
            env.admit("q1", "r", job=envelope.PROBE, attempt="p1")
            env.settle("q1", 1.0)
            env.admit("q2", "r", job=envelope.PROBE, attempt="p2")
            with self.assertRaises(Refused):
                env.admit("q3", "r", job=envelope.PROBE, attempt="p3")  # 1 + 1 + 1 > 2

    def test_an_unvalidated_route_admits_no_job(self):
        with self.owned() as env:
            self.ready(env, validated=False)
            env.reserve("job", 1.0)
            with self.assertRaises(Refused):
                env.admit("q1", "r", job="job", attempt="a")

    def test_reservations_are_full_holds_released_only_after_settlement(self):
        with self.owned() as env:
            self.ready(env, credit=10.0, fees=0.0, taxes=0.0, allocation=10.0, max_charge=1.0, slots=4)
            env.reserve("j1", 6.0)
            with self.assertRaises(Refused):
                env.reserve("j2", 5.0)  # 6 + 5 > 10
            env.reserve("j1", 9.0)  # a resumed job keeps its reservation
            env.admit("q1", "r", job="j1", attempt="a")
            self.assertFalse(env.release("j1"))  # between turns, a request still unresolved
            env.settle("q1", 1.0)
            self.assertTrue(env.release("j1"))
            env.reserve("j2", 5.0)  # j1's $1 settled + j2's $5 hold fit

    def test_an_overrun_counts_in_full(self):
        with self.owned() as env:
            self.ready(env, credit=10.0, fees=0.0, taxes=0.0, allocation=10.0, max_charge=3.0, slots=4)
            env.reserve("j1", 1.0)
            for i in range(3):
                env.admit(f"q{i}", "r", job="j1", attempt="a")
                env.settle(f"q{i}", 2.5)  # $7.50 against a $1 reservation
            with self.assertRaises(Refused):
                env.reserve("j2", 3.0)  # 7.5 + 3 > 10

    def test_estimates_use_the_dearest_build_or_the_bootstrap(self):
        self.assertEqual(envelope.job_estimate([], 2.0), 3.0)
        self.assertEqual(envelope.job_estimate([1.0, 4.0], 2.0), 6.0)
        self.assertEqual(envelope.job_estimate([1.0], 2.0, unknown=True), 3.0)


if __name__ == "__main__":
    unittest.main()


class SpendCommandTest(Fixture):
    def spend(self, *argv):
        from eval.__main__ import main
        with mock.patch.object(envelope, "DEFAULT_ROOT", self.root), mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            return main(["spend", *argv])

    def test_create_fund_resolve_and_status_from_the_command_line(self):
        self.assertEqual(self.spend("create", "--envelope", "B", "--usd", "150"), 0)
        self.assertEqual(self.spend("fund", "--envelope", "B", "--credit", "100", "--fees", "2", "--taxes", "3"), 0)
        self.assertEqual(self.spend("fund", "--envelope", "B", "--credit", "60"), 3)  # 165 > 150
        self.assertEqual(self.spend("status", "--envelope", "B"), 0)
        with self.owned() as env:
            env.register_scope("or", provider="openrouter", fingerprint="fp", opening_usage=0.0, allocation=10.0,
                               provider_limit=10.0)
            env.set_route("r", scope="or", max_charge=1.0, slots=2, policy={}, bootstrap_build_usd=1.0)
            env.validate_route("r")
            env.reconcile("or", 0.0)
            env.reserve("job", 1.0)
            env.admit("q1", "r", job="job", attempt="a1")
            # A mutating command is refused while a run owns the envelope; status still reads.
            self.assertEqual(self.spend("resolve", "--envelope", "B", "--request", "q1", "--billed", "0.3"), 3)
            self.assertEqual(self.spend("status", "--envelope", "B"), 0)
        self.assertEqual(self.spend("resolve", "--envelope", "B", "--request", "q1"), 2)  # neither flag
        self.assertEqual(self.spend("resolve", "--envelope", "B", "--request", "q1", "--billed", "0.3"), 0)
        self.assertEqual(envelope.load(self.root / "B.json")["requests"]["q1"]["billed"], 0.3)
