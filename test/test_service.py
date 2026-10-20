"""Deterministic service recovery; real gateway proof is recorded separately."""

from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from atc_service import LiveService, WORK_ITEM_FIELDS
from atc_upstream import Read


def work(title="First", version=1):
    return dict.fromkeys(WORK_ITEM_FIELDS) | {
        "id": "work", "title": title, "state": "open", "createdAt": 1,
        "rowVersion": version,
    }


class PublishedReads:
    def __init__(self):
        self.read_count = 0
        self.calls = []
        self.partial = set()
        self.rows = {
            ("work-items", None): [work()],
            ("assignments", None): [{
                "id": "assignment", "holderKey": "agent s_author",
                "workItemId": "work", "state": "open", "effectKind": "code",
            }],
            ("sessions", None): [{
                "sessionKey": "agent s_author", "state": "active",
                "displayName": "Author", "topologyParent": None,
            }],
            ("attests", "assignment"): [{
                "id": "progress", "assignmentId": "assignment", "kind": "progress",
                "ts": 1, "bySession": "agent s_author", "commitRefs": [],
            }],
        }

    def read(self, resource, scope=None):
        self.read_count += 1
        self.calls.append(("read", resource, scope))
        return Read(deepcopy(self.rows.get((resource, scope), [])),
                    (resource, scope) not in self.partial, 10, "published-read")

    def detail(self, resource, key, scope=None):
        self.read_count += 1
        self.calls.append(("detail", resource, scope, key))
        rows = [r for r in self.rows.get((resource, scope), []) if r.get("id") == key]
        return Read(deepcopy(rows), bool(rows), 11, "published-detail")


class ServiceRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.gateway = PublishedReads()
        self.service = LiveService(self.gateway)
        self.service.connected = True
        self.service.rebuild()

    def item(self):
        return self.service.model.snapshot(20)["items"][0]

    def test_bootstrap_covers_assignments_attests_and_visible_session_histories(self):
        self.assertEqual("live", self.service.state)
        self.assertEqual(1, self.item()["stage"])
        self.assertEqual({"open": 1, "terminal": 0}, self.item()["assignments"])
        for resource in ("turns", "wakes", "messages"):
            self.assertIn(("read", resource, "agent s_author"), self.gateway.calls)
        self.assertTrue(self.service.metrics["bootstrapComplete"])

    def test_failed_rebuild_retains_rows_and_recovery_can_establish_absence(self):
        self.gateway.rows[("work-items", None)] = []
        self.gateway.partial.add(("work-items", None))
        self.service.rebuild()
        self.assertEqual("work", self.item()["id"])
        self.assertEqual("stale", self.item()["evidence"]["identity"]["freshness"])
        self.assertEqual("stale", self.service.state)
        self.gateway.partial.clear()
        self.service.rebuild()
        self.assertEqual([], self.service.model.snapshot(20)["items"])
        self.assertEqual("live", self.service.state)

    def test_unannounced_correction_is_recovered_by_interval_read_scope(self):
        self.gateway.rows[("assignments", None)][0]["commitRefCorrections"] = [{
            "actorRef": "author", "commitRefs": [{"commit": "a" * 40, "repo": "repo"}],
        }]
        self.service.rebuild()
        self.assertEqual("author", self.item()["commitRefCorrections"][0]["actorRef"])
        self.assertEqual("agent s_author", self.item()["evidenceRecords"][0]["bySession"])

    def test_verified_payload_applies_without_reads_and_does_not_roll_back(self):
        # Inject a verified policy to exercise the algorithm. This is not
        # evidence that a real build qualifies for production PAYLOAD_BUILDS.
        self.service.policies = {"work-items": "strict-version"}
        before = self.gateway.read_count
        notice = {"resource": "work-items", "class": "work_item.updated",
                  "op": "upsert", "refs": {"workItemId": "work"},
                  "payload": work("New", 3)}
        self.service.change(notice)
        self.service.change(dict(notice, payload=work("Older", 2)))
        self.assertEqual("New", self.item()["title"])
        self.assertEqual(before, self.gateway.read_count)

    def test_partial_or_unverified_payload_uses_only_targeted_read(self):
        self.service.policies = {"work-items": "strict-version"}
        self.gateway.rows[("work-items", None)] = [work("Current", 3)]
        self.gateway.calls.clear()
        self.service.change({
            "resource": "work-items", "class": "work_item.updated", "op": "upsert",
            "refs": {"workItemId": "work"}, "payload": {"id": "work", "rowVersion": 4},
        })
        self.assertEqual("Current", self.item()["title"])
        self.assertEqual([("detail", "work-items", None, "work")], self.gateway.calls)

    def test_publication_does_not_change_an_already_observed_revision(self):
        self.service._publish()
        old = self.service.snapshot()
        self.service.model.inventory("work-items").rows["work"]["title"] = "Changed"
        self.assertEqual("First", old["items"][0]["title"])
        self.assertEqual("First", self.service.snapshot()["items"][0]["title"])
        self.service._publish()
        self.assertGreater(self.service.snapshot()["revision"], old["revision"])
        self.assertEqual("Changed", self.service.snapshot()["items"][0]["title"])

    def test_new_assignment_fills_histories_of_previously_inactive_holder(self):
        self.gateway.rows[("sessions", None)].append({
            "sessionKey": "agent s_retired", "state": "retired",
            "displayName": "Retired", "topologyParent": None,
        })
        self.service.rebuild()
        self.gateway.rows[("assignments", None)].append({
            "id": "new", "holderKey": "agent s_retired", "workItemId": "work",
            "state": "open", "effectKind": "code",
        })
        self.gateway.calls.clear()
        self.service.change({
            "resource": "assignments", "op": "upsert", "class": "assignment.opened",
            "refs": {"assignmentId": "new"}, "payload": {},
        })
        self.assertEqual([
            ("detail", "assignments", None, "new"), ("read", "attests", "new"),
            ("read", "turns", "agent s_retired"), ("read", "wakes", "agent s_retired"),
            ("read", "messages", "agent s_retired"),
        ], self.gateway.calls)
