"""Interp runtime support (rt_conc) - verbatim segment of the
original boot/interp.py (lines 184..403), split for maintainability."""
from .rt_core import (
    HLPanic, threading,
)

class HLChan:
    """A channel value: FIFO message queue (interpreter representation).

    cap == 0 means unbounded (the default); cap >= 1 is a bounded
    channel whose send blocks while `cap` messages are pending.
    waiters = number of threads currently blocked in recv/select on
    THIS channel (bookkeeping for the deadlock detector — see
    ConcRuntime.deadlock_check).
    """

    __slots__ = ("q", "cap", "waiters", "send_waiters", "_registry")

    def __init__(self, cap=0):
        self.q = []  # list of values (messages); guarded by the runtime lock
        self.cap = cap  # 0 = unbounded; >= 1 = bounded (blocking send)
        self.waiters = 0  # recv/select waiters on this channel
        self.send_waiters = 0  # senders blocked on `full` (bounded only)
        self._registry = None  # ConcRuntime.chans list (set on register)

    def __del__(self):
        # Deregister from the runtime's live-channel registry. Removal
        # during another thread's registry iteration can make that
        # iterator skip an element — but a channel being collected here
        # holds no stack references, hence has no waiters, hence cannot
        # affect the deadlock scan's verdict (only channels with BOTH
        # pending messages AND waiters matter).
        reg = getattr(self, "_registry", None)
        if reg is not None:
            try:
                reg.remove(self)
            except (ValueError, AttributeError):
                pass


class HLTask:
    """A spawned task: Python thread + join state."""

    __slots__ = ("thread", "done", "joined", "result")

    def __init__(self, thread):
        self.thread = thread
        self.done = False
        self.joined = False
        self.result = None


class ConcRuntime:
    """Global state for spawn / channel operations (one per Interp)."""

    def __init__(self):
        self.mu = threading.Lock()
        self.cv = threading.Condition(self.mu)
        self.tasks_alive = 0   # spawned tasks not yet finished (main excluded)
        self.blocked = 0       # threads currently blocked in recv/select/join/send
        self.msgs = 0          # total pending messages across all channels
        self.next_id = 0
        # Deep-scan-20: mirror the native Stage-33 counters so the
        # deadlock verdicts agree — done_unjoined counts finished tasks
        # nobody joined yet; join_waiters counts threads currently
        # blocked INSIDE join(). A done-but-unjoined task only excuses a
        # deadlock when a join waiter can actually proceed on it.
        self.done_unjoined = 0
        self.join_waiters = 0
        # Live-channel registry (for the deadlock scan). HLChan.__del__
        # deregisters; see the soundness note there.
        self.chans = []

    def register(self, chan):
        with self.cv:
            self.chans.append(chan)
            chan._registry = self.chans

    def deadlock_check(self):
        """Call with the lock HELD, before blocking. Soundness contract:

        1. A thread counts itself as `blocked` ONLY while it holds no
           progress opportunity: every wait loop re-checks its condition
           under the same lock before blocking, and the check itself runs
           with the lock held (so no other thread can be mid-operation
           and uncounted at that instant).
        2. A woken-but-not-yet-rescheduled receiver is STILL counted in
           `blocked` (its decrement happens only after wait() re-acquires
           the lock) — which is exactly why `blocked == alive` alone is
           NOT sufficient: it can fire while a receiver's message is
           already pending but the receiver has not been scheduled yet.
           The waiter counters close this hole in BOTH directions:
           a channel with pending messages AND recv waiters, or a
           not-full bounded channel with send waiters, means some
           thread WILL make progress as soon as the lock is released.

        Deadlock iff: every thread is blocked AND no channel has a
        progress opportunity (pending message with a recv waiter, or
        free capacity with a send waiter). This also catches cycles the
        pre-v0.29 `msgs == 0` guard missed (e.g. a producer blocked on a
        full channel that nobody consumes). Mirrors
        hl_rt_deadlock_check() in C. Raises HLPanic (the `with self.cv:`
        caller releases the lock on unwind; the process is halting
        anyway)."""
        alive = self.tasks_alive + 1  # +1: the main thread
        if self.blocked != alive:
            return
        # Deep-scan-20 fix (MED, parity + correctness): a finished-but-
        # unjoined task only means "not a deadlock" when a thread is
        # ACTUALLY blocked in join() and will proceed on it. The old
        # check (no guard at all, opposite of the native's overly broad
        # one) panicked while the native hung; with this mirrored guard
        # both sides now report the same verdict: a program whose every
        # remaining thread blocks on an empty channel with an abandoned
        # done task is a REAL deadlock and panics on both backends.
        if self.done_unjoined > 0 and self.join_waiters > 0:
            return
        for c in self.chans:
            if c.q and c.waiters > 0:
                return  # a consumer can make progress once scheduled
            if c.send_waiters > 0 and not self._full(c):
                return  # a woken sender can proceed (capacity freed)
        raise HLPanic(
            "deadlock: all tasks are blocked on channel operations "
            "(no possible progress)", 0)

    def _full(self, chan):
        return chan.cap > 0 and len(chan.q) >= chan.cap

    def send(self, chan, value):
        """Blocking send: on a bounded channel, waits while full.
        Unbounded channels never block the sender."""
        with self.cv:
            while self._full(chan):
                chan.send_waiters += 1
                self.blocked += 1
                self.deadlock_check()
                self.cv.wait()
                self.blocked -= 1
                chan.send_waiters -= 1
            chan.q.append(value)
            self.msgs += 1
            self.cv.notify_all()

    def try_send(self, chan, value):
        """Non-blocking send: False iff a bounded channel is full (the
        value is NOT enqueued in that case); True otherwise."""
        with self.cv:
            if self._full(chan):
                return False
            chan.q.append(value)
            self.msgs += 1
            self.cv.notify_all()
            return True

    def recv(self, chan, line):
        with self.cv:
            while not chan.q:
                chan.waiters += 1
                self.blocked += 1
                self.deadlock_check()
                self.cv.wait()
                self.blocked -= 1
                chan.waiters -= 1
            value = chan.q.pop(0)
            self.msgs -= 1
            # A dequeue frees capacity on a bounded channel — wake any
            # sender blocked on `full` (v0.29.0-alpha: recv previously
            # never notified; with unbounded-only channels that was fine,
            # but bounded senders wait for exactly this signal).
            self.cv.notify_all()
            return value

    def recv_or(self, chan, default):
        """Non-blocking recv: the pending message if one exists, else
        `default` (ownership of the unused default stays with the
        caller in the interpreter — GC makes the release a no-op)."""
        with self.cv:
            if chan.q:
                value = chan.q.pop(0)
                self.msgs -= 1
                self.cv.notify_all()  # free capacity -> wake blocked senders
                return value
            return default

    def select(self, chans, line):
        with self.cv:
            if not chans:
                raise HLPanic("select() on empty channel list", line)
            while True:
                for i, c in enumerate(chans):
                    if c.q:
                        return i
                for c in chans:
                    c.waiters += 1
                self.blocked += 1
                self.deadlock_check()
                self.cv.wait()
                self.blocked -= 1
                for c in chans:
                    c.waiters -= 1

    def join(self, task, line):
        with self.cv:
            if task.joined:
                raise HLPanic("task already joined", line)
            while not task.done:
                self.blocked += 1
                self.join_waiters += 1
                self.deadlock_check()
                self.cv.wait()
                self.blocked -= 1
                self.join_waiters -= 1
            task.joined = True
            self.done_unjoined -= 1
            return task.result

    def task_finished(self, task, result):
        with self.cv:
            task.result = result
            task.done = True
            self.tasks_alive -= 1
            self.done_unjoined += 1
            self.cv.notify_all()




__all__ = [
    "ConcRuntime",
    "HLChan",
    "HLTask",
]
