"""Published observation times are independent conservative scope minima."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from atc_service import LiveService
from publication_assertions import assert_publication_equal, capture_publication
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


if __name__ == "__main__":
    unittest.main()
