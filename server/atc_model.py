"""Derive the view from scoped evidence inventories, without viewer state."""

from collections import Counter, defaultdict
from copy import deepcopy
from math import isfinite
from urllib.parse import quote
import hashlib
import json
from atc_summary import summarize, recent_marks, HistoryFold, HISTORY_KEYS, DetailFingerprint

from atc_evidence import (
    Inventory, HistoryInventory, ArtifactInventory, agent_id, code_state, combined_evidence, evidence_stage, merge_ring,
    turn_state, merge_value_for_refs,
)
from atc_payloads import has_work_item_summary
from atc_contributions import AssignmentContribution, ContributionEvidence, ItemContributions


KEYS = {"sessions": "sessionKey", "roles": "name", "turns": "seq",
        "wakes": "wakeId", "artifacts": "artifactId"}

# Inputs still needed to derive every published item field. Full source detail
# is read on demand after an older item's row has been folded.
ITEM_FACT_FIELDS = frozenset("""
    id title state createdAt bodyUpdatedAt rowVersion closedAt iceboxedAt
    holders started attestCounts verdictCounts effectiveCommitRefs landings
""".split())


def latest_time(*values):
    return max((value for value in values if isinstance(value, (int, float))
                and not isinstance(value, bool) and isfinite(value) and value > 0), default=None)


def activity_time(value):
    """Use the history fold's numeric maximum, including its zero baseline."""
    if isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value):
        return max(0, value)
    return None


def fold_session_activity_record(values, records, session, timestamp, kind, scope, facts, *, owner=None):
    """Fold into two fixed per-session maps, including temporary read staging."""
    timestamp = activity_time(timestamp)
    if not session or timestamp is None:
        return False
    if kind == "attest":
        source = "cli:attests " + str(scope)
    elif kind == "turn":
        source = "/api/sessions/" + quote(str(session), safe="") + "/turns"
    else:
        raise ValueError("session activity requires attest or turn")
    previous_at = values.get(session)
    previous = records.get(session)
    kinds = {kind}
    winner = source
    winner_owner = owner if kind == "attest" else None
    if previous is not None:
        kinds.update(s for s in previous["source"] if s in {"attest", "turn"})
        previous_source = next((s for s in previous["source"]
                                if s not in {"attest", "turn"}), None)
        if previous_source is not None and previous_at is not None:
            if ("_retainedAt" in previous and previous_source.startswith("/api/sessions/")
                    and kind != "turn" and timestamp >= previous_at):
                # A session has only its own turn source. Once that winner
                # exits, a later attest cannot revive the exited turn kind.
                kinds.discard("turn")
            if previous_at > timestamp:
                winner = previous_source
                winner_owner = previous.get("_owner")
            elif previous_at == timestamp:
                winner = min(previous_source, source)
                if winner != source:
                    winner_owner = previous.get("_owner")
    merged = combined_evidence(*(fact for fact in (previous, facts) if fact is not None))
    # combined_evidence retains its scalar confirmation semantics. Only
    # its per-scope source union is replaced by the fixed source contract.
    merged["source"] = sorted(kinds | {winner})
    merged["_owner"] = winner_owner
    if previous and "_retainedAt" in previous and previous_at > timestamp:
        merged["_retainedAt"] = previous["_retainedAt"]
    latest = timestamp if previous_at is None else max(previous_at, timestamp)
    changed = previous_at != latest or previous != merged
    values[session] = latest
    records[session] = merged
    return changed


class ClosedAssignmentFold:
    """Accumulate one item's published closed facts without retaining source rows."""

    def __init__(self):
        self.part = {
            "count": 0, "closedAt": None, "activity": None, "turnActivity": None,
            "code": False, "unknownEffect": False, "refs": [],
            "corrections": [], "summary": summarize("attests", ()),
            "facts": None, "detailFingerprint": DetailFingerprint(),
            "assignmentFingerprint": DetailFingerprint(), "detailsKnown": True,
            "attestsRead": True,
        }

    def add(self, row, inventory, *, order, position):
        part = self.part
        part["detailFingerprint"].add(row, inventory.fingerprint, position)
        part["assignmentFingerprint"].add(row, None, position)
        details_known = part["detailsKnown"] and inventory.fingerprint is not None
        self.add_contribution(AssignmentContribution.capture(row, inventory, order))
        part["detailsKnown"] = details_known

    def add_contribution(self, contribution):
        """Reduce current facts; source detail pins remain a separate input.

        A caller rebuilding from released contributions must not treat this as
        a reconstruction of the discarded assignment/attest detail text.
        ``add`` captures those pins while the source rows are still available.
        """
        part = self.part
        part["detailsKnown"] = False
        # Source eligibility survives raw-text disposal. A compact reduction
        # cannot reconstruct the original detail pin, but that does not turn
        # an already-read attest scope into a summary-skipped scope.
        part["attestsRead"] &= contribution.evidence.observed_at is not None
        summary = contribution.attests
        facts = contribution.evidence.publication()
        order = contribution.order
        part["count"] += 1
        close = contribution.closed_at
        if isinstance(close, (int, float)):
            part["closedAt"] = max(part["closedAt"], close) if part["closedAt"] is not None else close
        part["activity"] = latest_time(part["activity"], contribution.opened_at, close, summary.activity)
        part["code"] |= contribution.effect in {"code", "release", "live_mutation"}
        part["unknownEffect"] |= not bool(contribution.effect)
        corrections = contribution.corrections
        refs = (corrections[-1].get("commitRefs") or []) if corrections else summary.refs
        part["refs"].extend((order, i, ref) for i, ref in enumerate(refs))
        if not contribution.reviews:
            part["code"] |= bool(refs)
        part["corrections"].extend((order, i, correction)
                                  for i, correction in enumerate(corrections))
        folded = part["summary"]
        folded["count"] += summary.count
        folded["kinds"].update(dict(summary.kinds))
        folded["verdicts"].update(dict(summary.verdicts))
        for field in ("latest", "authors"):
            for key, at in getattr(summary, field):
                folded[field][key] = max(folded[field].get(key, 0), at)
        folded["authorIds"].update(summary.author_ids)
        part["facts"] = combined_evidence(part["facts"], facts) if part["facts"] else combined_evidence(facts)

    def finish(self):
        part = self.part
        part["detailFingerprint"] = part["detailFingerprint"].hexdigest()
        part["assignmentFingerprint"] = part["assignmentFingerprint"].hexdigest()
        return part


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
        self.session_activity = {}
        self.session_activity_evidence = {}
        self.activity_refresh = None
        self.retired_activity_revision = 0
        self.closed_items = {}
        self.item_contributions = ItemContributions()
        self.closed_revision = 0
        self.open_assignment_order = {}
        self.live_assignment_items = {}
        self.closed_sources_released = False
        self.item_assignment_facts = {}
        self.item_assignment_pending = set()
        self.item_assignment_summary = {}
        self.older_items = {}
        self._work_item_order = []
        self.older_item_sources_released = False

    def begin_activity_refresh(self):
        self.activity_refresh = ({}, {})

    def finish_activity_refresh(self):
        if self.activity_refresh is not None:
            values, records = self.activity_refresh
            for session, facts in self.session_activity_evidence.items():
                if ("_retainedAt" in facts and self.session_activity[session] >
                        values.get(session, -1)):
                    values[session] = self.session_activity[session]
                    records[session] = facts
            self.session_activity, self.session_activity_evidence = self.activity_refresh
            self.activity_refresh = None

    def fold_session_activity(self, session, timestamp, kind, scope, facts, *, owner=None):
        changed = fold_session_activity_record(self.session_activity, self.session_activity_evidence,
            session, timestamp, kind, scope, facts, owner=owner)
        if self.activity_refresh is not None and facts.get("complete"):
            fold_session_activity_record(*self.activity_refresh, session, timestamp, kind, scope,
                                         facts, owner=owner)
        return changed

    def fold_activity_inventory(self, resource, scope, *, releasing=False):
        if resource not in {"attests", "turns"}:
            return
        inventory = self.inventory(resource, scope)
        if resource == "attests" and not getattr(inventory, "activity_eligible", True):
            return
        summary = inventory.folded
        # The rows establish positive activity even when the scoped walk is
        # incomplete. Its temporary completeness/staleness stays on the held
        # inventory and is combined at publication, so a successful retry can
        # clear only that scope's warning. Existing outage marks on the fixed
        # record are not reset here.
        observation = inventory.evidence()
        if not releasing:
            observation = dict(observation, status="known", complete=True,
                               freshness="live", unconfirmedSince=None)
        if resource == "attests":
            assignment = self.inventory("assignments").rows.get(str(scope))
            if assignment is None or "workItemId" not in assignment:
                return
            for author, timestamp in summary["authors"].items():
                self.fold_session_activity(author, timestamp, "attest", scope, observation,
                                           owner=assignment["workItemId"])
        else:
            if inventory.complete and summary["activity"] is None:
                self.release_turn_activity(scope, inventory.observed_at)
            elif summary["activity"] is not None:
                self.fold_session_activity(scope, summary["activity"], "turn", scope, observation)

    def release_turn_activity(self, session, at):
        record = self.session_activity_evidence.get(session)
        if record is None or "turn" not in record["source"]:
            return
        if any(source.startswith("/api/sessions/") for source in record["source"]):
            # Scope exit cannot erase the already observed winning activity.
            # This fixed record is retained even while its session is hidden.
            record.setdefault("_retainedAt", at)
        else:
            record["source"].remove("turn")

    def retain_assignment_winner_exits(self, absent, at):
        for facts in self.session_activity_evidence.values():
            winner = next((source[len("cli:attests "):] for source in facts["source"]
                           if source.startswith("cli:attests ")), None)
            if winner in absent:
                facts.setdefault("_retainedAt", at)

    def retain_exited_activity(self, eligible_items, at):
        for facts in self.session_activity_evidence.values():
            owner = facts.get("_owner")
            if owner is not None and owner not in eligible_items:
                facts.setdefault("_retainedAt", at)

    def session_activity_facts(self, session, turn_facts, attest_facts=None):
        record = self.session_activity_evidence.get(session)
        facts = combined_evidence(turn_facts, *(fact for fact in (record, attest_facts)
                                               if fact is not None))
        facts["source"] = list(record["source"]) if record else []
        if record and "_retainedAt" in record:
            facts.update(status="unknown", freshness="unconfirmed", complete=False,
                         unconfirmedSince=record["_retainedAt"])
        return facts

    @staticmethod
    def item_fingerprint(row):
        digest = hashlib.blake2b(digest_size=16)
        for chunk in json.JSONEncoder(sort_keys=True, separators=(",", ":")).iterencode(row):
            digest.update(chunk.encode())
        return digest.hexdigest()

    def work_items(self):
        raw = self.inventory("work-items").rows
        if not self.older_item_sources_released:
            return raw
        # Preserve source order across current rows and folded older items.
        return {wid: raw[wid] if wid in raw else self.older_items[wid]["fields"]
                for wid in self._work_item_order if wid in raw or wid in self.older_items}

    def work_item_evidence(self, wid):
        inventory = self.inventory("work-items")
        if wid in inventory.rows or wid not in self.older_items:
            return inventory.evidence_for(wid)
        status, freshness, observed_at, source, complete = self.older_items[wid]["observation"]
        if inventory.stale or inventory.budget_stale:
            freshness = "stale"
        elif inventory.unconfirmed_since is not None:
            freshness = "unconfirmed"
        return dict(status=status, freshness=freshness, observedAt=observed_at,
                    source=source, complete=complete,
                    unconfirmedSince=inventory.unconfirmed_since)

    def same_work_item(self, wid, row):
        inventory = self.inventory("work-items")
        if wid in inventory.rows or wid not in self.older_items:
            return inventory.rows.get(wid) == row
        return self.older_items[wid]["fingerprint"] == self.item_fingerprint(row)

    def payload_work_item(self, row, *, at, source, policy):
        inventory = self.inventory("work-items")
        wid = str(row["id"])
        same = self.same_work_item(wid, row)
        older = self.older_items.get(wid) if wid not in inventory.rows else None
        if older is not None:
            version, previous = row.get("rowVersion"), older["fields"].get("rowVersion")
            if policy != "strict-version" or not isinstance(version, int) or not isinstance(previous, int):
                return False
            if version < previous:
                return True
            if version == previous:
                if not same:
                    inventory.invalidate()
                    return False
                older["observation"] = ("known", "live", at, source, True)
                inventory.data_revision += 1
                return True
        before = inventory.rows.get(wid)
        if not inventory.payload(row, at=at, source=source, policy=policy):
            return False
        # Inventory accepts an ignored older version without changing its row.
        if inventory.rows.get(wid) is before:
            return True
        self.older_items.pop(wid, None)
        if wid not in self._work_item_order:
            self._work_item_order.append(wid)
        if not same:
            self.details_revision += 1
        return True

    def fold_older_work_items(self):
        """Release older source rows while keeping the full board's inputs.

        Called after publication; a compact item is not a full detail response.
        """
        inventory = self.inventory("work-items")
        if not self.older_item_sources_released:
            self._work_item_order = list(inventory.rows)
        for wid, row in list(inventory.rows.items()):
            if row.get("state") not in {"closed", "iceboxed"}:
                continue
            fact = inventory.observations.get(wid, inventory.evidence())
            self.older_items[wid] = {
                "fields": {key: value for key, value in row.items() if key in ITEM_FACT_FIELDS},
                "fingerprint": self.item_fingerprint(row),
                "observation": tuple(fact.get(key) for key in
                                     ("status", "freshness", "observedAt", "source", "complete")),
            }
            del inventory.rows[wid]
            inventory.observations.pop(wid, None)
            self.older_item_sources_released = True

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
        if resource == "item-assignment-source":
            return {turn.get("assignmentId") for nested, session in self.inventories
                    if nested == "turns" for turn in self.summary(nested, session)["live"]}
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
        work = self.work_items()
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
        work = self.work_items()
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

    def release_session_histories(self, visible, *, at=None):
        if at is None:
            at = self.inventory("sessions").observed_at
        assignments = self.inventory("assignments").rows
        released = {key for key in self.inventories
                    if key[0] in {"turns", "wakes", "messages"} and key[1] not in visible}
        activity = {}
        for resource, session in released:
            if resource == "turns":
                self.release_turn_activity(session, at)
                for aid, activity_at in self.summary(resource, session)["assignmentActivity"].items():
                    wid = assignments.get(aid, {}).get("workItemId")
                    if wid:
                        activity[wid] = max(activity.get(wid, 0), activity_at)
        self.fold_retired_activity(activity)
        for key in released:
            del self.inventories[key]
            self.summaries.pop(key, None)
        # Closed-item author maxima already fold exactly the fresh attest
        # scope. Retirement changes rendering, not their source membership.
        # Keep these existing folds so complete replacements can lower/remove
        # a maximum without an additional contributor ledger or return scan.
        if released:
            self._item_activity_parts.pop("turns", None)
        return released

    def mark_unconfirmed(self, at):
        for inventory in self.inventories.values():
            inventory.mark_unconfirmed(at)
        for fact in self.item_assignment_facts.values():
            if fact["complete"] and fact.get("unconfirmedSince") is None:
                fact["unconfirmedSince"] = at
                if fact["freshness"] != "stale":
                    fact["freshness"] = "unconfirmed"
        facts = [part["facts"] for part in self.closed_items.values()]
        facts.extend(self.session_activity_evidence.values())
        if self.activity_refresh is not None:
            facts.extend(self.activity_refresh[1].values())
        for fact in facts:
            if fact["complete"] and fact.get("unconfirmedSince") is None:
                fact["unconfirmedSince"] = at
                if fact["freshness"] != "stale":
                    fact["freshness"] = "unconfirmed"

    def release_auxiliary_sources(self):
        self.item_contributions.retain_items(self.work_items())
        for (resource, _scope), inventory in self.inventories.items():
            if resource == "artifacts":
                inventory.release_rows()
            elif resource == "decision-requests":
                # The board uses scope completeness, not decision source text.
                inventory.rows.clear()
                inventory.observations.clear()

    def capture_assignment_contributions(self, *, items=None):
        """Capture current facts before releasing their construction rows."""
        published = self.work_items()
        self.item_contributions.retain_items(published)
        assignments = self.inventory("assignments")
        full_order = assignments.complete and not self.closed_sources_released
        for index, (aid, row) in enumerate(assignments.rows.items()):
            wid = row.get("workItemId")
            if (wid not in published or wid in self.item_assignment_pending
                    or (items is not None and wid not in items)):
                continue
            previous = self.item_contributions.entries.get(aid)
            # A complete source walk supplies the new order, including inserted
            # members. A released/partial inventory is only a subset and must
            # not renumber the remaining contributors around its missing rows.
            order = (index if full_order else previous.order if previous is not None
                     else self.open_assignment_order.get(aid, index))
            inventory = self.inventory("attests", aid)
            contribution = AssignmentContribution.capture(row, inventory, order)
            if previous is not None and "commitRefCorrections" not in row:
                # Membership/feed projections without correction history do
                # not establish that an existing correction ceased to apply.
                contribution = contribution._replace(corrections=previous.corrections)
            if previous is not None and not inventory.complete:
                # Reading assignment membership alone does not replace its
                # released attest contribution. An incomplete attest response
                # can mark it unconfirmed, but cannot establish absent facts.
                facts = previous.evidence
                if inventory.observed_at is not None:
                    facts = ContributionEvidence.capture(combined_evidence(
                        facts.publication(), inventory.evidence()))
                contribution = contribution._replace(
                    attests=previous.attests, evidence=facts,
                    detail_fingerprint=previous.detail_fingerprint)
            self.item_contributions.entries[aid] = contribution

    def fold_closed_assignments(self, *, items=None):
        """Fold a settled assignment read into per-item board facts."""
        assignments = self.inventory("assignments")
        self.capture_assignment_contributions(items=items)
        if ((items is None and not assignments.complete)
                or not any(row.get("state") != "open" for row in assignments.rows.values())):
            return set()
        grouped = {}
        item_positions = Counter()
        previous_order = self.open_assignment_order
        raw_closed_items = {row.get("workItemId") for row in assignments.rows.values()
                            if row.get("state") != "open"}
        carried_items = set()
        for aid, contribution in self.item_contributions.entries.items():
            wid = contribution.item
            if (aid not in assignments.rows and contribution.state != "open"
                    and wid in raw_closed_items and (items is None or wid in items)):
                grouped.setdefault(wid, ClosedAssignmentFold()).add_contribution(contribution)
                carried_items.add(wid)
        self.open_assignment_order = ({} if items is None else dict(previous_order))
        live_ids = {turn.get("assignmentId") for resource, scope in self.inventories
                    if resource == "turns" for turn in self.summary(resource, scope)["live"]}
        self.live_assignment_items = {aid: wid for aid, wid in self.live_assignment_items.items()
                                      if aid in live_ids}
        released = set()
        for order, (aid, row) in enumerate(assignments.rows.items()):
            wid = row.get("workItemId")
            contribution = self.item_contributions.entries.get(aid)
            if contribution is not None:
                order = contribution.order
            position = item_positions[wid]
            item_positions[wid] += 1
            if items is not None and wid not in items:
                continue
            if row.get("state") == "open":
                self.open_assignment_order[aid] = (order if wid in raw_closed_items else
                                                    previous_order.get(aid, order))
                continue
            self.fold_activity_inventory("attests", aid, releasing=True)
            if wid is not None:
                fold = grouped.setdefault(wid, ClosedAssignmentFold())
                fold.add(row, self.inventory("attests", aid), order=order, position=position)
            if aid in live_ids:
                self.live_assignment_items[aid] = wid
            released.add(aid)
        if not released:
            return set()
        for wid, fold in grouped.items():
            part = fold.finish()
            previous = self.closed_items.get(wid)
            if wid in carried_items and previous:
                # Only part of the raw source remains. Retain the last actual
                # detail observation, never a hash of the incomplete source.
                # An on-demand read checks it under the publication pin.
                part["detailFingerprint"] = previous["detailFingerprint"]
                part["assignmentFingerprint"] = previous["assignmentFingerprint"]
                part["facts"] = combined_evidence(previous["facts"], part["facts"])
                part["turnActivity"] = previous["turnActivity"]
                part["activity"] = latest_time(part["activity"], part["turnActivity"])
            elif (not part["detailsKnown"] and previous and
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
                    if wid is not None:
                        part = self.closed_items[wid]
                        part["turnActivity"] = latest_time(part["turnActivity"], linked[aid])
                        part["activity"] = latest_time(part["activity"], linked[aid])
                    del linked[aid]
        for aid in released:
            assignments.rows.pop(aid)
            assignments.observations.pop(aid, None)
            self.inventories.pop(("attests", aid), None)
            self.summaries.pop(("attests", aid), None)
        self.closed_revision += 1
        self.closed_items.pop(None, None)
        self.closed_sources_released = True
        self._item_activity_parts.clear()
        return {("attests", aid) for aid in released}

    def closed_item(self, wid, owned):
        # A fresh complete collection supplies replacement source rows. Until
        # folded, those rows are the contribution, not both them and its prior fold.
        return self.closed_items.get(wid) if not any(a.get("state") != "open" for a in owned) else None

    def update_closed_assignment_contribution(self, row):
        """Apply an ordered projection to a released, still-closed contributor.

        The caller owns source validation and read/notice ordering. A partial
        item replacement, a reopening, or a construction row still in flight
        uses its own integration path; none may silently replace held facts.
        Assignment projections do not establish new attest observations.
        """
        aid = str(row["id"])
        previous = self.item_contributions.entries.get(aid)
        if previous is None or previous.state == "open":
            return False
        wid = previous.item
        assignments = self.inventory("assignments")
        if (wid not in self.work_items() or wid in self.item_assignment_pending
                or aid in assignments.rows or row.get("state") != "closed"
                or row.get("workItemId") != wid
                or any(value.get("workItemId") == wid and value.get("state") != "open"
                       for value in assignments.rows.values())):
            return False
        part = self.closed_items.get(wid)
        if part is None:
            return False
        current = previous.assignment_update(row)
        if current == previous:
            return True
        self.item_contributions.entries[aid] = current
        fold = ClosedAssignmentFold()
        for contribution in self.item_contributions.entries.values():
            if contribution.item == wid and contribution.state != "open":
                fold.add_contribution(contribution)
        replacement = fold.finish()
        # No source text was read. Keep its last observed pin and any outage
        # affecting other contributors; changing assignment fields cannot
        # reconfirm their attest scopes. The next requested detail read checks
        # the actual source under the existing publication revision guard.
        replacement["detailFingerprint"] = part["detailFingerprint"]
        replacement["assignmentFingerprint"] = part["assignmentFingerprint"]
        replacement["facts"] = combined_evidence(part["facts"], replacement["facts"])
        replacement["turnActivity"] = part["turnActivity"]
        replacement["activity"] = latest_time(replacement["activity"], part["turnActivity"])
        self.closed_items[wid] = replacement
        self.closed_revision += 1
        self.details_revision += 1
        self._item_activity_parts.clear()
        return True

    def hold_item_assignments(self, wid):
        item = self.work_items().get(wid)
        if item is None:
            return False
        self.item_assignment_facts.setdefault(wid, self.inventory("assignments").evidence())
        if wid not in self.item_assignment_pending:
            # Only currently published assignment-derived summary values are
            # held. Titles, state and independent landing evidence stay live.
            self.item_assignment_summary[wid] = ({key: item[key] for key in (
                "holders", "started", "attestCounts", "verdictCounts", "effectiveCommitRefs")}
                if has_work_item_summary(item) else None)
        self.item_assignment_pending.add(wid)
        return True

    def assignment_evidence(self, wid):
        return self.item_assignment_facts.get(wid, self.inventory("assignments").evidence())

    def replace_item_assignments(self, wid, result):
        """Commit a complete item source on the model worker, never a subset."""
        if not result.complete or result.folded is None:
            raise ValueError("item assignment replacement must be complete")
        staged = result.folded
        self.item_contributions.replace_item(wid, staged.contributions, complete=True)
        self.retain_assignment_winner_exits(staged.absent_activity_winners, result.at)
        for author, timestamp in staged.session_activity.items():
            facts = staged.session_evidence[author]
            source = next(value for value in facts["source"] if value.startswith("cli:attests "))
            self.fold_session_activity(author, timestamp, "attest", source[len("cli:attests "):],
                                       facts, owner=wid)
        assignments = self.inventory("assignments")
        old = {aid for aid, row in assignments.rows.items() if row.get("workItemId") == wid}
        released = {("attests", aid) for aid in old - staged.open_rows.keys()}
        previous = self.closed_items.get(wid)
        turn_at = latest_time(previous.get("turnActivity") if previous else None, staged.activity)
        # Preserve turn contributions independently of corrected assignment
        # times. Retaining the old combined maximum would hide a correction.
        for resource, scope in tuple(self.inventories):
            if resource != "turns":
                continue
            linked = self.summary(resource, scope)["assignmentActivity"]
            for aid in old - staged.open_rows.keys():
                turn_at = latest_time(turn_at, linked.pop(aid, None))
        self.fold_retired_activity({wid: turn_at} if turn_at is not None else {})
        for aid in old:
            assignments.rows.pop(aid)
            assignments.observations.pop(aid, None)
            self.open_assignment_order.pop(aid, None)
            self.inventories.pop(("attests", aid), None)
            self.summaries.pop(("attests", aid), None)
        assignments.rows.update(staged.open_rows)
        facts = dict(self.assignment_evidence(wid), status="known", complete=True,
                     freshness="live", observedAt=result.at, unconfirmedSince=None)
        self.item_assignment_facts[wid] = facts
        assignments.observations.update((aid, dict(facts)) for aid in staged.open_rows)
        assignments.data_revision += 1
        self.open_assignment_order.update(staged.open_order)
        self.inventories.update((("attests", aid), inventory)
                                for aid, inventory in staged.open_attests.items())
        self.live_assignment_items = {aid: item for aid, item in self.live_assignment_items.items()
                                      if item != wid}
        self.live_assignment_items.update(staged.live_items)
        if staged.closed:
            if (not staged.closed["detailsKnown"] and previous and
                    previous["assignmentFingerprint"] == staged.closed["assignmentFingerprint"]):
                # Preserve an on-demand detail pin for summary-backed sources,
                # without making their attest histories background-eligible.
                staged.closed["detailFingerprint"] = previous["detailFingerprint"]
            staged.closed["turnActivity"] = turn_at
            staged.closed["activity"] = latest_time(staged.closed["activity"], turn_at)
            self.closed_items[wid] = staged.closed
        else:
            self.closed_items.pop(wid, None)
        self.item_assignment_pending.discard(wid)
        self.item_assignment_summary.pop(wid, None)
        self.closed_sources_released = True
        self.closed_revision += 1
        self.details_revision += 1
        self._item_activity_parts.clear()
        return released

    def accept(self, resource, scope, result, *, activity_eligible=True):
        inventory = self.inventory(resource, scope)
        held_closed = {}
        if resource == "assignments" and result.complete:
            present = {str(row["id"]) for row in result.rows}
            for aid, contribution in tuple(self.item_contributions.entries.items()):
                if aid not in present and contribution.item not in self.item_assignment_pending:
                    del self.item_contributions.entries[aid]
            winners = {source[len("cli:attests "):] for facts in self.session_activity_evidence.values()
                       for source in facts["source"] if source.startswith("cli:attests ")}
            self.retain_assignment_winner_exits(winners - present, result.at)
        if resource == "assignments" and result.complete and self.item_assignment_pending:
            # A global read cannot expose an intermediate replacement of an
            # item whose complete source is being assembled separately.
            held_closed = {wid: part for wid, part in self.closed_items.items()
                           if wid in self.item_assignment_pending}
            result.rows = [row for row in result.rows
                           if row.get("workItemId") not in self.item_assignment_pending]
            result.rows.extend(row for row in inventory.rows.values()
                               if row.get("workItemId") in self.item_assignment_pending)
        if isinstance(inventory, HistoryInventory):
            inventory.activity_eligible = activity_eligible
            fold = result.folded
            if fold is None:
                fold = HistoryFold(resource, result.at, self.current_ids(resource, scope))
                fold.add(result.rows)
            if resource == "attests" and inventory.fingerprint != fold.fingerprint.hexdigest():
                self.details_revision += 1
            inventory.read_fold(fold, complete=result.complete, at=result.at, source=result.source)
            self.fold_activity_inventory(resource, scope)
            return
        if resource in {"attests", "assignments", "work-items"}:
            incoming = {str(row[inventory.key]): row for row in result.rows}
            changed = (any(not self.same_work_item(k, row) for k, row in incoming.items())
                       if resource == "work-items" else
                       any(inventory.rows.get(k) != row for k, row in incoming.items()))
            keys = self.work_items().keys() if resource == "work-items" else inventory.rows.keys()
            if changed or (result.complete and keys != incoming.keys()):
                self.details_revision += 1
            if resource == "work-items":
                if result.complete:
                    self.older_items.clear()
                    self._work_item_order = list(incoming)
                    self.older_item_sources_released = False
                else:
                    self._work_item_order.extend(k for k in incoming
                                                 if k not in self._work_item_order)
                    for wid in incoming:
                        self.older_items.pop(wid, None)
        inventory.read(
            result.rows, complete=result.complete, at=result.at, source=result.source)
        if resource == "work-items":
            self.item_contributions.retain_items(self.work_items())
        if resource == "assignments" and result.complete:
            self.closed_sources_released = False
            closed = {row.get("workItemId") for row in result.rows if row.get("state") != "open"}
            self.closed_items = {wid: part for wid, part in self.closed_items.items() if wid in closed}
            self.closed_items.update(held_closed)
            self.item_assignment_facts = {wid: fact for wid, fact in self.item_assignment_facts.items()
                                          if wid in self.item_assignment_pending}
            self.live_assignment_items = {aid: wid for aid, wid in self.live_assignment_items.items()
                                          if wid in self.item_assignment_pending}
            self.open_assignment_order = {aid: order for aid, order in self.open_assignment_order.items()
                                          if inventory.rows.get(aid, {}).get("workItemId")
                                          in self.item_assignment_pending}
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
                changed = (not self.same_work_item(key, row) if resource == "work-items"
                           else inventory.rows.get(key) != row)
                if resource in {"attests", "assignments", "work-items"} and changed:
                    self.details_revision += 1
                if resource == "work-items":
                    self.older_items.pop(key, None)
                    if key not in self._work_item_order:
                        self._work_item_order.append(key)
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
        for fact in self.item_assignment_facts.values():
            fact["freshness"] = "stale"
        for part in self.closed_items.values():
            part["facts"]["freshness"] = "stale"
        for fact in self.session_activity_evidence.values():
            fact["freshness"] = "stale"

    def active_sessions(self):
        holders = {a.get("holderKey") for a in self.inventory("assignments").rows.values()
                   if a.get("state") == "open"}
        for item in self.work_items().values():
            summary = (self.item_assignment_summary.get(item["id"])
                       if item["id"] in self.item_assignment_pending else item)
            if summary is not None and (item["id"] in self.item_assignment_pending
                                        or has_work_item_summary(summary)):
                holders.update(summary["holders"])
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
                    turn_at = linked.pop(aid)
                    closed["turnActivity"] = latest_time(closed.get("turnActivity"), turn_at)
                    at = latest_time(closed["activity"], turn_at)
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
                versions["assignments"].append((key, inventory.data_revision,
                                                getattr(inventory, "activity_eligible", True)))
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
                    inventory = self.inventory("attests", assignment["id"])
                    record(assignment.get("workItemId"), assignment.get("openedAt"),
                           assignment.get("closedAt"),
                           self.summary("attests", assignment["id"])["activity"]
                           if getattr(inventory, "activity_eligible", True) else None)
                # Missing attest scopes are created by summary on first use.
                revision = [(key, inv.data_revision, getattr(inv, "activity_eligible", True))
                            for key, inv in self.inventories.items()
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

    def snapshot(self, now, *, assignment_source_unavailable=False):
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
        published = self.work_items().keys()
        self.item_assignment_pending.intersection_update(published)
        self.item_assignment_summary = {wid: summary for wid, summary in self.item_assignment_summary.items()
                                        if wid in self.item_assignment_pending}
        self.item_assignment_facts = {wid: fact for wid, fact in self.item_assignment_facts.items()
                                      if wid in published}
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
        for session in visible:
            key = session["sessionKey"]
            turn_evidence.append(self.inventory("turns", key).evidence())
            summary = self.summary("turns", key)
            turns.extend(summary["live"])
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
        # Temporary scalar reductions of already-retained scopes, not a
        # contributor ledger. Closed sources are folded only once complete;
        # open/partial sources keep their current warning until their retry.
        activity_scope_facts = {}
        for (resource, _scope), inventory in self.inventories.items():
            if resource != "attests" or not getattr(inventory, "activity_eligible", True):
                continue
            scope_facts = dict(inventory.evidence(), source=[])
            for author in inventory.folded["authors"]:
                if author not in ids:
                    continue
                previous = activity_scope_facts.get(author)
                activity_scope_facts[author] = combined_evidence(
                    *([previous] if previous is not None else []), scope_facts)
        for assignment in assignments.rows.values():
            wid = assignment.get("workItemId")
            summary = self.summary("attests", assignment["id"])
            for key, at in summary["authors"].items():
                authored_at[key][wid] = max(authored_at[key].get(wid, 0), at)
        for wid in self.closed_items:
            closed = self.closed_item(wid, by_item[wid])
            if closed is None:
                continue
            for author, at in closed["summary"]["authors"].items():
                authored_at[author][wid] = max(authored_at[author].get(wid, 0), at)

        # Source reads and eligible notices fold the fixed session records.
        # Item attachment facts above are not the source of idle publication.
        activity = self.session_activity
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
                    "idleFor": self.session_activity_facts(key, turn_inventory.evidence(),
                                                            activity_scope_facts.get(key)),
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
        for wid, item in self.work_items().items():
            assignment_facts = self.assignment_evidence(wid)
            held_summary = self.item_assignment_summary.get(wid)
            if held_summary is not None:
                item = dict(item, **held_summary)
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
            identity = self.work_item_evidence(wid)
            item_summary = item if has_work_item_summary(item) else None
            if wid in self.item_assignment_pending and held_summary is None:
                item_summary = None
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
                    assignment_facts["complete"]
                    and all(fact["complete"] and (fact["freshness"] == "live"
                                                 or assignment_source_unavailable)
                            for fact in inventory_facts)
                    and {k: v for k, v in detail_summary["kinds"].items() if k is not None}
                    == attest_counts
                    and dict(detail_summary["verdicts"]) == verdict_counts
                )
                review_details_fresh = (assignment_facts["freshness"] == "live"
                                        or assignment_source_unavailable)
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
                if assignment_source_unavailable and review_timing_needed:
                    # Unavailable is not new data. Complete retained timing
                    # still determines the last known stage; health stays stale.
                    stage_facts = dict(identity, freshness="stale")
                if not review_timing_known:
                    detail_facts = [assignment_facts, *inventory_facts]
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
                attest_facts = combined_evidence(assignment_facts, *inventory_facts)
                stage_facts = combined_evidence(identity, assignment_facts, attest_facts)
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
                             assignment_facts["complete"] and all(f["complete"] for f in inventory_facts))
            code = code_state(owned, producer_refs,
                              complete=assignment_facts["complete"] if item_summary is not None else complete)
            if folded_closed:
                if folded_closed["code"]:
                    code = True
                elif code is not True:
                    code = (False if (assignment_facts["complete"] if item_summary is not None else complete)
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
                        "freshness": ("live" if merged is not None and assignment_facts["freshness"] == "live"
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
                    "freshness": ("live" if merged is not None and assignment_facts["freshness"] == "live"
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
                started = (bool(owned or folded_closed) if assignment_facts["complete"] else None)
                summary_facts = dict(assignment_facts, status="unknown", complete=False)
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
                            holders if assignment_facts["complete"] else (holders or None)),
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
                    "open": sum(a["state"] == "open" for a in owned) if assignment_facts["complete"] else None,
                    "terminal": (sum(a["state"] != "open" for a in owned)
                                 + (folded_closed["count"] if folded_closed else 0)) if assignment_facts["complete"] else None,
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
                             "ring": ring_facts, "assignments": assignment_facts,
                             "summary": summary_facts,
                             "attests": attest_facts, "turns": item_turn_facts,
                             "merged": merge_facts, "code": stage_facts},
            })

        for item in items:
            if item["id"] in self.item_assignment_pending:
                item["commitRefsComplete"] = False
                for name in ("assignments", "attests", "stage", "ring", "code"):
                    item["evidence"][name] = dict(item["evidence"][name],
                                                 status="unknown", complete=False, freshness="stale")
        for agent in agents:
            if self.item_assignment_pending.intersection(authored_at[agent["sessionKey"]]):
                agent["evidence"]["idleFor"] = dict(agent["evidence"]["idleFor"],
                                                    status="unknown", complete=False, freshness="stale")

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
