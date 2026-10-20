"""Bounded background reads; the model worker alone consumes their results."""

from collections import OrderedDict, defaultdict
from concurrent.futures import ThreadPoolExecutor

from atc_upstream import Read, now_ms


class ReadQueue:
    def __init__(self, gateway, events, workers):
        self.gateway, self.events, self.workers = gateway, events, workers
        self.executor = ThreadPoolExecutor(max_workers=workers)
        self.pending = OrderedDict()
        self.active = {}
        self.epochs = defaultdict(int)
        self.dispatched = 0

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
            del self.pending[job]
            self.dispatched += 1
            epoch = self.epochs[inventory]
            self.active[inventory] = job, epoch
            future = self.executor.submit(self._read, job)
            future.add_done_callback(
                lambda done, job=job, epoch=epoch:
                None if done.cancelled() else
                self.events.put(("read", (job, epoch, done.result()))))

    def _read(self, job):
        resource, scope, key = job
        try:
            if key is None:
                return self.gateway.read(resource, scope)
            return self.gateway.detail(resource, key, scope=scope)
        except Exception as error:
            # Exception bodies may contain credentials or upstream responses.
            return Read([], False, now_ms(), resource, type(error).__name__)

    def finish(self, job, epoch):
        self.active.pop(job[:2])
        if epoch != self.epochs[job[:2]]:
            self.schedule(*job)
            return False
        return True

    def close(self):
        self.executor.shutdown(wait=False, cancel_futures=True)
