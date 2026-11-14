"""Bounded background reads; the model worker alone consumes their results."""

from collections import OrderedDict, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from threading import Lock
import time

from atc_upstream import Read, now_ms
from atc_contributions import AssignmentContribution


@dataclass
class ItemAssignmentSource:
    """A complete replacement: current rows and one item's closed facts only."""
    open_rows: dict = field(default_factory=dict)
    open_attests: dict = field(default_factory=dict)
    open_order: dict = field(default_factory=dict)
    live_items: dict = field(default_factory=dict)
    closed: dict | None = None
    activity: int | float | None = None
    session_activity: dict = field(default_factory=dict)
    session_evidence: dict = field(default_factory=dict)
    absent_activity_winners: set = field(default_factory=set)
    contributions: dict = field(default_factory=dict)

    def replay_attests(self, notices):
        """Prepare a complete item stage for atomic replacement plus replay.

        Only the complete-read application path may call this, with eligible
        per-contribution buffers. The caller still owns flight boundaries and
        buffer disposal. No live model or source reads are touched here.
        """
        from atc_model import ClosedAssignmentFold, fold_session_activity_record, latest_time

        # Validate and reduce every buffer before changing any staged fields.
        # An absent member needs the assignment-notice path, not an invented
        # empty contribution inferred from an attest alone.
        changed = {}
        for aid, rows in notices.items():
            previous = self.contributions[aid]
            updated = previous.attests.replay_complete(rows)
            if updated is not previous.attests:
                changed[aid] = previous._replace(attests=updated)

        self.contributions.update(changed)
        closed_changed = False
        for aid, contribution in changed.items():
            if aid in self.open_attests:
                inventory = self.open_attests[aid]
                inventory.folded = contribution.attests.summary()
                inventory.attest_frontier = contribution.attests.frontier
                inventory.data_revision += 1
                # fingerprint still identifies the actual read, not the
                # replayed projection. No raw detail hash is fabricated.
            else:
                closed_changed = True
            for author, timestamp in contribution.attests.authors:
                fold_session_activity_record(self.session_activity, self.session_evidence,
                    author, timestamp, 'attest', aid, contribution.evidence.publication(),
                    owner=contribution.item)

        if closed_changed:
            original = self.closed
            fold = ClosedAssignmentFold()
            for contribution in self.contributions.values():
                if contribution.state != 'open':
                    fold.add_contribution(contribution)
            updated = fold.finish()
            # The original source pins are retained as pins, not claimed to
            # describe added notices. On-demand detail reconciliation owns
            # obtaining a new exact pin; the compact reduction stays unknown.
            updated['detailFingerprint'] = original['detailFingerprint']
            updated['assignmentFingerprint'] = original['assignmentFingerprint']
            updated['turnActivity'] = original['turnActivity']
            updated['activity'] = latest_time(updated['activity'], original['turnActivity'])
            self.closed = updated
        return bool(changed)


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
        self.attest_owner_attempts = 0
        self.attest_owner_counter_lock = Lock()
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
            if resource == "attest-owner":
                with self.attest_owner_counter_lock:
                    self.attest_owner_attempts += 1
                return self.gateway.detail("assignments", scope)
            if resource == "retired-turns":
                return self._retired_activity(keep_ids)
            if resource == "item-assignment-source":
                return self._item_assignments(scope, keep_ids)
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
        from atc_model import ClosedAssignmentFold, fold_session_activity_record
        from atc_summary import HistoryFold

        read_attests, visible = True, None
        activity_winners = set()
        if isinstance(live_ids, dict):
            activity_winners = set(live_ids.get("activityWinners", ()))
            visible = live_ids.get("visibleSessions")
            read_attests, live_ids = live_ids["attests"], live_ids["liveIds"]
        source = "item-assignment-source"
        membership = self.gateway.assignment_membership(wid, include_rows=True)
        if not membership.complete:
            return Read([], False, membership.at, source, membership.error)
        staged, closed = ItemAssignmentSource(), ClosedAssignmentFold()
        # Only already-published winners participate in this temporary check.
        # Never keep a membership ledger after the complete result is served.
        staged.absent_activity_winners = activity_winners
        retired = {}
        at = membership.at
        for position, member in enumerate(membership.rows):
            aid = member["id"]
            staged.absent_activity_winners.discard(aid)
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
                for author, timestamp in inventory.folded["authors"].items():
                    fold_session_activity_record(staged.session_activity, staged.session_evidence,
                        author, timestamp, "attest", aid, inventory.evidence(), owner=wid)
                at = result.at
            staged.contributions[aid] = AssignmentContribution.capture(row, inventory, position)
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
