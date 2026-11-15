"""Transient mutable-assignment conflicts, with no source ordering guess."""

import hashlib
import json
from copy import deepcopy
from threading import RLock
from typing import NamedTuple

from atc_payloads import ASSIGNMENT_FIELDS


# Both routes expose these fields. Derived status and natural rowVersion are
# not a reopening generation; the collection also has detail-only corrections.
# Hash the shared projection so a flight retains no subject, file list or
# revocation prose. This fingerprint establishes equality, never precedence.
_SHARED_FIELDS = tuple(sorted(ASSIGNMENT_FIELDS - {'derivedStatus', 'rowVersion'}))
_DETAIL_SHARED_FIELDS = tuple(key for key in _SHARED_FIELDS if key != 'files')


def assignment_projection_fingerprint(row, *, detail=False):
    # The existing assignment-get transport omits files. Compare its actual
    # common fields with a notice; never fill a missing source field by guess.
    fields = _DETAIL_SHARED_FIELDS if detail else _SHARED_FIELDS
    values = [row.get(key) for key in fields]
    return hashlib.sha256(json.dumps(values, sort_keys=True,
                                    separators=(',', ':')).encode('utf-8')).digest()


class _NoticeState(NamedTuple):
    item: str | None
    first_projection: bytes
    first_delivery: int
    different_delivery: int | None = None


class AssignmentReadConflicts:
    """One attempt's close/reopen equality and delivered creation identities.

    The caller serializes dispatch, queued/delivered notice admission and read
    application. There is one fixed tuple per affected assignment, not an event
    list. Two distinct projections suffice to prove that any single returned
    row conflicts with at least one notice; keeping only the latest would lose
    that fact. Creation identity protects only a current member missing from
    the snapshot. Discard both maps on success, failure or cancellation.
    """
    __slots__ = ('notices', 'creations', 'closed', 'detail')

    def __init__(self, *, detail=False):
        self.notices = {}
        self.creations = {}
        self.closed = False
        self.detail = detail

    def observe(self, notice, *, delivery=0):
        if self.closed:
            return False
        row, refs = notice.get('payload') or {}, notice.get('refs') or {}
        if (notice.get('resource') != 'assignments' or notice.get('op') != 'upsert'
                or notice.get('class') not in {
                    'assignment.opened', 'assignment.closed', 'assignment.reopened'}
                or not ASSIGNMENT_FIELDS <= row.keys()
                or row.get('id') is None or refs.get('assignmentId') != row['id']):
            return False
        aid = str(row['id'])
        if notice['class'] == 'assignment.opened':
            # Pinned assign/dispatch emit this only for creation, not their
            # idempotent replay. Keep identity/delivery only for this attempt.
            # This is not a version for subsequent close/reopen projections.
            self.creations.setdefault(aid, delivery)
            return True
        signature = assignment_projection_fingerprint(row, detail=self.detail)
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
        return self.conflict_fingerprints(
            ((str(row['id']), assignment_projection_fingerprint(row, detail=self.detail)) for row in rows),
            complete=complete)

    def conflict_fingerprints(self, fingerprints, *, complete):
        """Compare actual read projections after closed raw rows are discarded."""
        if self.closed:
            raise ValueError('assignment read attempt already disposed')
        if not complete:
            return None
        conflicts = {}
        for aid, signature in fingerprints:
            notice = self.notices.get(aid)
            if notice is None:
                continue
            if notice.first_projection != signature:
                conflicts[aid] = notice.first_delivery
            elif notice.different_delivery is not None:
                conflicts[aid] = notice.different_delivery
        return conflicts

    def created_after_snapshot(self, present, current, *, consumed):
        """Current delivered creations absent from this complete snapshot.

        A queued, unapplied creation cannot supply a current contribution.
        The caller handles it in feed order after result application. Ordinary
        absent IDs without a creation in this attempt still follow removal.
        """
        if self.closed:
            raise ValueError('assignment read attempt already disposed')
        return {aid for aid, delivery in self.creations.items()
                if delivery <= consumed and aid not in present and aid in current}

    def discard(self):
        self.notices.clear()
        self.creations.clear()
        self.closed = True


class _HeldAssignment(NamedTuple):
    item: str
    since: int | float
    after_delivery: int
    conflict: bool = True
    incomplete_contribution: bool = False
    producer_recovery: bool = False
    producer: str | None = None


class AssignmentHolds:
    """Current unresolved conditions and one actual publication per item.

    Publication frames are immutable after commit. Share their item object
    until resolution rather than copying one frame per conflicting assignment.
    The publisher must copy before adding the existing unconfirmed marking.
    None records prior absence, never a synthetic published item. There is
    no completed-hold history, timer or read scheduler here.
    """
    __slots__ = ('assignments', 'items')

    def __init__(self):
        self.assignments = {}
        self.items = {}

    def hold(self, assignment, item, published_item, *, at, after_delivery=0,
             incomplete_contribution=False, producer_recovery=False, producer=None):
        if published_item is not None and published_item.get('id') != item:
            raise ValueError('hold requires the actual published item')
        previous = self.assignments.get(assignment)
        if previous is not None:
            if previous.item != item:
                raise ValueError('unresolved assignment changed item scope')
            # A repeat conflict preserves the original publication/time, but
            # its establishing delivery is not a subsequent resolver either.
            self.assignments[assignment] = previous._replace(
                after_delivery=max(previous.after_delivery, after_delivery),
                conflict=previous.conflict or not (incomplete_contribution or producer_recovery),
                incomplete_contribution=previous.incomplete_contribution or incomplete_contribution,
                producer_recovery=previous.producer_recovery or producer_recovery,
                producer=producer if producer_recovery else previous.producer)
            return False
        self.assignments[assignment] = _HeldAssignment(
            item, at, after_delivery, not (incomplete_contribution or producer_recovery),
            incomplete_contribution, producer_recovery, producer if producer_recovery else None)
        self.items.setdefault(item, published_item)
        return True

    def resolve(self, assignment):
        """Resolve mutable ordering only; membership is not a complete contribution."""
        return self._resolve(assignment, conflict=True)

    def resolve_contribution(self, assignment):
        """Caller has complete contribution facts, not just an ownership row."""
        return self._resolve(assignment, contribution=True)

    def resolve_producer(self, assignment):
        """Caller committed the identified producer or a complete covering read.

        An ordinary assignment notice and contribution confirmation are not
        proof that the hidden reviewed-producer state was recovered (R36).
        Unknown identity remains a hold, not a request to discover an owner.
        """
        return self._resolve(assignment, producer=True)

    def _resolve(self, assignment, *, conflict=False, contribution=False, producer=False):
        previous = self.assignments.get(assignment)
        if previous is None:
            return False
        remaining = previous._replace(
            conflict=previous.conflict and not conflict,
            incomplete_contribution=previous.incomplete_contribution and not contribution,
            producer_recovery=previous.producer_recovery and not producer,
            producer=None if producer else previous.producer)
        if remaining == previous:
            return False
        if remaining.conflict or remaining.incomplete_contribution or remaining.producer_recovery:
            self.assignments[assignment] = remaining
            return True
        del self.assignments[assignment]
        if not self.assignments:
            self.items.clear()
        elif not any(value.producer_recovery for value in self.assignments.values()):
            held_items = {value.item for value in self.assignments.values()}
            self.items = {item: prior for item, prior in self.items.items() if item in held_items}
        # While another P recovery remains, its item can equal this A's item.
        # Publication derives those owners from current source and prunes the
        # shared prior frames atomically; no persistent owner map is needed.
        return True

    def resolve_notice(self, assignment, delivery):
        previous = self.assignments.get(assignment)
        if previous is None or not previous.conflict or delivery <= previous.after_delivery:
            return False
        return self.resolve(assignment)

    def retain_items(self, items):
        for assignment, held in tuple(self.assignments.items()):
            if held.item not in items:
                self._resolve(assignment, conflict=True, contribution=True, producer=True)

    def since(self, item, *, producer_items=None):
        producer_items = producer_items or {}
        return min((held.since for aid, held in self.assignments.items()
                    if held.item == item or producer_items.get(aid) == item),
                   default=None)

    def published_items(self, items, *, producer_items=None):
        """Overlay only held items onto a new frame, without mutating old frames.

        The caller supplies the current published item set after its existing
        incomplete-removal handling. A missing item here has actually left the
        board; it is not evidence that one failed source read removed it.
        This neither confirms nor resolves any retained assignment.
        """
        visible = {item['id'] for item in items}
        self.retain_items(visible)
        producer_items = {aid: item for aid, item in (producer_items or {}).items()
                          if aid in self.assignments and self.assignments[aid].producer_recovery
                          and item in visible}
        held_items = {held.item for held in self.assignments.values()} | set(producer_items.values())
        self.items = {item: prior for item, prior in self.items.items() if item in held_items}
        result = []
        for item in items:
            if item['id'] not in self.items:
                result.append(item)
                continue
            previous = self.items[item['id']]
            if previous is None:
                # R30: a never-published item stays absent until resolution;
                # there is no visible evidence object to mark unconfirmed.
                continue
            retained = deepcopy(previous)
            since = self.since(item['id'], producer_items=producer_items)
            for fact in retained.get('evidence', {}).values():
                if fact['freshness'] != 'stale':
                    fact['freshness'] = 'unconfirmed'
                old_since = fact.get('unconfirmedSince')
                fact['unconfirmedSince'] = (since if old_since is None
                                            else min(old_since, since))
            result.append(retained)
        return result


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
        if job[0] not in {'assignments', 'item-assignment-source', 'reviewed-producer'}:
            return
        with self.lock:
            if self.closed or job in self.flights:
                raise ValueError('assignment read outside a new attempt')
            self.flights[job] = AssignmentReadConflicts(
                detail=job[0] == 'reviewed-producer' or (job[0] == 'assignments' and job[2] is not None))

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
                            or (resource == 'reviewed-producer' and str(row['id']) != str(scope))
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
