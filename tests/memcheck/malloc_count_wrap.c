/* tests/memcheck/malloc_count_wrap.c — Stage 30 (v0.47.0-alpha)
 *
 * Link-time malloc interposer for the Stage 30 escape-analysis
 * acceptance gate. Linked with -Wl,--wrap=malloc -Wl,--wrap=realloc,
 * it counts every heap allocation performed by the compiled Halis
 * program and prints the total to stderr at process exit:
 *
 *     HL_MALLOC_COUNT=<n>
 *
 * The gate: examples/fibonacci.hls runs its O(n) inner loop 200,000
 * times; with the stack layout the count stays a small CONSTANT (the
 * startup + print allocations only), while the #[boxed] twin of the
 * same program allocates ~2,000,000+ objects (4+ per loop iteration).
 * This is the deterministic, CI-friendly equivalent of
 * `valgrind --tool=massif` (which the acceptance also runs when
 * available): it proves the inner loop allocates ZERO heap objects.
 */
#include <stdio.h>
#include <stdlib.h>
#include <stddef.h>

/* The --wrap linker option redirects calls to malloc/realloc here;
 * the real libc functions are available as __real_malloc/__real_realloc
 * but need explicit prototypes. */
extern void* __real_malloc(size_t n);
extern void* __real_realloc(void* p, size_t n);

static long g_hl_mallocs = 0;

void* __wrap_malloc(size_t n) {
    g_hl_mallocs++;
    return __real_malloc(n);
}

void* __wrap_realloc(void* p, size_t n) {
    /* realloc(NULL, n) is a fresh allocation; growth of an existing
     * block is not (hl_list_push grows the items array in place). */
    if (p == NULL) {
        g_hl_mallocs++;
    }
    return __real_realloc(p, n);
}

static void hl_report_mallocs(void) {
    fflush(stdout);
    fprintf(stderr, "HL_MALLOC_COUNT=%ld\n", g_hl_mallocs);
}

__attribute__((constructor))
static void hl_install_malloc_report(void) {
    atexit(hl_report_mallocs);
}
