"""Compact current assignment contributions; never retain source rows here."""

from copy import deepcopy
from typing import NamedTuple


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

    @classmethod
    def capture(cls, summary):
        return cls(summary['count'], tuple(summary['kinds'].items()),
                   tuple(summary['verdicts'].items()), tuple(summary['latest'].items()),
                   tuple(summary['authors'].items()), tuple(sorted(summary['authorIds'])),
                   tuple(deepcopy(summary['refs'])), summary['activity'])


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

    @classmethod
    def capture(cls, row, inventory, order):
        # Correction text is never a published contribution. Preserve all
        # remaining current correction fields and their source order.
        corrections = tuple(deepcopy({key: value for key, value in correction.items()
                                      if key not in {'reason', 'note'}})
                            for correction in row.get('commitRefCorrections') or ())
        return cls(row['workItemId'], row.get('holderKey'), row['state'],
                   row.get('openedAt'), row.get('closedAt'), row.get('effectKind'),
                   row.get('reviewsAssignmentId'), order, corrections,
                   AttestContribution.capture(inventory.folded),
                   ContributionEvidence.capture(inventory.evidence()), inventory.fingerprint)


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
