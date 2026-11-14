"""Published observation times are independent conservative scope minima."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from atc_service import LiveService
from publication_assertions import assert_publication_equal, assert_replay_current, capture_publication
from test_service import PublishedReads


class ObservationReads(PublishedReads):
    def __init__(self, c_observed, *, reread=False):
        super().__init__()
        self.rows["work-items", None] = []
        self.rows["sessions", None] = [dict(sessionKey="S", state="active",
                                              displayName="S", topologyParent=None)]
        self.rows["assignments", None] = [dict(id=aid, workItemId=None,
            state="open" if aid == "A" else "closed", holderKey="S", effectKind="code")
            for aid in ("A", "B", "C")]
        for aid, timestamp in (("A", 900000), ("B", 1000000), ("C", 750000)):
            self.rows["attests", aid] = [dict(id="attest-" + aid, assignmentId=aid,
                kind="progress", bySession="S", ts=timestamp)]
        self.observation_times = {("attests", "A"): 5000 if reread else 1000,
            ("attests", "B"): 4000, ("attests", "C"): c_observed,
            ("turns", "S"): 4000}
        self.observations = {}

    def read(self, resource, scope=None):
        result = super().read(resource, scope)
        result.at = self.observation_times.get((resource, scope), 4000)
        self.observations[resource, scope] = result.at
        return result

    def supporting_observations(self):
        # Complete diagnostic inputs, not the production fold being checked.
        # Every assignment here is null-item and every attest is by S; the
        # active session's empty turn inventory also supports idle evidence.
        sources = [("turns", "S")] + [("attests", row["id"])
            for row in self.rows["assignments", None]
            if any(a["bySession"] == "S" for a in self.rows["attests", row["id"]])]
        return {resource + ":" + scope: self.observations[resource, scope]
                for resource, scope in sources}


class ActivityObservationBoundTest(unittest.TestCase):
    maxDiff = None

    def drain(self, service):
        while service.reads.pending or service.reads.active:
            service.reads.pump()
            service._handle(*service.events.get(timeout=5))
        service._publish()

    def load(self, gateway):
        service = LiveService(gateway, workers=1)
        service.connected = True
        self.addCleanup(service.stop)
        service.rebuild()
        self.drain(service)
        return service

    def test_warm_and_fresh_observation_is_no_later_than_own_scope_minimum(self):
        with patch("atc_service.now_ms", return_value=6000):
            for c_observed in (2000, 2500):
                with self.subTest(c_observed=c_observed):
                    warm = self.load(ObservationReads(c_observed))
                    initial = deepcopy(warm.current)
                    warm.gateway.observation_times["attests", "A"] = 5000
                    warm._refresh("attests", "A")
                    self.drain(warm)
                    fresh = self.load(ObservationReads(c_observed, reread=True))
                    checks = {}
                    for name, service in (("warm", warm), ("fresh", fresh)):
                        scopes = service.gateway.supporting_observations()
                        exact_minimum = min(scopes.values())
                        agent = next(a for a in service.current["agents"] if a["sessionKey"] == "S")
                        facts = agent["evidence"]["idleFor"]
                        checks[name] = dict(scopeObservations=scopes,
                            exactMinimum=exact_minimum, publishedObservedAt=facts["observedAt"],
                            passes=facts["observedAt"] is not None and facts["observedAt"] <= exact_minimum)
                    label = "observed-at-lower-bound-" + str(c_observed)
                    capture_publication(fresh.current, warm.current, label,
                        initialWarm=initial, lowerBoundChecks=checks)
                    for name, service in (("warm", warm), ("fresh", fresh)):
                        self.assertEqual(c_observed, checks[name]["exactMinimum"])
                        self.assertTrue(checks[name]["passes"], checks[name])
                        agent = next(a for a in service.current["agents"] if a["sessionKey"] == "S")
                        self.assertEqual(1000000, agent["idleSince"])
                        self.assertEqual(["attest", "cli:attests B"], agent["evidence"]["idleFor"]["source"])
                        self.assertEqual("known", agent["evidence"]["idleFor"]["status"])
                        self.assertEqual("live", agent["evidence"]["idleFor"]["freshness"])
                        self.assertTrue(agent["evidence"]["idleFor"]["complete"])
                        self.assertIsNone(agent["evidence"]["idleFor"]["unconfirmedSince"])
                    assert_publication_equal(self, fresh.current, warm.current, label + "-parity")

    def test_scoped_retries_clear_only_their_own_activity_warning(self):
        with patch("atc_service.now_ms", return_value=6000):
            gateway = ObservationReads(2000)
            gateway.rows["turns", "S"] = [dict(seq=1, sessionKey="S", createdAt=700000,
                                                startedAt=750000, endedAt=800000)]
            gateway.rows["sessions", None].append(dict(sessionKey="unaffected", state="active"))
            service = self.load(gateway)
            baseline = deepcopy(service.current)
            prior_other = next(a for a in baseline["agents"] if a["sessionKey"] == "unaffected")
            gateway.partial.update({("attests", "A"), ("turns", "S")})
            with patch.object(service, "rebuild", side_effect=AssertionError("unexpected recovery scan")):
                service._refresh("attests", "A")
                service._refresh("turns", "S")
                self.drain(service)
                failed = deepcopy(service.current)
                subject = next(a for a in failed["agents"] if a["sessionKey"] == "S")
                self.assertEqual(1000000, subject["idleSince"])
                self.assertEqual("stale", subject["evidence"]["idleFor"]["freshness"])
                self.assertFalse(subject["evidence"]["idleFor"]["complete"])

                gateway.partial.remove(("attests", "A"))
                gateway.observation_times["attests", "A"] = 5000
                service.read_retries.pop(("attests", "A", None))
                service._refresh("attests", "A")
                self.drain(service)
                one_recovered = deepcopy(service.current)
                subject = next(a for a in one_recovered["agents"] if a["sessionKey"] == "S")
                self.assertEqual("stale", subject["evidence"]["idleFor"]["freshness"])
                self.assertFalse(subject["evidence"]["idleFor"]["complete"])

                gateway.partial.remove(("turns", "S"))
                service.read_retries.pop(("turns", "S", None))
                service._refresh("turns", "S")
                self.drain(service)
            subject = next(a for a in service.current["agents"] if a["sessionKey"] == "S")
            self.assertEqual(1000000, subject["idleSince"])
            self.assertEqual("known", subject["evidence"]["idleFor"]["status"])
            self.assertEqual("live", subject["evidence"]["idleFor"]["freshness"])
            self.assertTrue(subject["evidence"]["idleFor"]["complete"])
            self.assertIsNone(subject["evidence"]["idleFor"]["unconfirmedSince"])
            for frame in (failed, one_recovered, service.current):
                self.assertEqual(prior_other,
                    next(a for a in frame["agents"] if a["sessionKey"] == "unaffected"))
            fresh_gateway = ObservationReads(2000, reread=True)
            fresh_gateway.rows = deepcopy(gateway.rows)
            fresh = self.load(fresh_gateway)
            capture_publication(fresh.current, service.current, "scoped-activity-retry",
                initialWarm=baseline, failed=failed, oneRecovered=one_recovered)
            assert_publication_equal(self, fresh.current, service.current, "scoped-activity-retry-parity")
            assert_replay_current(self, service, baseline, "scoped-activity-retry-replay")


if __name__ == "__main__":
    unittest.main()
