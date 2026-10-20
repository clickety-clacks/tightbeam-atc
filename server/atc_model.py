"""Derive the view from scoped evidence inventories, without viewer state."""

from collections import Counter, defaultdict
from copy import deepcopy
from atc_summary import summarize, recent_marks

from atc_evidence import (
    Inventory, agent_id, code_state, combined_evidence, evidence_stage, merge_ring,
    turn_state,
)


KEYS = {"sessions": "sessionKey", "roles": "name", "turns": "seq",
        "wakes": "wakeId", "artifacts": "artifactId"}


class EvidenceModel:
    def __init__(self):
        self.inventories = {}
        self.merges = {}
        self.next_expiry = None
        self.details_revision = 0
        self.recent_expiries = {}
        self.summaries = {}

    def summary(self, resource, scope):
        inventory = self.inventory(resource, scope)
        cached = self.summaries.get((resource, scope))
        if cached is None or cached[0] != inventory.data_revision:
            cached = inventory.data_revision, summarize(resource, inventory.rows.values())
            self.summaries[resource, scope] = cached
        return cached[1]

    def inventory(self, resource, scope=None):
        return self.inventories.setdefault((resource, scope), Inventory(KEYS.get(resource, "id")))

    def accept(self, resource, scope, result):
        inventory = self.inventory(resource, scope)
        if resource in {"attests", "assignments", "work-items"}:
            incoming = {str(row[inventory.key]): row for row in result.rows}
            if (any(inventory.rows.get(k) != row for k, row in incoming.items())
                    or (result.complete and inventory.rows.keys() != incoming.keys())):
                self.details_revision += 1
        inventory.read(
            result.rows, complete=result.complete, at=result.at, source=result.source)

    def detail(self, resource, scope, result):
        inventory = self.inventory(resource, scope)
        if result.complete:
            for row in result.rows:
                key = str(row[inventory.key])
                if resource in {"attests", "assignments", "work-items"} and inventory.rows.get(key) != row:
                    self.details_revision += 1
                inventory.rows[key] = deepcopy(row)
                inventory.data_revision += 1
                inventory.observations[key] = {
                    "status": "known", "freshness": "live", "observedAt": result.at,
                    "source": result.source, "complete": True,
                }
        else:
            inventory.invalidate()

    def invalidate(self):
        for inventory in self.inventories.values():
            inventory.invalidate()

    def active_sessions(self):
        holders = {a.get("holderKey") for a in self.inventory("assignments").rows.values()
                   if a.get("state") == "open"}
        return [s for s in self.inventory("sessions").rows.values()
                if s.get("state") == "active" or s["sessionKey"] in holders]

    def commit_refs(self, assignment, attests):
        corrections = assignment.get("commitRefCorrections") or []
        if corrections:
            # Corrections replace a historical assignment's refs, preserving
            # the attest and the correction's independent authorship.
            return corrections[-1].get("commitRefs") or []
        return [ref for attest in attests for ref in (attest.get("commitRefs") or [])]

    def snapshot(self, now):
        self.next_expiry = None
        self.recent_expiries = {key: at for key, at in self.recent_expiries.items() if at > now}
        work = self.inventory("work-items")
        assignments = self.inventory("assignments")
        sessions = self.inventory("sessions")
        roles = self.inventory("roles")
        by_item = defaultdict(list)
        for assignment in assignments.rows.values():
            by_item[assignment.get("workItemId")].append(assignment)

        visible = self.active_sessions()
        ids = {s["sessionKey"]: agent_id(s["sessionKey"]) for s in visible}
        roles_by_session = defaultdict(list)
        for role in roles.rows.values():
            roles_by_session[role.get("boundSessionKey")].append(role["name"])

        turns, wakes, messages = [], {}, []
        turn_evidence = []
        activity = {}
        for session in visible:
            key = session["sessionKey"]
            turn_evidence.append(self.inventory("turns", key).evidence())
            summary = self.summary("turns", key)
            turns.extend(summary["live"])
            if summary["activity"] is not None:
                activity[key] = summary["activity"]
            # Only live-turn wake references need historical wake lookup.
            wake_rows = self.inventory("wakes", key).rows
            for turn in summary["live"]:
                wake = wake_rows.get(str(turn.get("wakeId")))
                if wake:
                    wakes[str(wake["wakeId"])] = wake
            for wake in recent_marks(self.summary("wakes", key), "firedAt", now):
                wakes[str(wake["wakeId"])] = wake
            messages.extend(recent_marks(self.summary("messages", key), "at", now))
        all_turn_facts = combined_evidence(*turn_evidence) if visible else sessions.evidence()

        authored_at = defaultdict(dict)
        activity_evidence = defaultdict(list)
        for assignment in assignments.rows.values():
            wid = assignment.get("workItemId")
            summary = self.summary("attests", assignment["id"])
            for key, at in summary["authors"].items():
                activity[key] = max(activity.get(key, 0), at)
                authored_at[key][wid] = max(authored_at[key].get(wid, 0), at)
            facts = self.inventory("attests", assignment["id"]).evidence()
            for author in summary["authorIds"]:
                activity_evidence[author].append(facts)

        agents = []
        for session in visible:
            key = session["sessionKey"]
            facts = sessions.observations.get(key, sessions.evidence())
            turn_inventory = self.inventory("turns", key)
            actual_parent = session.get("topologyParent")
            # Missing parents stay unresolved, including a parent outside the
            # visible population. They are never attached to an arbitrary root.
            parent = ids.get(actual_parent)
            kind = "main" if "main" in roles_by_session[key] else {
                "product-owner": "po", "orchestrator": "orch", "coder": "coder",
                "reviewer-code": "rev", "reviewer-spec": "rev",
                "spec-writer": "spec", "recon": "recon",
            }.get(session.get("archetype"), "agent")
            parent_facts = dict(facts)
            if actual_parent and parent is None:
                parent_facts["status"] = "unknown"
            agents.append({
                "id": ids[key], "sessionKey": key,
                "name": session.get("displayName") or key, "kind": kind,
                "archetype": session.get("archetype"), "harness": session.get("harness"),
                "model": session.get("model"), "parent": parent,
                "parentSessionKey": actual_parent, "spawnedBy": session.get("spawnedBy"),
                "retired": session.get("state") != "active",
                "turn": turn_state(self.summary("turns", key)["live"], complete=turn_inventory.complete),
                "idleFor": max(0, (now - activity[key]) // 1000) if key in activity else None,
                "idleSince": activity.get(key),
                "evidence": {
                    "identity": facts, "parent": parent_facts,
                    "turn": turn_inventory.evidence(),
                    "idleFor": combined_evidence(
                        turn_inventory.evidence(), *activity_evidence[key]),
                },
            })

        item_turns = defaultdict(dict)
        live_turns = [t for t in turns if t.get("endedAt") is None]
        for turn in live_turns:
            holder = ids.get(turn.get("sessionKey"))
            if holder is None:
                continue
            assignment = assignments.rows.get(turn.get("assignmentId"))
            touched = {assignment["workItemId"]} if assignment else set()
            if not turn.get("assignmentId") and isinstance(turn.get("startedAt"), (int, float)):
                for wid, at in authored_at[turn["sessionKey"]].items():
                    if at >= turn["startedAt"]:
                        touched.add(wid)
            state = "run" if turn.get("startedAt") is not None else "wait"
            for wid in touched:
                if item_turns[wid].get(holder) != "run":
                    item_turns[wid][holder] = state

        items = []
        for wid, item in work.rows.items():
            owned = by_item[wid]
            summary = {"count": 0, "kinds": Counter(), "verdicts": Counter(), "latest": {}}
            for assignment in owned:
                part = self.summary("attests", assignment["id"])
                summary["count"] += part["count"]
                summary["kinds"].update(part["kinds"])
                summary["verdicts"].update(part["verdicts"])
                for verdict, timestamp in part["latest"].items():
                    summary["latest"][verdict] = max(summary["latest"].get(verdict, 0), timestamp)
            inventory_facts = [self.inventory("attests", a["id"]).evidence() for a in owned]
            attest_facts = combined_evidence(assignments.evidence(), *inventory_facts)
            identity = work.observations.get(wid, work.evidence())
            stage_facts = combined_evidence(identity, assignments.evidence(), attest_facts)
            complete = stage_facts["complete"]
            stage = evidence_stage(item, owned, (), complete=complete, summary=summary)
            kinds = sorted(k for k in summary["kinds"] if k is not None)
            verdicts = sorted(summary["verdicts"])
            refs, producer_refs = [], []
            for assignment in owned:
                corrections = assignment.get("commitRefCorrections") or []
                found = ((corrections[-1].get("commitRefs") or []) if corrections else
                         self.summary("attests", assignment["id"])["refs"])
                refs.extend(found)
                if not assignment.get("reviewsAssignmentId"):
                    producer_refs.extend(found)
            code = code_state(owned, producer_refs, complete=complete)
            merge = self.merges.get(wid, {"value": None, "observedAt": None, "refs": refs})
            merged = merge["value"] if merge.get("refs") == refs else None
            merge_facts = {
                "status": "known" if merged is not None else "unknown",
                "freshness": "live" if merged is not None and not assignments.stale else "stale",
                "observedAt": merge["observedAt"], "source": "git",
                "complete": merged is not None,
            }
            ready = "ready-to-merge" in verdicts if complete else None
            ring = merge_ring(stage, merged, ready, code)
            ring_facts = combined_evidence(stage_facts, merge_facts)
            # A known higher-precedence ring does not require answers to the
            # lower-precedence questions. Floating items also need no merge.
            if stage is not None and (stage < 5 or merged is True):
                ring_facts = combined_evidence(stage_facts, *([merge_facts] if merged is True else []))
            elif ready is True or code is False:
                # A missing merge answer can still supersede green/gray later.
                ring_facts = dict(stage_facts)
            holders = list(dict.fromkeys(ids[a["holderKey"]] for a in owned
                                        if a["state"] == "open" and a["holderKey"] in ids))
            closed = [a["closedAt"] for a in owned if isinstance(a.get("closedAt"), (int, float))]
            items.append({
                "id": wid, "short": wid[:11], "title": item["title"], "state": item["state"],
                "stage": stage, "ring": ring, "merged": merged, "code": code,
                "readyToMerge": ready, "closedAt": max(closed) if closed else None,
                "holders": holders if assignments.complete else (holders or None),
                "turns": item_turns.get(wid, {}) if all_turn_facts["complete"] else (item_turns.get(wid) or None),
                "flow": {holder: "in" for holder in item_turns.get(wid, {})},
                "attestKinds": kinds, "verdictKinds": verdicts,
                "assignments": {
                    "open": sum(a["state"] == "open" for a in owned) if assignments.complete else None,
                    "terminal": sum(a["state"] != "open" for a in owned) if assignments.complete else None,
                },
                "evidenceCounts": {
                    "total": summary["count"] if attest_facts["complete"] else None,
                    "byKind": dict(summary["kinds"]) if attest_facts["complete"] else None,
                    "byVerdict": dict(summary["verdicts"]) if attest_facts["complete"] else None,
                },
                "commitRefs": refs,
                "commitRefCorrections": [
                    {k: v for k, v in c.items() if k not in {"reason", "note"}}
                    for a in owned for c in a.get("commitRefCorrections", [])],
                "evidence": {"identity": identity, "stage": stage_facts,
                             "ring": ring_facts, "assignments": assignments.evidence(),
                             "attests": attest_facts, "turns": all_turn_facts,
                             "merged": merge_facts, "code": stage_facts},
            })

        links, wake_marks, message_marks = [], [], []
        for turn in live_turns:
            wake = wakes.get(str(turn.get("wakeId")))
            if wake:
                src, dst = ids.get(wake.get("creatorSessionKey")), ids.get(turn["sessionKey"])
                if src and dst and src != dst:
                    links.append({"id": str(turn["seq"]), "from": src, "to": dst,
                                  "state": "run" if turn.get("startedAt") is not None else "wait"})
        for wake in wakes.values():
            src, dst = ids.get(wake.get("creatorSessionKey")), ids.get(wake.get("sessionKey"))
            if (src and dst and src != dst and wake.get("state") == "fired"
                    and isinstance(wake.get("firedAt"), (int, float))
                    and wake["firedAt"] > now - 300_000):
                wake_marks.append({"id": wake["wakeId"], "from": src, "to": dst})
                self.recent_expiries["wakes", wake["wakeId"]] = wake["firedAt"] + 300_000
                self._expires(wake["firedAt"] + 300_000)
        bindings = {"agent:" + r["name"]: r.get("boundSessionKey") for r in roles.rows.values()}
        for message in messages:
            if not isinstance(message.get("at"), (int, float)) or message["at"] <= now - 300_000:
                continue
            dst, sender = ids.get(message.get("sessionKey")), message.get("sender") or ""
            if not dst:
                continue
            src = ids.get(sender) or ids.get(bindings.get(sender))
            if sender.startswith("agent:") and not src:
                continue
            message_marks.append({"id": message["id"], "from": src, "to": dst,
                                  "kind": "agent" if src else (
                                      "user" if sender.startswith("user:") else "substrate")})
            self.recent_expiries["messages", message["id"]] = message["at"] + 300_000
            self._expires(message["at"] + 300_000)
        return {"agents": agents, "items": items, "links": links,
                "wakes": wake_marks, "messages": message_marks}

    def _expires(self, at):
        self.next_expiry = min(self.next_expiry, at) if self.next_expiry is not None else at
