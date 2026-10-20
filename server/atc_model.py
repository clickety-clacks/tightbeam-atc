"""Derive the view from scoped evidence inventories, without viewer state."""

from collections import Counter, defaultdict
from copy import deepcopy

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

    def inventory(self, resource, scope=None):
        return self.inventories.setdefault((resource, scope), Inventory(KEYS.get(resource, "id")))

    def accept(self, resource, scope, result):
        self.inventory(resource, scope).read(
            result.rows, complete=result.complete, at=result.at, source=result.source)

    def detail(self, resource, scope, result):
        inventory = self.inventory(resource, scope)
        if result.complete:
            for row in result.rows:
                key = str(row[inventory.key])
                inventory.rows[key] = deepcopy(row)
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
        work = self.inventory("work-items")
        assignments = self.inventory("assignments")
        sessions = self.inventory("sessions")
        roles = self.inventory("roles")
        by_item, evidence_by_item = defaultdict(list), defaultdict(list)
        for assignment in assignments.rows.values():
            by_item[assignment.get("workItemId")].append(assignment)
        for assignment in assignments.rows.values():
            evidence_by_item[assignment.get("workItemId")].extend(
                self.inventory("attests", assignment["id"]).rows.values())

        visible = self.active_sessions()
        ids = {s["sessionKey"]: agent_id(s["sessionKey"]) for s in visible}
        roles_by_session = defaultdict(list)
        for role in roles.rows.values():
            roles_by_session[role.get("boundSessionKey")].append(role["name"])

        turns, wakes, messages = [], {}, []
        turn_evidence = []
        for session in visible:
            key = session["sessionKey"]
            t = self.inventory("turns", key)
            turn_evidence.append(t.evidence())
            turns.extend(t.rows.values())
            wakes.update(self.inventory("wakes", key).rows)
            messages.extend(self.inventory("messages", key).rows.values())
        all_turn_facts = combined_evidence(*turn_evidence) if visible else sessions.evidence()

        activity = {}
        activity_evidence = defaultdict(list)
        for turn in turns:
            at = next((turn.get(k) for k in ("endedAt", "startedAt", "createdAt")
                       if isinstance(turn.get(k), (int, float))), None)
            if at is not None:
                key = turn["sessionKey"]
                activity[key] = max(activity.get(key, 0), at)
        authored_at = defaultdict(dict)
        for wid, attests in evidence_by_item.items():
            for attest in attests:
                key, at = attest.get("bySession"), attest.get("ts")
                if key and isinstance(at, (int, float)):
                    activity[key] = max(activity.get(key, 0), at)
                    authored_at[key][wid] = max(authored_at[key].get(wid, 0), at)
        for assignment in assignments.rows.values():
            inventory = self.inventory("attests", assignment["id"])
            for author in {r.get("bySession") for r in inventory.rows.values()} - {None}:
                activity_evidence[author].append(inventory.evidence())

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
                "turn": turn_state(turn_inventory.rows.values(), complete=turn_inventory.complete),
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
            owned, attests = by_item[wid], evidence_by_item[wid]
            inventory_facts = [self.inventory("attests", a["id"]).evidence() for a in owned]
            attest_facts = combined_evidence(assignments.evidence(), *inventory_facts)
            identity = work.observations.get(wid, work.evidence())
            stage_facts = combined_evidence(identity, assignments.evidence(), attest_facts)
            complete = stage_facts["complete"]
            stage = evidence_stage(item, owned, attests, complete=complete)
            kinds = sorted({a["kind"] for a in attests})
            verdicts = sorted({a["verdictKind"] for a in attests
                               if a["kind"] == "verdict" and a.get("verdictKind")})
            refs, producer_refs = [], []
            for assignment in owned:
                found = self.commit_refs(assignment, self.inventory("attests", assignment["id"]).rows.values())
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
                    "total": len(attests) if attest_facts["complete"] else None,
                    "byKind": dict(Counter(a["kind"] for a in attests)) if attest_facts["complete"] else None,
                    "byVerdict": dict(Counter(a["verdictKind"] for a in attests
                                             if a["kind"] == "verdict" and a.get("verdictKind")))
                    if attest_facts["complete"] else None,
                },
                "commitRefs": refs,
                "commitRefCorrections": [c for a in owned for c in a.get("commitRefCorrections", [])],
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
            self._expires(message["at"] + 300_000)
        return {"agents": agents, "items": items, "links": links,
                "wakes": wake_marks, "messages": message_marks}

    def _expires(self, at):
        self.next_expiry = min(self.next_expiry, at) if self.next_expiry is not None else at
