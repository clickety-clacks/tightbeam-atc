"""Active confirmation deadlines are independent of terminal-history rotation."""

ACTIVE_BUDGET_MS = 30 * 60 * 1000
ACTIVE_REFRESH_MS = 15 * 60 * 1000
RECENT_WINDOW_MS = 30 * 60 * 1000
ACTIVE_BATCH_LIMIT = 32
RETRY_MS = 60 * 1000


class ActiveBackstop:
    def __init__(self):
        self.active = set()
        self.history = set()
        self.attempted = {}
        self.activity_deadline = None

    def classify(self, model, scopes, at):
        assignments = model.inventory("assignments").rows
        open_rows = [a for a in assignments.values() if a.get("state") == "open"]
        sessions = {a.get("holderKey") for a in open_rows} - {None}
        active = {("attests", a["id"]) for a in open_rows}
        recent = {}
        all_scopes = set(scopes)
        session_scopes = {s for r, s in all_scopes if r != "attests"}

        def activity(session, timestamp):
            if session and isinstance(timestamp, (int, float)):
                recent[session] = max(recent.get(session, 0), timestamp)

        for resource, scope in all_scopes:
            inventory = model.inventory(resource, scope)
            if resource != "attests" and not inventory.complete:
                sessions.add(scope)  # Incomplete evidence cannot prove inactivity.
            summary = model.summary(resource, scope)
            if resource == "attests":
                for author, timestamp in summary["authors"].items():
                    activity(author, timestamp)
            else:
                if resource == "turns" and summary["live"]:
                    sessions.add(scope)  # Includes a queued turn.
                activity(scope, summary["recentActivity"])
        deadlines = []
        for session, timestamp in recent.items():
            if session in session_scopes and timestamp + RECENT_WINDOW_MS > at:
                sessions.add(session)
                deadlines.append(timestamp + RECENT_WINDOW_MS)
        active.update((r, s) for r, s in all_scopes if r != "attests" and s in sessions)
        if not model.inventory("assignments").complete:
            active.update(s for s in self.active if s[0] == "attests")
        self.active = active & all_scopes
        self.history = all_scopes - self.active
        self.activity_deadline = min(deadlines, default=None)
        self.attempted = {s: t for s, t in self.attempted.items() if s in all_scopes}
        for scope in all_scopes:
            inventory = model.inventory(*scope)
            inventory.budget_stale = (scope in self.active and inventory.confirmed_at is not None
                                      and at >= inventory.confirmed_at + ACTIVE_BUDGET_MS)

    @staticmethod
    def group(model, scopes, *, active=False):
        inventories = [model.inventory(*scope) for scope in scopes]
        times = [i.confirmed_at for i in inventories if i.confirmed_at is not None]
        result = {"scopeCount": len(scopes),
                  "unconfirmedScopes": sum(i.confirmed_at is None for i in inventories),
                  "oldestConfirmedAt": min(times, default=None)}
        if active:
            result["overBudgetScopes"] = sum(i.budget_stale for i in inventories)
        return result

    def metadata(self, model, connected):
        return {"announcedChanges": "live" if connected else "stale",
                "unannouncedChanges": "swept", "activeBudgetMs": ACTIVE_BUDGET_MS,
                "activeRefreshMs": ACTIVE_REFRESH_MS, "recentWindowMs": RECENT_WINDOW_MS,
                "activeBatchLimit": ACTIVE_BATCH_LIMIT,
                "active": self.group(model, self.active, active=True),
                "history": self.group(model, self.history)}

    def due(self, model, scope):
        confirmed = model.inventory(*scope).confirmed_at
        return max(confirmed + ACTIVE_REFRESH_MS if confirmed is not None else 0,
                   self.attempted.get(scope, -RETRY_MS) + RETRY_MS)

    def candidates(self, model, reads):
        queued = {job[:2] for job, priority in reads.pending.items()
                  if job[2] is None and priority >= 1}
        running = {job[:2] for job, _ in reads.active.values() if job[2] is None}
        capacity = max(0, ACTIVE_BATCH_LIMIT - len(queued & self.active))
        candidates = sorted(self.active - queued - running,
                            key=lambda scope: (self.due(model, scope), scope))
        return candidates[:capacity]

    def schedule(self, model, reads, at):
        for scope in self.candidates(model, reads):
            if self.due(model, scope) > at:
                break
            reads.schedule(*scope, active=True)
            self.attempted[scope] = at

    def deadline(self, model, reads, at, connected):
        deadlines = [self.activity_deadline] if self.activity_deadline is not None else []
        for scope in self.active:
            confirmed = model.inventory(*scope).confirmed_at
            if confirmed is not None and confirmed + ACTIVE_BUDGET_MS > at:
                deadlines.append(confirmed + ACTIVE_BUDGET_MS)
        if connected:
            deadlines.extend(self.due(model, scope) for scope in self.candidates(model, reads))
        return min(deadlines, default=None)
