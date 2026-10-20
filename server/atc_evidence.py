"""Evidence inventories: incomplete reads never establish absence."""

from copy import deepcopy
from dataclasses import dataclass, field
import hashlib


def agent_id(session_key):
    tail = session_key.rsplit(" ", 1)[-1]
    return tail if tail.startswith("s_") else "a" + hashlib.blake2b(
        session_key.encode(), digest_size=4).hexdigest()


@dataclass
class Inventory:
    key: str = "id"
    rows: dict = field(default_factory=dict)
    observations: dict = field(default_factory=dict)
    complete: bool = False
    observed_at: int | None = None
    stale: bool = True
    source: str = ""

    def read(self, rows, *, complete, at, source):
        """A read covers this inventory's entire declared scope.

        Callers collect all pages before replacing anything. Partial rows can
        add knowledge, but cannot delete retained rows or certify absence.
        """
        incoming = {str(row[self.key]): deepcopy(row) for row in rows}
        if complete:
            self.rows = incoming
            self.observations = {}
        else:
            self.rows.update(incoming)
            for observation in self.observations.values():
                observation["freshness"] = "stale"
        for key in incoming:
            self.observations[key] = {
                "status": "known", "freshness": "live" if complete else "stale",
                "observedAt": at, "source": source, "complete": complete,
            }
        self.complete = complete
        self.stale = not complete
        self.observed_at = at
        self.source = source

    def invalidate(self):
        self.stale = True
        for observation in self.observations.values():
            observation["freshness"] = "stale"

    def payload(self, row, *, at, source, policy):
        """Only the follower's version-verified resource policy calls this.

        Immutable rows deduplicate by identity. Strict versions order mutable
        rows against reads and buffered events. Neither policy grants deletion.
        """
        key = str(row[self.key])
        old = self.rows.get(key)
        if policy == "immutable":
            if old is not None:
                if old != row:
                    self.invalidate()
                    return False
                return True
        elif policy == "strict-version":
            version = row.get("rowVersion")
            if not isinstance(version, int):
                return False
            if old is not None:
                previous = old.get("rowVersion")
                if not isinstance(previous, int):
                    return False
                if version < previous:
                    return True
                if version == previous and old != row:
                    self.invalidate()
                    return False
        else:
            raise ValueError("payload requires an established resource policy")
        self.rows[key] = deepcopy(row)
        self.observations[key] = {
            "status": "known", "freshness": "live", "observedAt": at,
            "source": source, "complete": True,
        }
        # A new row does not prove that the rest of the inventory is complete.
        return True

    def evidence(self):
        return {
            "status": "known" if self.complete else "unknown",
            "freshness": "stale" if self.stale else "live",
            "observedAt": self.observed_at, "source": self.source,
            "complete": self.complete,
        }


def combined_evidence(*facts):
    known = bool(facts) and all(f["status"] == "known" for f in facts)
    times = [f["observedAt"] for f in facts if f.get("observedAt") is not None]
    return {
        "status": "known" if known else "unknown",
        "freshness": "live" if facts and all(
            f["freshness"] == "live" for f in facts) else "stale",
        "observedAt": min(times) if times else None,
        "source": sorted({s for f in facts for s in (
            f["source"] if isinstance(f["source"], list) else [f["source"]]) if s}),
        "complete": known,
    }


def evidence_stage(item, assignments, attests, *, complete):
    """The canonical stages inventory actual evidence, without filling steps."""
    if not complete:
        return None
    has_open = any(a["state"] == "open" for a in assignments)
    if item["state"] == "closed" and not has_open:
        return 6
    kinds = {a["kind"] for a in attests}
    verdicts = {a.get("verdictKind") for a in attests if a["kind"] == "verdict"}
    stage = 0
    if "progress" in kinds:
        stage = 1
    if "tests-passed" in verdicts:
        stage = 2
    if "completion" in kinds:
        stage = 3
    if verdicts & {"reviewed-clean", "spec-reviewed", "verified"}:
        stage = 4
    clean = max((a["ts"] for a in attests
                 if a.get("verdictKind") == "reviewed-clean"), default=None)
    tested = max((a["ts"] for a in attests
                  if a.get("verdictKind") == "tests-passed"), default=None)
    review_stale = clean is not None and tested is not None and tested > clean + 300_000
    if "completion" in kinds and "reviewed-clean" in verdicts and not has_open and not review_stale:
        stage = 5
    return stage


def code_state(assignments, producer_refs, *, complete):
    if producer_refs or any(a.get("effectKind") in {
            "code", "release", "live_mutation"} for a in assignments):
        return True
    if complete and assignments and all(a.get("effectKind") for a in assignments):
        return False
    return None


def merge_ring(stage, merged, ready, code):
    if stage is None or stage < 5:
        return None
    if merged is True:
        return "black"
    if ready is True:
        return "green"
    if code is False:
        return "light-gray"
    if code is True and merged is None:
        return "yellow"
    return None


def turn_state(turns, *, complete):
    """A pending wake or a coarse session status cannot prove a queued turn."""
    if any(t.get("startedAt") is not None and t.get("endedAt") is None for t in turns):
        return "run"
    if any(t.get("startedAt") is None and t.get("endedAt") is None for t in turns):
        return "wait"
    return "idle" if complete else None
