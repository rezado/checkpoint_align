#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>

static int first_event = 1;

__attribute__((noinline, noipa))
static void emit_marker(const void *pc, uint64_t workload_icount) {
  printf("%s{\"pc\":\"0x%" PRIxPTR "\",\"workload_icount\":%" PRIu64 "}",
         first_event ? "" : ",", (uintptr_t)pc, workload_icount);
  first_event = 0;
}

__attribute__((noinline, noipa))
void marker_begin(uint64_t workload_icount) {
  emit_marker((const void *)&marker_begin, workload_icount);
}

__attribute__((noinline, noipa))
void marker_step(uint64_t workload_icount) {
  emit_marker((const void *)&marker_step, workload_icount);
}

__attribute__((noinline, noipa))
void marker_extra(uint64_t workload_icount) {
  emit_marker((const void *)&marker_extra, workload_icount);
}

__attribute__((noinline, noipa))
void marker_end(uint64_t workload_icount) {
  emit_marker((const void *)&marker_end, workload_icount);
}

int main(void) {
  putchar('[');
  marker_begin(100);
  marker_step(200);
#ifdef INSERT_EXTRA_MARKER
  marker_extra(250);
#endif
  marker_step(300);
  marker_step(400);
  marker_end(500);
  puts("]");
  return 0;
}
