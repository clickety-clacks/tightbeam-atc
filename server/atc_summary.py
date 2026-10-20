"""Compact history projections, rebuilt only when their own scope changes."""

from collections import Counter


def summarize(resource, rows):
    result = {"count": 0, "kinds": Counter(), "verdicts": Counter(), "latest": {},
              "authors": {}, "authorIds": set(), "refs": [], "activity": None,
              "live": [], "marks": [], "recentActivity": None}
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
            times = []
        elif resource == "turns":
            if row.get("endedAt") is None:
                result["live"].append(row)
            times = [next((row[k] for k in ("endedAt", "startedAt", "createdAt")
                           if isinstance(row.get(k), (int, float))), None)]
        elif resource == "messages":
            times = [row.get("at")]
            if isinstance(row.get("at"), (int, float)):
                result["marks"].append(row)
        elif resource == "wakes":
            times = [row.get("firedAt")]
            if row.get("state") == "fired" and isinstance(row.get("firedAt"), (int, float)):
                result["marks"].append(row)
        else:
            times = []
        recent_times = ([row.get(k) for k in ("endedAt", "startedAt", "createdAt")]
                        if resource == "turns" else times)
        for at in recent_times:
            if isinstance(at, (int, float)):
                result["recentActivity"] = max(result["recentActivity"] or 0, at)
        for at in times:
            if isinstance(at, (int, float)):
                result["activity"] = max(result["activity"] or 0, at)
    if resource in {"wakes", "messages"}:
        field = "firedAt" if resource == "wakes" else "at"
        result["marks"].sort(key=lambda row: row[field], reverse=True)
    return result


def recent_marks(summary, field, now):
    for row in summary["marks"]:
        if row[field] <= now - 300_000:
            break
        yield row
