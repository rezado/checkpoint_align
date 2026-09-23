#include <errno.h>
#include <inttypes.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <qemu-plugin.h>

QEMU_PLUGIN_EXPORT int qemu_plugin_version = QEMU_PLUGIN_VERSION;

typedef struct {
    uint32_t id;
    uint64_t pc;
} WatchEntry;

static WatchEntry *watchlist;
static size_t watch_count;
static FILE *trace_file;
static const char *status_path;
static uint64_t event_count;
static uint64_t max_events = UINT64_MAX;
static uint64_t max_per_watch = UINT64_MAX;
static uint64_t *watch_events;
static bool *truncated_watches;
static unsigned int vcpu_count;
static bool write_error;
static bool budget_exceeded;

static int compare_pc(const void *left, const void *right)
{
    const WatchEntry *a = left;
    const WatchEntry *b = right;

    return (a->pc > b->pc) - (a->pc < b->pc);
}

static const WatchEntry *find_watch(uint64_t pc)
{
    WatchEntry key = { .pc = pc };

    return bsearch(&key, watchlist, watch_count, sizeof(*watchlist), compare_pc);
}

static bool parse_u64(const char *text, uint64_t *value)
{
    char *end;
    unsigned long long parsed;

    errno = 0;
    parsed = strtoull(text, &end, 0);
    if (errno || end == text || (*end != '\0' && *end != '\n')) {
        return false;
    }
    *value = parsed;
    return true;
}

static bool load_watchlist(const char *path)
{
    FILE *file = fopen(path, "r");
    char *line = NULL;
    size_t capacity = 0;
    ssize_t length;

    if (!file) {
        fprintf(stderr, "occurrence-plugin: cannot open watchlist %s: %s\n",
                path, strerror(errno));
        return false;
    }
    while ((length = getline(&line, &capacity, file)) >= 0) {
        char *tab;
        char *id_end;
        unsigned long parsed_id;
        uint64_t pc;
        WatchEntry *grown;

        if (length == 0 || line[0] == '\n' || line[0] == '#') {
            continue;
        }
        tab = strchr(line, '\t');
        if (!tab) {
            fprintf(stderr, "occurrence-plugin: invalid watchlist row: %s", line);
            goto fail;
        }
        *tab = '\0';
        errno = 0;
        parsed_id = strtoul(line, &id_end, 10);
        if (errno || *id_end != '\0' || parsed_id > UINT32_MAX ||
            !parse_u64(tab + 1, &pc)) {
            fprintf(stderr, "occurrence-plugin: invalid watchlist row\n");
            goto fail;
        }
        grown = realloc(watchlist, (watch_count + 1) * sizeof(*watchlist));
        if (!grown) {
            fprintf(stderr, "occurrence-plugin: out of memory\n");
            goto fail;
        }
        watchlist = grown;
        watchlist[watch_count++] = (WatchEntry) { .id = parsed_id, .pc = pc };
    }
    free(line);
    fclose(file);
    if (!watch_count) {
        fprintf(stderr, "occurrence-plugin: empty watchlist\n");
        return false;
    }
    qsort(watchlist, watch_count, sizeof(*watchlist), compare_pc);
    for (size_t i = 1; i < watch_count; i++) {
        if (watchlist[i - 1].pc == watchlist[i].pc ||
            watchlist[i - 1].id == watchlist[i].id) {
            fprintf(stderr, "occurrence-plugin: duplicate PC or adjacent watch ID\n");
            return false;
        }
    }
    for (size_t i = 0; i < watch_count; i++) {
        for (size_t j = i + 1; j < watch_count; j++) {
            if (watchlist[i].id == watchlist[j].id) {
                fprintf(stderr, "occurrence-plugin: duplicate watch ID\n");
                return false;
            }
        }
    }
    return true;

fail:
    free(line);
    fclose(file);
    return false;
}

static void vcpu_init(unsigned int vcpu_index, void *userdata)
{
    (void)vcpu_index;
    (void)userdata;
    vcpu_count++;
}

static void record_event(unsigned int vcpu_index, void *userdata)
{
    uint32_t id = (uint32_t)(uintptr_t)userdata;
    uint8_t bytes[4] = {
        id & 0xff,
        (id >> 8) & 0xff,
        (id >> 16) & 0xff,
        (id >> 24) & 0xff,
    };

    if (event_count >= max_events) {
        budget_exceeded = true;
        return;
    }
    if (id >= watch_count) {
        write_error = true;
        return;
    }
    if (watch_events[id] >= max_per_watch) {
        truncated_watches[id] = true;
        return;
    }
    if (vcpu_index != 0 || fwrite(bytes, sizeof(bytes), 1, trace_file) != 1) {
        write_error = true;
        return;
    }
    event_count++;
    watch_events[id]++;
}

static void translate_tb(struct qemu_plugin_tb *tb, void *userdata)
{
    size_t count = qemu_plugin_tb_n_insns(tb);

    (void)userdata;
    for (size_t i = 0; i < count; i++) {
        struct qemu_plugin_insn *insn = qemu_plugin_tb_get_insn(tb, i);
        const WatchEntry *entry = find_watch(qemu_plugin_insn_vaddr(insn));

        if (entry) {
            qemu_plugin_register_vcpu_insn_exec_cb(
                insn, record_event, QEMU_PLUGIN_CB_NO_REGS,
                (void *)(uintptr_t)entry->id);
        }
    }
}

static void plugin_exit(void *userdata)
{
    FILE *status;
    bool close_error;

    (void)userdata;
    close_error = fflush(trace_file) != 0 || fclose(trace_file) != 0;
    status = fopen(status_path, "w");
    if (!status) {
        return;
    }
    fprintf(status,
            "{\"schema_version\":1,\"events\":%" PRIu64
            ",\"vcpus\":%u,\"budget_exceeded\":%s,"
            "\"write_error\":%s,\"close_error\":%s,"
            "\"truncated_watch_ids\":[",
            event_count, vcpu_count, budget_exceeded ? "true" : "false",
            write_error ? "true" : "false",
            close_error ? "true" : "false");
    bool first = true;
    for (size_t i = 0; i < watch_count; i++) {
        if (truncated_watches[i]) {
            fprintf(status, "%s%zu", first ? "" : ",", i);
            first = false;
        }
    }
    fprintf(status, "]}\n");
    fclose(status);
    free(watchlist);
    free(watch_events);
    free(truncated_watches);
}

QEMU_PLUGIN_EXPORT int qemu_plugin_install(qemu_plugin_id_t id,
                                            const qemu_info_t *info,
                                            int argc, char **argv)
{
    const char *watchlist_path = NULL;
    const char *trace_path = NULL;

    (void)info;
    for (int i = 0; i < argc; i++) {
        if (strncmp(argv[i], "watchlist=", 10) == 0) {
            watchlist_path = argv[i] + 10;
        } else if (strncmp(argv[i], "trace=", 6) == 0) {
            trace_path = argv[i] + 6;
        } else if (strncmp(argv[i], "status=", 7) == 0) {
            status_path = argv[i] + 7;
        } else if (strncmp(argv[i], "max-events=", 11) == 0) {
            if (!parse_u64(argv[i] + 11, &max_events)) {
                fprintf(stderr, "occurrence-plugin: invalid max-events\n");
                return -1;
            }
        } else if (strncmp(argv[i], "max-per-watch=", 14) == 0) {
            if (!parse_u64(argv[i] + 14, &max_per_watch)) {
                fprintf(stderr, "occurrence-plugin: invalid max-per-watch\n");
                return -1;
            }
        } else {
            fprintf(stderr, "occurrence-plugin: unknown option: %s\n", argv[i]);
            return -1;
        }
    }
    if (!watchlist_path || !trace_path || !status_path || max_events == 0 ||
        !load_watchlist(watchlist_path)) {
        fprintf(stderr, "occurrence-plugin: watchlist, trace and status are required\n");
        return -1;
    }
    watch_events = calloc(watch_count, sizeof(*watch_events));
    truncated_watches = calloc(watch_count, sizeof(*truncated_watches));
    if (!watch_events || !truncated_watches) {
        fprintf(stderr, "occurrence-plugin: out of memory\n");
        free(watch_events);
        free(truncated_watches);
        return -1;
    }
    trace_file = fopen(trace_path, "wb");
    if (!trace_file) {
        fprintf(stderr, "occurrence-plugin: cannot open trace %s: %s\n",
                trace_path, strerror(errno));
        return -1;
    }
    qemu_plugin_register_vcpu_init_cb(id, vcpu_init, NULL);
    qemu_plugin_register_vcpu_tb_trans_cb(id, translate_tb, NULL);
    qemu_plugin_register_atexit_cb(id, plugin_exit, NULL);
    return 0;
}
