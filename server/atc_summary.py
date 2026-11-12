"""Compact history projections, rebuilt only when their own scope changes."""

from collections import Counter


def summarize(resource, rows, *, into=None, now=None):
    """Fold a page into board facts, without retaining history payloads.

    ``into`` belongs to one complete read, not successive overlapping reads.
    The caller commits it only after the final page establishes completeness.
    ``now`` selects the current display window; omitted for full-load callers.
    """
    result = into if into is not None else {"count": 0, "kinds": Counter(), "verdicts": Counter(), "latest": {},
              "authors": {}, "authorIds": set(), "refs": [], "activity": None,
              "live": [], "marks": [], "recentActivity": None, "assignmentActivity": {}}
    for row in rows:
        result["count"] += 1
        if resource == "attests":
            result["kinds"][row.get("kind")] += 1
            verdict = row.get("verdictKind")
            if row.get("kind") == "verdict" and verdict:
                result["verdicts"][verdict] += 1
            if verdict and isinstance(row.get("ts"), (int, float)):
                result["latest"][verdict] = max(result["latest"].get(verdict, 0), row["ts"])
            author, at = row.get("bySession"), row.get("ts")
            if author:
                result["authorIds"].add(author)
                if isinstance(at, (int, float)):
                    result["authors"][author] = max(result["authors"].get(author, 0), at)
            result["refs"].extend(row.get("commitRefs") or [])
            times = [row.get("ts")]
        elif resource == "turns":
            if row.get("endedAt") is None:
                result["live"].append({key: row[key] for key in (
                    "seq", "sessionKey", "assignmentId", "wakeId", "createdAt",
                    "startedAt", "endedAt") if key in row})
            times = [next((row[k] for k in ("endedAt", "startedAt", "createdAt")
                           if isinstance(row.get(k), (int, float))), None)]
        elif resource == "messages":
            times = [row.get("at")]
            if (isinstance(row.get("at"), (int, float))
                    and (now is None or row["at"] > now - 300_000)):
                result["marks"].append({key: row[key] for key in (
                    "id", "sessionKey", "sender", "at") if key in row})
        elif resource == "wakes":
            times = [row.get("firedAt")]
            if (row.get("state") == "fired"
                    and isinstance(row.get("firedAt"), (int, float))
                    and (now is None or row["firedAt"] > now - 300_000)):
                result["marks"].append({key: row[key] for key in (
                    "wakeId", "sessionKey", "creatorSessionKey", "state", "firedAt")
                    if key in row})
        else:
            times = []
        recent_times = ([row.get(k) for k in ("endedAt", "startedAt", "createdAt")]
                        if resource == "turns" else times)
        if resource == "turns" and row.get("assignmentId"):
            recorded = [at for at in recent_times if isinstance(at, (int, float))]
            if recorded:
                aid = row["assignmentId"]
                result["assignmentActivity"][aid] = max(
                    result["assignmentActivity"].get(aid, 0), *recorded)
        for at in recent_times:
            if isinstance(at, (int, float)):
                result["recentActivity"] = max(result["recentActivity"] or 0, at)
        for at in times:
            if isinstance(at, (int, float)):
                result["activity"] = max(result["activity"] or 0, at)
    if resource in {"wakes", "messages"}:
        field = "firedAt" if resource == "wakes" else "at"
        if now is not None:
            result["marks"][:] = [mark for mark in result["marks"]
                                  if mark[field] > now - 300_000]
        result["marks"].sort(key=lambda row: row[field], reverse=True)
    return result


def recent_marks(summary, field, now):
    for row in summary["marks"]:
        if row[field] <= now - 300_000:
            break
        yield row
