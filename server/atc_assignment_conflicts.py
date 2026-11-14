"""Transient mutable-assignment conflicts, with no source ordering guess."""

import hashlib
import json
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
    different_projection_seen: bool = False


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

    def observe(self, notice):
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
            self.notices[aid] = _NoticeState(row['workItemId'], signature)
        elif signature != previous.first_projection and not previous.different_projection_seen:
            self.notices[aid] = previous._replace(different_projection_seen=True)
        return True

    def conflicting_rows(self, rows, *, complete):
        """Return conflicting present IDs, or None for an unusable partial read.

        This does not interpret missing membership or resolve existing holds.
        The source consumer owns scope/absence and ordinary creation handling.
        No returned row is mutated, selected or replayed by this comparison.
        """
        if self.closed:
            raise ValueError('assignment read attempt already disposed')
        if not complete:
            return None
        conflicts = set()
        for row in rows:
            aid = str(row['id'])
            notice = self.notices.get(aid)
            if notice is not None and (notice.different_projection_seen
                                       or notice.first_projection != _projection(row)):
                conflicts.add(aid)
        return conflicts

    def discard(self):
        self.notices.clear()
        self.closed = True


class _HeldAssignment(NamedTuple):
    item: str
    since: int | float


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

    def hold(self, assignment, item, published_item, *, at):
        if published_item is None or published_item.get('id') != item:
            raise ValueError('hold requires the actual published item')
        previous = self.assignments.get(assignment)
        if previous is not None:
            if previous.item != item:
                raise ValueError('unresolved assignment changed item scope')
            return False  # A repeat conflict cannot renew or confirm the hold.
        self.assignments[assignment] = _HeldAssignment(item, at)
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

    def retain_items(self, items):
        for assignment, held in tuple(self.assignments.items()):
            if held.item not in items:
                self.resolve(assignment)

    def since(self, item):
        return min((held.since for held in self.assignments.values() if held.item == item),
                   default=None)
