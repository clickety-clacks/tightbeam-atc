"""Transient mutable-assignment conflicts, with no source ordering guess."""

import hashlib
import json
from threading import RLock
from typing import NamedTuple

from atc_payloads import ASSIGNMENT_FIELDS


# Both routes expose these fields. Derived status and natural rowVersion are
# not a reopening generation; the collection also has detail-only corrections.
# Hash the shared projection so a flight retains no subject, file list or
# revocation prose. This fingerprint establishes equality, never precedence.
_SHARED_FIELDS = tuple(sorted(ASSIGNMENT_FIELDS - {'derivedStatus', 'rowVersion'}))


def _projection(row):
    values = [row.get(key) for key in _SHARED_FIELDS]
    return hashlib.sha256(json.dumps(values, sort_keys=True,
                                    separators=(',', ':')).encode('utf-8')).digest()


class _NoticeState(NamedTuple):
    item: str | None
    first_projection: bytes
    first_delivery: int
    different_delivery: int | None = None


class AssignmentReadConflicts:
    """One read attempt's close/reopen equality summary.

    The caller serializes dispatch, queued/delivered notice admission and read
    application. There is one fixed tuple per affected assignment, not an event
    list. Two distinct projections suffice to prove that any single returned
    row conflicts with at least one notice; keeping only the latest would lose
    that fact. Discard this attempt on success, failure or cancellation.
    """
    __slots__ = ('notices', 'closed')

    def __init__(self):
        self.notices = {}
        self.closed = False

    def observe(self, notice, *, delivery=0):
        if self.closed:
            return False
        row, refs = notice.get('payload') or {}, notice.get('refs') or {}
        if (notice.get('resource') != 'assignments' or notice.get('op') != 'upsert'
                or notice.get('class') not in {'assignment.closed', 'assignment.reopened'}
                or not ASSIGNMENT_FIELDS <= row.keys()
                or row.get('id') is None or refs.get('assignmentId') != row['id']):
            return False
        aid = str(row['id'])
        signature = _projection(row)
        previous = self.notices.get(aid)
        if previous is None:
            self.notices[aid] = _NoticeState(row['workItemId'], signature, delivery)
        elif signature != previous.first_projection and previous.different_delivery is None:
            self.notices[aid] = previous._replace(different_delivery=delivery)
        return True

    def conflicting_rows(self, rows, *, complete):
        """Return conflicting present IDs, or None for an unusable partial read.

        This does not interpret missing membership or resolve existing holds.
        The source consumer owns scope/absence and ordinary creation handling.
        No returned row is mutated, selected or replayed by this comparison.
        """
        conflicts = self.conflict_deliveries(rows, complete=complete)
        return None if conflicts is None else set(conflicts)

    def conflict_deliveries(self, rows, *, complete):
        """Identify the first conflicting local delivery, not a source version."""
        if self.closed:
            raise ValueError('assignment read attempt already disposed')
        if not complete:
            return None
        conflicts = {}
        for row in rows:
            aid = str(row['id'])
            notice = self.notices.get(aid)
            if notice is None:
                continue
            if notice.first_projection != _projection(row):
                conflicts[aid] = notice.first_delivery
            elif notice.different_delivery is not None:
                conflicts[aid] = notice.different_delivery
        return conflicts

    def discard(self):
        self.notices.clear()
        self.closed = True


class _HeldAssignment(NamedTuple):
    item: str
    since: int | float
    after_delivery: int


class AssignmentHolds:
    """Current unresolved conditions and one actual publication per item.

    Publication frames are immutable after commit. Share their item object
    until resolution rather than copying one frame per conflicting assignment.
    The publisher must copy before adding the existing unconfirmed marking.
    There is no completed-hold history, timer or read scheduler here.
    """
    __slots__ = ('assignments', 'items')

    def __init__(self):
        self.assignments = {}
        self.items = {}

    def hold(self, assignment, item, published_item, *, at, after_delivery=0):
        if published_item is None or published_item.get('id') != item:
            raise ValueError('hold requires the actual published item')
        previous = self.assignments.get(assignment)
        if previous is not None:
            if previous.item != item:
                raise ValueError('unresolved assignment changed item scope')
            # A repeat conflict preserves the original publication/time, but
            # its establishing delivery is not a subsequent resolver either.
            self.assignments[assignment] = previous._replace(
                after_delivery=max(previous.after_delivery, after_delivery))
            return False
        self.assignments[assignment] = _HeldAssignment(item, at, after_delivery)
        self.items.setdefault(item, published_item)
        return True

    def resolve(self, assignment):
        """Caller has accepted a next notice or a qualifying complete read."""
        previous = self.assignments.pop(assignment, None)
        if previous is None:
            return False
        if not any(value.item == previous.item for value in self.assignments.values()):
            self.items.pop(previous.item)
        return True

    def resolve_notice(self, assignment, delivery):
        previous = self.assignments.get(assignment)
        if previous is None or delivery <= previous.after_delivery:
            return False
        return self.resolve(assignment)

    def retain_items(self, items):
        for assignment, held in tuple(self.assignments.items()):
            if held.item not in items:
                self.resolve(assignment)

    def since(self, item):
        return min((held.since for held in self.assignments.values() if held.item == item),
                   default=None)


class AssignmentReadRegistry:
    """Dispatch-to-consumption ownership, shared with feed enqueue.

    The local delivery ordinal only distinguishes an establishing notice from
    the next consumed notice. It cannot order a source snapshot. Admission and
    queue insertion are one operation; consume holds this lock through the
    model callback so later deliveries belong to the next application boundary.
    No upstream operation runs under this lock.
    """
    __slots__ = ('lock', 'flights', 'delivery', 'closed')

    def __init__(self):
        self.lock = RLock()
        self.flights = {}
        self.delivery = 0
        self.closed = False

    def start(self, job):
        if job[0] not in {'assignments', 'item-assignment-source'}:
            return
        with self.lock:
            if self.closed or job in self.flights:
                raise ValueError('assignment read outside a new attempt')
            self.flights[job] = AssignmentReadConflicts()

    def admit(self, notice, enqueue=None):
        with self.lock:
            row, refs = notice.get('payload') or {}, notice.get('refs') or {}
            valid = (not self.closed and notice.get('resource') == 'assignments'
                and notice.get('op') == 'upsert'
                and notice.get('class') in {
                    'assignment.opened', 'assignment.closed', 'assignment.reopened'}
                and ASSIGNMENT_FIELDS <= row.keys() and row.get('id') is not None
                and refs.get('assignmentId') == row['id'])
            delivery = None
            if valid:
                self.delivery += 1
                delivery = self.delivery
                for (resource, scope, key), flight in self.flights.items():
                    if ((resource == 'item-assignment-source' and row['workItemId'] != scope)
                            or (resource == 'assignments' and key is not None
                                and str(row['id']) != str(key))):
                        continue
                    flight.observe(notice, delivery=delivery)
            if enqueue is not None:
                enqueue(delivery)
            return delivery

    def consume(self, job, apply):
        with self.lock:
            flight = self.flights.get(job)
            try:
                return apply(flight)
            finally:
                self.discard(job)

    def discard(self, job):
        with self.lock:
            flight = self.flights.pop(job, None)
            if flight is not None:
                flight.discard()

    def close(self):
        with self.lock:
            self.closed = True
            for job in tuple(self.flights):
                self.discard(job)
