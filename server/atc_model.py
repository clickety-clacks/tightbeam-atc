"""Derive the view from scoped evidence inventories, without viewer state."""

from collections import Counter, defaultdict
from copy import deepcopy
from math import isfinite
import hashlib
import json
from atc_summary import summarize, recent_marks, HistoryFold, HISTORY_KEYS, DetailFingerprint

from atc_evidence import (
    Inventory, HistoryInventory, ArtifactInventory, agent_id, code_state, combined_evidence, evidence_stage, merge_ring,
    turn_state, merge_value_for_refs,
)
from atc_payloads import has_work_item_summary


KEYS = {"sessions": "sessionKey", "roles": "name", "turns": "seq",
        "wakes": "wakeId", "artifacts": "artifactId"}


def latest_time(*values):
    return max((value for value in values if isinstance(value, (int, float))
                and not isinstance(value, bool) and isfinite(value) and value > 0), default=None)


class EvidenceModel:
    def __init__(self):
        self.inventories = {}
        self.merges = {}
        self.next_expiry = None
        self.next_raw_expiry = None
        self.details_revision = 0
        self.recent_expiries = {}
        self.summaries = {}
        self._item_activity_parts = {}
        self._item_activity = {}
        self.retired_item_activity = {}
        self.retired_activity_revision = 0
        self.closed_items = {}
        self.closed_revision = 0
        self.open_assignment_order = {}
        self.live_assignment_items = {}
        self.closed_sources_released = False

    def summary(self, resource, scope):
        inventory = self.inventory(resource, scope)
        if isinstance(inventory, HistoryInventory):
            return inventory.folded
        cached = self.summaries.get((resource, scope))
        if cached is None or cached[0] != inventory.data_revision:
            cached = inventory.data_revision, summarize(resource, inventory.rows.values())
            self.summaries[resource, scope] = cached
        return cached[1]

    def inventory(self, resource, scope=None):
        key = resource, scope
        if key not in self.inventories:
            self.inventories[key] = (HistoryInventory(resource) if resource in HISTORY_KEYS
                                     else ArtifactInventory() if resource == "artifacts"
                                     else Inventory(KEYS.get(resource, "id")))
        return self.inventories[key]

    def current_ids(self, resource, scope):
        if resource == "retired-turns":
            return self.retired_activity_plan()
        if resource == "turns":
            return {str(turn["seq"]) for turn in self.summary("turns", scope)["live"]}
        if resource != "wakes":
            return set()
        return {str(turn["wakeId"]) for turn in self.summary("turns", scope)["live"]
                if turn.get("wakeId") is not None}

    def retired_activity_plan(self):
        visible = {row["sessionKey"] for row in self.active_sessions()}
        work = self.inventory("work-items").rows
        assignments = self.inventory("assignments").rows
        linked = {row.get("holderKey") for row in assignments.values()
                  if row.get("workItemId") in work and row.get("holderKey")}
        sessions = tuple(sorted(linked - visible))
        assignment_items = {row["id"]: row["workItemId"] for row in assignments.values()
                            if row.get("workItemId") in work}
        marker = hashlib.blake2b(json.dumps((sessions, assignment_items), sort_keys=True).encode(),
                                 digest_size=16).hexdigest()
        # Warm instances already read these scopes while the holders were
        # visible; publication will transfer their maxima before releasing them.
        unread = tuple(session for session in sessions
                       if not (self.inventories.get(("turns", session))
                               and self.inventories["turns", session].complete
                               and not self.inventories["turns", session].stale
                               and self.inventories["turns", session].unconfirmed_since is None))
        return marker, unread, assignment_items

    def fold_retired_activity(self, activity):
        work = self.inventory("work-items").rows
        changed = False
        for wid, at in activity.items():
            if wid in work and at > self.retired_item_activity.get(wid, 0):
                self.retired_item_activity[wid] = at
                changed = True
        if self.inventory("work-items").complete:
            for wid in list(self.retired_item_activity):
                if wid not in work:
                    del self.retired_item_activity[wid]
                    changed = True
        if changed:
            self.retired_activity_revision += 1

    def release_session_histories(self, visible):
        assignments = self.inventory("assignments").rows
        released = {key for key in self.inventories
                    if key[0] in {"turns", "wakes", "messages"} and key[1] not in visible}
        activity = {}
        for resource, session in released:
            if resource == "turns":
                for aid, at in self.summary(resource, session)["assignmentActivity"].items():
                    wid = assignments.get(aid, {}).get("workItemId")
                    if wid:
                        activity[wid] = max(activity.get(wid, 0), at)
        self.fold_retired_activity(activity)
        for key in released:
            del self.inventories[key]
            self.summaries.pop(key, None)
        for part in self.closed_items.values():
            # Retired authors no longer supply an agent's idle or live-turn
            # attribution. The item's activity maximum remains in part itself.
            summary = part["summary"]
            summary["authors"] = {key: at for key, at in summary["authors"].items()
                                  if key in visible}
            summary["authorIds"].intersection_update(visible)
            part["authorFacts"] = {key: facts for key, facts in part["authorFacts"].items()
                                   if key in visible}
        if released:
            self._item_activity_parts.pop("turns", None)
        return released

    def mark_unconfirmed(self, at):
        for inventory in self.inventories.values():
            inventory.mark_unconfirmed(at)
        for part in self.closed_items.values():
            for fact in (part["facts"], *part["authorFacts"].values()):
                if fact["complete"] and fact.get("unconfirmedSince") is None:
                    fact["unconfirmedSince"] = at
                    if fact["freshness"] != "stale":
                        fact["freshness"] = "unconfirmed"

    def release_auxiliary_sources(self):
        for (resource, _scope), inventory in self.inventories.items():
            if resource == "artifacts":
                inventory.release_rows()
            elif resource == "decision-requests":
                # The board uses scope completeness, not decision source text.
                inventory.rows.clear()
                inventory.observations.clear()

    def fold_closed_assignments(self):
        """Fold a settled assignment read into per-item board facts."""
        assignments = self.inventory("assignments")
        if not assignments.complete or not any(row.get("state") != "open" for row in assignments.rows.values()):
            return set()
        grouped = {}
        item_positions = Counter()
        self.open_assignment_order = {}
        live_ids = {turn.get("assignmentId") for resource, scope in self.inventories
                    if resource == "turns" for turn in self.summary(resource, scope)["live"]}
        self.live_assignment_items = {aid: wid for aid, wid in self.live_assignment_items.items()
                                      if aid in live_ids}
        released = set()
        for order, (aid, row) in enumerate(assignments.rows.items()):
            wid = row.get("workItemId")
            position = item_positions[wid]
            item_positions[wid] += 1
            if row.get("state") == "open":
                self.open_assignment_order[aid] = order
                continue
            part = grouped.setdefault(wid, {
                "count": 0, "closedAt": None, "activity": None,
                "code": False, "unknownEffect": False, "refs": [],
                "corrections": [], "summary": summarize("attests", ()),
                "facts": [], "authorFacts": {}, "detailFingerprint": DetailFingerprint(),
                "assignmentFingerprint": DetailFingerprint(), "detailsKnown": True,
            })
            summary = self.summary("attests", aid)
            inventory = self.inventory("attests", aid)
            facts = inventory.evidence()
            part["detailFingerprint"].add(row, inventory.fingerprint, position)
            part["assignmentFingerprint"].add(row, None, position)
            part["detailsKnown"] &= inventory.fingerprint is not None
            part["count"] += 1
            close = row.get("closedAt")
            if isinstance(close, (int, float)):
                part["closedAt"] = max(part["closedAt"], close) if part["closedAt"] is not None else close
            part["activity"] = latest_time(part["activity"], row.get("openedAt"), close, summary["activity"])
            part["code"] |= row.get("effectKind") in {"code", "release", "live_mutation"}
            part["unknownEffect"] |= not bool(row.get("effectKind"))
            corrections = row.get("commitRefCorrections") or []
            refs = (corrections[-1].get("commitRefs") or []) if corrections else summary["refs"]
            part["refs"].extend((order, i, ref) for i, ref in enumerate(refs))
            if not row.get("reviewsAssignmentId"):
                part["code"] |= bool(refs)
            part["corrections"].extend((order, i, {k: v for k, v in correction.items()
                                      if k not in {"reason", "note"}})
                                      for i, correction in enumerate(corrections))
            folded = part["summary"]
            folded["count"] += summary["count"]
            folded["kinds"].update(summary["kinds"])
            folded["verdicts"].update(summary["verdicts"])
            for field in ("latest", "authors"):
                for key, at in summary[field].items():
                    folded[field][key] = max(folded[field].get(key, 0), at)
            folded["authorIds"].update(summary["authorIds"])
            for author in summary["authorIds"]:
                existing = part["authorFacts"].get(author)
                part["authorFacts"][author] = combined_evidence(existing, facts) if existing else combined_evidence(facts)
            part["facts"].append(facts)
            if aid in live_ids:
                self.live_assignment_items[aid] = wid
            released.add(aid)
        if not released:
            return set()
        for wid, part in grouped.items():
            part["facts"] = combined_evidence(*part["facts"])
            part["detailFingerprint"] = part["detailFingerprint"].hexdigest()
            part["assignmentFingerprint"] = part["assignmentFingerprint"].hexdigest()
            previous = self.closed_items.get(wid)
            if (not part["detailsKnown"] and previous and
                    previous["assignmentFingerprint"] == part["assignmentFingerprint"]):
                # Summary-backed items intentionally omit some attest reads.
                # An on-demand pin remains valid until another detail read
                # observes different text; it does not widen source eligibility.
                part["detailFingerprint"] = previous["detailFingerprint"]
            self.closed_items[wid] = part
        # Finished turn attribution becomes item state before its assignment
        # identity is released; only unfinished turns keep a lookup entry.
        for resource, scope in tuple(self.inventories):
            if resource != "turns":
                continue
            linked = self.summary(resource, scope)["assignmentActivity"]
            for aid in tuple(linked):
                if aid in released:
                    wid = assignments.rows[aid].get("workItemId")
                    part = self.closed_items[wid]
                    part["activity"] = latest_time(part["activity"], linked[aid])
                    del linked[aid]
        for aid in released:
            assignments.rows.pop(aid)
            assignments.observations.pop(aid, None)
            self.inventories.pop(("attests", aid), None)
            self.summaries.pop(("attests", aid), None)
        self.closed_revision += 1
        self.closed_sources_released = True
        self._item_activity_parts.clear()
        return {("attests", aid) for aid in released}

    def closed_item(self, wid, owned):
        # A fresh complete collection supplies replacement source rows. Until
        # folded, those rows are the contribution, not both them and its prior fold.
        return self.closed_items.get(wid) if not any(a.get("state") != "open" for a in owned) else None

    def accept(self, resource, scope, result):
        inventory = self.inventory(resource, scope)
        if isinstance(inventory, HistoryInventory):
            fold = result.folded
            if fold is None:
                fold = HistoryFold(resource, result.at, self.current_ids(resource, scope))
                fold.add(result.rows)
            if resource == "attests" and inventory.fingerprint != fold.fingerprint.hexdigest():
                self.details_revision += 1
            inventory.read_fold(fold, complete=result.complete, at=result.at, source=result.source)
            return
        if resource in {"attests", "assignments", "work-items"}:
            incoming = {str(row[inventory.key]): row for row in result.rows}
            if (any(inventory.rows.get(k) != row for k, row in incoming.items())
                    or (result.complete and inventory.rows.keys() != incoming.keys())):
                self.details_revision += 1
        inventory.read(
            result.rows, complete=result.complete, at=result.at, source=result.source)
        if resource == "assignments" and result.complete:
            self.closed_sources_released = False
            closed = {row.get("workItemId") for row in result.rows if row.get("state") != "open"}
            self.closed_items = {wid: part for wid, part in self.closed_items.items() if wid in closed}
            self.live_assignment_items.clear()
            self.open_assignment_order.clear()
            self.closed_revision += 1

    def detail(self, resource, scope, result):
        inventory = self.inventory(resource, scope)
        if isinstance(inventory, HistoryInventory):
            if result.complete:
                for row in result.rows:
                    policy = "immutable" if resource in {"attests", "messages"} else "strict-version"
                    if not inventory.payload(row, at=result.at, source=result.source, policy=policy):
                        inventory.invalidate()
            else:
                inventory.invalidate()
            return
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
            if isinstance(inventory, ArtifactInventory):
                inventory.refresh_activity(replace=not inventory.released)
        else:
            inventory.invalidate()

    def invalidate(self):
        for inventory in self.inventories.values():
            inventory.invalidate()
        for part in self.closed_items.values():
            for fact in (part["facts"], *part["authorFacts"].values()):
                fact["freshness"] = "stale"

    def active_sessions(self):
        holders = {a.get("holderKey") for a in self.inventory("assignments").rows.values()
                   if a.get("state") == "open"}
        for item in self.inventory("work-items").rows.values():
            if has_work_item_summary(item):
                holders.update(item["holders"])
        return [s for s in self.inventory("sessions").rows.values()
                if s.get("state") == "active" or s["sessionKey"] in holders]

    def commit_refs(self, assignment, attests):
        corrections = assignment.get("commitRefCorrections") or []
        if corrections:
            # Corrections replace a historical assignment's refs, preserving
            # the attest and the correction's independent authorship.
            return corrections[-1].get("commitRefs") or []
        return [ref for attest in attests for ref in (attest.get("commitRefs") or [])]

    def item_activity(self):
        # A closed assignment may still have an unfinished turn. Its small
        # live lookup survives source disposal until that turn finishes, so
        # later feed/read updates still advance the item's activity maximum.
        if self.live_assignment_items:
            for resource, scope in tuple(self.inventories):
                if resource != "turns":
                    continue
                linked = self.summary(resource, scope)["assignmentActivity"]
                for aid in tuple(linked):
                    wid = self.live_assignment_items.get(aid)
                    closed = self.closed_items.get(wid)
                    if closed is None:
                        continue
                    at = latest_time(closed["activity"], linked.pop(aid))
                    if at != closed["activity"]:
                        closed["activity"] = at
                        self.closed_revision += 1
        # Keep independently changing histories separate: a turn must not make
        # publication rescan all artifacts or assignments. Revision checks also
        # cover direct feed payload updates and complete-inventory removals.
        versions = {"assignments": [], "artifacts": [], "turns": []}
        for key, inventory in self.inventories.items():
            resource = key[0]
            if resource in {"assignments", "attests"}:
                versions["assignments"].append((key, inventory.data_revision))
            if resource in {"artifacts", "turns"}:
                versions[resource].append((key, inventory.data_revision))
        assignments = self.inventory("assignments")
        versions["assignments"].append(("closed-items", self.closed_revision))
        versions["turns"].append((("assignments", None), assignments.data_revision))
        versions["turns"].append((("retired-item-activity", None), self.retired_activity_revision))
        changed = False
        for group, revision in versions.items():
            cached = self._item_activity_parts.get(group)
            if cached is not None and cached[0] == revision:
                continue
            changed = True
            activity = {}
            def record(wid, *times):
                if wid:
                    activity[wid] = latest_time(activity.get(wid), *times)
            if group == "assignments":
                raw_closed = {row.get("workItemId") for row in assignments.rows.values()
                              if row.get("state") != "open"}
                for wid, part in self.closed_items.items():
                    if wid not in raw_closed or not assignments.complete:
                        record(wid, part["activity"])
                for assignment in assignments.rows.values():
                    record(assignment.get("workItemId"), assignment.get("openedAt"),
                           assignment.get("closedAt"),
                           self.summary("attests", assignment["id"])["activity"])
                # Missing attest scopes are created by summary on first use.
                revision = [(key, inv.data_revision) for key, inv in self.inventories.items()
                            if key[0] in {"assignments", "attests"}]
                revision.append(("closed-items", self.closed_revision))
            elif group == "artifacts":
                for (resource, _scope), inventory in self.inventories.items():
                    if resource == "artifacts":
                        for wid, at in inventory.activity.items():
                            record(wid, at)
            else:
                activity.update(self.retired_item_activity)
                for resource, scope in self.inventories:
                    if resource == "turns":
                        for aid, at in self.summary(resource, scope)["assignmentActivity"].items():
                            record(assignments.rows.get(aid, {}).get("workItemId"), at)
            self._item_activity_parts[group] = revision, activity
        if changed:
            combined = {}
            for _revision, activity in self._item_activity_parts.values():
                for wid, at in activity.items():
                    combined[wid] = latest_time(combined.get(wid), at)
            self._item_activity = combined
        return self._item_activity

    def snapshot(self, now):
        self.next_expiry = None
        self.next_raw_expiry = None
        for (resource, scope), inventory in list(self.inventories.items()):
            if isinstance(inventory, HistoryInventory):
                inventory.keep_ids = self.current_ids(resource, scope)
                inventory.prune(now)
                if inventory.raw_expiry is not None:
                    self.next_raw_expiry = (min(self.next_raw_expiry, inventory.raw_expiry)
                                            if self.next_raw_expiry is not None else inventory.raw_expiry)
        self.recent_expiries = {key: at for key, at in self.recent_expiries.items() if at > now}
        work = self.inventory("work-items")
        assignments = self.inventory("assignments")
        sessions = self.inventory("sessions")
        roles = self.inventory("roles")
        by_item = defaultdict(list)
        for assignment in assignments.rows.values():
            by_item[assignment.get("workItemId")].append(assignment)

        item_activity = self.item_activity()

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
        # Item evidence names the turns collection, not every session URL.
        # Session-level observations and timestamps remain on each agent.
        item_turn_facts = dict(all_turn_facts, observedAt=None, source=["turns"])

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
        for wid in self.closed_items:
            closed = self.closed_item(wid, by_item[wid])
            if closed is None:
                continue
            for author, at in closed["summary"]["authors"].items():
                activity[author] = max(activity.get(author, 0), at)
                authored_at[author][wid] = max(authored_at[author].get(wid, 0), at)
            for author, facts in closed["authorFacts"].items():
                activity_evidence[author].append(facts)

        agents = []
        for session in visible:
            key = session["sessionKey"]
            facts = sessions.evidence_for(key)
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
        live_assignment_ids = {turn.get("assignmentId") for turn in live_turns}
        self.live_assignment_items = {aid: wid for aid, wid in self.live_assignment_items.items()
                                      if aid in live_assignment_ids}
        for turn in live_turns:
            holder = ids.get(turn.get("sessionKey"))
            if holder is None:
                continue
            assignment = assignments.rows.get(turn.get("assignmentId"))
            touched = {assignment["workItemId"]} if assignment else set()
            if not assignment and turn.get("assignmentId") in self.live_assignment_items:
                touched.add(self.live_assignment_items[turn["assignmentId"]])
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
            folded_closed = self.closed_item(wid, owned)
            detail_summary = {"count": 0, "kinds": Counter(), "verdicts": Counter(), "latest": {}}
            parts = [self.summary("attests", assignment["id"]) for assignment in owned]
            if folded_closed:
                parts.append(folded_closed["summary"])
            for part in parts:
                detail_summary["count"] += part["count"]
                detail_summary["kinds"].update(part["kinds"])
                detail_summary["verdicts"].update(part["verdicts"])
                for verdict, timestamp in part["latest"].items():
                    detail_summary["latest"][verdict] = max(
                        detail_summary["latest"].get(verdict, 0), timestamp)
            inventory_facts = [self.inventory("attests", a["id"]).evidence() for a in owned]
            if folded_closed:
                inventory_facts.append(folded_closed["facts"])
            identity = work.evidence_for(wid)
            item_summary = item if has_work_item_summary(item) else None
            if item_summary is not None:
                attest_counts = item_summary["attestCounts"]
                verdict_counts = item_summary["verdictCounts"]
                summary = {
                    "count": sum(attest_counts.values()),
                    "kinds": Counter(attest_counts),
                    "verdicts": Counter(verdict_counts),
                    "latest": detail_summary["latest"],
                }
                attest_facts = identity
                detail_counts_match = (
                    assignments.complete
                    and all(fact["complete"] and fact["freshness"] == "live"
                            for fact in inventory_facts)
                    and {k: v for k, v in detail_summary["kinds"].items() if k is not None}
                    == attest_counts
                    and dict(detail_summary["verdicts"]) == verdict_counts
                )
                review_details_fresh = assignments.evidence()["freshness"] == "live"
                stage_facts = identity
                review_timing_needed = (
                    item["state"] != "closed" and not item_summary["holders"]
                    and "completion" in attest_counts
                    and "reviewed-clean" in verdict_counts
                    and "tests-passed" in verdict_counts
                )
                review_timing_known = (
                    not review_timing_needed
                    or (review_details_fresh and detail_counts_match)
                )
                if not review_timing_known:
                    detail_facts = [assignments.evidence(), *inventory_facts]
                    freshness = identity["freshness"]
                    if any(fact["freshness"] == "stale" for fact in detail_facts):
                        freshness = "stale"
                    elif any(fact["freshness"] == "unconfirmed" for fact in detail_facts):
                        freshness = "unconfirmed"
                    stage_facts = dict(identity, status="unknown", complete=False,
                                       freshness=freshness,
                                       source=sorted(set(
                                           identity["source"] if isinstance(identity["source"], list)
                                           else [identity["source"]]) | {"attests"}))
                stage = evidence_stage(
                    item, owned, (), complete=identity["complete"], summary=summary,
                    has_open=bool(item_summary["holders"]),
                    review_timing_complete=review_timing_known)
                kinds = sorted(k for k in attest_counts if k is not None)
                verdicts = sorted(verdict_counts)
            else:
                attest_facts = combined_evidence(assignments.evidence(), *inventory_facts)
                stage_facts = combined_evidence(identity, assignments.evidence(), attest_facts)
                complete = stage_facts["complete"]
                summary = detail_summary
                stage = evidence_stage(item, owned, (), complete=complete, summary=summary)
                kinds = sorted(k for k in summary["kinds"] if k is not None)
                verdicts = sorted(summary["verdicts"])
            ordered_refs = list(folded_closed["refs"]) if folded_closed else []
            ordered_corrections = list(folded_closed["corrections"]) if folded_closed else []
            producer_refs = []
            for index, assignment in enumerate(owned):
                order = (self.open_assignment_order.get(assignment["id"],
                         len(self.open_assignment_order) + folded_closed["count"] + index)
                         if folded_closed else index)
                corrections = assignment.get("commitRefCorrections") or []
                found = ((corrections[-1].get("commitRefs") or []) if corrections else
                         self.summary("attests", assignment["id"])["refs"])
                ordered_refs.extend((order, i, ref) for i, ref in enumerate(found))
                ordered_corrections.extend((order, i, {k: v for k, v in correction.items()
                                           if k not in {"reason", "note"}})
                                           for i, correction in enumerate(corrections))
                if not assignment.get("reviewsAssignmentId"):
                    producer_refs.extend(found)
            refs = [ref for _order, _index, ref in sorted(ordered_refs, key=lambda entry: entry[:2])]
            if item_summary is not None:
                refs = list(item_summary["effectiveCommitRefs"])
            refs_complete = (identity["complete"] if item_summary is not None else
                             assignments.complete and all(f["complete"] for f in inventory_facts))
            code = code_state(owned, producer_refs,
                              complete=assignments.complete if item_summary is not None else complete)
            if folded_closed:
                if folded_closed["code"]:
                    code = True
                elif code is not True:
                    code = (False if (assignments.complete if item_summary is not None else complete)
                            and not folded_closed["unknownEffect"]
                            and all(a.get("effectKind") for a in owned) else None)
            if item_summary is not None:
                # Effective refs aggregate every assignment, including review
                # assignments. Without producer attribution, they prevent a
                # durable non-code conclusion but do not prove code ownership.
                if refs and code is False:
                    code = None
                landings = list(item_summary["landings"])
                newest_landing = max(landings, key=lambda landing: landing["observedAt"], default=None)
                if landings:
                    merged = True
                    merge_facts = {
                        "status": "known", "freshness": identity["freshness"],
                        "observedAt": newest_landing["observedAt"],
                        "source": ["work-items"], "complete": True,
                        "landings": landings,
                    }
                else:
                    # Empty summary landings mean no observed landing exists;
                    # use the same shipping-line result as older gateways.
                    merge = self.merges.get(
                        wid, {"value": None, "observedAt": None, "refs": refs})
                    merged = merge_value_for_refs(merge, refs, refs_complete)
                    merge_facts = {
                        "status": "known" if merged is not None else "unknown",
                        "freshness": ("live" if merged is not None and not assignments.stale
                                      and merge.get("freshness") != "stale"
                                      and refs_complete and merge.get("refs") == refs else "stale"),
                        "observedAt": merge["observedAt"], "source": "git",
                        "complete": merged is not None, "landings": landings,
                    }
            else:
                landings = None
                merge = self.merges.get(wid, {"value": None, "observedAt": None, "refs": refs})
                merged = merge_value_for_refs(merge, refs, refs_complete)
                merge_facts = {
                    "status": "known" if merged is not None else "unknown",
                    "freshness": ("live" if merged is not None and not assignments.stale
                                  and merge.get("freshness") != "stale"
                                  and refs_complete and merge.get("refs") == refs else "stale"),
                    "observedAt": merge["observedAt"], "source": "git",
                    "complete": merged is not None,
                }
            ready = "ready-to-merge" in verdicts if (
                item_summary is not None and identity["complete"] or
                item_summary is None and attest_facts["complete"]
            ) else None
            ring = merge_ring(stage, merged, ready, code)
            ring_facts = combined_evidence(stage_facts, merge_facts)
            # A known higher-precedence ring does not require answers to the
            # lower-precedence questions. Floating items also need no merge.
            if stage is not None and (stage < 5 or merged is True):
                ring_facts = combined_evidence(stage_facts, *([merge_facts] if merged is True else []))
            elif ready is True or code is False:
                # A missing merge answer can still supersede green/gray later.
                ring_facts = dict(stage_facts)
            if item_summary is not None:
                holders = list(dict.fromkeys(agent_id(key) for key in item_summary["holders"]))
                started = item_summary["started"]
                summary_facts = identity
            else:
                holders = list(dict.fromkeys(ids[a["holderKey"]] for a in owned
                                            if a["state"] == "open" and a["holderKey"] in ids))
                started = (bool(owned or folded_closed) if assignments.complete else None)
                summary_facts = dict(assignments.evidence(), status="unknown", complete=False)
            closed = [a["closedAt"] for a in owned if isinstance(a.get("closedAt"), (int, float))]
            if folded_closed and folded_closed["closedAt"] is not None:
                closed.append(folded_closed["closedAt"])
            latest_activity = latest_time(item.get("createdAt"), item.get("bodyUpdatedAt"),
                                         item.get("rowVersion"), item_activity.get(wid))
            # Public work-item rows currently expose no terminal-state time.
            # Never substitute an assignment's closedAt as an exact item close.
            ended = item["state"] in {"closed", "iceboxed"}
            exact_finish = latest_time(item.get("closedAt") if item["state"] == "closed"
                                       else item.get("iceboxedAt")) if ended else None
            trace_inventory = self.inventories.get(("finish-time", wid))
            trace = trace_inventory.rows.get(wid, {}) if trace_inventory and trace_inventory.complete else {}
            if ended and trace.get("state") == item["state"]:
                exact_finish = latest_time(exact_finish, trace.get("finishedAt"))
            items.append({
                "id": wid, "short": wid[:11], "title": item["title"], "state": item["state"],
                "stage": stage, "ring": ring, "merged": merged, "code": code,
                "readyToMerge": ready, "closedAt": max(closed) if closed else None,
                "holders": (holders if item_summary is not None else
                            holders if assignments.complete else (holders or None)),
                "started": started, "landings": landings,
                "latestActivityAt": latest_activity,
                "landingAt": (self.merges.get(wid, {}).get("landingAt") if merged is True else None),
                "finishedAt": (exact_finish or latest_activity) if ended else None,
                "finishedAtApproximate": ended and exact_finish is None,
                "turns": (item_turns.get(wid, {}) if item_turn_facts["complete"]
                          else (item_turns.get(wid) or None)),
                "flow": {holder: "in" for holder in item_turns.get(wid, {})},
                "attestKinds": kinds, "verdictKinds": verdicts,
                "assignments": {
                    "open": sum(a["state"] == "open" for a in owned) if assignments.complete else None,
                    "terminal": (sum(a["state"] != "open" for a in owned)
                                 + (folded_closed["count"] if folded_closed else 0)) if assignments.complete else None,
                },
                "evidenceCounts": {
                    "total": summary["count"] if attest_facts["complete"] else None,
                    "byKind": dict(summary["kinds"]) if attest_facts["complete"] else None,
                    "byVerdict": dict(summary["verdicts"]) if attest_facts["complete"] else None,
                },
                "commitRefs": refs, "commitRefsComplete": refs_complete,
                "commitRefCorrections": [correction for _order, _index, correction in
                                         sorted(ordered_corrections, key=lambda entry: entry[:2])],
                "evidence": {"identity": identity, "stage": stage_facts,
                             "ring": ring_facts, "assignments": assignments.evidence(),
                             "summary": summary_facts,
                             "attests": attest_facts, "turns": item_turn_facts,
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
