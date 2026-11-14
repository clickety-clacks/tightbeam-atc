"""Compact current assignment contributions; never retain source rows here."""

from copy import deepcopy
from math import isfinite
from typing import NamedTuple


class AttestFrontier(NamedTuple):
    """Counted IDs at the maximum only, under the pinned append-only source.

    Complete attest snapshots and ordered post-commit notices establish that
    lower timestamps are already counted. A backward source clock invalidates
    that premise; this is the recorded R25 limit, not a monotonic clock claim.
    """

    maximum: int | float | None = None
    ids: frozenset = frozenset()

    @staticmethod
    def usable(row):
        timestamp = row.get('ts')
        return (row.get('id') is not None and isinstance(timestamp, (int, float))
                and not isinstance(timestamp, bool) and isfinite(timestamp))

    def includes(self, row):
        if not self.usable(row) or self.maximum is None:
            return False
        return (row['ts'] < self.maximum or
                (row['ts'] == self.maximum and str(row['id']) in self.ids))

    def observe(self, row):
        # Source fold counts are authoritative; this stores only the current
        # maximum's identities, independent of page boundaries or row order.
        if not self.usable(row):
            return self
        timestamp, identity = row['ts'], str(row['id'])
        if self.maximum is None or timestamp > self.maximum:
            return AttestFrontier(timestamp, frozenset((identity,)))
        if timestamp == self.maximum and identity not in self.ids:
            return AttestFrontier(timestamp, self.ids | {identity})
        return self


class ContributionEvidence(NamedTuple):
    status: str
    freshness: str
    observed_at: int | None
    source: str | tuple
    complete: bool
    unconfirmed_since: int | None

    @classmethod
    def capture(cls, facts):
        source = facts['source']
        return cls(facts['status'], facts['freshness'], facts.get('observedAt'),
                   tuple(source) if isinstance(source, list) else source,
                   facts['complete'], facts.get('unconfirmedSince'))

    def publication(self):
        return dict(status=self.status, freshness=self.freshness,
                    observedAt=self.observed_at,
                    source=list(self.source) if isinstance(self.source, tuple) else self.source,
                    complete=self.complete, unconfirmedSince=self.unconfirmed_since)


class AttestContribution(NamedTuple):
    count: int
    kinds: tuple
    verdicts: tuple
    latest: tuple
    authors: tuple
    author_ids: tuple
    refs: tuple
    activity: int | None
    frontier: AttestFrontier

    @classmethod
    def capture(cls, summary, frontier):
        return cls(summary['count'], tuple(summary['kinds'].items()),
                   tuple(summary['verdicts'].items()), tuple(summary['latest'].items()),
                   tuple(summary['authors'].items()), tuple(sorted(summary['authorIds'])),
                   tuple(deepcopy(summary['refs'])), summary['activity'], frontier)

    def summary(self):
        """Restore the existing current fold when a contributor becomes open."""
        from atc_summary import summarize
        result = summarize('attests', ())
        result['count'] = self.count
        result['kinds'].update(dict(self.kinds))
        result['verdicts'].update(dict(self.verdicts))
        result['latest'].update(self.latest)
        result['authors'].update(self.authors)
        result['authorIds'].update(self.author_ids)
        result['refs'].extend(deepcopy(self.refs))
        result['activity'] = result['recentActivity'] = self.activity
        return result


class AssignmentContribution(NamedTuple):
    item: str
    holder: str | None
    state: str
    opened_at: int | None
    closed_at: int | None
    effect: str | None
    reviews: str | None
    order: int
    corrections: tuple
    attests: AttestContribution
    evidence: ContributionEvidence
    detail_fingerprint: str | None

    @staticmethod
    def current_corrections(rows):
        return tuple(deepcopy({key: value for key, value in correction.items()
                               if key not in {'reason', 'note'}})
                     for correction in rows or ())

    @classmethod
    def capture(cls, row, inventory, order):
        # Correction text is never a published contribution. Preserve all
        # remaining current correction fields and their source order.
        corrections = cls.current_corrections(row.get('commitRefCorrections'))
        return cls(row['workItemId'], row.get('holderKey'), row['state'],
                   row.get('openedAt'), row.get('closedAt'), row.get('effectKind'),
                   row.get('reviewsAssignmentId'), order, corrections,
                   AttestContribution.capture(inventory.folded, inventory.attest_frontier),
                   ContributionEvidence.capture(inventory.evidence()), inventory.fingerprint)

    def assignment_update(self, row, *, order=None):
        """Merge a source-eligible assignment projection into current facts.

        The service must establish notice/read ordering before calling this.
        Assignment notices do not carry commitRefCorrections or attest history:
        their absence cannot erase either contribution. An explicit complete
        correction list from an existing source read can replace that field.
        """
        fields = {
            'item': 'workItemId', 'holder': 'holderKey', 'state': 'state',
            'opened_at': 'openedAt', 'closed_at': 'closedAt',
            'effect': 'effectKind', 'reviews': 'reviewsAssignmentId',
        }
        changes = {field: row[key] for field, key in fields.items() if key in row}
        if 'commitRefCorrections' in row:
            changes['corrections'] = self.current_corrections(row['commitRefCorrections'])
        if order is not None:
            changes['order'] = order
        return self._replace(**changes)


class ItemContributions:
    """One entry per current contributor, with no retained source generations."""

    __slots__ = ('entries',)

    def __init__(self):
        self.entries = {}

    def replace_item(self, item, incoming, *, complete):
        # A partial response cannot establish absence or replace a complete
        # published contribution. Its existing service retry owns recovery.
        if not complete:
            return False
        if any(value.item != item for value in incoming.values()):
            raise ValueError('contribution belongs to another item')
        for assignment in tuple(self.entries):
            if self.entries[assignment].item == item and assignment not in incoming:
                del self.entries[assignment]
        self.entries.update(incoming)
        return True

    def retain_items(self, items):
        for assignment in tuple(self.entries):
            if self.entries[assignment].item not in items:
                del self.entries[assignment]
