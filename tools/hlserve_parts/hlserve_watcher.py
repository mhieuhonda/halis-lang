"""hlserve_watcher — Stage 75 debounced multi-directory file watcher.

Stage 24's ``FileWatcher`` polled the cwd and ``std/`` for ``.hls``
file mtime changes with a 200 ms debounce. Stage 75 extends it:

* Accepts an arbitrary list of watch roots (the config file's
  ``[watch].dirs`` plus the ``--watch`` CLI flag's args).
* Skips well-known build-output / VCS dirs (``WATCH_IGNORE_DIRS`` in
  ``hlserve_common``) — the Stage 24 watcher only skipped dot-dirs,
  so a 100 MB ``node_modules/`` would slow every poll to a crawl.
* Reports the changed file list to the callback (so the compiler
  can decide whether to log "changed: foo.hls" once per file rather
  than once per recompile).
* Re-entrant lock + a deterministic cooldown so a multi-file save
  (e.g. editor's "save all") triggers exactly one recompile.
* ``stop()`` is now thread-safe and idempotent.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Callable, Dict, List, Optional

from hlserve_common import WATCH_IGNORE_DIRS, debug, info


class FileWatcher(threading.Thread):
    """Watch ``.hls`` files across ``watch_dirs``; call ``on_change``
    (with the list of changed file paths) when changes settle for
    ``debounce_ms`` milliseconds.

    The callback runs in the watcher thread; if it raises, the error
    is logged but the watcher keeps running (a broken on_change must
    not take down the whole dev server).
    """

    def __init__(self,
                 watch_dirs: List[str],
                 on_change: Callable[[List[str]], None],
                 debounce_ms: int = 200,
                 extensions: Optional[List[str]] = None):
        super().__init__(daemon=True, name="hlserve-watcher")
        self.watch_dirs = list(dict.fromkeys(  # dedup, preserve order
            os.path.abspath(d) for d in watch_dirs if d))
        self.on_change = on_change
        self.debounce = max(50, debounce_ms) / 1000.0
        # Extensions to watch. Default: just .hls. Config can add .toml
        # (for hls.serve.toml self-watch), .js, .css (for public dir
        # changes that should trigger a reload).
        self.extensions = tuple(extensions or [".hls"])
        self._mtimes: Dict[str, float] = {}
        self._stop_flag = False
        self._lock = threading.Lock()
        self._pending_files: List[str] = []
        self._pending_since: float = 0.0
        # Pre-seed the mtime table at construction so the FIRST poll
        # after start doesn't treat every existing file as "new".
        # (Stage 24's behaviour was to seed on the first run, which
        # worked but meant the initial scan ran in the watcher thread
        # — delaying the first user-visible event by ~50 ms.)
        self._seed_mtimes()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def stop(self) -> None:
        """Idempotent stop. Safe to call from any thread."""
        self._stop_flag = True

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _seed_mtimes(self) -> None:
        """Populate ``_mtimes`` with the current mtimes of every
        watched file, so the first ``_scan`` after ``start`` only fires
        on files that changed AFTER the watcher was constructed."""
        for d in self.watch_dirs:
            if not os.path.isdir(d):
                continue
            for root, dirs, files in os.walk(d):
                # In-place filter of ignored dirs.
                dirs[:] = [x for x in dirs
                           if x not in WATCH_IGNORE_DIRS
                           and not x.startswith(".")]
                for fn in files:
                    if not fn.endswith(self.extensions):
                        continue
                    full = os.path.join(root, fn)
                    try:
                        self._mtimes[full] = os.path.getmtime(full)
                    except OSError:
                        pass

    def _scan_once(self) -> List[str]:
        """Walk the watch roots once. Returns the list of files whose
        mtime is NEW or CHANGED since the previous scan. Deleted files
        are NOT reported (a delete doesn't trigger a recompile — the
        next save of any remaining file does)."""
        changed: List[str] = []
        seen: set = set()
        for d in self.watch_dirs:
            if not os.path.isdir(d):
                continue
            for root, dirs, files in os.walk(d):
                dirs[:] = [x for x in dirs
                           if x not in WATCH_IGNORE_DIRS
                           and not x.startswith(".")]
                for fn in files:
                    if not fn.endswith(self.extensions):
                        continue
                    full = os.path.join(root, fn)
                    seen.add(full)
                    try:
                        m = os.path.getmtime(full)
                    except OSError:
                        continue
                    prev = self._mtimes.get(full)
                    if prev is None:
                        self._mtimes[full] = m
                        # Treat a newly-discovered file as "changed"
                        # only if it wasn't seeded (i.e. it was created
                        # after start).
                        changed.append(full)
                    elif m != prev:
                        self._mtimes[full] = m
                        changed.append(full)
        # Prune deleted files from the mtime table so it doesn't grow
        # unboundedly over a long-running session.
        if len(self._mtimes) > len(seen) + 64:
            for k in list(self._mtimes.keys()):
                if k not in seen:
                    del self._mtimes[k]
        return changed

    def run(self) -> None:
        """Main loop: scan, debounce, fire callback, repeat."""
        poll_interval = min(self.debounce, 0.1) if self.debounce else 0.1
        while not self._stop_flag:
            try:
                changed = self._scan_once()
            except Exception as e:
                # Defensive: an OSError inside _scan_once shouldn't
                # kill the watcher (a deleted dir mid-walk raises).
                debug("watcher scan raised: %s" % e)
                changed = []
            if changed:
                with self._lock:
                    self._pending_files.extend(changed)
                    # Dedup while preserving order.
                    seen = set()
                    deduped: List[str] = []
                    for f in self._pending_files:
                        if f not in seen:
                            seen.add(f)
                            deduped.append(f)
                    self._pending_files = deduped
                    self._pending_since = time.time()
                    for f in changed:
                        info("change detected: %s" % f)
            time.sleep(poll_interval)
            with self._lock:
                if (self._pending_files
                        and (time.time() - self._pending_since)
                        >= self.debounce):
                    to_fire = list(self._pending_files)
                    self._pending_files = []
                    self._pending_since = 0.0
                else:
                    to_fire = []
            if to_fire:
                try:
                    self.on_change(to_fire)
                except Exception as e:
                    # Don't let a broken callback take down the server.
                    debug("on_change raised: %s" % e)
                    import sys
                    sys.stderr.write("hlserve: on_change failed: %s\n" % e)
