/* tools/rss_parts/malloc_balance_wrap.c — Stage 125 (v0.143.0-alpha)
 *
 * Link-time interposer for hls-rss, the Stage 125 RSS-stability
 * verifier. Linked with
 *
 *     -Wl,--wrap=malloc -Wl,--wrap=calloc \
 *     -Wl,--wrap=realloc -Wl,--wrap=free
 *
 * it tracks every heap allocation the compiled Halis program makes
 * and prints the totals to stderr at process exit (atexit, so the
 * report survives both `return 0` and the runtime's exit(101) panic
 * path; only a hard kill loses it, which the verifier treats as an
 * instrument failure, never as a pass):
 *
 *     HL_RSS_MALLOC=<n>          malloc calls (tracked)
 *     HL_RSS_CALLOC=<n>          calloc calls (tracked)
 *     HL_RSS_REALLOC=<n>         realloc calls (p != NULL, n > 0)
 *     HL_RSS_REALLOC_NULL=<n>    realloc(NULL, n) — semantically malloc
 *     HL_RSS_REALLOC_ZERO=<n>    realloc(p, 0)  — semantically free
 *     HL_RSS_FREE=<n>            free(p) calls of TRACKED pointers
 *     HL_RSS_FOREIGN_FREE=<n>    free(p) of pointers the program never
 *                                allocated through the wrapped path —
 *                                buffers libc allocated internally
 *                                (e.g. getcwd(NULL, 0)) that the
 *                                runtime hands back to free(); not a
 *                                leak, not a balance event
 *     HL_RSS_LIVE=<n>            live tracked allocations right now
 *     HL_RSS_OVERFLOW=<0|1>      1 when the tracking table hit its
 *                                hard capacity and the balance
 *                                degraded (the verifier refuses to
 *                                certify on it)
 *
 * LIVE is the exact-free instrument: for a clean, non-leaking exit it
 * is a small CONSTANT (the stdio buffers and whatever the runtime
 * legitimately retains), and — the point of Stage 125 — INDEPENDENT
 * of how many work cycles the program performed. The verifier runs
 * the same workload at two cycle counts and demands the same LIVE;
 * a retained-per-cycle object (a leak, or a scope frozen by an
 * in-scope exit(0)) moves it, whatever the OS page granularity did
 * to RSS.
 *
 * Two properties the instrument owes the measurement:
 *
 *   1. Pointer TRACKING keeps the balance honest. --wrap intercepts
 *      only calls from the program's own object files, so an
 *      allocation performed INSIDE libc (getcwd's buffer, stdio
 *      internals reached without crossing the TU boundary) never
 *      appears as an allocation here — but the runtime returning such
 *      a buffer to free() WOULD appear as an unbalanced free if
 *      frees were counted blindly (that misbalance is how a
 *      counting-only draft of this file reported LIVE = -1994 for
 *      2000 cwd_get() calls). Only frees of pointers this
 *      interposer allocated count; the rest pass through uncounted
 *      as FOREIGN_FREE.
 *
 *   2. The table must not perturb RSS. A static 32 MiB BSS table (the
 *      first draft) hash-spreads live pointers one per page and
 *      touches a 4 KiB page per 8-byte slot used — the INSTRUMENT
 *      added ~7500 resident pages to a 250-page program, and the
 *      verifier spent its time measuring the measurement. The table
 *      here is dynamically grown through __real_malloc directly
 *      (invisible to the counters, no re-entrancy), doubling from
 *      4096 slots at 70% load: the resident cost stays proportional
 *      to the LIVE set (a few KiB for realistic workloads), not to
 *      the address-space reservation.
 *
 * Differences from the Stage 30 interposer (tests/memcheck/
 * malloc_count_wrap.c), which counted mallocs only, for the escape-
 * analysis gate: frees are tracked too (the balance is the signal,
 * not the volume); the tracking table makes the balance immune to
 * libc-internal allocation pairs and keeps its own RSS footprint
 * proportional to the live set; the table is guarded by a mutex —
 * the Halis runtime spawns real pthreads (hl_spawn), and concurrent
 * allocations from task entry functions must not corrupt it (the
 * counters remain C11 atomics so uncontended runs stay exact);
 * free(NULL) is neither a free nor a live change (glibc's own no-op
 * semantics), and realloc's two degenerate forms are classified, not
 * lumped, so the LIVE identity stays exact.
 */
#include <stdio.h>
#include <stdlib.h>
#include <stddef.h>
#include <string.h>
#include <pthread.h>

/* The --wrap linker option redirects calls from the program's TU to
 * the __wrap_ symbols; the real libc functions are reachable as
 * __real_* but need explicit prototypes. */
extern void* __real_malloc(size_t n);
extern void* __real_calloc(size_t n, size_t sz);
extern void* __real_realloc(void* p, size_t n);
extern void  __real_free(void* p);

/* ---- the live-pointer table ------------------------------------------------
 *
 * Dynamically grown open addressing (linear probe, tombstones on
 * removal). All of the table's own memory comes from __real_malloc
 * DIRECTLY — it never passes through the wraps, so it is invisible
 * to the counters and cannot re-enter them.
 */
#define RSS_INIT_BITS 12                     /* 4096 slots, 32 KiB   */
#define RSS_MAX_BITS 26                      /* 64 Mi slots hard cap */
#define RSS_TOMB ((void*)1)

static void** g_tab = NULL;                  /* slots */
static size_t g_cap = 0;                     /* power of two */
static size_t g_used = 0;                    /* live + tombstones */
static size_t g_live = 0;                    /* live entries */
static int g_overflow = 0;
static pthread_mutex_t g_mu = PTHREAD_MUTEX_INITIALIZER;

static size_t rss_hash(void* p) {
    unsigned long long h = (unsigned long long)(size_t)p;
    h >>= 4; /* allocations are at least 16-byte aligned in practice */
    h *= 0x9E3779B97F4A7C15ULL;
    return (size_t)h;
}

static int rss_grow(void) {
    size_t nbits = RSS_INIT_BITS;
    while (((size_t)1 << nbits) <= g_cap && nbits < RSS_MAX_BITS) {
        nbits++;
    }
    if (nbits >= RSS_MAX_BITS && ((size_t)1 << nbits) <= g_cap) {
        return 0;                            /* hard cap reached */
    }
    size_t ncap = (size_t)1 << nbits;
    void** nt = (void**)__real_malloc(ncap * sizeof(void*));
    if (!nt) return 0;
    memset(nt, 0, ncap * sizeof(void*));
    /* rehash live entries into the new table (skip tombstones) */
    for (size_t i = 0; i < g_cap; i++) {
        void* k = g_tab[i];
        if (k == NULL || k == RSS_TOMB) continue;
        size_t j = rss_hash(k) & (ncap - 1);
        while (nt[j] != NULL) j = (j + 1) & (ncap - 1);
        nt[j] = k;
    }
    if (g_tab) __real_free(g_tab);
    g_tab = nt;
    g_cap = ncap;
    g_used = g_live;
    return 1;
}

/* returns 1 inserted, 0 table failed (overflow flagged by caller) */
static int rss_insert(void* p) {
    if (p == NULL) return 1;
    if (g_cap == 0 || (g_used + 1) * 10 >= g_cap * 7) {
        if (!rss_grow()) return 0;
    }
    size_t i = rss_hash(p) & (g_cap - 1);
    for (size_t n = 0; n < g_cap; n++) {
        void* k = g_tab[i];
        if (k == p) return 1;                /* already tracked */
        if (k == NULL) {
            g_tab[i] = p;
            g_used++;
            g_live++;
            return 1;
        }
        if (k == RSS_TOMB) {
            g_tab[i] = p;                    /* reuse the tombstone */
            g_live++;
            /* g_used unchanged: tombstone became live */
            return 1;
        }
        i = (i + 1) & (g_cap - 1);
    }
    return 0;
}

/* returns 1 when the pointer was tracked, 0 when not present */
static int rss_remove(void* p) {
    if (p == NULL) return 1;
    if (g_cap == 0) return 0;
    size_t i = rss_hash(p) & (g_cap - 1);
    for (size_t n = 0; n < g_cap; n++) {
        void* k = g_tab[i];
        if (k == NULL) return 0;             /* not present */
        if (k == p) {
            g_tab[i] = RSS_TOMB;
            g_live--;
            return 1;
        }
        i = (i + 1) & (g_cap - 1);
    }
    return 0;
}

/* ---- the counters (atomics: exact even under contention) ----------------- */
static _Atomic long g_mallocs = 0;
static _Atomic long g_callocs = 0;
static _Atomic long g_reallocs = 0;
static _Atomic long g_realloc_null = 0;
static _Atomic long g_realloc_zero = 0;
static _Atomic long g_frees = 0;
static _Atomic long g_foreign_frees = 0;

void* __wrap_malloc(size_t n) {
    void* p = __real_malloc(n);
    pthread_mutex_lock(&g_mu);
    if (!rss_insert(p)) g_overflow = 1;
    pthread_mutex_unlock(&g_mu);
    if (p) g_mallocs++;
    return p;
}

void* __wrap_calloc(size_t n, size_t sz) {
    void* p = __real_calloc(n, sz);
    pthread_mutex_lock(&g_mu);
    if (!rss_insert(p)) g_overflow = 1;
    pthread_mutex_unlock(&g_mu);
    if (p) g_callocs++;
    return p;
}

void* __wrap_realloc(void* p, size_t n) {
    if (p == NULL) {
        /* realloc(NULL, n) is a fresh allocation (the Stage 30 wrap
         * classified it the same way); LIVE must count it. */
        void* q = __real_realloc(p, n);
        pthread_mutex_lock(&g_mu);
        if (!rss_insert(q)) g_overflow = 1;
        pthread_mutex_unlock(&g_mu);
        if (q) g_realloc_null++;
        return q;
    }
    if (n == 0) {
        /* C89/glibc semantics: realloc(p, 0) frees p and returns NULL
         * (some libcs return a minimal block; the counter records the
         * intent, LIVE treats it as a free, and the Halis runtime
         * never emits this form today — the list growth path guards
         * n > 0). */
        pthread_mutex_lock(&g_mu);
        int was = rss_remove(p);
        pthread_mutex_unlock(&g_mu);
        void* q = __real_realloc(p, n);
        if (was) g_realloc_zero++;
        return q;
    }
    /* In-place growth keeps LIVE constant when the pointer is
     * unchanged; a moved block is a remove + insert. */
    void* q = __real_realloc(p, n);
    pthread_mutex_lock(&g_mu);
    if (q == p) {
        /* same address: still tracked */
    } else {
        rss_remove(p);
        if (!rss_insert(q)) g_overflow = 1;
    }
    pthread_mutex_unlock(&g_mu);
    g_reallocs++;
    return q;
}

void __wrap_free(void* p) {
    if (p != NULL) {
        int tracked;
        pthread_mutex_lock(&g_mu);
        tracked = rss_remove(p);
        pthread_mutex_unlock(&g_mu);
        if (tracked) {
            g_frees++;
        } else {
            /* The program frees a pointer it never allocated through
             * the wrapped path — a buffer libc allocated internally
             * (getcwd(NULL, 0) is the canonical case). Pass it through
             * uncounted: it is neither a leak nor a balance event. */
            g_foreign_frees++;
        }
    }
    /* free(NULL) is a no-op on the counters as well as in glibc. */
    __real_free(p);
}

static void hl_rss_report_balance(void) {
    long m = g_mallocs, c = g_callocs, rn = g_realloc_null;
    long rz = g_realloc_zero, f = g_frees;
    pthread_mutex_lock(&g_mu);
    long live = (long)g_live;
    pthread_mutex_unlock(&g_mu);
    fflush(stdout);
    /* fprintf to stderr is unbuffered by default and does not call
     * the wrapped malloc from THIS TU, so no re-entrancy hazard. */
    fprintf(stderr, "HL_RSS_MALLOC=%ld\n", m);
    fprintf(stderr, "HL_RSS_CALLOC=%ld\n", c);
    fprintf(stderr, "HL_RSS_REALLOC=%ld\n", (long)g_reallocs);
    fprintf(stderr, "HL_RSS_REALLOC_NULL=%ld\n", rn);
    fprintf(stderr, "HL_RSS_REALLOC_ZERO=%ld\n", rz);
    fprintf(stderr, "HL_RSS_FREE=%ld\n", f);
    fprintf(stderr, "HL_RSS_FOREIGN_FREE=%ld\n", (long)g_foreign_frees);
    fprintf(stderr, "HL_RSS_LIVE=%ld\n", live);
    fprintf(stderr, "HL_RSS_OVERFLOW=%d\n", g_overflow);
}

__attribute__((constructor))
static void hl_rss_install_balance_report(void) {
    atexit(hl_rss_report_balance);
}
