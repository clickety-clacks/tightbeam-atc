"""Bounded background reads; the model worker alone consumes their results."""

from collections import OrderedDict, defaultdict
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from threading import Lock
import time

from atc_upstream import Read, now_ms
from atc_contributions import AssignmentContribution, AttestContribution
from atc_assignment_conflicts import AssignmentReadRegistry, assignment_projection_fingerprint


def replay_attest_read(result, notices):
    """Replay onto a complete scoped result without fabricating a raw pin."""
    from atc_summary import HistoryFold
    if not result.complete:
        raise ValueError('cannot replace from incomplete attest read')
    fold = result.folded
    if fold is None:
        fold = HistoryFold('attests', result.at)
        fold.add(result.rows)
    updated = AttestContribution.capture(fold.summary, fold.attest_frontier).replay_complete(notices)
    fold.summary = updated.summary()
    fold.attest_frontier = updated.frontier
    # Keep the actual read fingerprint/rows. The model's normal read revision
    # guards detail responses; replayed notices are not invented source rows.
    result.folded = fold


class AttestReadFlight:
    """One attempt's protected snapshots through local atomic application.

    The model worker owns notice delivery and application. The read worker
    opens each contribution immediately before its snapshot, after all turns
    have been folded and discarded. This lock serializes those boundaries;
    no upstream operation runs while it is held.
    """
    def __init__(self, owner=()):
        self.lock = Lock()
        # Dispatch-time ownership only, for dropping a delayed scoped result
        # after its current contribution leaves. () means not established;
        # (None,) is a known null-item source. No completed-flight ledger.
        self.owner = owner
        self.buffers = {}
        self.opened_at = {}
        self.read_completed_at = {}
        self.closed = False
        self.notice_count = 0
        self.peak_notice_count = 0
        self.maximum_lifetime = 0.0
        self.maximum_read_duration = 0.0

    def open(self, aid):
        with self.lock:
            if self.closed or aid in self.buffers:
                raise ValueError('attest protection opened outside a new snapshot')
            self.buffers[aid] = []
            self.opened_at[aid] = time.monotonic()

    def notice(self, aid, row):
        with self.lock:
            if self.closed or aid not in self.buffers:
                return False
            # Replay needs the normal contribution fields, not notes, bodies
            # or a second copy of the transport envelope. References retain
            # their existing shape and cannot alias the caller's payload.
            self.buffers[aid].append({key: deepcopy(row.get(key)) for key in (
                'id', 'assignmentId', 'kind', 'verdictKind', 'bySession', 'ts', 'commitRefs')})
            self.notice_count += 1
            self.peak_notice_count = max(self.peak_notice_count, self.notice_count)
            return True

    def read_completed(self, aid):
        with self.lock:
            if not self.closed:
                self.read_completed_at[aid] = time.monotonic()
                self.maximum_read_duration = max(self.maximum_read_duration,
                    self.read_completed_at[aid] - self.opened_at[aid])
            # Keep protection while other members or local application wait.

    def apply(self, consume):
        with self.lock:
            if self.closed:
                raise ValueError('attest protection already disposed')
            try:
                return consume(self.buffers)
            finally:
                self._discard()

    def discard(self):
        with self.lock:
            self._discard()

    def _discard(self):
        if self.opened_at:
            self.maximum_lifetime = max(self.maximum_lifetime,
                                       time.monotonic() - min(self.opened_at.values()))
        self.buffers.clear()
        self.opened_at.clear()
        self.read_completed_at.clear()
        self.owner = ()
        self.notice_count = 0
        self.closed = True


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
    # Transient complete-read equality only. Closed source rows are discarded
    # before result delivery; compact published fields cannot reconstruct their
    # shared projection. This map is not installed in the live model.
    assignment_fingerprints: dict = field(default_factory=dict)

    def preserve_assignments(self, model, protected):
        """Keep conflicted mutable projections while retaining staged attests.

        Call after complete snapshot replay and before atomic replacement.
        Validate every member before changing the stage. This method selects
        no winner, changes no live model, and retains no mutable notice history.
        """
        from atc_evidence import HistoryInventory
        from atc_model import ClosedAssignmentFold, latest_time

        changed, rows, inventories = {}, {}, {}
        assignments = model.inventory('assignments').rows
        for aid in protected:
            incoming = self.contributions.get(aid)
            current = model.item_contributions.entries.get(aid)
            if incoming is None or current is None or incoming.item != current.item:
                raise ValueError('item conflict requires an established present contributor')
            if current.state not in {'open', 'closed'}:
                raise ValueError('item conflict has no supported current assignment state')
            changed[aid] = current._replace(order=incoming.order,
                attests=incoming.attests, evidence=incoming.evidence,
                detail_fingerprint=incoming.detail_fingerprint)
            if current.state == 'open':
                row = assignments.get(aid)
                if row is None or row.get('state') != 'open' or row.get('workItemId') != current.item:
                    raise ValueError('open item conflict has no current open projection')
                rows[aid] = deepcopy(row)
                inventory = self.open_attests.get(aid)
                if inventory is None:
                    # The complete snapshot folded this now-open member as
                    # closed. Restore its *new snapshot/replay* fold and actual
                    # attest pin, not the older warm count or discarded rows.
                    inventory = HistoryInventory('attests')
                    inventory.folded = incoming.attests.summary()
                    inventory.attest_frontier = incoming.attests.frontier
                    inventory.fingerprint = incoming.detail_fingerprint
                    facts = incoming.evidence.publication()
                    inventory.complete = facts['complete']
                    inventory.stale = facts['freshness'] == 'stale'
                    inventory.observed_at = facts['observedAt']
                    inventory.confirmed_at = facts['observedAt'] if facts['complete'] else None
                    inventory.unconfirmed_since = facts.get('unconfirmedSince')
                    inventory.source = facts['source']
                    inventory.data_revision += 1
                inventories[aid] = inventory
        if not changed:
            return False
        wid = next(iter(changed.values())).item
        if any(value.item != wid for value in self.contributions.values()):
            raise ValueError('item conflict stage contains another item')
        # The fold below is a mixed projection, not a new raw-source hash.
        original = model.closed_items.get(wid) or self.closed
        has_closed = any(changed.get(aid, value).state != 'open'
                         for aid, value in self.contributions.items())
        if has_closed and original is None:
            raise ValueError('item conflict has no actual closed-source pin')

        self.contributions.update(changed)
        for aid, value in changed.items():
            if value.state == 'open':
                self.open_rows[aid] = rows[aid]
                self.open_attests[aid] = inventories[aid]
                self.open_order[aid] = value.order
            else:
                self.open_rows.pop(aid, None)
                self.open_attests.pop(aid, None)
                self.open_order.pop(aid, None)
            if aid in model.live_assignment_items:
                self.live_items[aid] = model.live_assignment_items[aid]
            else:
                self.live_items.pop(aid, None)
        self.open_rows = dict(sorted(self.open_rows.items(),
                                     key=lambda pair: self.open_order[pair[0]]))
        if has_closed:
            fold = ClosedAssignmentFold()
            for value in self.contributions.values():
                if value.state != 'open':
                    fold.add_contribution(value)
            closed = fold.finish()
            closed['detailFingerprint'] = original['detailFingerprint']
            closed['assignmentFingerprint'] = original['assignmentFingerprint']
            closed['turnActivity'] = original['turnActivity']
            closed['activity'] = latest_time(closed['activity'], original['turnActivity'])
            self.closed = closed
        else:
            self.closed = None
        return True

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
        # Model-worker owned registry; each flight synchronizes its own read
        # worker boundary. Results queued for application still own a flight.
        self.item_flights = {}
        self.attest_flights = {}
        self.assignment_reads = AssignmentReadRegistry()
        self.flight_peak_notices = 0
        self.flight_maximum_lifetime = 0.0
        self.flight_maximum_read_duration = 0.0
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
            plan = self.current_ids(*job[:2])
            self.assignment_reads.start(job)
            if job[0] == 'item-assignment-source':
                flight = AttestReadFlight()
                self.item_flights[job[1]] = flight
                future = self.executor.submit(self._read, job, plan, flight)
            elif job[0] == 'attests' and job[1] is not None and job[2] is None:
                owner = plan.get('owner', ()) if isinstance(plan, dict) else ()
                if isinstance(plan, dict):
                    plan = plan['keepIds']
                flight = AttestReadFlight(owner)
                self.attest_flights[job[1]] = flight
                future = self.executor.submit(self._read, job, plan, flight)
            else:
                future = self.executor.submit(self._read, job, plan)
            future.add_done_callback(
                lambda done, job=job, epoch=epoch:
                None if done.cancelled() else
                self.events.put(("read", (job, epoch, done.result()))))

    def _read(self, job, keep_ids=(), flight=None):
        resource, scope, key = job
        try:
            if resource == "attest-owner":
                with self.attest_owner_counter_lock:
                    self.attest_owner_attempts += 1
                return self.gateway.detail("assignments", scope)
            if resource == "retired-turns":
                return self._retired_activity(keep_ids)
            if resource == "item-assignment-source":
                return self._item_assignments(scope, keep_ids, flight)
            if key is None:
                if flight is not None:
                    flight.open(scope)
                if hasattr(self.gateway, "read_current"):
                    result = self.gateway.read_current(resource, scope, keep_ids=keep_ids)
                else:
                    result = self.gateway.read(resource, scope)
                if flight is not None:
                    flight.read_completed(scope)
                return result
            return self.gateway.detail(resource, key, scope=scope)
        except Exception as error:
            # Exception bodies may contain credentials or upstream responses.
            return Read([], False, now_ms(), resource, type(error).__name__)

    def _item_assignments(self, wid, live_ids, flight=None):
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
        # Validate the same membership/details before reading its selected
        # retired holders. Keep only the existing membership projection;
        # detail-only rows do not survive to the protected attest portion.
        for member in membership.rows:
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
            staged.assignment_fingerprints[str(aid)] = assignment_projection_fingerprint(member)
            row = member
            holder = row.get("holderKey")
            if visible is not None and holder and holder not in visible:
                retired.setdefault(holder, {})[aid] = wid
            del row, member, detail
        # R28: complete the existing turn portion before any attest snapshot
        # can open protection. A growing turn tail must not retain notices
        # belonging to an earlier completed attest snapshot.
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
        retired.clear()
        for position, member in enumerate(membership.rows):
            aid, row = member["id"], member
            if read_attests and flight is not None:
                flight.open(aid)
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
            del row, member, result, fold, inventory
            if read_attests and flight is not None:
                flight.read_completed(aid)
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
        flight = self.attest_flights.get(job[1]) if job[0] == 'attests' else None
        if flight is not None and flight.closed:
            # Scope exit already released protection. The consumer discards
            # this result; an obsolete epoch must not reschedule an exit.
            return True
        if epoch != self.epochs[job[:2]]:
            self.assignment_reads.discard(job)
            if job[0] == 'item-assignment-source':
                self.discard_item_flight(job[1])
            elif job[0] == 'attests':
                self.discard_attest_flight(job[1])
            self.epoch_discards += 1
            self.epoch_discards_by_resource[job[0]] += 1
            self.schedule(*job, priority=priority == 2, active=priority == 1)
            return False
        return True

    def discard_item_flight(self, wid):
        self.assignment_reads.discard(('item-assignment-source', wid, None))
        self._discard_flight(self.item_flights.pop(wid, None))

    def discard_attest_flight(self, aid):
        self._discard_flight(self.attest_flights.pop(aid, None))

    def _discard_flight(self, flight):
        if flight is not None:
            flight.discard()
            # Bounded maxima only; no completed-attempt history is retained.
            self.flight_peak_notices = max(self.flight_peak_notices, flight.peak_notice_count)
            self.flight_maximum_lifetime = max(self.flight_maximum_lifetime, flight.maximum_lifetime)
            self.flight_maximum_read_duration = max(
                self.flight_maximum_read_duration, flight.maximum_read_duration)

    def close(self):
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.assignment_reads.close()
        for wid in tuple(self.item_flights):
            self.discard_item_flight(wid)
        for aid in tuple(self.attest_flights):
            self.discard_attest_flight(aid)
