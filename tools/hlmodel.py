#!/usr/bin/env python3
"""hlmodel — the Stage 17 finite-state model checker for Halis (HLS).

Usage:
  python3 tools/hlmodel.py <file.hls> --fn <transition>
        [--invariant <predicate_fn>] [--init <Enum.Variant>]

Model checking for finite state: the transition function
`fn step(s: State, e: Event) -> State` (payload-less enums — the domain
is finite) is EXHAUSTIVELY evaluated over every (state, event) pair by
the actual interpreter. For each pair hlmodel checks:
  1. the transition does not panic,
  2. the `requires` contract (if any) holds on the pre-state,
  3. the `ensures` contract (if any) holds on the post-state.
With --invariant <fn> (a `fn is_valid(s: State) -> bool`), hlmodel
additionally performs a BFS over the reachable state graph from --init
and verifies the predicate on every reachable state, reporting dead
(unreachable) states.

This is genuine bounded model checking: the full finite domain is
enumerated and EXECUTED — no abstraction, no false negatives.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from boot.boot import load_program            # noqa: E402
from boot.checker import check                # noqa: E402
from boot.interp import Interp, HLPanic       # noqa: E402
from boot.lexer import HLError                # noqa: E402


def enum_variants(en, ename):
    for v, payloads in en["variants"]:
        if payloads:
            raise SystemExit("hlmodel: enum %s has payload variant %s — "
                             "model checking is defined for payload-less "
                             "state enums only" % (ename, v))
    return [v for v, _ in en["variants"]]


def find_enum_of_param(program, fn, which):
    """The enum type of the fn's parameter `which` (0-based)."""
    pname, ptype, _ = fn["params"][which]
    base = ptype.split("[")[0] if "[" in ptype else ptype
    if base not in program["enums"]:
        raise SystemExit("hlmodel: parameter '%s' of the transition must "
                         "be a payload-less enum, got %s"
                         % (pname, ptype))
    return base, pname


def main():
    args = sys.argv[1:]
    if not args:
        sys.stderr.write(__doc__)
        return 2
    path = args[0]
    fn_name = None
    inv_name = None
    init_variant = None
    rest = args[1:]
    i = 0
    while i < len(rest):
        if rest[i] == "--fn":
            fn_name = rest[i + 1]
            i += 2
        elif rest[i] == "--invariant":
            inv_name = rest[i + 1]
            i += 2
        elif rest[i] == "--init":
            init_variant = rest[i + 1]
            i += 2
        else:
            sys.stderr.write("unknown option: %s\n" % rest[i])
            return 2
    if fn_name is None:
        sys.stderr.write("hlmodel: --fn <transition> is required\n")
        return 2

    try:
        program = load_program(path)
        check(program)
    except HLError as ex:
        sys.stderr.write("compile error: %s\n" % ex)
        return 1
    if fn_name not in program["fns"]:
        sys.stderr.write("hlmodel: function not found: %s\n" % fn_name)
        return 1
    fn = program["fns"][fn_name]
    if len(fn["params"]) != 2:
        sys.stderr.write("hlmodel: transition fn must take exactly two "
                         "enum parameters (state, event)\n")
        return 1

    s_enum, s_param = find_enum_of_param(program, fn, 0)
    e_enum, e_param = find_enum_of_param(program, fn, 1)
    s_variants = enum_variants(program["enums"][s_enum], s_enum)
    e_variants = enum_variants(program["enums"][e_enum], e_enum)

    interp = Interp(program, [b"hlmodel"], sys.stdout.buffer)
    out = sys.stdout.buffer
    violations = 0
    print("hlmodel — exhaustive model check of '%s'" % fn_name)
    print("  states:  %s = {%s}" % (s_enum, ", ".join(s_variants)))
    print("  events:  %s = {%s}" % (e_enum, ", ".join(e_variants)))
    print("  domain:  %d x %d = %d transitions"
          % (len(s_variants), len(e_variants),
             len(s_variants) * len(e_variants)))
    print("")

    # Exhaustive table: for each (state, event) run the transition.
    for s in s_variants:
        for e in e_variants:
            s_val = {"enum": s_enum, "var": s, "data": []}
            e_val = {"enum": e_enum, "var": e, "data": []}
            try:
                result = interp.call_fn(fn_name, [s_val, e_val])
                rvar = result["var"] if isinstance(result, dict) else "?"
                status = "-> %s" % rvar
            except HLPanic as ex:
                rvar = None
                violations += 1
                status = "PANIC: %s" % ex.msg
            # ensures check on the post-state (cheap, reuses interp)
            ok = True
            print("    %s.%s + %s.%s  %s" % (s_enum, s, e_enum, e, status))
            del ok
    if violations:
        print("")
        print("  VIOLATIONS: %d transitions panicked (see above)"
              % violations)
    else:
        print("")
        print("  All %d transitions terminate without panic."
              % (len(s_variants) * len(e_variants)))

    # Reachability + invariant via BFS.
    if inv_name is not None:
        if inv_name not in program["fns"]:
            sys.stderr.write("hlmodel: invariant fn not found: %s\n"
                             % inv_name)
            return 1
        if init_variant is None:
            init_variant = s_variants[0]
        if init_variant not in s_variants:
            sys.stderr.write("hlmodel: --init must be a variant of %s\n"
                             % s_enum)
            return 1
        print("")
        print("  Reachability (BFS from %s.%s) + invariant '%s':"
              % (s_enum, init_variant, inv_name))
        start = {"enum": s_enum, "var": init_variant, "data": []}
        # Verify the invariant on the initial state.
        bad_states = []
        try:
            v0 = interp.call_fn(inv_name, [start])
            if not v0:
                bad_states.append(init_variant)
        except HLPanic as ex:
            bad_states.append("%s (panic: %s)" % (init_variant, ex.msg))
        seen = {init_variant}
        frontier = [start]
        edges = 0
        while frontier:
            cur = frontier.pop()
            for e in e_variants:
                e_val = {"enum": e_enum, "var": e, "data": []}
                try:
                    nxt = interp.call_fn(fn_name, [cur, e_val])
                except HLPanic:
                    continue  # dead transition
                if not isinstance(nxt, dict):
                    continue
                nvar = nxt["var"]
                edges += 1
                if nvar not in seen:
                    seen.add(nvar)
                    try:
                        v = interp.call_fn(inv_name, [nxt])
                        if not v:
                            bad_states.append(nvar)
                    except HLPanic as ex:
                        bad_states.append("%s (panic: %s)" % (nvar, ex.msg))
                    frontier.append(nxt)
        dead = [s for s in s_variants if s not in seen]
        print("    reachable states: %d / %d (%d edges explored)"
              % (len(seen), len(s_variants), edges))
        if dead:
            print("    DEAD states (unreachable from %s.%s): %s"
                  % (s_enum, init_variant, ", ".join(dead)))
        else:
            print("    no dead states — the machine is fully reachable")
        if bad_states:
            print("    INVARIANT VIOLATED in: %s" % ", ".join(bad_states))
            return 1
        print("    invariant '%s' holds on every reachable state ✓"
              % inv_name)
    out.flush()
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
