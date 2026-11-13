"""Bounded background reads; the model worker alone consumes their results."""

from collections import OrderedDict, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import time

from atc_upstream import Read, now_ms


@dataclass
class ItemAssignmentSource:
    """A complete replacement: current rows and one item's closed facts only."""
    open_rows: dict = field(default_factory=dict)
    open_attests: dict = field(default_factory=dict)
    open_order: dict = field(default_factory=dict)
    live_items: dict = field(default_factory=dict)
    closed: dict | None = None
    activity: int | float | None = None


class ReadQueue:
    def __init__(self, gateway, events, workers, current_ids=None):
        self.gateway, self.events, self.workers = gateway, events, workers
        self.current_ids = current_ids or (lambda resource, scope: ())
        self.executor = ThreadPoolExecutor(max_workers=workers)
        self.pending = OrderedDict()
        self.active = {}
        # Epoch-invalidated reads must keep their original scheduling urgency.
        self.active_priorities = {}
        self.epochs = defaultdict(int)
        self.dispatched = 0
        self.epoch_discards = 0
        self.epoch_discards_by_resource = defaultdict(int)

    def touch(self, resource, scope):
        # A notice received during a read can describe a change after that
        # read's snapshot. Such a read must not overwrite the newer evidence.
        self.epochs[resource, scope] += 1

    def schedule(self, resource, scope=None, key=None, *, priority=False, active=False):
        job = resource, scope, key
        if self.active.get(job[:2]) == (job, self.epochs[job[:2]]):
            return
        self.pending[job] = max(2 if priority else 1 if active else 0,
                                self.pending.get(job, 0))
        if priority:
            self.pending.move_to_end(job, last=False)

    def pump(self):
        while len(self.active) < self.workers:
            eligible = [job for job in self.pending if job[:2] not in self.active]
            if not eligible:
                break
            # Notices go first, then active confirmations, then terminal
            # rotation. Every fourth dispatch admits reconciliation even under
            # sustained notices; active scopes take that slot before history.
            background = [job for job in eligible if self.pending[job] < 2]
            choices = background if background and self.dispatched % 4 == 3 else eligible
            job = max(choices, key=lambda candidate: self.pending[candidate])
            inventory = job[:2]
            priority = self.pending[job]
            del self.pending[job]
            self.dispatched += 1
            epoch = self.epochs[inventory]
            self.active[inventory] = job, epoch
            self.active_priorities[inventory] = priority
            future = self.executor.submit(self._read, job, self.current_ids(*job[:2]))
            future.add_done_callback(
                lambda done, job=job, epoch=epoch:
                None if done.cancelled() else
                self.events.put(("read", (job, epoch, done.result()))))

    def _read(self, job, keep_ids=()):
        resource, scope, key = job
        try:
            if resource == "retired-turns":
                return self._retired_activity(keep_ids)
            if resource == "item-assignment-source":
                return self._item_assignments(scope, keep_ids)
            if resource == "returning-authors":
                return self._returning_authors(keep_ids)
            if key is None:
                if hasattr(self.gateway, "read_current"):
                    return self.gateway.read_current(resource, scope, keep_ids=keep_ids)
                return self.gateway.read(resource, scope)
            return self.gateway.detail(resource, key, scope=scope)
        except Exception as error:
            # Exception bodies may contain credentials or upstream responses.
            return Read([], False, now_ms(), resource, type(error).__name__)

    def _item_assignments(self, wid, live_ids):
        from atc_evidence import HistoryInventory
        from atc_model import ClosedAssignmentFold
        from atc_summary import HistoryFold

        read_attests, visible = True, None
        if isinstance(live_ids, dict):
            visible = live_ids.get("visibleSessions")
            read_attests, live_ids = live_ids["attests"], live_ids["liveIds"]
        source = "item-assignment-source"
        membership = self.gateway.assignment_membership(wid, include_rows=True)
        if not membership.complete:
            return Read([], False, membership.at, source, membership.error)
        staged, closed = ItemAssignmentSource(), ClosedAssignmentFold()
        retired = {}
        at = membership.at
        for position, member in enumerate(membership.rows):
            aid = member["id"]
            detail = self.gateway.detail("assignments", aid)
            if not detail.complete:
                return Read([], False, detail.at, source, detail.error)
            if len(detail.rows) != 1:
                return Read([], False, detail.at, source, "assignment-membership-mismatch")
            row = detail.rows[0]
            if (row.get("id") != aid or row.get("workItemId") != wid
                    or not isinstance(row.get("commitRefCorrections"), list)):
                return Read([], False, detail.at, source, "assignment-detail-incomplete")
            # Keep the collection's published shape, including files. The
            # detail route adds reopenings/cannotProceed but omits files; its
            # shared fields must still describe the same membership revision.
            if any(row.get(key) != value for key, value in member.items() if key != "files"):
                return Read([], False, detail.at, source, "assignment-source-changed")
            row = member
            holder = row.get("holderKey")
            if visible is not None and holder and holder not in visible:
                retired.setdefault(holder, {})[aid] = wid
            if not read_attests:
                result = None
            elif hasattr(self.gateway, "read_current"):
                result = self.gateway.read_current("attests", aid)
            else:
                result = self.gateway.read("attests", aid)
            if result is not None and not result.complete:
                return Read([], False, result.at, source, result.error)
            inventory = HistoryInventory("attests")
            fold = None
            if result is not None:
                fold = result.folded
                if fold is None:
                    fold = HistoryFold("attests", result.at)
                    fold.add(result.rows)
                inventory.read_fold(fold, complete=True, at=result.at, source=result.source)
                at = result.at
            if row.get("state") == "open":
                staged.open_rows[aid] = row
                staged.open_attests[aid] = inventory
                staged.open_order[aid] = position
            else:
                closed.add(row, inventory, order=position, position=position)
            if aid in live_ids:
                staged.live_items[aid] = wid
            # Only current open sources survive the next member. Closed raw
            # assignment/attest text and temporary page folds are not staged.
            membership.rows[position] = None
            del row, member, detail, result, fold, inventory
        # The same existing warm-path turn history used by fresh full loads.
        # Only holders discovered in this item's complete membership qualify.
        from atc_summary import ItemActivityFold
        for session, mapping in retired.items():
            if hasattr(self.gateway, "read_item_activity"):
                result = self.gateway.read_item_activity(session, mapping)
                fold = result.folded
            else:
                result = self.gateway.read("turns", session)
                fold = ItemActivityFold(mapping)
                fold.add(result.rows)
            if not result.complete:
                return Read([], False, result.at, source, result.error)
            value = fold.activity.get(wid)
            if value is not None:
                staged.activity = max(staged.activity or 0, value)
            at = result.at
            del result, fold
        if closed.part["count"]:
            staged.closed = closed.finish()
        return Read([], True, at, source, folded=staged)

    def _retired_activity(self, plan):
        from atc_summary import ItemActivityFold
        marker, sessions, assignment_items = plan
        activity = {}
        at = now_ms()
        for session in sessions:
            if hasattr(self.gateway, "read_item_activity"):
                result = self.gateway.read_item_activity(session, assignment_items)
                folded = result.folded
            else:
                result = self.gateway.read("turns", session)
                folded = ItemActivityFold(assignment_items)
                folded.add(result.rows)
            for wid, timestamp in folded.activity.items():
                activity[wid] = max(activity.get(wid, 0), timestamp)
            at = result.at
            if not result.complete:
                return Read([], False, at, "turns", result.error, result.page,
                            (marker, activity))
            # No previous session's rows or page accumulator survive the next read.
            del result, folded
        return Read([], True, at, "turns", folded=(marker, activity))

    def _returning_authors(self, plan):
        from atc_evidence import HistoryInventory
        from atc_model import ClosedAssignmentFold
        from atc_summary import HistoryFold

        authors, excluded_items, live_ids = plan
        started, at = time.monotonic(), now_ms()
        if not authors:
            return Read([], True, at, "returning-authors", folded={
                "authors": set(), "items": {}, "sources": {}, "excludedItems": set(),
                "assignments": 0, "durationMs": 0})
        # This is the same complete assignment source used to select a fresh
        # load's attest scopes. Per-item membership omits null-item assignments.
        membership = self.gateway.read("assignments")
        if not membership.complete:
            return Read([], False, membership.at, "returning-authors", membership.error)
        sources, closed, positions, count = {}, {}, defaultdict(int), 0
        for order, row in enumerate(membership.rows):
            wid, aid = row.get("workItemId"), row["id"]
            if wid in excluded_items:
                membership.rows[order] = None
                continue
            position = positions[wid]
            positions[wid] += 1
            if hasattr(self.gateway, "read_current"):
                result = self.gateway.read_current("attests", aid)
            else:
                result = self.gateway.read("attests", aid)
            count += 1
            if not result.complete:
                return Read([], False, result.at, "returning-authors", result.error)
            at = result.at
            fold = result.folded
            if fold is None:
                fold = HistoryFold("attests", at)
                fold.add(result.rows)
            inventory = HistoryInventory("attests")
            inventory.read_fold(fold, complete=True, at=at, source=result.source)
            staged = sources.setdefault(wid, ItemAssignmentSource())
            if row.get("state") == "open":
                staged.open_rows[aid] = row
                staged.open_attests[aid] = inventory
                staged.open_order[aid] = order
            else:
                closed.setdefault(wid, ClosedAssignmentFold()).add(
                    row, inventory, order=order, position=position)
            if aid in live_ids:
                staged.live_items[aid] = wid
            membership.rows[order] = None
            del row, result, fold, inventory
        items = {}
        for wid, accumulator in closed.items():
            part = accumulator.finish()
            sources[wid].closed = part
            summary = part["summary"]
            selected = authors.intersection(summary["authorIds"])
            items[wid] = {"authors": {key: value for key, value in summary["authors"].items()
                                      if key in selected},
                          "authorIds": selected,
                          "authorFacts": {key: part["authorFacts"][key] for key in selected}}
        # Completed item folds can reconcile newer item activity atomically
        # without rereading any attest. No closed assignment rows are retained.
        return Read([], True, at, "returning-authors", folded={
            "authors": authors, "items": items, "sources": sources, "excludedItems": excluded_items,
            "assignments": count, "durationMs": (time.monotonic() - started) * 1000})

    def finish(self, job, epoch):
        inventory = job[:2]
        self.active.pop(inventory)
        priority = self.active_priorities.pop(inventory)
        if epoch != self.epochs[job[:2]]:
            self.epoch_discards += 1
            self.epoch_discards_by_resource[job[0]] += 1
            self.schedule(*job, priority=priority == 2, active=priority == 1)
            return False
        return True

    def close(self):
        self.executor.shutdown(wait=False, cancel_futures=True)
