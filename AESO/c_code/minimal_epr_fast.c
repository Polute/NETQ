#define _GNU_SOURCE

#include <arpa/inet.h>
#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <math.h>
#include <netdb.h>
#include <netinet/tcp.h>
#include <pthread.h>
#include <sched.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <sys/types.h>
#include <time.h>
#include <unistd.h>

#define MSG_SIZE 21
#define UDP_MSG_SIZE 25
#define CLOCK_SYNC_SYNC_SIZE 8
#define CLOCK_SYNC_DELAY_REQ_SIZE 24
#define CLOCK_SYNC_DELAY_RESP_SIZE 32
#define UDP_HELLO_MAGIC "AESOUDP1"
#define UDP_HELLO_MAGIC_SIZE 8
#define UDP_READY_BYTE 'U'
#define DATA_ACK_BYTE 'A'

#ifndef SO_TIMESTAMPNS
#define SO_TIMESTAMPNS 64
#endif

#ifndef SCM_TIMESTAMPNS
#define SCM_TIMESTAMPNS SO_TIMESTAMPNS
#endif

typedef enum {
    ROLE_NONE = 0,
    ROLE_REPEATER,
    ROLE_CLIENT
} Role;

typedef enum {
    PROTO_UDP = 0,
    PROTO_TCP
} DataProtocol;

typedef enum {
    SEND_MODE_BURST = 0,
    SEND_MODE_PACED,
    SEND_MODE_ACK
} SendMode;

typedef enum {
    PACE_SLEEP = 0,
    PACE_SPIN,
    PACE_HYBRID
} PaceMode;

typedef struct {
    uint64_t ts_emit_ns;
    uint32_t peer_id;
    uint8_t bits;
    double w_swap;
} Msg;

typedef struct {
    int local_id;
    double werner;
    int peer_id;
    bool peer_none;
} State;

typedef struct {
    int sample_idx;
    uint64_t t1_ns;
    uint64_t t2_ns;
    uint64_t t3_ns;
    uint64_t t4_ns;
    int64_t master_to_slave_ns;
    int64_t slave_to_master_ns;
    int64_t offset_ns;
    int64_t path_delay_ns;
} ClockSyncRow;

typedef struct {
    int64_t delta_ns;
    int count_idx;
    Msg msg;
    State state_out;
} SampleMsg;

typedef struct {
    Role role;
    const char *listen_host_a;
    int listen_port_a;
    const char *listen_host_b;
    int listen_port_b;
    const char *repeater_host;
    int repeater_port;
    int count;
    int warmup;
    double accept_timeout;
    double connect_timeout;
    double detect_timeout;
    double detect_interval;
    int cpu;
    bool cpu_set;
    int rt_priority;
    bool rt_priority_set;
    int sock_buf;
    int busy_poll_us;
    int repeater_id;
    int client_a_id;
    int client_b_id;
    int client_id;
    double werner_ar;
    bool werner_ar_set;
    double werner_br;
    bool werner_br_set;
    double werner_in;
    bool parallel;
    int cpu_a;
    int cpu_b;
    double count_interval;
    bool quiet;
    bool plot;
    const char *plot_prefix;
    const char *plot_dir;
    bool diag;
    bool clock_sync;
    int clock_sync_samples;
    bool clock_offset_set;
    int64_t clock_offset_ns;
    bool center_delay;
    bool shared_send_timestamp;
    PaceMode pace_mode;
    double spin_margin_us;
    bool json_output;
    const char *json_dir;
    bool kernel_timestamp;
    DataProtocol data_protocol;
    SendMode send_mode;
    double udp_ready_timeout;
    double udp_idle_timeout;
    double t1_ns;
    int argc;
    char **argv;
} Options;

typedef struct {
    int fd;
    unsigned char *buf;
    int count;
    int msg_size;
    DataProtocol proto;
    int peer_id;
    int cpu_pin;
    bool diag;
    int64_t *send_block_samples;
    uint8_t *correction_bits_samples;
    pthread_barrier_t *barrier_ready;
    pthread_barrier_t *barrier_done;
    Msg *last_msg_ref;
} SenderCtx;

static void die(const char *msg) {
    perror(msg);
    exit(1);
}

static void die_msg(const char *msg) {
    fprintf(stderr, "%s\n", msg);
    exit(1);
}

static bool sudo_owner(uid_t *uid, gid_t *gid) {
    const char *sudo_uid = getenv("SUDO_UID");
    const char *sudo_gid = getenv("SUDO_GID");
    if (!sudo_uid || !sudo_gid || !*sudo_uid || !*sudo_gid) {
        return false;
    }
    char *end_uid = NULL;
    char *end_gid = NULL;
    unsigned long parsed_uid = strtoul(sudo_uid, &end_uid, 10);
    unsigned long parsed_gid = strtoul(sudo_gid, &end_gid, 10);
    if ((end_uid && *end_uid) || (end_gid && *end_gid)) {
        return false;
    }
    *uid = (uid_t)parsed_uid;
    *gid = (gid_t)parsed_gid;
    return true;
}

static void chown_to_sudo_user(const char *path) {
    uid_t uid;
    gid_t gid;
    if (!sudo_owner(&uid, &gid)) {
        return;
    }
    if (chown(path, uid, gid) != 0 && errno != ENOENT) {
        die("chown");
    }
}

static uint64_t now_ns(clockid_t clock_id) {
    struct timespec ts;
    if (clock_gettime(clock_id, &ts) != 0) {
        die("clock_gettime");
    }
    return (uint64_t)ts.tv_sec * 1000000000ull + (uint64_t)ts.tv_nsec;
}

static uint64_t time_ns(void) {
    return now_ns(CLOCK_REALTIME);
}

static uint64_t monotonic_ns(void) {
    return now_ns(CLOCK_MONOTONIC);
}

static void sleep_seconds(double seconds) {
    if (seconds <= 0.0) {
        return;
    }
    struct timespec req;
    req.tv_sec = (time_t)seconds;
    req.tv_nsec = (long)((seconds - (double)req.tv_sec) * 1000000000.0);
    if (req.tv_nsec < 0) {
        req.tv_nsec = 0;
    }
    while (nanosleep(&req, &req) != 0 && errno == EINTR) {
    }
}

static const char *pace_mode_name(PaceMode mode) {
    switch (mode) {
        case PACE_SLEEP:
            return "sleep";
        case PACE_SPIN:
            return "spin";
        case PACE_HYBRID:
            return "hybrid";
    }
    return "unknown";
}

static void pace_wait(uint64_t interval_ns, PaceMode mode, uint64_t spin_margin_ns) {
    if (interval_ns == 0) {
        return;
    }
    if (mode == PACE_SLEEP) {
        sleep_seconds((double)interval_ns / 1e9);
        return;
    }
    uint64_t deadline_ns = monotonic_ns() + interval_ns;
    if (mode == PACE_HYBRID) {
        while (true) {
            uint64_t now = monotonic_ns();
            if (now >= deadline_ns) {
                return;
            }
            uint64_t remaining = deadline_ns - now;
            if (remaining <= spin_margin_ns) {
                break;
            }
            uint64_t sleep_ns = remaining - spin_margin_ns;
            sleep_seconds((double)sleep_ns / 1e9);
        }
    }
    while (monotonic_ns() < deadline_ns) {
    }
}

static void pack_u32be(unsigned char *p, uint32_t v) {
    p[0] = (unsigned char)((v >> 24) & 0xff);
    p[1] = (unsigned char)((v >> 16) & 0xff);
    p[2] = (unsigned char)((v >> 8) & 0xff);
    p[3] = (unsigned char)(v & 0xff);
}

static uint32_t unpack_u32be(const unsigned char *p) {
    return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) | ((uint32_t)p[2] << 8) | (uint32_t)p[3];
}

static void pack_u64be(unsigned char *p, uint64_t v) {
    for (int i = 7; i >= 0; --i) {
        p[7 - i] = (unsigned char)((v >> (i * 8)) & 0xff);
    }
}

static uint64_t unpack_u64be(const unsigned char *p) {
    uint64_t v = 0;
    for (int i = 0; i < 8; ++i) {
        v = (v << 8) | (uint64_t)p[i];
    }
    return v;
}

static void pack_f64be(unsigned char *p, double d) {
    uint64_t u;
    memcpy(&u, &d, sizeof(u));
    pack_u64be(p, u);
}

static double unpack_f64be(const unsigned char *p) {
    uint64_t u = unpack_u64be(p);
    double d;
    memcpy(&d, &u, sizeof(d));
    return d;
}

static void pack_msg(unsigned char *buf, uint64_t ts_emit_ns, uint32_t peer_id, uint8_t bits, double w_swap) {
    pack_u64be(buf, ts_emit_ns);
    pack_u32be(buf + 8, peer_id);
    buf[12] = bits;
    pack_f64be(buf + 13, w_swap);
}

static Msg unpack_msg(const unsigned char *buf) {
    Msg msg;
    msg.ts_emit_ns = unpack_u64be(buf);
    msg.peer_id = unpack_u32be(buf + 8);
    msg.bits = buf[12];
    msg.w_swap = unpack_f64be(buf + 13);
    return msg;
}

static void pack_udp_msg(
    unsigned char *buf,
    uint32_t count_idx,
    uint64_t ts_emit_ns,
    uint32_t peer_id,
    uint8_t bits,
    double w_swap
) {
    pack_u32be(buf, count_idx);
    pack_u64be(buf + 4, ts_emit_ns);
    pack_u32be(buf + 12, peer_id);
    buf[16] = bits;
    pack_f64be(buf + 17, w_swap);
}

static Msg unpack_udp_msg(const unsigned char *buf, int *count_idx) {
    Msg msg;
    *count_idx = (int)unpack_u32be(buf);
    msg.ts_emit_ns = unpack_u64be(buf + 4);
    msg.peer_id = unpack_u32be(buf + 12);
    msg.bits = buf[16];
    msg.w_swap = unpack_f64be(buf + 17);
    return msg;
}

static ssize_t send_all(int fd, const void *buf, size_t len) {
    const unsigned char *p = (const unsigned char *)buf;
    size_t sent = 0;
    while (sent < len) {
        ssize_t n = send(fd, p + sent, len - sent, 0);
        if (n < 0) {
            if (errno == EINTR) {
                continue;
            }
            return -1;
        }
        if (n == 0) {
            errno = ECONNRESET;
            return -1;
        }
        sent += (size_t)n;
    }
    return (ssize_t)sent;
}

static ssize_t recv_exact(int fd, void *buf, size_t len) {
    unsigned char *p = (unsigned char *)buf;
    size_t got_total = 0;
    while (got_total < len) {
        ssize_t n = recv(fd, p + got_total, len - got_total, 0);
        if (n < 0) {
            if (errno == EINTR) {
                continue;
            }
            return -1;
        }
        if (n == 0) {
            errno = ECONNRESET;
            return -1;
        }
        got_total += (size_t)n;
    }
    return (ssize_t)got_total;
}

static void enable_low_latency_socket(int fd, int sock_buf, int busy_poll_us) {
    int one = 1;
    (void)setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
#ifdef TCP_QUICKACK
    (void)setsockopt(fd, IPPROTO_TCP, TCP_QUICKACK, &one, sizeof(one));
#endif
    if (sock_buf > 0) {
        (void)setsockopt(fd, SOL_SOCKET, SO_SNDBUF, &sock_buf, sizeof(sock_buf));
        (void)setsockopt(fd, SOL_SOCKET, SO_RCVBUF, &sock_buf, sizeof(sock_buf));
    }
#ifdef SO_BUSY_POLL
    if (busy_poll_us > 0) {
        (void)setsockopt(fd, SOL_SOCKET, SO_BUSY_POLL, &busy_poll_us, sizeof(busy_poll_us));
    }
#endif
}

static bool try_setsockopt_int(int fd, int level, int optname, int value) {
    return setsockopt(fd, level, optname, &value, sizeof(value)) == 0;
}

static void enable_kernel_timestamp_ns(int fd) {
    if (try_setsockopt_int(fd, SOL_SOCKET, SO_TIMESTAMPNS, 1)) {
        return;
    }
    if (SO_TIMESTAMPNS != 64 && try_setsockopt_int(fd, SOL_SOCKET, 64, 1)) {
        return;
    }
    if (SO_TIMESTAMPNS != 35 && try_setsockopt_int(fd, SOL_SOCKET, 35, 1)) {
        return;
    }
    die("setsockopt SO_TIMESTAMPNS");
}

static uint64_t parse_kernel_timestamp_ns(struct msghdr *msg, bool *found) {
    *found = false;
    for (struct cmsghdr *cmsg = CMSG_FIRSTHDR(msg); cmsg; cmsg = CMSG_NXTHDR(msg, cmsg)) {
        if (cmsg->cmsg_level != SOL_SOCKET) {
            continue;
        }
        if (cmsg->cmsg_type != SCM_TIMESTAMPNS && cmsg->cmsg_type != SO_TIMESTAMPNS &&
            cmsg->cmsg_type != 64 && cmsg->cmsg_type != 35) {
            continue;
        }
        if (cmsg->cmsg_len >= CMSG_LEN(sizeof(struct timespec))) {
            struct timespec ts;
            memcpy(&ts, CMSG_DATA(cmsg), sizeof(ts));
            *found = true;
            return (uint64_t)ts.tv_sec * 1000000000ull + (uint64_t)ts.tv_nsec;
        }
    }
    return 0;
}

static void apply_cpu_rt(const Options *o) {
    if (o->cpu_set) {
        cpu_set_t set;
        CPU_ZERO(&set);
        CPU_SET(o->cpu, &set);
        if (sched_setaffinity(0, sizeof(set), &set) != 0) {
            die("sched_setaffinity");
        }
    }
    if (o->rt_priority_set && o->rt_priority > 0) {
        struct sched_param param;
        memset(&param, 0, sizeof(param));
        param.sched_priority = o->rt_priority;
        if (sched_setscheduler(0, SCHED_FIFO, &param) != 0) {
            die("sched_setscheduler");
        }
    }
}

static void set_thread_affinity(int cpu) {
    if (cpu < 0) {
        return;
    }
    cpu_set_t set;
    CPU_ZERO(&set);
    CPU_SET(cpu, &set);
    int res = pthread_setaffinity_np(pthread_self(), sizeof(set), &set);
    if (res != 0) {
        errno = res;
        die("pthread_setaffinity_np");
    }
}

static void fmt_ns(char *buf, size_t size, int64_t v) {
    snprintf(buf, size, "%lld (%.9f s)", (long long)v, (double)v / 1e9);
}

static void fmt_ts_emit(char *buf, size_t size, uint64_t ts_ns) {
    time_t total_s = (time_t)(ts_ns / 1000000000ull);
    long ns_part = (long)(ts_ns % 1000000000ull);
    struct tm tm_value;
    gmtime_r(&total_s, &tm_value);
    snprintf(buf, size, "%02d:%02d.%09ld", tm_value.tm_min, tm_value.tm_sec, ns_part);
}

static void fmt_state(char *buf, size_t size, State state) {
    if (state.peer_none) {
        snprintf(buf, size, "(%d,%.6f,None)", state.local_id, state.werner);
    } else {
        snprintf(buf, size, "(%d,%.6f,%d)", state.local_id, state.werner, state.peer_id);
    }
}

static void print_client_group(const char *label, int64_t delta_ns, double werner, const char *delay_label) {
    char nsbuf[96];
    fmt_ns(nsbuf, sizeof(nsbuf), delta_ns);
    printf("\n");
    printf("client_%s\n", label);
    printf("metric\t\t\t\t value\n");
    printf("%s\t\t %s\n", delay_label, nsbuf);
    printf("werner\t\t\t\t %.6f\n", werner);
}

static void print_client_message_state(
    const char *label,
    int64_t delta_ns,
    Msg msg,
    State state_out,
    int count_idx,
    bool has_count_idx,
    const char *delay_label
) {
    char nsbuf[96];
    char tsbuf[64];
    char statebuf[96];
    fmt_ns(nsbuf, sizeof(nsbuf), delta_ns);
    fmt_ts_emit(tsbuf, sizeof(tsbuf), msg.ts_emit_ns);
    fmt_state(statebuf, sizeof(statebuf), state_out);
    printf("\n");
    printf("client_%s\n", label);
    printf("metric\t\t\t\t value\n");
    printf("%s\t %s\n", delay_label, nsbuf);
    if (has_count_idx) {
        printf("count_idx\t\t\t %d\n", count_idx);
    }
    printf(
        "msg=(ts_emit_ns=%llu, ts_emit=%s, peer_id=%u, bits=%02u, w_swap=%.6f)\n",
        (unsigned long long)msg.ts_emit_ns,
        tsbuf,
        msg.peer_id,
        msg.bits,
        msg.w_swap
    );
    printf("state_out=%s\n", statebuf);
}

static int cmp_i64(const void *a, const void *b) {
    int64_t av = *(const int64_t *)a;
    int64_t bv = *(const int64_t *)b;
    return (av > bv) - (av < bv);
}

static int cmp_double(const void *a, const void *b) {
    double av = *(const double *)a;
    double bv = *(const double *)b;
    return (av > bv) - (av < bv);
}

static int64_t percentile_i64(const int64_t *sorted_vals, int n, double p) {
    if (n <= 0) {
        return 0;
    }
    int idx = (int)((double)(n - 1) * p);
    return sorted_vals[idx];
}

static double percentile_inverse_double(const double *sorted_vals, int n, double p) {
    if (n <= 0) {
        return 0.0;
    }
    int idx = (int)((double)(n - 1) * (1.0 - p));
    return sorted_vals[idx];
}

static double stddev_i64(const int64_t *vals, int n, double mean_value) {
    if (n <= 0) {
        return 0.0;
    }
    double acc = 0.0;
    for (int i = 0; i < n; ++i) {
        double diff = (double)vals[i] - mean_value;
        acc += diff * diff;
    }
    return sqrt(acc / (double)n);
}

static double stddev_double(const double *vals, int n, double mean_value) {
    if (n <= 0) {
        return 0.0;
    }
    double acc = 0.0;
    for (int i = 0; i < n; ++i) {
        double diff = vals[i] - mean_value;
        acc += diff * diff;
    }
    return sqrt(acc / (double)n);
}

static double decay_werner(double base, int64_t age_ns, double t1_ns) {
    if (t1_ns <= 0.0) {
        die_msg("t1_ns must be positive");
    }
    if (age_ns < 0) {
        age_ns = 0;
    }
    return base * exp(-(double)age_ns / t1_ns);
}

static int64_t i64_abs_value(int64_t v) {
    return v < 0 ? -v : v;
}

static void ensure_dir(const char *path) {
    if (!path || !*path) {
        return;
    }
    char tmp[4096];
    snprintf(tmp, sizeof(tmp), "%s", path);
    size_t len = strlen(tmp);
    if (len == 0) {
        return;
    }
    if (tmp[len - 1] == '/') {
        tmp[len - 1] = '\0';
    }
    for (char *p = tmp + 1; *p; ++p) {
        if (*p == '/') {
            *p = '\0';
            if (mkdir(tmp, 0777) == 0) {
                chown_to_sudo_user(tmp);
            } else if (errno != EEXIST) {
                die("mkdir");
            }
            *p = '/';
        }
    }
    if (mkdir(tmp, 0777) == 0) {
        chown_to_sudo_user(tmp);
    } else if (errno != EEXIST) {
        die("mkdir");
    }
    chown_to_sudo_user(tmp);
}

static void unique_csv_path(char *out, size_t out_size, const char *dir, const char *base, const char *suffix_hint) {
    char suffix[64] = "";
    int idx = 1;
    (void)suffix_hint;
    while (true) {
        snprintf(out, out_size, "%s/%s%s.csv", dir, base, suffix);
        if (access(out, F_OK) != 0) {
            return;
        }
        ++idx;
        snprintf(suffix, sizeof(suffix), "_%d", idx);
    }
}

static const char *path_suffix(const char *path, const char *base) {
    size_t dir_len = 0;
    const char *slash = strrchr(path, '/');
    if (slash) {
        dir_len = (size_t)(slash - path + 1);
    }
    const char *name = path + dir_len;
    size_t base_len = strlen(base);
    if (strncmp(name, base, base_len) != 0) {
        return "";
    }
    const char *after_base = name + base_len;
    const char *dot = strrchr(after_base, '.');
    static char suffix[64];
    if (!dot || dot <= after_base) {
        suffix[0] = '\0';
        return suffix;
    }
    size_t len = (size_t)(dot - after_base);
    if (len >= sizeof(suffix)) {
        len = sizeof(suffix) - 1;
    }
    memcpy(suffix, after_base, len);
    suffix[len] = '\0';
    return suffix;
}

static void default_json_dir(char *out, size_t out_size, const char *plot_dir) {
    char clean[4096];
    snprintf(clean, sizeof(clean), "%s", plot_dir ? plot_dir : "csv");
    size_t len = strlen(clean);
    while (len > 0 && clean[len - 1] == '/') {
        clean[--len] = '\0';
    }
    char *slash = strrchr(clean, '/');
    const char *base = slash ? slash + 1 : clean;
    char parent[4096] = "";
    if (slash) {
        size_t parent_len = (size_t)(slash - clean);
        if (parent_len >= sizeof(parent)) {
            parent_len = sizeof(parent) - 1;
        }
        memcpy(parent, clean, parent_len);
        parent[parent_len] = '\0';
    }
    char json_base[4096];
    if (strncmp(base, "csv", 3) == 0) {
        snprintf(json_base, sizeof(json_base), "json%s", base + 3);
    } else if (strncmp(base, "plots", 5) == 0) {
        snprintf(json_base, sizeof(json_base), "json%s", base + 5);
    } else if (*base) {
        snprintf(json_base, sizeof(json_base), "%s_json", base);
    } else {
        snprintf(json_base, sizeof(json_base), "json");
    }
    if (parent[0]) {
        snprintf(out, out_size, "%s/%s", parent, json_base);
    } else {
        snprintf(out, out_size, "%s", json_base);
    }
}

static void json_print_string(FILE *f, const char *s) {
    fputc('"', f);
    for (const unsigned char *p = (const unsigned char *)(s ? s : ""); *p; ++p) {
        switch (*p) {
            case '\\':
                fputs("\\\\", f);
                break;
            case '"':
                fputs("\\\"", f);
                break;
            case '\n':
                fputs("\\n", f);
                break;
            case '\r':
                fputs("\\r", f);
                break;
            case '\t':
                fputs("\\t", f);
                break;
            default:
                if (*p < 0x20) {
                    fprintf(f, "\\u%04x", *p);
                } else {
                    fputc(*p, f);
                }
                break;
        }
    }
    fputc('"', f);
}

static void json_print_argv(FILE *f, const Options *o) {
    fputc('[', f);
    for (int i = 0; i < o->argc; ++i) {
        if (i > 0) {
            fputs(", ", f);
        }
        json_print_string(f, o->argv[i]);
    }
    fputc(']', f);
}

static void parse_defaults(Options *o, Role role) {
    memset(o, 0, sizeof(*o));
    o->role = role;
    o->listen_host_a = "0.0.0.0";
    o->listen_port_a = 7401;
    o->listen_host_b = "0.0.0.0";
    o->listen_port_b = 7402;
    o->repeater_host = "127.0.0.1";
    o->repeater_port = 7401;
    o->count = 2000;
    o->warmup = 50;
    o->accept_timeout = 30.0;
    o->connect_timeout = 10.0;
    o->detect_timeout = 30.0;
    o->detect_interval = 0.05;
    o->cpu = -1;
    o->rt_priority = 50;
    o->rt_priority_set = true;
    o->sock_buf = 65536;
    o->busy_poll_us = 25;
    o->repeater_id = 0;
    o->client_a_id = 1;
    o->client_b_id = 2;
    o->client_id = 1;
    o->werner_in = 1.0;
    o->cpu_a = 2;
    o->cpu_b = 3;
    o->count_interval = 0.0;
    o->plot_prefix = role == ROLE_REPEATER ? "repeater_send_hist" : "delay_hist_client";
    o->plot_dir = "csv";
    o->json_output = true;
    o->clock_sync_samples = 8;
    o->pace_mode = PACE_SLEEP;
    o->spin_margin_us = 100.0;
    o->data_protocol = PROTO_UDP;
    o->send_mode = SEND_MODE_BURST;
    o->udp_ready_timeout = 30.0;
    o->udp_idle_timeout = 5.0;
    o->t1_ns = 1000000.0;
}

static const char *need_arg(int argc, char **argv, int *i) {
    if (*i + 1 >= argc) {
        fprintf(stderr, "Missing value for %s\n", argv[*i]);
        exit(2);
    }
    ++(*i);
    return argv[*i];
}

static bool streq(const char *a, const char *b) {
    return strcmp(a, b) == 0;
}

static const char *protocol_name(DataProtocol protocol) {
    return protocol == PROTO_UDP ? "udp" : "tcp";
}

static const char *send_mode_name(SendMode mode) {
    switch (mode) {
        case SEND_MODE_BURST:
            return "burst";
        case SEND_MODE_PACED:
            return "paced";
        case SEND_MODE_ACK:
            return "ack";
    }
    return "unknown";
}

static void send_data_ack(int fd, DataProtocol protocol) {
    char ack = DATA_ACK_BYTE;
    ssize_t sent = protocol == PROTO_UDP ? send(fd, &ack, 1, 0) : send_all(fd, &ack, 1);
    if (sent != 1) {
        die("send data ACK");
    }
}

static void recv_data_ack(int fd, DataProtocol protocol) {
    char ack = 0;
    ssize_t got;
    if (protocol == PROTO_UDP) {
        do {
            got = recv(fd, &ack, 1, 0);
        } while (got < 0 && errno == EINTR);
    } else {
        got = recv_exact(fd, &ack, 1);
    }
    if (got != 1) {
        die("recv data ACK");
    }
    if (ack != DATA_ACK_BYTE) {
        die_msg("Received invalid data ACK");
    }
}

static void usage(const char *prog) {
    printf("usage: %s {repeater,client} [options]\n", prog);
    printf("Fast3 unified C port: repeater/client with persistent sockets and low-jitter options.\n");
    printf("\nCommon options:\n");
    printf("  --count N --quiet --plot --plot-dir DIR --json/--no-json --json-dir DIR\n");
    printf("  --cpu N --rt-priority N --sock-buf N --busy-poll-us N\n");
    printf("  --data-protocol udp|tcp --clock-sync --clock-sync-samples N\n");
    printf("\nRepeater options:\n");
    printf("  --listen-host-a HOST --listen-port-a PORT --listen-host-b HOST --listen-port-b PORT\n");
    printf("  --werner-ar X --werner-br X --parallel --cpu-a N --cpu-b N\n");
    printf("  --shared-send-timestamp --count-interval SEC --pace-mode sleep|spin|hybrid\n");
    printf("  --spin-margin-us US --send-mode burst|paced|ack --udp-ready-timeout SEC\n");
    printf("\nClient options:\n");
    printf("  --repeater-host HOST --repeater-port PORT --client-id N --warmup N\n");
    printf("  --connect-timeout SEC --detect-timeout SEC --detect-interval SEC\n");
    printf("  --clock-offset-ns NS --center-delay --udp-idle-timeout SEC --kernel-timestamp\n");
}

static void parse_args(int argc, char **argv, Options *o) {
    if (argc < 2 || streq(argv[1], "-h") || streq(argv[1], "--help")) {
        usage(argv[0]);
        exit(argc < 2 ? 2 : 0);
    }
    Role role;
    if (streq(argv[1], "repeater")) {
        role = ROLE_REPEATER;
    } else if (streq(argv[1], "client")) {
        role = ROLE_CLIENT;
    } else {
        usage(argv[0]);
        exit(2);
    }
    parse_defaults(o, role);
    o->argc = argc;
    o->argv = argv;
    for (int i = 2; i < argc; ++i) {
        const char *arg = argv[i];
        if (streq(arg, "--help") || streq(arg, "-h")) {
            usage(argv[0]);
            exit(0);
        } else if (streq(arg, "--listen-host-a")) {
            o->listen_host_a = need_arg(argc, argv, &i);
        } else if (streq(arg, "--listen-port-a")) {
            o->listen_port_a = atoi(need_arg(argc, argv, &i));
        } else if (streq(arg, "--listen-host-b")) {
            o->listen_host_b = need_arg(argc, argv, &i);
        } else if (streq(arg, "--listen-port-b")) {
            o->listen_port_b = atoi(need_arg(argc, argv, &i));
        } else if (streq(arg, "--repeater-host")) {
            o->repeater_host = need_arg(argc, argv, &i);
        } else if (streq(arg, "--repeater-port")) {
            o->repeater_port = atoi(need_arg(argc, argv, &i));
        } else if (streq(arg, "--count")) {
            o->count = atoi(need_arg(argc, argv, &i));
        } else if (streq(arg, "--warmup")) {
            o->warmup = atoi(need_arg(argc, argv, &i));
        } else if (streq(arg, "--accept-timeout")) {
            o->accept_timeout = atof(need_arg(argc, argv, &i));
        } else if (streq(arg, "--connect-timeout")) {
            o->connect_timeout = atof(need_arg(argc, argv, &i));
        } else if (streq(arg, "--detect-timeout")) {
            o->detect_timeout = atof(need_arg(argc, argv, &i));
        } else if (streq(arg, "--detect-interval")) {
            o->detect_interval = atof(need_arg(argc, argv, &i));
        } else if (streq(arg, "--cpu")) {
            o->cpu = atoi(need_arg(argc, argv, &i));
            o->cpu_set = true;
        } else if (streq(arg, "--rt-priority")) {
            o->rt_priority = atoi(need_arg(argc, argv, &i));
            o->rt_priority_set = true;
        } else if (streq(arg, "--sock-buf")) {
            o->sock_buf = atoi(need_arg(argc, argv, &i));
        } else if (streq(arg, "--busy-poll-us")) {
            o->busy_poll_us = atoi(need_arg(argc, argv, &i));
        } else if (streq(arg, "--repeater-id")) {
            o->repeater_id = atoi(need_arg(argc, argv, &i));
        } else if (streq(arg, "--client-a-id")) {
            o->client_a_id = atoi(need_arg(argc, argv, &i));
        } else if (streq(arg, "--client-b-id")) {
            o->client_b_id = atoi(need_arg(argc, argv, &i));
        } else if (streq(arg, "--client-id")) {
            o->client_id = atoi(need_arg(argc, argv, &i));
        } else if (streq(arg, "--werner-ar")) {
            o->werner_ar = atof(need_arg(argc, argv, &i));
            o->werner_ar_set = true;
        } else if (streq(arg, "--werner-br")) {
            o->werner_br = atof(need_arg(argc, argv, &i));
            o->werner_br_set = true;
        } else if (streq(arg, "--werner-in")) {
            o->werner_in = atof(need_arg(argc, argv, &i));
        } else if (streq(arg, "--parallel")) {
            o->parallel = true;
        } else if (streq(arg, "--cpu-a")) {
            o->cpu_a = atoi(need_arg(argc, argv, &i));
        } else if (streq(arg, "--cpu-b")) {
            o->cpu_b = atoi(need_arg(argc, argv, &i));
        } else if (streq(arg, "--count-interval")) {
            o->count_interval = atof(need_arg(argc, argv, &i));
        } else if (streq(arg, "--pace-mode")) {
            const char *v = need_arg(argc, argv, &i);
            if (streq(v, "sleep")) {
                o->pace_mode = PACE_SLEEP;
            } else if (streq(v, "spin")) {
                o->pace_mode = PACE_SPIN;
            } else if (streq(v, "hybrid")) {
                o->pace_mode = PACE_HYBRID;
            } else {
                die_msg("--pace-mode must be sleep, spin, or hybrid");
            }
        } else if (streq(arg, "--spin-margin-us")) {
            o->spin_margin_us = atof(need_arg(argc, argv, &i));
        } else if (streq(arg, "--shared-send-timestamp")) {
            o->shared_send_timestamp = true;
        } else if (streq(arg, "--quiet")) {
            o->quiet = true;
        } else if (streq(arg, "--plot")) {
            o->plot = true;
        } else if (streq(arg, "--plot-prefix")) {
            o->plot_prefix = need_arg(argc, argv, &i);
        } else if (streq(arg, "--plot-dir")) {
            o->plot_dir = need_arg(argc, argv, &i);
        } else if (streq(arg, "--json")) {
            o->json_output = true;
        } else if (streq(arg, "--no-json")) {
            o->json_output = false;
        } else if (streq(arg, "--json-dir")) {
            o->json_dir = need_arg(argc, argv, &i);
        } else if (streq(arg, "--diag")) {
            o->diag = true;
        } else if (streq(arg, "--clock-sync")) {
            o->clock_sync = true;
        } else if (streq(arg, "--clock-sync-samples")) {
            o->clock_sync_samples = atoi(need_arg(argc, argv, &i));
        } else if (streq(arg, "--clock-offset-ns")) {
            o->clock_offset_ns = atoll(need_arg(argc, argv, &i));
            o->clock_offset_set = true;
        } else if (streq(arg, "--center-delay")) {
            o->center_delay = true;
        } else if (streq(arg, "--data-protocol")) {
            const char *v = need_arg(argc, argv, &i);
            if (streq(v, "udp")) {
                o->data_protocol = PROTO_UDP;
            } else if (streq(v, "tcp")) {
                o->data_protocol = PROTO_TCP;
            } else {
                die_msg("--data-protocol must be udp or tcp");
            }
        } else if (streq(arg, "--send-mode")) {
            const char *v = need_arg(argc, argv, &i);
            if (streq(v, "burst")) {
                o->send_mode = SEND_MODE_BURST;
            } else if (streq(v, "paced")) {
                o->send_mode = SEND_MODE_PACED;
            } else if (streq(v, "ack")) {
                o->send_mode = SEND_MODE_ACK;
            } else {
                die_msg("--send-mode must be burst, paced, or ack");
            }
        } else if (streq(arg, "--udp-ready-timeout")) {
            o->udp_ready_timeout = atof(need_arg(argc, argv, &i));
        } else if (streq(arg, "--udp-idle-timeout")) {
            o->udp_idle_timeout = atof(need_arg(argc, argv, &i));
        } else if (streq(arg, "--kernel-timestamp")) {
            o->kernel_timestamp = true;
        } else if (streq(arg, "--t1-ns")) {
            o->t1_ns = atof(need_arg(argc, argv, &i));
        } else {
            fprintf(stderr, "unrecognized argument: %s\n", arg);
            exit(2);
        }
    }
    if (o->send_mode == SEND_MODE_PACED && o->count_interval <= 0.0) {
        die_msg("--send-mode paced requires --count-interval > 0");
    }
}

static int wait_fd_readable(int fd, double timeout_seconds) {
    fd_set set;
    FD_ZERO(&set);
    FD_SET(fd, &set);
    struct timeval tv;
    tv.tv_sec = (time_t)timeout_seconds;
    tv.tv_usec = (suseconds_t)((timeout_seconds - (double)tv.tv_sec) * 1000000.0);
    if (tv.tv_usec < 0) {
        tv.tv_usec = 0;
    }
    int res;
    do {
        res = select(fd + 1, &set, NULL, NULL, &tv);
    } while (res < 0 && errno == EINTR);
    return res;
}

static int wait_fd_writable(int fd, double timeout_seconds) {
    fd_set set;
    FD_ZERO(&set);
    FD_SET(fd, &set);
    struct timeval tv;
    tv.tv_sec = (time_t)timeout_seconds;
    tv.tv_usec = (suseconds_t)((timeout_seconds - (double)tv.tv_sec) * 1000000.0);
    if (tv.tv_usec < 0) {
        tv.tv_usec = 0;
    }
    int res;
    do {
        res = select(fd + 1, NULL, &set, NULL, &tv);
    } while (res < 0 && errno == EINTR);
    return res;
}

static int accept_one(const char *host, int port, double timeout, int sock_buf, int busy_poll_us) {
    int server = socket(AF_INET, SOCK_STREAM, 0);
    if (server < 0) {
        die("socket");
    }
    int one = 1;
    if (setsockopt(server, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one)) != 0) {
        die("setsockopt SO_REUSEADDR");
    }
    enable_low_latency_socket(server, sock_buf, busy_poll_us);
    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port = htons((uint16_t)port);
    if (inet_pton(AF_INET, host, &addr.sin_addr) != 1) {
        if (streq(host, "0.0.0.0")) {
            addr.sin_addr.s_addr = htonl(INADDR_ANY);
        } else {
            fprintf(stderr, "Invalid listen host: %s\n", host);
            exit(1);
        }
    }
    if (bind(server, (struct sockaddr *)&addr, sizeof(addr)) != 0) {
        die("bind");
    }
    if (listen(server, 1) != 0) {
        die("listen");
    }
    int ready = wait_fd_readable(server, timeout);
    if (ready <= 0) {
        if (ready == 0) {
            die_msg("accept timeout expired");
        }
        die("select accept");
    }
    int conn = accept(server, NULL, NULL);
    if (conn < 0) {
        die("accept");
    }
    close(server);
    enable_low_latency_socket(conn, sock_buf, busy_poll_us);
    return conn;
}

static int connect_with_timeout(const char *host, int port, double timeout, int sock_buf, int busy_poll_us) {
    char portbuf[32];
    snprintf(portbuf, sizeof(portbuf), "%d", port);
    struct addrinfo hints;
    memset(&hints, 0, sizeof(hints));
    hints.ai_family = AF_INET;
    hints.ai_socktype = SOCK_STREAM;
    struct addrinfo *results = NULL;
    int gai = getaddrinfo(host, portbuf, &hints, &results);
    if (gai != 0) {
        errno = EHOSTUNREACH;
        return -1;
    }
    int fd = -1;
    for (struct addrinfo *rp = results; rp; rp = rp->ai_next) {
        fd = socket(rp->ai_family, rp->ai_socktype, rp->ai_protocol);
        if (fd < 0) {
            continue;
        }
        int flags = fcntl(fd, F_GETFL, 0);
        (void)fcntl(fd, F_SETFL, flags | O_NONBLOCK);
        int res = connect(fd, rp->ai_addr, rp->ai_addrlen);
        if (res == 0) {
            (void)fcntl(fd, F_SETFL, flags);
            break;
        }
        if (errno == EINPROGRESS) {
            int w = wait_fd_writable(fd, timeout);
            if (w > 0) {
                int err = 0;
                socklen_t err_len = sizeof(err);
                if (getsockopt(fd, SOL_SOCKET, SO_ERROR, &err, &err_len) == 0 && err == 0) {
                    (void)fcntl(fd, F_SETFL, flags);
                    break;
                }
            }
        }
        close(fd);
        fd = -1;
    }
    freeaddrinfo(results);
    if (fd >= 0) {
        enable_low_latency_socket(fd, sock_buf, busy_poll_us);
    }
    return fd;
}

static int connect_repeater_until_ready(
    const char *host,
    int port,
    double connect_timeout,
    double detect_timeout,
    double detect_interval,
    int sock_buf,
    int busy_poll_us
) {
    uint64_t deadline = monotonic_ns() + (uint64_t)(detect_timeout * 1e9);
    while (monotonic_ns() < deadline) {
        double remaining = (double)(deadline - monotonic_ns()) / 1e9;
        if (remaining < 0.001) {
            remaining = 0.001;
        }
        double timeout = connect_timeout;
        if (timeout < 0.001) {
            timeout = 0.001;
        }
        if (timeout > remaining) {
            timeout = remaining;
        }
        int fd = connect_with_timeout(host, port, timeout, sock_buf, busy_poll_us);
        if (fd >= 0) {
            return fd;
        }
        remaining = (double)(deadline - monotonic_ns()) / 1e9;
        if (remaining <= 0.0) {
            break;
        }
        double sleep_s = detect_interval;
        if (sleep_s < 0.0) {
            sleep_s = 0.0;
        }
        if (sleep_s > remaining) {
            sleep_s = remaining;
        }
        sleep_seconds(sleep_s);
    }
    die_msg("Repeater was not detected before detect-timeout expired");
    return -1;
}

static int udp_bind_socket(const char *host, int port, int sock_buf, int busy_poll_us, double timeout) {
    int fd = socket(AF_INET, SOCK_DGRAM, 0);
    if (fd < 0) {
        die("socket UDP");
    }
    int one = 1;
    if (setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one)) != 0) {
        die("setsockopt UDP SO_REUSEADDR");
    }
    enable_low_latency_socket(fd, sock_buf, busy_poll_us);
    if (timeout >= 0.0) {
        struct timeval tv;
        tv.tv_sec = (time_t)timeout;
        tv.tv_usec = (suseconds_t)((timeout - (double)tv.tv_sec) * 1000000.0);
        if (tv.tv_usec < 0) {
            tv.tv_usec = 0;
        }
        (void)setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    }
    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port = htons((uint16_t)port);
    if (inet_pton(AF_INET, host, &addr.sin_addr) != 1) {
        if (streq(host, "0.0.0.0")) {
            addr.sin_addr.s_addr = htonl(INADDR_ANY);
        } else {
            fprintf(stderr, "Invalid UDP bind host: %s\n", host);
            exit(1);
        }
    }
    if (bind(fd, (struct sockaddr *)&addr, sizeof(addr)) != 0) {
        die("bind UDP");
    }
    return fd;
}

static int accept_udp_peer(int control_fd, int udp_fd) {
    unsigned char buf[64];
    struct sockaddr_in addr;
    socklen_t addr_len = sizeof(addr);
    while (true) {
        ssize_t n = recvfrom(udp_fd, buf, sizeof(buf), 0, (struct sockaddr *)&addr, &addr_len);
        if (n < 0) {
            die("recvfrom UDP hello");
        }
        if (n == UDP_HELLO_MAGIC_SIZE && memcmp(buf, UDP_HELLO_MAGIC, UDP_HELLO_MAGIC_SIZE) == 0) {
            if (connect(udp_fd, (struct sockaddr *)&addr, addr_len) != 0) {
                die("connect UDP peer");
            }
            char ready = UDP_READY_BYTE;
            if (send_all(control_fd, &ready, 1) != 1) {
                die("send UDP ready");
            }
            return udp_fd;
        }
    }
}

static int connect_udp_data(
    int control_fd,
    const char *host,
    int port,
    int sock_buf,
    int busy_poll_us,
    double detect_timeout,
    double detect_interval
) {
    int udp_fd = socket(AF_INET, SOCK_DGRAM, 0);
    if (udp_fd < 0) {
        die("socket UDP client");
    }
    enable_low_latency_socket(udp_fd, sock_buf, busy_poll_us);
    struct sockaddr_in local;
    memset(&local, 0, sizeof(local));
    local.sin_family = AF_INET;
    local.sin_addr.s_addr = htonl(INADDR_ANY);
    local.sin_port = htons(0);
    if (bind(udp_fd, (struct sockaddr *)&local, sizeof(local)) != 0) {
        die("bind UDP client");
    }
    struct sockaddr_in remote;
    memset(&remote, 0, sizeof(remote));
    remote.sin_family = AF_INET;
    remote.sin_port = htons((uint16_t)port);
    if (inet_pton(AF_INET, host, &remote.sin_addr) != 1) {
        struct hostent *he = gethostbyname(host);
        if (!he || he->h_addrtype != AF_INET) {
            die_msg("Could not resolve UDP host");
        }
        memcpy(&remote.sin_addr, he->h_addr, sizeof(remote.sin_addr));
    }
    if (connect(udp_fd, (struct sockaddr *)&remote, sizeof(remote)) != 0) {
        die("connect UDP client");
    }
    uint64_t deadline = monotonic_ns() + (uint64_t)(detect_timeout * 1e9);
    while (monotonic_ns() < deadline) {
        ssize_t sent = send(udp_fd, UDP_HELLO_MAGIC, UDP_HELLO_MAGIC_SIZE, 0);
        if (sent < 0 && errno != ECONNREFUSED && errno != EINTR) {
            die("send UDP hello");
        }
        double wait_s = detect_interval;
        if (wait_s < 0.001) {
            wait_s = 0.001;
        }
        if (wait_s > 0.05) {
            wait_s = 0.05;
        }
        int ready = wait_fd_readable(control_fd, wait_s);
        if (ready > 0) {
            char c;
            ssize_t n = recv(control_fd, &c, 1, 0);
            if (n == 1 && c == UDP_READY_BYTE) {
                return udp_fd;
            }
            if (n == 0) {
                die_msg("TCP control socket closed before UDP setup");
            }
        }
        sleep_seconds(detect_interval > 0.01 ? 0.01 : detect_interval);
    }
    close(udp_fd);
    die_msg("Repeater did not confirm UDP setup before detect-timeout expired");
    return -1;
}

static void serve_clock_sync(int fd, int samples) {
    if (samples < 0) {
        samples = 0;
    }
    unsigned char sync_buf[CLOCK_SYNC_SYNC_SIZE];
    unsigned char req_buf[CLOCK_SYNC_DELAY_REQ_SIZE];
    unsigned char resp_buf[CLOCK_SYNC_DELAY_RESP_SIZE];
    for (int i = 0; i < samples; ++i) {
        uint64_t t1 = time_ns();
        pack_u64be(sync_buf, t1);
        if (send_all(fd, sync_buf, sizeof(sync_buf)) != (ssize_t)sizeof(sync_buf)) {
            die("clock sync send");
        }
        if (recv_exact(fd, req_buf, sizeof(req_buf)) != (ssize_t)sizeof(req_buf)) {
            die("clock sync recv req");
        }
        uint64_t t4 = time_ns();
        uint64_t t1_echo = unpack_u64be(req_buf);
        uint64_t t2 = unpack_u64be(req_buf + 8);
        uint64_t t3 = unpack_u64be(req_buf + 16);
        if (t1_echo != t1) {
            die_msg("PTP clock-sync request did not match the Sync timestamp");
        }
        pack_u64be(resp_buf, t1);
        pack_u64be(resp_buf + 8, t2);
        pack_u64be(resp_buf + 16, t3);
        pack_u64be(resp_buf + 24, t4);
        if (send_all(fd, resp_buf, sizeof(resp_buf)) != (ssize_t)sizeof(resp_buf)) {
            die("clock sync send resp");
        }
    }
}

static void estimate_clock_offset(
    int fd,
    int samples,
    int64_t *clock_offset_ns,
    int64_t *clock_sync_path_delay_ns,
    ClockSyncRow **rows_out,
    int *row_count_out
) {
    if (samples < 0) {
        samples = 0;
    }
    ClockSyncRow *rows = NULL;
    if (samples > 0) {
        rows = calloc((size_t)samples, sizeof(*rows));
        if (!rows) {
            die("calloc clock rows");
        }
    }
    int64_t offset_total = 0;
    int64_t path_total = 0;
    int sample_count = 0;
    unsigned char sync_buf[CLOCK_SYNC_SYNC_SIZE];
    unsigned char req_buf[CLOCK_SYNC_DELAY_REQ_SIZE];
    unsigned char resp_buf[CLOCK_SYNC_DELAY_RESP_SIZE];
    for (int i = 0; i < samples; ++i) {
        if (recv_exact(fd, sync_buf, sizeof(sync_buf)) != (ssize_t)sizeof(sync_buf)) {
            die("clock sync recv sync");
        }
        uint64_t t1 = unpack_u64be(sync_buf);
        uint64_t t2 = time_ns();
        uint64_t t3 = time_ns();
        pack_u64be(req_buf, t1);
        pack_u64be(req_buf + 8, t2);
        pack_u64be(req_buf + 16, t3);
        if (send_all(fd, req_buf, sizeof(req_buf)) != (ssize_t)sizeof(req_buf)) {
            die("clock sync send req");
        }
        if (recv_exact(fd, resp_buf, sizeof(resp_buf)) != (ssize_t)sizeof(resp_buf)) {
            die("clock sync recv resp");
        }
        uint64_t echoed_t1 = unpack_u64be(resp_buf);
        uint64_t echoed_t2 = unpack_u64be(resp_buf + 8);
        uint64_t echoed_t3 = unpack_u64be(resp_buf + 16);
        uint64_t t4 = unpack_u64be(resp_buf + 24);
        if (echoed_t1 != t1 || echoed_t2 != t2 || echoed_t3 != t3) {
            die_msg("PTP clock-sync response did not match the Delay_Req timestamps");
        }
        int64_t master_to_slave = (int64_t)(t2 - t1);
        int64_t slave_to_master = (int64_t)(t4 - t3);
        int64_t mean_path = (master_to_slave + slave_to_master) / 2;
        int64_t offset = (slave_to_master - master_to_slave) / 2;
        offset_total += offset;
        path_total += mean_path;
        ++sample_count;
        rows[i].sample_idx = sample_count;
        rows[i].t1_ns = t1;
        rows[i].t2_ns = t2;
        rows[i].t3_ns = t3;
        rows[i].t4_ns = t4;
        rows[i].master_to_slave_ns = master_to_slave;
        rows[i].slave_to_master_ns = slave_to_master;
        rows[i].offset_ns = offset;
        rows[i].path_delay_ns = mean_path;
    }
    if (sample_count == 0) {
        *clock_offset_ns = 0;
        *clock_sync_path_delay_ns = 0;
    } else {
        *clock_offset_ns = offset_total / sample_count;
        *clock_sync_path_delay_ns = path_total / sample_count;
    }
    *rows_out = rows;
    *row_count_out = sample_count;
}

static void *sender_thread_main(void *arg) {
    SenderCtx *ctx = (SenderCtx *)arg;
    set_thread_affinity(ctx->cpu_pin);
    for (int idx = 0; idx < ctx->count; ++idx) {
        pthread_barrier_wait(ctx->barrier_ready);
        uint64_t ts_emit_ns = time_ns();
        uint8_t correction_bits = ctx->correction_bits_samples[idx];
        double w_swap = 1.0;
        if (ctx->proto == PROTO_UDP) {
            pack_udp_msg(ctx->buf, (uint32_t)(idx + 1), ts_emit_ns, (uint32_t)ctx->peer_id, correction_bits, w_swap);
        } else {
            pack_msg(ctx->buf, ts_emit_ns, (uint32_t)ctx->peer_id, correction_bits, w_swap);
        }
        if (ctx->diag) {
            uint64_t pre = monotonic_ns();
            ssize_t sent = ctx->proto == PROTO_UDP
                ? send(ctx->fd, ctx->buf, (size_t)ctx->msg_size, 0)
                : send_all(ctx->fd, ctx->buf, (size_t)ctx->msg_size);
            if (sent < 0) {
                die("sender send");
            }
            ctx->send_block_samples[idx] = (int64_t)(monotonic_ns() - pre);
        } else {
            ssize_t sent = ctx->proto == PROTO_UDP
                ? send(ctx->fd, ctx->buf, (size_t)ctx->msg_size, 0)
                : send_all(ctx->fd, ctx->buf, (size_t)ctx->msg_size);
            if (sent < 0) {
                die("sender send");
            }
        }
        ctx->last_msg_ref->ts_emit_ns = ts_emit_ns;
        ctx->last_msg_ref->peer_id = (uint32_t)ctx->peer_id;
        ctx->last_msg_ref->bits = correction_bits;
        ctx->last_msg_ref->w_swap = w_swap;
        pthread_barrier_wait(ctx->barrier_done);
    }
    return NULL;
}

static int run_repeater(const Options *o) {
    apply_cpu_rt(o);
    int count = o->count > 0 ? o->count : 1;
    double w_ar_init = o->werner_ar;
    double w_br_init = o->werner_br;
    if (!o->werner_ar_set) {
        printf("werner_ar: ");
        fflush(stdout);
        if (scanf("%lf", &w_ar_init) != 1) {
            die_msg("Invalid werner_ar");
        }
    }
    if (!o->werner_br_set) {
        printf("werner_br: ");
        fflush(stdout);
        if (scanf("%lf", &w_br_init) != 1) {
            die_msg("Invalid werner_br");
        }
    }
    State state_ar = {o->repeater_id, w_ar_init, o->client_a_id, false};
    State state_br = {o->repeater_id, w_br_init, o->client_b_id, false};
    State last_state_in_ar = state_ar;
    State last_state_in_br = state_br;
    Msg last_msg_a = {0, 0, 0, 0.0};
    Msg last_msg_b = {0, 0, 0, 0.0};
    int data_msg_size = o->data_protocol == PROTO_UDP ? UDP_MSG_SIZE : MSG_SIZE;
    unsigned char *outbuf_a = calloc((size_t)data_msg_size, 1);
    unsigned char *outbuf_b = calloc((size_t)data_msg_size, 1);
    int64_t *send_a_block_samples = o->diag ? calloc((size_t)count, sizeof(int64_t)) : NULL;
    int64_t *send_b_block_samples = o->diag ? calloc((size_t)count, sizeof(int64_t)) : NULL;
    int64_t *send_gap_ab_samples = o->diag ? calloc((size_t)count, sizeof(int64_t)) : NULL;
    uint8_t *correction_bits_samples = calloc((size_t)count, sizeof(uint8_t));
    if (!outbuf_a || !outbuf_b || !correction_bits_samples || (o->diag && (!send_a_block_samples || !send_b_block_samples || !send_gap_ab_samples))) {
        die("calloc repeater");
    }
    srand((unsigned int)(time(NULL) ^ getpid()));
    for (int i = 0; i < count; ++i) {
        correction_bits_samples[i] = (uint8_t)(rand() % 4);
    }
    uint64_t pace_interval_ns = o->count_interval > 0.0 ? (uint64_t)(o->count_interval * 1e9) : 0;
    uint64_t spin_margin_ns = o->spin_margin_us > 0.0 ? (uint64_t)(o->spin_margin_us * 1000.0) : 0;

    int conn_a = accept_one(o->listen_host_a, o->listen_port_a, o->accept_timeout, o->sock_buf, o->busy_poll_us);
    int conn_b = accept_one(o->listen_host_b, o->listen_port_b, o->accept_timeout, o->sock_buf, o->busy_poll_us);
    int udp_a = -1;
    int udp_b = -1;
    if (o->data_protocol == PROTO_UDP) {
        udp_a = udp_bind_socket(o->listen_host_a, o->listen_port_a, o->sock_buf, o->busy_poll_us, o->udp_ready_timeout);
        udp_b = udp_bind_socket(o->listen_host_b, o->listen_port_b, o->sock_buf, o->busy_poll_us, o->udp_ready_timeout);
    }
    if (o->clock_sync) {
        serve_clock_sync(conn_a, o->clock_sync_samples);
        serve_clock_sync(conn_b, o->clock_sync_samples);
    }
    int data_a = conn_a;
    int data_b = conn_b;
    if (o->data_protocol == PROTO_UDP) {
        data_a = accept_udp_peer(conn_a, udp_a);
        data_b = accept_udp_peer(conn_b, udp_b);
    }

    if (o->parallel) {
        pthread_barrier_t barrier_ready;
        pthread_barrier_t barrier_done;
        pthread_barrier_init(&barrier_ready, NULL, 3);
        pthread_barrier_init(&barrier_done, NULL, 3);
        SenderCtx ctx_a = {
            data_a, outbuf_a, count, data_msg_size, o->data_protocol, o->client_b_id, o->cpu_a, o->diag,
            send_a_block_samples, correction_bits_samples, &barrier_ready, &barrier_done, &last_msg_a
        };
        SenderCtx ctx_b = {
            data_b, outbuf_b, count, data_msg_size, o->data_protocol, o->client_a_id, o->cpu_b, o->diag,
            send_b_block_samples, correction_bits_samples, &barrier_ready, &barrier_done, &last_msg_b
        };
        pthread_t ta;
        pthread_t tb;
        if (pthread_create(&ta, NULL, sender_thread_main, &ctx_a) != 0) {
            die("pthread_create A");
        }
        if (pthread_create(&tb, NULL, sender_thread_main, &ctx_b) != 0) {
            die("pthread_create B");
        }
        for (int idx = 0; idx < count; ++idx) {
            last_state_in_ar = state_ar;
            last_state_in_br = state_br;
            pthread_barrier_wait(&barrier_ready);
            pthread_barrier_wait(&barrier_done);
            if (o->send_mode == SEND_MODE_ACK) {
                recv_data_ack(data_a, o->data_protocol);
                recv_data_ack(data_b, o->data_protocol);
            }
            if (o->diag) {
                send_gap_ab_samples[idx] = 0;
            }
            pace_wait(pace_interval_ns, o->pace_mode, spin_margin_ns);
        }
        pthread_join(ta, NULL);
        pthread_join(tb, NULL);
        pthread_barrier_destroy(&barrier_ready);
        pthread_barrier_destroy(&barrier_done);
    } else {
        for (int idx = 0; idx < count; ++idx) {
            uint8_t correction_bits = correction_bits_samples[idx];
            double w_swap = 1.0;
            last_state_in_ar = state_ar;
            last_state_in_br = state_br;
            uint64_t ts_emit_a_ns = time_ns();
            if (o->data_protocol == PROTO_UDP) {
                pack_udp_msg(outbuf_a, (uint32_t)(idx + 1), ts_emit_a_ns, (uint32_t)o->client_b_id, correction_bits, w_swap);
            } else {
                pack_msg(outbuf_a, ts_emit_a_ns, (uint32_t)o->client_b_id, correction_bits, w_swap);
            }
            if (o->diag) {
                uint64_t pre_a = monotonic_ns();
                ssize_t sent = o->data_protocol == PROTO_UDP
                    ? send(data_a, outbuf_a, (size_t)data_msg_size, 0)
                    : send_all(data_a, outbuf_a, (size_t)data_msg_size);
                if (sent < 0) {
                    die("send A");
                }
                uint64_t post_a = monotonic_ns();
                send_a_block_samples[idx] = (int64_t)(post_a - pre_a);
                last_msg_a = (Msg){ts_emit_a_ns, (uint32_t)o->client_b_id, correction_bits, w_swap};
                uint64_t ts_emit_b_ns = o->shared_send_timestamp ? ts_emit_a_ns : time_ns();
                if (o->data_protocol == PROTO_UDP) {
                    pack_udp_msg(outbuf_b, (uint32_t)(idx + 1), ts_emit_b_ns, (uint32_t)o->client_a_id, correction_bits, w_swap);
                } else {
                    pack_msg(outbuf_b, ts_emit_b_ns, (uint32_t)o->client_a_id, correction_bits, w_swap);
                }
                uint64_t pre_b = monotonic_ns();
                send_gap_ab_samples[idx] = (int64_t)(pre_b - pre_a);
                sent = o->data_protocol == PROTO_UDP
                    ? send(data_b, outbuf_b, (size_t)data_msg_size, 0)
                    : send_all(data_b, outbuf_b, (size_t)data_msg_size);
                if (sent < 0) {
                    die("send B");
                }
                send_b_block_samples[idx] = (int64_t)(monotonic_ns() - pre_b);
                last_msg_b = (Msg){ts_emit_b_ns, (uint32_t)o->client_a_id, correction_bits, w_swap};
                if (o->send_mode == SEND_MODE_ACK) {
                    recv_data_ack(data_a, o->data_protocol);
                    recv_data_ack(data_b, o->data_protocol);
                }
            } else {
                ssize_t sent = o->data_protocol == PROTO_UDP
                    ? send(data_a, outbuf_a, (size_t)data_msg_size, 0)
                    : send_all(data_a, outbuf_a, (size_t)data_msg_size);
                if (sent < 0) {
                    die("send A");
                }
                last_msg_a = (Msg){ts_emit_a_ns, (uint32_t)o->client_b_id, correction_bits, w_swap};
                uint64_t ts_emit_b_ns = o->shared_send_timestamp ? ts_emit_a_ns : time_ns();
                if (o->data_protocol == PROTO_UDP) {
                    pack_udp_msg(outbuf_b, (uint32_t)(idx + 1), ts_emit_b_ns, (uint32_t)o->client_a_id, correction_bits, w_swap);
                } else {
                    pack_msg(outbuf_b, ts_emit_b_ns, (uint32_t)o->client_a_id, correction_bits, w_swap);
                }
                sent = o->data_protocol == PROTO_UDP
                    ? send(data_b, outbuf_b, (size_t)data_msg_size, 0)
                    : send_all(data_b, outbuf_b, (size_t)data_msg_size);
                if (sent < 0) {
                    die("send B");
                }
                last_msg_b = (Msg){ts_emit_b_ns, (uint32_t)o->client_a_id, correction_bits, w_swap};
                if (o->send_mode == SEND_MODE_ACK) {
                    recv_data_ack(data_a, o->data_protocol);
                    recv_data_ack(data_b, o->data_protocol);
                }
            }
            pace_wait(pace_interval_ns, o->pace_mode, spin_margin_ns);
        }
    }

    if (o->plot) {
        ensure_dir(o->plot_dir);
        char csv_path[4096];
        unique_csv_path(csv_path, sizeof(csv_path), o->plot_dir, o->plot_prefix, "");
        FILE *f = fopen(csv_path, "w");
        if (!f) {
            die("fopen repeater csv");
        }
        if (o->diag) {
            fprintf(f, "count_idx,send_a_block_ns,send_b_block_ns,send_gap_ab_ns\n");
            for (int i = 0; i < count; ++i) {
                fprintf(
                    f,
                    "%d,%lld,%lld,%lld\n",
                    i + 1,
                    (long long)send_a_block_samples[i],
                    (long long)send_b_block_samples[i],
                    (long long)send_gap_ab_samples[i]
                );
            }
        } else {
            fprintf(f, "count_idx\n");
            for (int i = 0; i < count; ++i) {
                fprintf(f, "%d\n", i + 1);
            }
        }
        fclose(f);
        chown_to_sudo_user(csv_path);
        printf("repeater_plot=data_saved (%s)\n", csv_path);
        if (o->json_output) {
            char json_dir_buf[8192];
            const char *json_dir = o->json_dir;
            if (!json_dir) {
                default_json_dir(json_dir_buf, sizeof(json_dir_buf), o->plot_dir);
                json_dir = json_dir_buf;
            }
            ensure_dir(json_dir);
            const char *suffix = path_suffix(csv_path, o->plot_prefix);
            size_t json_path_len = strlen(json_dir) + strlen(o->plot_prefix) + strlen(suffix) + 8;
            char *json_path = malloc(json_path_len);
            if (!json_path) {
                die("malloc repeater json path");
            }
            snprintf(json_path, json_path_len, "%s/%s%s.json", json_dir, o->plot_prefix, suffix);
            FILE *jf = fopen(json_path, "w");
            if (!jf) {
                die("fopen repeater json");
            }
            fprintf(jf, "{\n");
            fprintf(jf, "  \"role\": \"repeater\",\n");
            fprintf(jf, "  \"argv\": ");
            json_print_argv(jf, o);
            fprintf(jf, ",\n");
            fprintf(jf, "  \"args\": {\n");
            fprintf(jf, "    \"count\": %d,\n", count);
            fprintf(jf, "    \"listen_host_a\": ");
            json_print_string(jf, o->listen_host_a);
            fprintf(jf, ",\n    \"listen_port_a\": %d,\n", o->listen_port_a);
            fprintf(jf, "    \"listen_host_b\": ");
            json_print_string(jf, o->listen_host_b);
            fprintf(jf, ",\n    \"listen_port_b\": %d,\n", o->listen_port_b);
            fprintf(jf, "    \"data_protocol\": \"%s\",\n", protocol_name(o->data_protocol));
            fprintf(jf, "    \"send_mode\": \"%s\",\n", send_mode_name(o->send_mode));
            fprintf(jf, "    \"pace_mode\": \"%s\",\n", pace_mode_name(o->pace_mode));
            fprintf(jf, "    \"shared_send_timestamp\": %s,\n", o->shared_send_timestamp ? "true" : "false");
            fprintf(jf, "    \"count_interval\": %.12g,\n", o->count_interval);
            fprintf(jf, "    \"sock_buf\": %d,\n", o->sock_buf);
            fprintf(jf, "    \"busy_poll_us\": %d,\n", o->busy_poll_us);
            fprintf(jf, "    \"cpu\": %d,\n", o->cpu);
            fprintf(jf, "    \"rt_priority\": %d\n", o->rt_priority);
            fprintf(jf, "  },\n");
            fprintf(jf, "  \"exchanges\": %d,\n", count);
            fprintf(jf, "  \"csv_path\": ");
            json_print_string(jf, csv_path);
            fprintf(jf, ",\n");
            fprintf(jf, "  \"last_msg_a\": {\"ts_emit_ns\": %llu, \"peer_id\": %u, \"correction_bits\": %u, \"w_swap\": %.17g},\n",
                    (unsigned long long)last_msg_a.ts_emit_ns, last_msg_a.peer_id, last_msg_a.bits, last_msg_a.w_swap);
            fprintf(jf, "  \"last_msg_b\": {\"ts_emit_ns\": %llu, \"peer_id\": %u, \"correction_bits\": %u, \"w_swap\": %.17g},\n",
                    (unsigned long long)last_msg_b.ts_emit_ns, last_msg_b.peer_id, last_msg_b.bits, last_msg_b.w_swap);
            fprintf(jf, "  \"send_samples\": [");
            if (o->diag) {
                for (int i = 0; i < count; ++i) {
                    fprintf(
                        jf,
                        "%s\n    {\"count_idx\": %d, \"send_a_block_ns\": %lld, \"send_b_block_ns\": %lld, \"send_gap_ab_ns\": %lld}",
                        i == 0 ? "" : ",",
                        i + 1,
                        (long long)send_a_block_samples[i],
                        (long long)send_b_block_samples[i],
                        (long long)send_gap_ab_samples[i]
                    );
                }
                if (count > 0) {
                    fputc('\n', jf);
                }
            }
            fprintf(jf, "  ]\n");
            fprintf(jf, "}\n");
            fclose(jf);
            chown_to_sudo_user(json_path);
            printf("repeater_json=data_saved (%s)\n", json_path);
            free(json_path);
        }
    }

    if (!o->quiet) {
        printf("repeater_mode=fast3\n");
        printf("exchanges=%d\n", count);
        printf("repeater_id=%d\n", o->repeater_id);
        printf("data_protocol=%s\n", protocol_name(o->data_protocol));
        printf("send_mode=%s\n", send_mode_name(o->send_mode));
        printf("pace_mode=%s\n", pace_mode_name(o->pace_mode));
        printf("shared_send_timestamp=%s\n", o->shared_send_timestamp ? "true" : "false");
    }
    char statebuf_a[96];
    char statebuf_b[96];
    char tsbuf_a[64];
    char tsbuf_b[64];
    fmt_state(statebuf_a, sizeof(statebuf_a), state_ar);
    fmt_state(statebuf_b, sizeof(statebuf_b), state_br);
    fmt_ts_emit(tsbuf_a, sizeof(tsbuf_a), last_msg_a.ts_emit_ns);
    fmt_ts_emit(tsbuf_b, sizeof(tsbuf_b), last_msg_b.ts_emit_ns);
    printf("\n");
    printf("state_ar_start=%s\n", statebuf_a);
    printf("state_br_start=%s\n", statebuf_b);
    printf("\n");
    printf("repeater_last\n");
    printf(
        "msg_a=(ts_emit_ns=%llu, ts_emit=%s, peer_id=%u, bits=%02u, w_swap=%.6f)\n",
        (unsigned long long)last_msg_a.ts_emit_ns,
        tsbuf_a,
        last_msg_a.peer_id,
        last_msg_a.bits,
        last_msg_a.w_swap
    );
    printf(
        "msg_b=(ts_emit_ns=%llu, ts_emit=%s, peer_id=%u, bits=%02u, w_swap=%.6f)\n",
        (unsigned long long)last_msg_b.ts_emit_ns,
        tsbuf_b,
        last_msg_b.peer_id,
        last_msg_b.bits,
        last_msg_b.w_swap
    );
    fmt_state(statebuf_a, sizeof(statebuf_a), last_state_in_ar);
    fmt_state(statebuf_b, sizeof(statebuf_b), last_state_in_br);
    printf("\n");
    printf("state_in_ar=%s\n", statebuf_a);
    printf("state_in_br=%s\n", statebuf_b);
    State zero_a = {o->repeater_id, 0.0, 0, true};
    State zero_b = {o->repeater_id, 0.0, 0, true};
    fmt_state(statebuf_a, sizeof(statebuf_a), zero_a);
    fmt_state(statebuf_b, sizeof(statebuf_b), zero_b);
    printf("\n");
    printf("state_out_ar=%s\n", statebuf_a);
    printf("state_out_br=%s\n", statebuf_b);

    if (o->data_protocol == PROTO_UDP) {
        close(data_a);
        close(data_b);
        close(conn_a);
        close(conn_b);
    } else {
        close(conn_a);
        close(conn_b);
    }
    free(outbuf_a);
    free(outbuf_b);
    free(send_a_block_samples);
    free(send_b_block_samples);
    free(send_gap_ab_samples);
    free(correction_bits_samples);
    return 0;
}

static SampleMsg pick_by_delta(const SampleMsg *samples, int n, bool want_max, int client_id) {
    if (n <= 0) {
        SampleMsg empty;
        memset(&empty, 0, sizeof(empty));
        empty.state_out = (State){client_id, 0.0, 0, true};
        return empty;
    }
    int best = 0;
    int64_t best_key = i64_abs_value(samples[0].delta_ns);
    for (int i = 1; i < n; ++i) {
        int64_t key = i64_abs_value(samples[i].delta_ns);
        if ((want_max && key > best_key) || (!want_max && key < best_key)) {
            best = i;
            best_key = key;
        }
    }
    return samples[best];
}

static int run_client(const Options *o) {
    apply_cpu_rt(o);
    int count = o->count > 0 ? o->count : 1;
    int warmup = o->warmup;
    if (warmup < 0) {
        warmup = 0;
    }
    if (warmup > count - 1) {
        warmup = count - 1;
    }
    int sample_count = count - warmup;
    int64_t *delta_samples = calloc((size_t)sample_count, sizeof(int64_t));
    int64_t *delay_stat_samples = calloc((size_t)sample_count, sizeof(int64_t));
    int64_t *delay_abs_samples = calloc((size_t)sample_count, sizeof(int64_t));
    double *werner_samples = calloc((size_t)sample_count, sizeof(double));
    double *werner_raw_samples = calloc((size_t)sample_count, sizeof(double));
    SampleMsg *sample_msgs = calloc((size_t)sample_count, sizeof(SampleMsg));
    int *delta_record_counts = calloc((size_t)sample_count, sizeof(int));
    int64_t *loop_gap_samples = o->diag ? calloc((size_t)sample_count, sizeof(int64_t)) : NULL;
    int64_t *recv_block_samples = o->diag ? calloc((size_t)sample_count, sizeof(int64_t)) : NULL;
    unsigned char *udp_seen_counts = o->data_protocol == PROTO_UDP ? calloc((size_t)count + 1, 1) : NULL;
    if (!delta_samples || !delay_stat_samples || !delay_abs_samples || !werner_samples ||
        !werner_raw_samples || !sample_msgs || !delta_record_counts ||
        (o->diag && (!loop_gap_samples || !recv_block_samples)) ||
        (o->data_protocol == PROTO_UDP && !udp_seen_counts)) {
        die("calloc client");
    }
    int data_msg_size = o->data_protocol == PROTO_UDP ? UDP_MSG_SIZE : MSG_SIZE;
    unsigned char *inbuf = calloc((size_t)data_msg_size, 1);
    if (!inbuf) {
        die("calloc inbuf");
    }
    int sample_idx = 0;
    int udp_received = 0;
    int udp_lost_est = 0;
    int udp_seen_total = 0;
    int kernel_timestamp_received = 0;
    int kernel_timestamp_fallback = 0;
    bool kernel_timestamp_enabled = false;
    int64_t last_delta = 0;
    Msg last_raw_msg = {0, 0, 0, 0.0};
    ClockSyncRow *clock_rows = NULL;
    int clock_row_count = 0;
    int64_t clock_offset_ns = 0;
    int64_t clock_sync_path_delay_ns = 0;

    int control_fd = connect_repeater_until_ready(
        o->repeater_host,
        o->repeater_port,
        o->connect_timeout,
        o->detect_timeout,
        o->detect_interval,
        o->sock_buf,
        o->busy_poll_us
    );
    if (o->clock_offset_set) {
        clock_offset_ns = o->clock_offset_ns;
        clock_sync_path_delay_ns = 0;
    } else if (o->clock_sync) {
        estimate_clock_offset(
            control_fd,
            o->clock_sync_samples,
            &clock_offset_ns,
            &clock_sync_path_delay_ns,
            &clock_rows,
            &clock_row_count
        );
    }
    int data_fd = control_fd;
    if (o->data_protocol == PROTO_UDP) {
        data_fd = connect_udp_data(
            control_fd,
            o->repeater_host,
            o->repeater_port,
            o->sock_buf,
            o->busy_poll_us,
            o->detect_timeout,
            o->detect_interval
        );
        struct timeval tv;
        tv.tv_sec = (time_t)o->udp_idle_timeout;
        tv.tv_usec = (suseconds_t)((o->udp_idle_timeout - (double)tv.tv_sec) * 1000000.0);
        if (tv.tv_usec < 0) {
            tv.tv_usec = 0;
        }
        (void)setsockopt(data_fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
        if (o->kernel_timestamp) {
            enable_kernel_timestamp_ns(data_fd);
            kernel_timestamp_enabled = true;
        }
    } else if (o->kernel_timestamp) {
        die_msg("--kernel-timestamp is only supported with --data-protocol udp");
    }

    uint64_t prev_loop_ns = o->diag ? monotonic_ns() : 0;
    if (o->data_protocol == PROTO_UDP) {
        while (true) {
            int64_t loop_gap_ns = 0;
            int64_t recv_block_ns = 0;
            uint64_t ts_recv_ns = 0;
            if (o->diag) {
                uint64_t loop_now = monotonic_ns();
                loop_gap_ns = (int64_t)(loop_now - prev_loop_ns);
                prev_loop_ns = loop_now;
            }
            uint64_t pre_recv = o->diag ? monotonic_ns() : 0;
            ssize_t got;
            if (kernel_timestamp_enabled) {
                struct iovec iov;
                char control[128];
                struct msghdr hdr;
                memset(&iov, 0, sizeof(iov));
                memset(&hdr, 0, sizeof(hdr));
                memset(control, 0, sizeof(control));
                iov.iov_base = inbuf;
                iov.iov_len = (size_t)data_msg_size;
                hdr.msg_iov = &iov;
                hdr.msg_iovlen = 1;
                hdr.msg_control = control;
                hdr.msg_controllen = sizeof(control);
                got = recvmsg(data_fd, &hdr, 0);
                if (got >= 0) {
                    bool found_ts = false;
                    ts_recv_ns = parse_kernel_timestamp_ns(&hdr, &found_ts);
                    if (found_ts) {
                        ++kernel_timestamp_received;
                    } else {
                        ts_recv_ns = time_ns();
                        ++kernel_timestamp_fallback;
                    }
                }
            } else {
                got = recv(data_fd, inbuf, (size_t)data_msg_size, 0);
                if (got >= 0) {
                    ts_recv_ns = time_ns();
                }
            }
            if (got < 0) {
                if (errno == EAGAIN || errno == EWOULDBLOCK) {
                    break;
                }
                if (errno == EINTR) {
                    continue;
                }
                die("recv UDP data");
            }
            if (o->diag) {
                recv_block_ns = (int64_t)(monotonic_ns() - pre_recv);
            }
            if (got != UDP_MSG_SIZE) {
                continue;
            }
            int count_idx = 0;
            Msg msg = unpack_udp_msg(inbuf, &count_idx);
            if (count_idx <= 0 || count_idx > count) {
                continue;
            }
            ++udp_received;
            if (!udp_seen_counts[count_idx]) {
                udp_seen_counts[count_idx] = 1;
                ++udp_seen_total;
            }
            last_raw_msg = msg;
            last_delta = (int64_t)((int64_t)ts_recv_ns + clock_offset_ns - (int64_t)msg.ts_emit_ns);
            if (count_idx > warmup && sample_idx < sample_count) {
                delta_samples[sample_idx] = last_delta;
                werner_raw_samples[sample_idx] = msg.w_swap;
                sample_msgs[sample_idx].delta_ns = last_delta;
                sample_msgs[sample_idx].count_idx = count_idx;
                sample_msgs[sample_idx].msg = msg;
                delta_record_counts[sample_idx] = count_idx;
                if (o->diag) {
                    loop_gap_samples[sample_idx] = loop_gap_ns;
                    recv_block_samples[sample_idx] = recv_block_ns;
                }
                ++sample_idx;
            }
            if (o->send_mode == SEND_MODE_ACK) {
                send_data_ack(data_fd, o->data_protocol);
            }
            if (count_idx >= count) {
                break;
            }
            if (o->count_interval > 0.0) {
                sleep_seconds(o->count_interval);
            }
        }
    } else {
        for (int i = 0; i < count; ++i) {
            int64_t loop_gap_ns = 0;
            int64_t recv_block_ns = 0;
            if (o->diag) {
                uint64_t loop_now = monotonic_ns();
                loop_gap_ns = (int64_t)(loop_now - prev_loop_ns);
                prev_loop_ns = loop_now;
            }
            uint64_t pre_recv = o->diag ? monotonic_ns() : 0;
            if (recv_exact(data_fd, inbuf, (size_t)data_msg_size) != (ssize_t)data_msg_size) {
                die("recv TCP data");
            }
            if (o->diag) {
                recv_block_ns = (int64_t)(monotonic_ns() - pre_recv);
            }
            Msg msg = unpack_msg(inbuf);
            uint64_t ts_recv_ns = time_ns();
            last_raw_msg = msg;
            last_delta = (int64_t)((int64_t)ts_recv_ns + clock_offset_ns - (int64_t)msg.ts_emit_ns);
            if (i >= warmup && sample_idx < sample_count) {
                delta_samples[sample_idx] = last_delta;
                werner_raw_samples[sample_idx] = msg.w_swap;
                sample_msgs[sample_idx].delta_ns = last_delta;
                sample_msgs[sample_idx].count_idx = i + 1;
                sample_msgs[sample_idx].msg = msg;
                delta_record_counts[sample_idx] = i + 1;
                if (o->diag) {
                    loop_gap_samples[sample_idx] = loop_gap_ns;
                    recv_block_samples[sample_idx] = recv_block_ns;
                }
                ++sample_idx;
            }
            if (o->send_mode == SEND_MODE_ACK) {
                send_data_ack(data_fd, o->data_protocol);
            }
            if (o->count_interval > 0.0) {
                sleep_seconds(o->count_interval);
            }
        }
    }

    if (o->data_protocol == PROTO_UDP) {
        udp_lost_est = count - udp_seen_total;
        if (udp_lost_est < 0) {
            udp_lost_est = 0;
        }
    }

    if (o->data_protocol == PROTO_UDP) {
        close(data_fd);
    }
    close(control_fd);

    int64_t delay_center_ns = 0;
    if (o->center_delay && sample_idx > 0) {
        int64_t *sorted = malloc((size_t)sample_idx * sizeof(int64_t));
        if (!sorted) {
            die("malloc sorted");
        }
        memcpy(sorted, delta_samples, (size_t)sample_idx * sizeof(int64_t));
        qsort(sorted, (size_t)sample_idx, sizeof(int64_t), cmp_i64);
        delay_center_ns = percentile_i64(sorted, sample_idx, 0.50);
        free(sorted);
    }
    for (int i = 0; i < sample_idx; ++i) {
        delay_stat_samples[i] = delta_samples[i] - delay_center_ns;
        double werner = decay_werner(werner_raw_samples[i], delay_stat_samples[i] > 0 ? delay_stat_samples[i] : 0, o->t1_ns);
        werner_samples[i] = werner * werner;
        sample_msgs[i].delta_ns = delay_stat_samples[i];
        sample_msgs[i].msg.w_swap = werner_samples[i];
        sample_msgs[i].state_out = (State){o->client_id, werner_samples[i], (int)sample_msgs[i].msg.peer_id, false};
        delay_abs_samples[i] = i64_abs_value(delay_stat_samples[i]);
    }
    int64_t last_stat_delta = last_delta - delay_center_ns;
    double last_werner = decay_werner(last_raw_msg.w_swap, last_stat_delta > 0 ? last_stat_delta : 0, o->t1_ns);
    last_werner *= last_werner;
    Msg last_msg = last_raw_msg;
    last_msg.w_swap = last_werner;
    State last_state_out = {o->client_id, last_werner, (int)last_raw_msg.peer_id, false};

    int64_t *delta_sorted = malloc((size_t)(sample_idx > 0 ? sample_idx : 1) * sizeof(int64_t));
    double *w_sorted = malloc((size_t)(sample_idx > 0 ? sample_idx : 1) * sizeof(double));
    if (!delta_sorted || !w_sorted) {
        die("malloc sort arrays");
    }
    if (sample_idx > 0) {
        memcpy(delta_sorted, delay_abs_samples, (size_t)sample_idx * sizeof(int64_t));
        memcpy(w_sorted, werner_samples, (size_t)sample_idx * sizeof(double));
        qsort(delta_sorted, (size_t)sample_idx, sizeof(int64_t), cmp_i64);
        qsort(w_sorted, (size_t)sample_idx, sizeof(double), cmp_double);
    }
    double mean_delay_raw = 0.0;
    double mean_werner = 0.0;
    for (int i = 0; i < sample_idx; ++i) {
        mean_delay_raw += (double)delay_abs_samples[i];
        mean_werner += werner_samples[i];
    }
    if (sample_idx > 0) {
        mean_delay_raw /= (double)sample_idx;
        mean_werner /= (double)sample_idx;
    }
    int64_t mean_delay = (int64_t)mean_delay_raw;
    double std_delay = stddev_i64(delay_abs_samples, sample_idx, mean_delay_raw);
    double std_werner = stddev_double(werner_samples, sample_idx, mean_werner);
    SampleMsg min_sample = pick_by_delta(sample_msgs, sample_idx, false, o->client_id);
    SampleMsg max_sample = pick_by_delta(sample_msgs, sample_idx, true, o->client_id);
    const char *abs_delay_label = o->center_delay ? "abs_centered_repeater_to_client" : "abs_repeater_to_client";
    const char *signed_delay_label = o->center_delay ? "signed_centered_repeater_to_client" : "signed_repeater_to_client";

    if (o->plot) {
        ensure_dir(o->plot_dir);
        char base[1024];
        snprintf(base, sizeof(base), "%s_%d", o->plot_prefix, o->client_id);
        char csv_path[4096];
        unique_csv_path(csv_path, sizeof(csv_path), o->plot_dir, base, "");
        FILE *f = fopen(csv_path, "w");
        if (!f) {
            die("fopen client csv");
        }
        if (o->diag) {
            fprintf(f, "count_idx,delay_ns,delay_center_ns,delay_centered_ns,clock_offset_ns,clock_sync_path_delay_ns,loop_gap_ns,recv_block_ns\n");
            for (int i = 0; i < sample_idx; ++i) {
                fprintf(
                    f,
                    "%d,%lld,%lld,%lld,%lld,%lld,%lld,%lld\n",
                    delta_record_counts[i],
                    (long long)delta_samples[i],
                    (long long)delay_center_ns,
                    (long long)delay_stat_samples[i],
                    (long long)clock_offset_ns,
                    (long long)clock_sync_path_delay_ns,
                    (long long)loop_gap_samples[i],
                    (long long)recv_block_samples[i]
                );
            }
        } else {
            fprintf(f, "count_idx,delay_ns,delay_center_ns,delay_centered_ns,clock_offset_ns,clock_sync_path_delay_ns\n");
            for (int i = 0; i < sample_idx; ++i) {
                fprintf(
                    f,
                    "%d,%lld,%lld,%lld,%lld,%lld\n",
                    delta_record_counts[i],
                    (long long)delta_samples[i],
                    (long long)delay_center_ns,
                    (long long)delay_stat_samples[i],
                    (long long)clock_offset_ns,
                    (long long)clock_sync_path_delay_ns
                );
            }
        }
        fclose(f);
        chown_to_sudo_user(csv_path);
        printf("plot=data_saved (%s)\n", csv_path);
        if (clock_row_count > 0) {
            const char *suffix = path_suffix(csv_path, base);
            char clock_path[4096];
            snprintf(clock_path, sizeof(clock_path), "%s/clock_sync_client_%d%s.csv", o->plot_dir, o->client_id, suffix);
            FILE *cf = fopen(clock_path, "w");
            if (!cf) {
                die("fopen clock csv");
            }
            fprintf(
                cf,
                "sample_idx,t1_ns,t2_ns,t3_ns,t4_ns,master_to_slave_ns,slave_to_master_ns,offset_ns,path_delay_ns,clock_offset_mean_ns,clock_sync_path_delay_mean_ns\n"
            );
            for (int i = 0; i < clock_row_count; ++i) {
                fprintf(
                    cf,
                    "%d,%llu,%llu,%llu,%llu,%lld,%lld,%lld,%lld,%lld,%lld\n",
                    clock_rows[i].sample_idx,
                    (unsigned long long)clock_rows[i].t1_ns,
                    (unsigned long long)clock_rows[i].t2_ns,
                    (unsigned long long)clock_rows[i].t3_ns,
                    (unsigned long long)clock_rows[i].t4_ns,
                    (long long)clock_rows[i].master_to_slave_ns,
                    (long long)clock_rows[i].slave_to_master_ns,
                    (long long)clock_rows[i].offset_ns,
                    (long long)clock_rows[i].path_delay_ns,
                    (long long)clock_offset_ns,
                    (long long)clock_sync_path_delay_ns
                );
            }
            fclose(cf);
            chown_to_sudo_user(clock_path);
            printf("clock_sync=data_saved (%s)\n", clock_path);
        }
        if (o->json_output) {
            const char *suffix = path_suffix(csv_path, base);
            char json_dir_buf[8192];
            const char *json_dir = o->json_dir;
            if (!json_dir) {
                default_json_dir(json_dir_buf, sizeof(json_dir_buf), o->plot_dir);
                json_dir = json_dir_buf;
            }
            ensure_dir(json_dir);
            size_t json_path_len = strlen(json_dir) + strlen(base) + strlen(suffix) + 8;
            char *json_path = malloc(json_path_len);
            if (!json_path) {
                die("malloc client json path");
            }
            snprintf(json_path, json_path_len, "%s/%s%s.json", json_dir, base, suffix);
            FILE *jf = fopen(json_path, "w");
            if (!jf) {
                die("fopen client json");
            }
            fprintf(jf, "{\n");
            fprintf(jf, "  \"role\": \"client\",\n");
            fprintf(jf, "  \"argv\": ");
            json_print_argv(jf, o);
            fprintf(jf, ",\n");
            fprintf(jf, "  \"args\": {\n");
            fprintf(jf, "    \"client_id\": %d,\n", o->client_id);
            fprintf(jf, "    \"repeater_host\": ");
            json_print_string(jf, o->repeater_host);
            fprintf(jf, ",\n    \"repeater_port\": %d,\n", o->repeater_port);
            fprintf(jf, "    \"count\": %d,\n", count);
            fprintf(jf, "    \"warmup\": %d,\n", warmup);
            fprintf(jf, "    \"data_protocol\": \"%s\",\n", protocol_name(o->data_protocol));
            fprintf(jf, "    \"send_mode\": \"%s\",\n", send_mode_name(o->send_mode));
            fprintf(jf, "    \"kernel_timestamp\": %s,\n", kernel_timestamp_enabled ? "true" : "false");
            fprintf(jf, "    \"sock_buf\": %d,\n", o->sock_buf);
            fprintf(jf, "    \"busy_poll_us\": %d,\n", o->busy_poll_us);
            fprintf(jf, "    \"cpu\": %d,\n", o->cpu);
            fprintf(jf, "    \"rt_priority\": %d\n", o->rt_priority);
            fprintf(jf, "  },\n");
            fprintf(jf, "  \"client_id\": %d,\n", o->client_id);
            fprintf(jf, "  \"repeater_id\": %d,\n", o->repeater_id);
            fprintf(jf, "  \"exchanges\": %d,\n", count);
            fprintf(jf, "  \"warmup\": %d,\n", warmup);
            fprintf(jf, "  \"data_protocol\": \"%s\",\n", protocol_name(o->data_protocol));
            fprintf(jf, "  \"udp_received\": %d,\n", udp_received);
            fprintf(jf, "  \"udp_lost_est\": %d,\n", udp_lost_est);
            fprintf(jf, "  \"clock_offset_ns\": %lld,\n", (long long)clock_offset_ns);
            fprintf(jf, "  \"clock_sync_path_delay_ns\": %lld,\n", (long long)clock_sync_path_delay_ns);
            fprintf(jf, "  \"kernel_timestamp\": %s,\n", kernel_timestamp_enabled ? "true" : "false");
            fprintf(jf, "  \"kernel_timestamp_received\": %d,\n", kernel_timestamp_received);
            fprintf(jf, "  \"kernel_timestamp_fallback\": %d,\n", kernel_timestamp_fallback);
            fprintf(jf, "  \"delay_center_ns\": %lld,\n", (long long)delay_center_ns);
            fprintf(jf, "  \"csv_path\": ");
            json_print_string(jf, csv_path);
            fprintf(jf, ",\n");
            fprintf(jf, "  \"summary\": {\n");
            fprintf(jf, "    \"abs_delay_ns\": {\"p50\": %lld, \"p90\": %lld, \"p95\": %lld, \"p99\": %lld, \"mean\": %lld, \"std\": %.17g},\n",
                    (long long)percentile_i64(delta_sorted, sample_idx, 0.50),
                    (long long)percentile_i64(delta_sorted, sample_idx, 0.90),
                    (long long)percentile_i64(delta_sorted, sample_idx, 0.95),
                    (long long)percentile_i64(delta_sorted, sample_idx, 0.99),
                    (long long)mean_delay,
                    std_delay);
            fprintf(jf, "    \"werner\": {\"p50\": %.17g, \"p90\": %.17g, \"p95\": %.17g, \"p99\": %.17g, \"mean\": %.17g, \"std\": %.17g}\n",
                    percentile_inverse_double(w_sorted, sample_idx, 0.50),
                    percentile_inverse_double(w_sorted, sample_idx, 0.90),
                    percentile_inverse_double(w_sorted, sample_idx, 0.95),
                    percentile_inverse_double(w_sorted, sample_idx, 0.99),
                    mean_werner,
                    std_werner);
            fprintf(jf, "  },\n");
            fprintf(jf, "  \"samples\": [");
            for (int i = 0; i < sample_idx; ++i) {
                fprintf(
                    jf,
                    "%s\n    {\"count_idx\": %d, \"delay_ns\": %lld, \"delay_center_ns\": %lld, \"delay_centered_ns\": %lld, \"clock_offset_ns\": %lld, \"clock_sync_path_delay_ns\": %lld, \"ts_emit_ns\": %llu, \"peer_id\": %u, \"correction_bits\": %u, \"w_swap_raw\": %.17g, \"werner\": %.17g}",
                    i == 0 ? "" : ",",
                    delta_record_counts[i],
                    (long long)delta_samples[i],
                    (long long)delay_center_ns,
                    (long long)delay_stat_samples[i],
                    (long long)clock_offset_ns,
                    (long long)clock_sync_path_delay_ns,
                    (unsigned long long)sample_msgs[i].msg.ts_emit_ns,
                    sample_msgs[i].msg.peer_id,
                    sample_msgs[i].msg.bits,
                    werner_raw_samples[i],
                    werner_samples[i]
                );
            }
            if (sample_idx > 0) {
                fputc('\n', jf);
            }
            fprintf(jf, "  ]\n");
            fprintf(jf, "}\n");
            fclose(jf);
            chown_to_sudo_user(json_path);
            printf("json=data_saved (%s)\n", json_path);
            free(json_path);
        }
    }

    if (o->quiet) {
        printf("exchanges=%d\n", count);
        printf("warmup=%d\n", warmup);
        printf("data_protocol=%s\n", protocol_name(o->data_protocol));
        printf("send_mode=%s\n", send_mode_name(o->send_mode));
        printf("kernel_timestamp=%s\n", kernel_timestamp_enabled ? "True" : "False");
        if (kernel_timestamp_enabled) {
            printf("kernel_timestamp_received=%d\n", kernel_timestamp_received);
            printf("kernel_timestamp_fallback=%d\n", kernel_timestamp_fallback);
        }
        if (o->data_protocol == PROTO_UDP) {
            printf("udp_received=%d\n", udp_received);
            printf("udp_lost_est=%d\n", udp_lost_est);
        }
        printf("clock_offset_ns=%lld\n", (long long)clock_offset_ns);
        printf("clock_sync_path_delay_ns=%lld\n", (long long)clock_sync_path_delay_ns);
        printf("delay_center_ns=%lld\n", (long long)delay_center_ns);
        char statebuf[96];
        State state_in = {o->client_id, o->werner_in, o->repeater_id, false};
        fmt_state(statebuf, sizeof(statebuf), state_in);
        printf("state_in=%s\n", statebuf);
        print_client_group("p50", percentile_i64(delta_sorted, sample_idx, 0.50), percentile_inverse_double(w_sorted, sample_idx, 0.50), abs_delay_label);
        print_client_group("p90", percentile_i64(delta_sorted, sample_idx, 0.90), percentile_inverse_double(w_sorted, sample_idx, 0.90), abs_delay_label);
        print_client_group("p95", percentile_i64(delta_sorted, sample_idx, 0.95), percentile_inverse_double(w_sorted, sample_idx, 0.95), abs_delay_label);
        print_client_group("p99", percentile_i64(delta_sorted, sample_idx, 0.99), percentile_inverse_double(w_sorted, sample_idx, 0.99), abs_delay_label);
        print_client_group("mean", mean_delay, mean_werner, abs_delay_label);
        print_client_group("std", (int64_t)std_delay, std_werner, abs_delay_label);
        print_client_message_state("min", min_sample.delta_ns, min_sample.msg, min_sample.state_out, min_sample.count_idx, true, signed_delay_label);
        print_client_message_state("max", max_sample.delta_ns, max_sample.msg, max_sample.state_out, max_sample.count_idx, true, signed_delay_label);
        print_client_message_state("last", last_stat_delta, last_msg, last_state_out, 0, false, signed_delay_label);
    } else {
        printf("client_mode=fast3\n");
        printf("exchanges=%d warmup=%d\n", count, warmup);
        printf("client_id=%d repeater_id=%d\n", o->client_id, o->repeater_id);
        printf("data_protocol=%s\n", protocol_name(o->data_protocol));
        printf("send_mode=%s\n", send_mode_name(o->send_mode));
        printf("kernel_timestamp=%s\n", kernel_timestamp_enabled ? "True" : "False");
        if (kernel_timestamp_enabled) {
            printf("kernel_timestamp_received=%d\n", kernel_timestamp_received);
            printf("kernel_timestamp_fallback=%d\n", kernel_timestamp_fallback);
        }
        if (o->data_protocol == PROTO_UDP) {
            printf("udp_received=%d\n", udp_received);
            printf("udp_lost_est=%d\n", udp_lost_est);
        }
        printf("clock_offset_ns=%lld\n", (long long)clock_offset_ns);
        printf("clock_sync_path_delay_ns=%lld\n", (long long)clock_sync_path_delay_ns);
        printf("delay_center_ns=%lld\n", (long long)delay_center_ns);
        char statebuf[96];
        State state_in = {o->client_id, o->werner_in, o->repeater_id, false};
        fmt_state(statebuf, sizeof(statebuf), state_in);
        printf("state_in=%s\n", statebuf);
        print_client_group("p50", percentile_i64(delta_sorted, sample_idx, 0.50), percentile_inverse_double(w_sorted, sample_idx, 0.50), abs_delay_label);
        print_client_group("p95", percentile_i64(delta_sorted, sample_idx, 0.95), percentile_inverse_double(w_sorted, sample_idx, 0.95), abs_delay_label);
        print_client_group("p90", percentile_i64(delta_sorted, sample_idx, 0.90), percentile_inverse_double(w_sorted, sample_idx, 0.90), abs_delay_label);
        print_client_group("p99", percentile_i64(delta_sorted, sample_idx, 0.99), percentile_inverse_double(w_sorted, sample_idx, 0.99), abs_delay_label);
        print_client_group("mean", mean_delay, mean_werner, abs_delay_label);
        print_client_group("std", (int64_t)std_delay, std_werner, abs_delay_label);
        print_client_message_state("min", min_sample.delta_ns, min_sample.msg, min_sample.state_out, min_sample.count_idx, true, signed_delay_label);
        print_client_message_state("max", max_sample.delta_ns, max_sample.msg, max_sample.state_out, max_sample.count_idx, true, signed_delay_label);
        print_client_message_state("last", last_stat_delta, last_msg, last_state_out, 0, false, signed_delay_label);
    }

    free(delta_samples);
    free(delay_stat_samples);
    free(delay_abs_samples);
    free(werner_samples);
    free(werner_raw_samples);
    free(sample_msgs);
    free(delta_record_counts);
    free(loop_gap_samples);
    free(recv_block_samples);
    free(udp_seen_counts);
    free(inbuf);
    free(clock_rows);
    free(delta_sorted);
    free(w_sorted);
    return 0;
}

int main(int argc, char **argv) {
    signal(SIGPIPE, SIG_IGN);
    Options options;
    parse_args(argc, argv, &options);
    if (options.role == ROLE_REPEATER) {
        return run_repeater(&options);
    }
    if (options.role == ROLE_CLIENT) {
        return run_client(&options);
    }
    die_msg("Unknown role");
    return 2;
}
