"""Interp runtime support (rt_conc) - verbatim segment of the
original boot/interp.py (lines 184..403), split for maintainability."""
from .rt_core import (
    HLPanic, threading,
)


class DetNode:
    """Stage 127 (v0.145.0-alpha): one rotation slot in the deterministic
    scheduler. A node exists for the main thread (task 0) and for every
    spawned task; `queued` is true while the node sits in the ready
    queue, `retired` is true once its task has left the rotation for
    good (the finish op retires; retired heads are skipped by pass)."""

    __slots__ = ("task_id", "queued", "retired")

    def __init__(self, task_id):
        self.task_id = task_id
        self.queued = False
        self.retired = False


class DetCore:
    """Stage 127 (v0.145.0-alpha): the deterministic scheduler — the
    interpreter half of the mode the native runtime implements in its
    Stage 127 region (SPEC section 67). Policy, mirrored exactly so the
    differential suite holds at the TRACE level:

      - STRICT SERIALIZATION. At most one thread runs HLS user code at
        any instant: the baton (`owner`) holder. Every communicating op
        (spawn / send / try_send / recv / recv_or / select / join /
        finish) is a scheduling point: the task enters the op, performs
        it, requeues ITSELF at the tail of the FIFO ready queue, and
        parks until its turn comes round again. chan_new / clone /
        release / len are quiet: they run inside the owner's segment
        and never touch the rotation.
      - WAKE ORDER. When an op satisfies parked waiters (a send makes
        receivers runnable, a dequeue frees bounded capacity, a finish
        releases join waiters), the woken nodes join the ready queue in
        their park order and BEFORE the waker requeues itself — the
        wakee runs ahead of the waker.
      - CHILD ORDER. A spawned task's node is enqueued by the SPAWNER,
        before the spawner requeues itself: the child's first turn
        comes before the parent's next one, whatever the OS does with
        the underlying thread.
      - DEADLOCK. When the last runnable task leaves the rotation and
        the ready queue is empty, every remaining thread is parked on
        a channel or a join. A pending message with a parked receiver,
        or free bounded capacity with a parked sender, would be a
        MISSED WAKE (a scheduler bug, reported loudly as such);
        otherwise a live waiter means the canonical Stage 16 deadlock
        message; no waiter at all means the process is simply
        finishing (the last thread exiting while nothing is blocked).

    The trace (HL_DET_TRACE=1): one line per scheduling point, emitted
    to stderr at the moment the op's turn begins,

        __HLDET_STEP__ <seq> t<task> <op>[ <detail>]

    with seq from 0, task ids by spawn order (main = t0) and channel
    ids by creation order (ch0...). Byte-identical to the native
    runtime's trace for the same program — that equality is the
    mode's acceptance witness."""

    def __init__(self, trace):
        self.trace = trace
        self.ready = []          # FIFO of DetNode (guarded by det_mu)
        self.owner = None        # the baton (guarded by det_mu)
        self.task_seq = 0        # last task id handed out (main = 0)
        self.chan_seq = 0        # last channel id handed out
        self.step = 0            # trace sequence number
        self.join_live = 0       # nodes parked in join lists (live cells)
        self.det_mu = threading.Lock()
        self.det_cv = threading.Condition(self.det_mu)
        self.tls = threading.local()
        # The thread that constructs the Interp is the mode's t0 (boot.py
        # and every hltest worker construct and run in the same thread).
        main = DetNode(0)
        self.owner = main
        self.tls.node = main
        # The runtime this core belongs to (set by ConcRuntime after
        # construction: the deadlock scan walks its channel registry,
        # and park_on must release/reacquire ITS mutex while parked).
        self.chans = []
        self.mu = None

    # ---- identity ----

    def me(self):
        """The calling thread's node, or None for a thread the scheduler
        does not know (a raw std.thread): the caller is unscheduled."""
        return getattr(self.tls, "node", None)

    def next_chan_id(self):
        cid = self.chan_seq
        self.chan_seq += 1
        return cid

    # ---- the baton primitives (det_mu guards owner/ready) ----

    def _pass_locked(self):
        """Hand the baton to the head of the ready queue. det_mu HELD by
        the leaving owner. An empty queue leaves the mode ownerless and
        runs the deadlock scan (a task leaving the rotation with nobody
        runnable is exactly the all-blocked moment)."""
        while self.ready and self.ready[0].retired:
            self.ready.pop(0)
        if self.ready:
            nxt = self.ready.pop(0)
            nxt.queued = False
            self.owner = nxt
        else:
            self.owner = None
            self._deadlock_locked()
        self.det_cv.notify_all()

    def _live_in(self, lst):
        n = 0
        if lst:
            for x in lst:
                if not x.queued and not x.retired:
                    n += 1
        return n

    def _deadlock_locked(self):
        """det_mu HELD, ready queue empty, called by the leaving owner.
        In det mode every non-running thread is parked in a wait list,
        so a satisfied-but-unwoken waiter would be a scheduler bug. In
        det mode only the owner runs user code, and the owner is the
        caller, so walking `chans` here needs no runtime lock (a channel
        collected under us held no waiters — the same soundness note
        HLChan.__del__ makes for the preemptive scan)."""
        import sys
        any_waiter = self.join_live > 0
        for ch in self.chans:
            recv_live = self._live_in(ch.det_recv)
            send_live = self._live_in(ch.det_send)
            if ch.q and recv_live > 0:
                raise HLPanic(
                    "det scheduler bug: missed wake (pending message "
                    "with a parked receiver)", 0)
            if ch.cap > 0 and len(ch.q) < ch.cap and send_live > 0:
                raise HLPanic(
                    "det scheduler bug: missed wake (free capacity "
                    "with a parked sender)", 0)
            if recv_live or send_live:
                any_waiter = True
        if any_waiter:
            raise HLPanic(
                "deadlock: all tasks are blocked on channel operations "
                "(no possible progress)", 0)
        # No waiter anywhere: the last runnable thread left the rotation
        # with nothing parked — the process is finishing, not stuck.

    # ---- the scheduling points ----

    def _emit(self, me, name, detail):
        import sys
        line = "__HLDET_STEP__ %d t%d %s" % (self.step, me.task_id, name)
        if detail:
            line += " " + detail
        sys.stderr.write(line + "\n")
        sys.stderr.flush()

    def op(self, name, detail=None):
        """Enter a communicating op: park until the baton is mine, then
        trace the op's turn. Called with NO runtime lock held. A thread
        the scheduler does not know (a raw std.thread) is refused: det
        mode's guarantee covers the task model, and silence here would
        make the certificate a lie."""
        me = self.me()
        if me is None:
            raise HLPanic(
                "det mode: channel operation from an unscheduled thread "
                "(std.thread is outside the deterministic scheduler)", 0)
        with self.det_mu:
            while self.owner is not me:
                self.det_cv.wait()
        if self.trace:
            self._emit(me, name, detail)
            self.step += 1
        else:
            self.step += 1

    def spawn_op(self):
        """The spawn op: trace it, mint the child's task id, enqueue the
        child's node (ahead of the spawner, which requeues at ITS yield).
        Task ids are 0-based with main = t0, so the first spawn is t1.
        Returns (node, task_id)."""
        me = self.me()
        if me is None:
            raise HLPanic(
                "det mode: channel operation from an unscheduled thread "
                "(std.thread is outside the deterministic scheduler)", 0)
        with self.det_mu:
            while self.owner is not me:
                self.det_cv.wait()
            self.task_seq += 1
            tid = self.task_seq
            node = DetNode(tid)
            node.queued = True
            self.ready.append(node)
        if self.trace:
            self._emit(me, "spawn", "t%d" % tid)
            self.step += 1
        else:
            self.step += 1
        return node, tid

    def yield_baton(self):
        """Leave a completed op: requeue myself at the tail, pass the
        baton, and park until my turn comes round again. Only the owner
        requeues; the wait inside the lock is what parks me while the
        rotation moves on (Condition.wait releases det_mu for me)."""
        me = self.me()
        with self.det_mu:
            if me.retired:
                return
            me.queued = True
            self.ready.append(me)
            self._pass_locked()
            while self.owner is not me:
                self.det_cv.wait()

    def park_on(self, wait_list):
        """Park inside a blocking op. Called with the RUNTIME lock (mu)
        held; it is released while the task is parked — exactly what
        the preemptive cv.wait does — and re-acquired before returning.
        Returns with the runtime lock held and the baton mine. A select
        appends its node to several channel lists BEFORE calling this
        with None as the carrier (no list of its own to join)."""
        me = self.me()
        if wait_list is not None:
            wait_list.append(me)
        with self.det_mu:
            self._pass_locked()
        self.mu.release()
        with self.det_mu:
            while self.owner is not me:
                self.det_cv.wait()
        self.mu.acquire()

    def park_join(self, task):
        """Park on a task's join list (the same protocol as park_on,
        plus the live-joiner bookkeeping the deadlock scan reads)."""
        if task.det_join is None:
            task.det_join = []
        self.join_live += 1
        self.park_on(task.det_join)
        # The live-counter arithmetic stays exact across re-parks: the
        # wake drains the list and spends its live count (wake_joiners),
        # and a loop that parks again adds itself back here.

    def wake(self, wait_list):
        """Move every live waiter of `wait_list` to the ready queue, in
        park order (the baton is NOT touched: the waker still holds it
        until its own yield/retire). Returns the number woken."""
        woken = 0
        if not wait_list:
            return 0
        with self.det_mu:
            for n in wait_list:
                if not n.queued and not n.retired:
                    n.queued = True
                    self.ready.append(n)
                    woken += 1
            del wait_list[:]
        return woken

    def wake_joiners(self, task):
        """A task finished: release its join waiters."""
        if task.det_join:
            self.join_live -= self.wake(task.det_join)

    def retire_baton(self):
        """Leave the rotation for good (task end). Idempotent. Called
        with the runtime lock HELD (task_finished): pass needs only
        det_mu — the lock order is always mu -> det_mu, never back."""
        me = self.me()
        if me is None or me.retired:
            return
        with self.det_mu:
            me.retired = True
            self._pass_locked()

    def tramp_enter(self, node):
        """A spawned thread's first act: adopt its node and wait for its
        first turn. The node was enqueued by the spawner, so the ORDER
        is already fixed — this wait only claims it."""
        self.tls.node = node
        with self.det_mu:
            while self.owner is not node:
                self.det_cv.wait()


class HLChan:
    """A channel value: FIFO message queue (interpreter representation).

    cap == 0 means unbounded (the default); cap >= 1 is a bounded
    channel whose send blocks while `cap` messages are pending.
    waiters = number of threads currently blocked in recv/select on
    THIS channel (bookkeeping for the deadlock detector — see
    ConcRuntime.deadlock_check).
    """

    __slots__ = ("q", "cap", "waiters", "send_waiters", "_registry",
                 "det_id", "det_recv", "det_send")

    def __init__(self, cap=0):
        self.q = []  # list of values (messages); guarded by the runtime lock
        self.cap = cap  # 0 = unbounded; >= 1 = bounded (blocking send)
        self.waiters = 0  # recv/select waiters on this channel
        self.send_waiters = 0  # senders blocked on `full` (bounded only)
        self._registry = None  # ConcRuntime.chans list (set on register)
        # Stage 127 (v0.145.0-alpha): the det-scheduler identity and
        # wait lists (None unless the mode is armed and they are used).
        self.det_id = -1
        self.det_recv = None  # nodes parked in recv/select on this channel
        self.det_send = None  # nodes parked sending to this full channel

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

    __slots__ = ("thread", "done", "joined", "result", "det_id", "det_join")

    def __init__(self, thread):
        self.thread = thread
        self.done = False
        self.joined = False
        self.result = None
        # Stage 127: the det-scheduler task id (for the trace) and the
        # join wait list (nodes parked in join() on this task).
        self.det_id = -1
        self.det_join = None


class ConcRuntime:
    """Global state for spawn / channel operations (one per Interp)."""

    def __init__(self, det=False, trace=False):
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
        # Stage 127 (v0.145.0-alpha): the deterministic scheduler. None
        # unless armed (boot.py --det / HL_DET_SCHED); every op below
        # branches on it, and an unarmed runtime behaves exactly as it
        # always has (one None test per op is the whole cost). The core
        # borrows this runtime's mutex and channel registry: park_on
        # releases mu while a task is parked, and the det deadlock scan
        # walks chans exactly the way the preemptive one does.
        self.det = DetCore(trace) if det else None
        if self.det is not None:
            self.det.mu = self.mu
            self.det.chans = self.chans

    def register(self, chan):
        with self.cv:
            self.chans.append(chan)
            chan._registry = self.chans
            if self.det is not None:
                # The det trace names channels by creation order; the
                # id is minted here so interpreter and native agree.
                chan.det_id = self.det.next_chan_id()

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

    # ---- Stage 127 det helpers (used by the ops below) ----

    def _det_recv_list(self, chan):
        if chan.det_recv is None:
            chan.det_recv = []
        return chan.det_recv

    def _det_send_list(self, chan):
        if chan.det_send is None:
            chan.det_send = []
        return chan.det_send

    def _det_wake_recv(self, chan):
        if chan.det_recv:
            self.det.wake(chan.det_recv)

    def _det_wake_send(self, chan):
        if chan.det_send:
            self.det.wake(chan.det_send)

    def send(self, chan, value):
        """Blocking send: on a bounded channel, waits while full.
        Unbounded channels never block the sender."""
        if self.det is not None:
            self.det.op("send", "ch%d" % chan.det_id)
        with self.cv:
            while self._full(chan):
                chan.send_waiters += 1
                self.blocked += 1
                if self.det is not None:
                    self.det.park_on(self._det_send_list(chan))
                else:
                    self.deadlock_check()
                    self.cv.wait()
                self.blocked -= 1
                chan.send_waiters -= 1
            chan.q.append(value)
            self.msgs += 1
            if self.det is not None:
                self._det_wake_recv(chan)
            else:
                self.cv.notify_all()
        if self.det is not None:
            self.det.yield_baton()

    def try_send(self, chan, value):
        """Non-blocking send: False iff a bounded channel is full (the
        value is NOT enqueued in that case); True otherwise."""
        if self.det is not None:
            self.det.op("try_send", "ch%d" % chan.det_id)
        with self.cv:
            if self._full(chan):
                ok = False
            else:
                chan.q.append(value)
                self.msgs += 1
                ok = True
                if self.det is not None:
                    self._det_wake_recv(chan)
                else:
                    self.cv.notify_all()
        if self.det is not None:
            self.det.yield_baton()
        return ok

    def recv(self, chan, line):
        if self.det is not None:
            self.det.op("recv", "ch%d" % chan.det_id)
        with self.cv:
            while not chan.q:
                chan.waiters += 1
                self.blocked += 1
                if self.det is not None:
                    self.det.park_on(self._det_recv_list(chan))
                else:
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
            if self.det is not None:
                self._det_wake_send(chan)
            else:
                self.cv.notify_all()
        if self.det is not None:
            self.det.yield_baton()
        return value

    def recv_or(self, chan, default):
        """Non-blocking recv: the pending message if one exists, else
        `default` (ownership of the unused default stays with the
        caller in the interpreter — GC makes the release a no-op)."""
        if self.det is not None:
            self.det.op("recv_or", "ch%d" % chan.det_id)
        with self.cv:
            if chan.q:
                value = chan.q.pop(0)
                self.msgs -= 1
                if self.det is not None:
                    self._det_wake_send(chan)  # free capacity -> senders
                else:
                    self.cv.notify_all()  # free capacity -> wake blocked senders
                got = value
            else:
                got = default
        if self.det is not None:
            self.det.yield_baton()
        return got

    def select(self, chans, line):
        if self.det is not None:
            self.det.op("select",
                        ",".join("ch%d" % c.det_id for c in chans))
        with self.cv:
            if not chans:
                raise HLPanic("select() on empty channel list", line)
            while True:
                idx = -1
                for i, c in enumerate(chans):
                    if c.q:
                        idx = i
                        break
                if idx >= 0:
                    if self.det is not None:
                        self._det_wake_send(chans[idx])
                    else:
                        self.cv.notify_all()
                    break
                for c in chans:
                    c.waiters += 1
                self.blocked += 1
                if self.det is not None:
                    # Park on EVERY listed channel: whichever receives a
                    # message first requeues this node (the wake dedups
                    # through the queued flag — a node already woken by
                    # one list is skipped by the others, and stale cells
                    # on the remaining lists are skipped as dead).
                    for c in chans:
                        self._det_recv_list(c).append(self.det.me())
                    self.det.park_on(None)
                else:
                    self.deadlock_check()
                    self.cv.wait()
                self.blocked -= 1
                for c in chans:
                    c.waiters -= 1
        if self.det is not None:
            self.det.yield_baton()
        return idx

    def join(self, task, line):
        if self.det is not None:
            self.det.op("join", "t%d" % task.det_id)
        with self.cv:
            if task.joined:
                raise HLPanic("task already joined", line)
            while not task.done:
                self.blocked += 1
                self.join_waiters += 1
                if self.det is not None:
                    self.det.park_join(task)
                else:
                    self.deadlock_check()
                    self.cv.wait()
                self.blocked -= 1
                self.join_waiters -= 1
            task.joined = True
            self.done_unjoined -= 1
            result = task.result
        if self.det is not None:
            self.det.yield_baton()
        return result

    def task_finished(self, task, result):
        if self.det is not None:
            self.det.op("finish")
        with self.cv:
            task.result = result
            task.done = True
            self.tasks_alive -= 1
            self.done_unjoined += 1
            if self.det is not None:
                # Release the join waiters, then leave the rotation for
                # good: a finished task does not requeue itself (the
                # retire is idempotent; the runner's trailing retire is
                # the same no-op the native trampoline performs).
                self.det.wake_joiners(task)
                self.det.retire_baton()
            else:
                self.cv.notify_all()




__all__ = [
    "ConcRuntime",
    "HLChan",
    "HLTask",
]
