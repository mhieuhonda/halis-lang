"""Interp mixin (spawn) - verbatim segment of the original
boot/interp.py Interp class (lines 2582..2921), split for
maintainability. The final Interp class assembles all mixins in
boot/interp_parts/interp.py - behavior is unchanged."""
from .rt import (
    HLChan, HLPanic, HLTask, INT64_MIN_SENTINEL, os, sys, threading, to_display,
)

class InterpSpawn(object):
    def do_spawn(self, fn_key, arg_nodes, env):
        """spawn(f, a1..aN) — start a task running f(args).

        Boundary ownership rule (mirrors the native codegen exactly):
        an owned value crossing the task boundary is deep-copied unless
        it is syntactically clone(...) (already a private deep copy).
        Channels cross by sharing (their internal state is guarded by the
        runtime lock). This guarantees no mutable value is visible to two
        threads at once — the interpreter-side mirror of the native
        refcount discipline."""
        values = []
        for a in arg_nodes:
            v = self.eval_expr(a, env)
            if isinstance(v, (list, dict)) and not (
                    a.get("k") == "call" and a.get("name") == "clone"):
                v = self.deep_clone(v)
            values.append(v)
        conc = self.conc
        task = HLTask(None)
        interp = self

        def runner():
            try:
                result = interp.call_fn(fn_key, values)
            except HLPanic as ex:
                # Safe-halt semantics: a panic in ANY task halts the whole
                # process (tasks share the process fate).
                interp.out.flush()
                sys.stderr.write("panic: %s (at line %d)\n"
                                 % (to_display(ex.msg), ex.line))
                os._exit(101)
            except SystemExit as ex:
                interp.out.flush()
                code = ex.code if isinstance(ex.code, int) else 0
                os._exit(code & 0xFF)
            except BaseException as ex:
                # Deep-scan-10 fix: an unexpected interpreter-level error
                # inside a task (e.g. RecursionError from runaway HLS
                # recursion) previously killed only the Python thread —
                # task_finished was never called, so join() waited
                # forever and the deadlock detector could never fire
                # (blocked < alive forever): the process HUNG instead of
                # safe-halting. Any unexpected task-side failure now
                # halts the whole process with a clean panic (101), the
                # same policy main-thread recursion already follows.
                interp.out.flush()
                sys.stderr.write("panic: %s (in task)\n" % ex)
                os._exit(101)
            conc.task_finished(task, result)

        with conc.cv:
            conc.tasks_alive += 1
        # Daemon thread: the process must be able to exit while a task is
        # still blocked in recv/select (mirrors the native semantics where
        # main() returning terminates the whole process, threads included).
        # Without daemon=True a deadlocked-at-exit task would hang the
        # interpreter at shutdown (Python waits for non-daemon threads).
        t = threading.Thread(target=runner)
        t.daemon = True
        task.thread = t
        t.start()
        return task

    # ---------- Stage 33 (v0.52.0-alpha): async_spawn ----------
    def do_async_spawn(self, fn_key, arg_nodes, env):
        """async_spawn(f, a1..aN) -> Future[R] — like spawn, but the
        result is delivered via a cap-1 bounded channel (the Future).

        The Future is represented at runtime as an HLChan with cap=1
        (bounded — backpressure: the producing task blocks at send time
        until the consumer takes the value, ensuring no unbounded
        buffering). The spawned task calls f(args...) and sends the
        result on the channel; await(fut) is a chan.recv().

        Boundary ownership rule: same as do_spawn (deep-copy owned
        values unless syntactically clone(...) or fresh).
        """
        # Evaluate + boundary-clone the arguments (same logic as do_spawn).
        values = []
        for a in arg_nodes:
            v = self.eval_expr(a, env)
            if isinstance(v, (list, dict)) and not (
                    a.get("k") == "call" and a.get("name") == "clone"):
                v = self.deep_clone(v)
            values.append(v)
        # Create the result channel (cap 1, bounded).
        result_chan = HLChan(1)
        self.conc.register(result_chan)
        conc = self.conc
        interp = self

        def runner():
            try:
                result = interp.call_fn(fn_key, values)
                # Send the result on the channel. On a cap-1 bounded
                # channel this blocks until the consumer takes it
                # (backpressure — the producer does not race ahead).
                # The result may be a primitive (int/float/bool/str)
                # or a heap value (list/dict). Channels deep-clone
                # owned values at the send boundary (same rule as
                # chan.send) — but our `result` is FRESH (the callee
                # just returned it), so no defensive clone is needed.
                conc.send(result_chan, result)
            except HLPanic as ex:
                interp.out.flush()
                sys.stderr.write("panic: %s (at line %d)\n"
                                 % (to_display(ex.msg), ex.line))
                os._exit(101)
            except SystemExit as ex:
                interp.out.flush()
                code = ex.code if isinstance(ex.code, int) else 0
                os._exit(code & 0xFF)
            except BaseException as ex:
                interp.out.flush()
                sys.stderr.write("panic: %s (in async task)\n" % ex)
                os._exit(101)
            # Note: we do NOT call conc.task_finished here because
            # async_spawn does not return a Task join handle — the
            # caller awaits the Future (the result channel) instead.
            # But we MUST decrement tasks_alive so the deadlock
            # detector's "alive" count stays correct.
            with conc.cv:
                conc.tasks_alive -= 1
                conc.cv.notify_all()

        with conc.cv:
            conc.tasks_alive += 1
        t = threading.Thread(target=runner)
        t.daemon = True
        t.start()
        # The Future IS the result channel (same runtime representation).
        return result_chan

    # ---------- Stage 34 (v0.53.0-alpha): gen_spawn ----------
    def do_gen_spawn(self, fn_key, arg_nodes, env):
        """gen_spawn(f, args...) -> Stream[T] — create a bounded stream,
        spawn f(stream, args...), return the stream.

        The target function f must take the Stream as its FIRST parameter
        (the checker enforces this). The generator writes values into the
        stream via stream_send and signals end-of-stream via stream_close
        (which sends a sentinel — for int streams, INT64_MIN).
        """
        # Default capacity for generator streams: 16 (bounded — backpressure
        # without excessive latency). The user can override by creating the
        # stream manually with stream_new(cap) and spawning the generator
        # with plain spawn().
        stream = HLChan(16)
        self.conc.register(stream)
        # Build the argument list: stream first, then the user's args.
        values = [stream]
        for a in arg_nodes:
            v = self.eval_expr(a, env)
            if isinstance(v, (list, dict)) and not (
                    a.get("k") == "call" and a.get("name") == "clone"):
                v = self.deep_clone(v)
            values.append(v)
        conc = self.conc
        interp = self

        def runner():
            try:
                # The generator function takes (stream, args...) and
                # returns void (it just writes to the stream).
                interp.call_fn(fn_key, values)
            except HLPanic as ex:
                interp.out.flush()
                sys.stderr.write("panic: %s (at line %d)\n"
                                 % (to_display(ex.msg), ex.line))
                os._exit(101)
            except SystemExit as ex:
                interp.out.flush()
                code = ex.code if isinstance(ex.code, int) else 0
                os._exit(code & 0xFF)
            except BaseException as ex:
                interp.out.flush()
                sys.stderr.write("panic: %s (in generator task)\n" % ex)
                os._exit(101)
            with conc.cv:
                conc.tasks_alive -= 1
                conc.cv.notify_all()

        with conc.cv:
            conc.tasks_alive += 1
        t = threading.Thread(target=runner)
        t.daemon = True
        t.start()
        return stream

    # ---------- Stage 34: stream combinators ----------
    def do_stream_combinator(self, kind, fn_key, arg_nodes, env):
        """stream_map_int / stream_filter_int / stream_fold_int /
        stream_flat_map_int — each spawns a worker that reads from the
        input stream, applies fn_key, and writes to the output stream
        (or, for fold, accumulates and returns the final value).

        For map/filter/flat_map: creates an output Stream[int] (cap 16),
        spawns a worker, returns the output stream. The pipeline runs
        concurrently — the consumer of the output stream pulls values
        as needed, and backpressure propagates through the bounded
        channels.

        For fold: BLOCKS the caller (drains the input stream to
        completion, applying fn_key to accumulate). Returns int.
        """
        # Evaluate the input stream argument.
        in_stream = self.eval_expr(arg_nodes[0], env)
        if kind == "stream_fold_int":
            # Blocking fold: drain the stream, apply fn_key(acc, v), return acc.
            init = self.eval_expr(arg_nodes[1], env)
            acc = init
            while True:
                v = self.conc.recv(in_stream, self.line)
                # Sentinel check: INT64_MIN signals end-of-stream.
                if v == INT64_MIN_SENTINEL:
                    break
                acc = self.call_fn(fn_key, [acc, v])
            return acc
        # map / filter / flat_map: create output stream, spawn worker.
        out_stream = HLChan(16)
        self.conc.register(out_stream)
        conc = self.conc
        interp = self

        def runner():
            try:
                if kind == "stream_map_int":
                    while True:
                        v = conc.recv(in_stream, interp.line)
                        if v == INT64_MIN_SENTINEL:
                            conc.send(out_stream, INT64_MIN_SENTINEL)
                            return
                        r = interp.call_fn(fn_key, [v])
                        conc.send(out_stream, r)
                elif kind == "stream_filter_int":
                    while True:
                        v = conc.recv(in_stream, interp.line)
                        if v == INT64_MIN_SENTINEL:
                            conc.send(out_stream, INT64_MIN_SENTINEL)
                            return
                        keep = interp.call_fn(fn_key, [v])
                        if keep:
                            conc.send(out_stream, v)
                elif kind == "stream_flat_map_int":
                    while True:
                        v = conc.recv(in_stream, interp.line)
                        if v == INT64_MIN_SENTINEL:
                            conc.send(out_stream, INT64_MIN_SENTINEL)
                            return
                        # fn_key(v) returns a Stream[int] (an HLChan).
                        inner = interp.call_fn(fn_key, [v])
                        # Forward all values from the inner stream.
                        while True:
                            iv = conc.recv(inner, interp.line)
                            if iv == INT64_MIN_SENTINEL:
                                break
                            conc.send(out_stream, iv)
            except HLPanic as ex:
                interp.out.flush()
                sys.stderr.write("panic: %s (at line %d)\n"
                                 % (to_display(ex.msg), ex.line))
                os._exit(101)
            except SystemExit as ex:
                interp.out.flush()
                code = ex.code if isinstance(ex.code, int) else 0
                os._exit(code & 0xFF)
            except BaseException as ex:
                interp.out.flush()
                sys.stderr.write("panic: %s (in stream combinator)\n" % ex)
                os._exit(101)
            with conc.cv:
                conc.tasks_alive -= 1
                conc.cv.notify_all()

        with conc.cv:
            conc.tasks_alive += 1
        t = threading.Thread(target=runner)
        t.daemon = True
        t.start()
        return out_stream

    def deep_clone(self, v, _seen=None):
        """Deep-copy an HLS runtime value (Stage-0 / Python)."""
        # Stage 27 perfection (v0.50.3-alpha) deep-scan-18: BUG-03 fix.
        # Add cycle detection via a `_seen` set keyed by id(v). A user-
        # constructed cyclic struct (e.g. `let mut n = Node{children:[]};
        # n.children.push(n)` — lists have reference semantics, so push
        # aliases the dict) previously caused unbounded Python recursion
        # and a RecursionError caught by boot.py as "stack overflow"
        # (exit 101). The checker's `clone_supported` already has a
        # `_seen` guard and ACCEPTS cyclic types — so the runtime HANG
        # was a soundness gap between checker and runtime. The fix:
        # on revisiting a seen object, return the already-cloned copy
        # (breaking the cycle by aliasing the clone — matches what the
        # native codegen would do via the typed hl_clone_<Struct> helper
        # if/when it grows the same cycle support).
        if _seen is None:
            _seen = {}
        if isinstance(v, (dict, list)):
            vid = id(v)
            if vid in _seen:
                return _seen[vid]
        if isinstance(v, HLChan):
            # Stage 16: a channel clones by SHARING (that is its purpose —
            # the queue is guarded by the runtime lock). Mirrors the
            # native hl_chan_clone (atomic refcount + 1).
            return v
        if isinstance(v, HLTask):
            # Stage 16: a Task join handle is single-consumer and cannot
            # be cloned (the checker rejects this; defensive halt here).
            raise HLPanic("cannot clone a Task join handle", self.line)
        if isinstance(v, bytes):
            return bytes(v)  # strings are immutable, shallow copy is fine
        if isinstance(v, list):
            new_list = []
            _seen[id(v)] = new_list
            for x in v:
                new_list.append(self.deep_clone(x, _seen))
            return new_list
        if isinstance(v, dict):
            # SCAN-A fix: distinguish enum values from struct values. An
            # enum value is `{"enum": name, "var": variant, "data": [...]}` —
            # check for ALL THREE keys. A struct value with a field literally
            # named "enum" would be `{"enum": value}` — missing "var" and
            # "data" — so it must be treated as a struct (a plain dict).
            if "enum" in v and "var" in v and "data" in v:
                new_enum = {"enum": v["enum"], "var": v["var"], "data": []}
                _seen[id(v)] = new_enum
                new_enum["data"] = [self.deep_clone(x, _seen) for x in v["data"]]
                return new_enum
            # map[str, T] — copy insertion-ordered dict. Also covers
            # struct values (which are dicts of field_name -> value).
            new = {}
            _seen[id(v)] = new
            for k in v:
                new[k] = self.deep_clone(v[k], _seen)
            return new
        # primitives (int, float, bool, None)
        return v

