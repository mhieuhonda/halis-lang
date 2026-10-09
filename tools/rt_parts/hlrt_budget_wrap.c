/* tools/rt_parts/hlrt_budget_wrap.c — Stage 126 (v0.144.0-alpha)
 *
 * The hls-rt allocation instrument. Linked with
 *
 *     -Wl,--wrap=malloc -Wl,--wrap=calloc \
 *     -Wl,--wrap=realloc -Wl,--wrap=free
 *
 * it counts every heap allocation the compiled Halis program makes,
 * attributes it to the OPEN cycle, and — on hlrt_cycle_mark(idx), which
 * the runtime's Stage 126 mode calls the moment the program prints a
 * `__HLRT_CYCLE__ <i>` marker — snapshots the just-closed cycle and opens
 * the next one. The per-cycle ledger is printed to stderr at process
 * exit (atexit, so the report survives both `return 0` and the runtime's
 * exit(101) panic path; only a hard kill loses it, which the verifier
 * treats as an instrument failure, never as a pass):
 *
 *     HLRT_REPORT_BEGIN
 *     HLRT_CYCLE=<i> ALLOCS=<n> BYTES=<b> LIVE=<lb> FREES=<f> NANOS=<ns>
 *     ...one line per closed cycle, in mark order...
 *     HLRT_TAIL ALLOCS=<n> BYTES=<b>       (open at exit: the window
 *                                           after the last mark)
 *     HLRT_TOTAL ALLOCS=<n> BYTES=<b> FREES=<f>   (closed + tail)
 *     HLRT_LIVE_EXIT=<n>                   (live tracked allocations)
 *     HLRT_OVERFLOW=<0|1>                  (tracking table hit its cap)
 *     HLRT_REPORT_END
 *
 * The report is the verifier's only sensor, so it is written between
 * BEGIN/END markers (a program that prints HLRT_ look-alikes to stderr
 * cannot inject records into the middle of it) and the verifier
 * re-derives the totals from the per-cycle lines before trusting any of
 * it — the same trust-nothing rule the Stage 125 balance report is read
 * with.
 *
 * Division of labour with the runtime (SPEC section 66): the instrument
 * is a LEDGER, never a judge. It does not read the budget variables, it
 * does not enforce anything, it cannot end the program — the runtime's
 * hl_srt_on_mark owns arming and enforcement and merely rolls this
 * counter set at each mark. That separation is what makes --no-enforce
 * honest: the same ledger is produced whether or not anyone is judging.
 *
 * Three properties the instrument owes the measurement (inherited from
 * the Stage 125 interposer, which earned each of them the hard way):
 *
 *   1. Pointer TRACKING with sizes. The per-cycle BYTES axis needs the
 *      requested size of every live allocation (realloc can change it;
 *      a move is a remove+insert, in-place growth updates in place).
 *      Only frees of pointers this interposer allocated count — libc's
 *      internal buffers (getcwd(NULL, 0) is the canonical case) pass
 *      through uncounted, exactly as in malloc_balance_wrap.c.
 *
 *   2. The table's own memory comes from __real_malloc DIRECTLY — it
 *      never passes through the wraps, so it is invisible to the
 *      counters and cannot re-enter them. Growing through the wrapped
 *      path would count the instrument's own bookkeeping into whatever
 *      cycle happens to be open — the verifier measuring the
 *      measurement, again.
 *
 *   3. Thread safety. The Halis runtime spawns real pthreads (hl_spawn),
 *      so the table is mutex-guarded and the counters are exact under
 *      the same mutex — concurrent allocations from task entry functions
 *      must not corrupt the ledger. A multi-threaded cycle's numbers are
 *      the UNION of its threads' traffic (the mode documents that the
 *      steady-state markers should come from one thread; the ledger
 *      stays correct regardless).
 */
#include <stdio.h>
#include <stdlib.h>
#include <stddef.h>
#include <string.h>
#include <pthread.h>
#include <time.h>

/* The --wrap linker option redirects calls from the program's TU to
 * the __wrap_ symbols; the real libc functions are reachable as
 * __real_* but need explicit prototypes. */
extern void* __real_malloc(size_t n);
extern void* __real_calloc(size_t n, size_t sz);
extern void* __real_realloc(void* p, size_t n);
extern void  __real_free(void* p);

/* ---- the live-pointer table (ptr -> size) --------------------------------
 *
 * Dynamically grown open addressing with linear probing and tombstones,
 * exactly the malloc_balance_wrap.c design plus a parallel size slot per
 * entry. All of the table's own memory comes from __real_malloc
 * DIRECTLY.
 */
#define HLRT_INIT_BITS 12                     /* 4096 slots, 64 KiB   */
#define HLRT_MAX_BITS 26                      /* 64 Mi slots hard cap */
#define HLRT_TOMB ((void*)1)

static void** g_tab = NULL;                   /* keys */
static size_t* g_sz = NULL;                   /* requested sizes */
static size_t g_cap = 0;                      /* power of two */
static size_t g_used = 0;                     /* live + tombstones */
static size_t g_live = 0;                     /* live entries */
static size_t g_live_bytes = 0;               /* sum of live sizes */
static int g_overflow = 0;
static pthread_mutex_t g_mu = PTHREAD_MUTEX_INITIALIZER;

static size_t hlrt_hash(void* p) {
    unsigned long long h = (unsigned long long)(size_t)p;
    h >>= 4; /* allocations are at least 16-byte aligned in practice */
    h *= 0x9E3779B97F4A7C15ULL;
    return (size_t)h;
}

static int hlrt_grow(void) {
    size_t nbits = HLRT_INIT_BITS;
    while (((size_t)1 << nbits) <= g_cap && nbits < HLRT_MAX_BITS) {
        nbits++;
    }
    if (nbits >= HLRT_MAX_BITS && ((size_t)1 << nbits) <= g_cap) {
        return 0;                             /* hard cap reached */
    }
    size_t ncap = (size_t)1 << nbits;
    void** nt = (void**)__real_malloc(ncap * sizeof(void*));
    size_t* ns = (size_t*)__real_malloc(ncap * sizeof(size_t));
    if (!nt || !ns) {
        __real_free(nt);
        __real_free(ns);
        return 0;
    }
    memset(nt, 0, ncap * sizeof(void*));
    /* rehash live entries into the new table (skip tombstones) */
    for (size_t i = 0; i < g_cap; i++) {
        void* k = g_tab[i];
        if (k == NULL || k == HLRT_TOMB) continue;
        size_t j = hlrt_hash(k) & (ncap - 1);
        while (nt[j] != NULL) j = (j + 1) & (ncap - 1);
        nt[j] = k;
        ns[j] = g_sz[i];
    }
    __real_free(g_tab);
    __real_free(g_sz);
    g_tab = nt;
    g_sz = ns;
    g_cap = ncap;
    g_used = g_live;
    return 1;
}

/* returns 1 inserted, 0 table failed (overflow flagged by caller) */
static int hlrt_insert(void* p, size_t n) {
    if (p == NULL) return 1;
    if (g_cap == 0 || (g_used + 1) * 10 >= g_cap * 7) {
        if (!hlrt_grow()) return 0;
    }
    size_t i = hlrt_hash(p) & (g_cap - 1);
    for (size_t s = 0; s < g_cap; s++) {
        void* k = g_tab[i];
        if (k == p) {
            g_sz[i] = n;                     /* re-insert of a live ptr */
            return 1;
        }
        if (k == NULL) {
            g_tab[i] = p;
            g_sz[i] = n;
            g_used++;
            g_live++;
            g_live_bytes += n;
            return 1;
        }
        if (k == HLRT_TOMB) {
            g_tab[i] = p;                    /* reuse the tombstone */
            g_sz[i] = n;
            g_live++;
            g_live_bytes += n;
            return 1;
        }
        i = (i + 1) & (g_cap - 1);
    }
    return 0;
}

/* returns 1 when the pointer was tracked (its size through *n_out when
 * n_out is non-NULL), 0 when not present */
static int hlrt_remove(void* p, size_t* n_out) {
    if (p == NULL) return 1;
    if (g_cap == 0) return 0;
    size_t i = hlrt_hash(p) & (g_cap - 1);
    for (size_t s = 0; s < g_cap; s++) {
        void* k = g_tab[i];
        if (k == NULL) return 0;             /* not present */
        if (k == p) {
            g_tab[i] = HLRT_TOMB;
            if (n_out) *n_out = g_sz[i];
            g_live--;
            g_live_bytes -= g_sz[i];
            return 1;
        }
        i = (i + 1) & (g_cap - 1);
    }
    return 0;
}

/* ---- the per-cycle counters ----------------------------------------------
 *
 * The OPEN cycle accumulates here; hlrt_cycle_mark snapshots it into the
 * closed record, appends the record to the ledger, and resets. Every
 * writer holds the table mutex, so the ledger is exact even when the
 * program's task threads allocate concurrently with a mark.
 */
static long g_cur_allocs = 0;                 /* open cycle: alloc events */
static size_t g_cur_bytes = 0;                /* open cycle: bytes asked */
static long g_cur_frees = 0;                  /* open cycle: tracked frees */

/* The closed-cycle snapshot: what mark() just rolled out of the open
 * accumulators. The runtime's getters read THIS — at the moment
 * hl_srt_on_mark runs, the open cycle is the NEXT one (already reset),
 * and the numbers the budget applies to are the ones below. */
static long long g_snap_allocs = 0;
static long long g_snap_bytes = 0;
static long long g_snap_live = 0;

/* the closed-cycle ledger (grown through __real_malloc directly) */
typedef struct {
    long long idx;
    long long allocs;
    long long bytes;
    long long live;
    long long frees;
    long long nanos;
} hlrt_rec;

static hlrt_rec* g_recs = NULL;
static size_t g_nrecs = 0;
static size_t g_caprecs = 0;

/* totals for the exit report */
static long long g_tot_allocs = 0;
static long long g_tot_bytes = 0;
static long long g_tot_frees = 0;

/* the mark clock: cycle 0 opens at constructor time */
static struct timespec g_last;
static int g_have_clock = 0;

static long long hlrt_nanos_between(struct timespec a, struct timespec b) {
    return (long long)(b.tv_sec - a.tv_sec) * 1000000000LL
         + (long long)(b.tv_nsec - a.tv_nsec);
}

static int g_reporting = 0;                   /* HLRT_REPORT=1 */

/* ---- the instrument surface (the runtime's weak hooks) ------------------- */

void* __wrap_malloc(size_t n) {
    void* p = __real_malloc(n);
    if (p) {
        pthread_mutex_lock(&g_mu);
        if (!hlrt_insert(p, n)) g_overflow = 1;
        g_cur_allocs++;
        g_cur_bytes += n;
        pthread_mutex_unlock(&g_mu);
    }
    return p;
}

void* __wrap_calloc(size_t n, size_t sz) {
    void* p = __real_calloc(n, sz);
    if (p) {
        size_t bytes = (sz != 0 && n > (size_t)-1 / sz) ? (size_t)-1
                                                        : n * sz;
        pthread_mutex_lock(&g_mu);
        if (!hlrt_insert(p, bytes)) g_overflow = 1;
        g_cur_allocs++;
        g_cur_bytes += bytes;
        pthread_mutex_unlock(&g_mu);
    }
    return p;
}

void* __wrap_realloc(void* p, size_t n) {
    if (p == NULL) {
        /* realloc(NULL, n) is a fresh allocation (the Stage 125 wrap
         * classified it the same way). */
        void* q = __real_realloc(p, n);
        if (q) {
            pthread_mutex_lock(&g_mu);
            if (!hlrt_insert(q, n)) g_overflow = 1;
            g_cur_allocs++;
            g_cur_bytes += n;
            pthread_mutex_unlock(&g_mu);
        }
        return q;
    }
    if (n == 0) {
        /* glibc semantics: realloc(p, 0) frees p. The Halis runtime
         * never emits this form today; classified, not lumped, so the
         * ledger stays exact if that ever changes. */
        size_t oldn = 0;
        pthread_mutex_lock(&g_mu);
        int was = hlrt_remove(p, &oldn);
        pthread_mutex_unlock(&g_mu);
        __real_realloc(p, n);
        if (was) {
            g_cur_frees++;
            g_tot_frees++;
        }
        return (void*)0;
    }
    void* q = __real_realloc(p, n);
    pthread_mutex_lock(&g_mu);
    if (q == p) {
        /* in-place growth: the size slot updates; the cycle's bytes
         * grow by the DELTA, not the whole new size */
        size_t oldn = 0;
        if (hlrt_remove(p, &oldn)) {
            hlrt_insert(p, n);
            if (n > oldn) g_cur_bytes += (n - oldn);
        }
    } else {
        size_t oldn = 0;
        int was = hlrt_remove(p, &oldn);
        if (!hlrt_insert(q, n)) g_overflow = 1;
        if (was) g_cur_bytes += (n > oldn) ? (n - oldn) : n;
    }
    g_cur_allocs++;   /* a realloc is an allocation event of its cycle
                       * (the allocs axis counts the window's traffic) */
    pthread_mutex_unlock(&g_mu);
    return q;
}

void __wrap_free(void* p) {
    if (p != NULL) {
        pthread_mutex_lock(&g_mu);
        int tracked = hlrt_remove(p, NULL);
        if (tracked) {
            g_cur_frees++;
            g_tot_frees++;
        }
        pthread_mutex_unlock(&g_mu);
        /* untracked: libc's own buffer handed back — FOREIGN business,
         * neither a cycle event nor a balance event (Stage 125's
         * honesty rule, inherited whole). */
    }
    __real_free(p);
}

/* The runtime's Stage 126 hooks (declared weak there). Marking closes
 * the open cycle: snapshot, ledger append, reset. The clock is stamped
 * BEFORE the roll so the recorded window is the cycle the program
 * actually ran, not the cost of bookkeeping it. */
void hlrt_cycle_mark(long long idx) {
    struct timespec now;
    long long nanos = 0;
    if (g_have_clock) {
        clock_gettime(CLOCK_MONOTONIC, &now);
        nanos = hlrt_nanos_between(g_last, now);
        g_last = now;
    }
    pthread_mutex_lock(&g_mu);
    if (g_nrecs == g_caprecs) {
        size_t ncap = g_caprecs ? g_caprecs * 2 : 256;
        hlrt_rec* nr = (hlrt_rec*)__real_malloc(ncap * sizeof(hlrt_rec));
        if (nr) {
            if (g_recs) memcpy(nr, g_recs, g_nrecs * sizeof(hlrt_rec));
            __real_free(g_recs);
            g_recs = nr;
            g_caprecs = ncap;
        }
        /* allocation failed: the ledger keeps the records it has and
         * the exit report notes the truncation through OVERFLOW — the
         * verifier refuses to certify on an incomplete ledger */
    }
    if (g_nrecs < g_caprecs) {
        g_recs[g_nrecs].idx = idx;
        g_recs[g_nrecs].allocs = g_cur_allocs;
        g_recs[g_nrecs].bytes = (long long)g_cur_bytes;
        g_recs[g_nrecs].live = (long long)g_live_bytes;
        g_recs[g_nrecs].frees = g_cur_frees;
        g_recs[g_nrecs].nanos = nanos;
        g_nrecs++;
    }
    /* the snapshot the runtime's getters read (the just-closed cycle) */
    g_snap_allocs = g_cur_allocs;
    g_snap_bytes = (long long)g_cur_bytes;
    g_snap_live = (long long)g_live_bytes;
    g_tot_allocs += g_cur_allocs;
    g_tot_bytes += (long long)g_cur_bytes;
    g_cur_allocs = 0;
    g_cur_bytes = 0;
    g_cur_frees = 0;
    pthread_mutex_unlock(&g_mu);
}

long long hlrt_cycle_allocs(void) {
    pthread_mutex_lock(&g_mu);
    long long a = g_snap_allocs;
    pthread_mutex_unlock(&g_mu);
    return a;
}

long long hlrt_cycle_bytes(void) {
    pthread_mutex_lock(&g_mu);
    long long b = g_snap_bytes;
    pthread_mutex_unlock(&g_mu);
    return b;
}

long long hlrt_cycle_live_bytes(void) {
    pthread_mutex_lock(&g_mu);
    long long b = g_snap_live;
    pthread_mutex_unlock(&g_mu);
    return b;
}

/* ---- the exit report ------------------------------------------------------ */

static void hlrt_report(void) {
    if (!g_reporting) return;
    long long tail_allocs, tail_bytes, live;
    pthread_mutex_lock(&g_mu);
    tail_allocs = g_cur_allocs;
    tail_bytes = (long long)g_cur_bytes;
    live = (long long)g_live;
    pthread_mutex_unlock(&g_mu);
    fflush(stdout);
    /* the BEGIN/END fence: a program's own stderr output cannot become
     * a record by landing outside the block, and the verifier parses
     * nothing outside it */
    fprintf(stderr, "HLRT_REPORT_BEGIN\n");
    for (size_t i = 0; i < g_nrecs; i++) {
        fprintf(stderr,
                "HLRT_CYCLE=%lld ALLOCS=%lld BYTES=%lld LIVE=%lld "
                "FREES=%lld NANOS=%lld\n",
                g_recs[i].idx, g_recs[i].allocs, g_recs[i].bytes,
                g_recs[i].live, g_recs[i].frees, g_recs[i].nanos);
    }
    fprintf(stderr, "HLRT_TAIL ALLOCS=%lld BYTES=%lld\n",
            tail_allocs, tail_bytes);
    /* the totals are the WHOLE traffic: the closed cycles plus the
     * tail window — the verifier re-derives exactly this identity
     * (sum of the per-cycle lines + the tail line = the totals) before
     * trusting the ledger at all */
    fprintf(stderr, "HLRT_TOTAL ALLOCS=%lld BYTES=%lld FREES=%lld\n",
            g_tot_allocs + tail_allocs, g_tot_bytes + tail_bytes,
            g_tot_frees);
    fprintf(stderr, "HLRT_LIVE_EXIT=%lld\n", live);
    fprintf(stderr, "HLRT_OVERFLOW=%d\n", g_overflow);
    fprintf(stderr, "HLRT_REPORT_END\n");
}

__attribute__((constructor))
static void hlrt_install(void) {
    const char* rep = getenv("HLRT_REPORT");
    g_reporting = (rep && rep[0] == '1' && rep[1] == 0);
    if (clock_gettime(CLOCK_MONOTONIC, &g_last) == 0) {
        g_have_clock = 1;
    }
    atexit(hlrt_report);
}
