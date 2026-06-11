#!/usr/bin/env python3
import argparse
import gc
import json
import math
import os
import random
import select
import socket
import struct
import sys
import time

UDP_HELLO_MAGIC = b"DEOSUDP1"
UDP_READY_BYTE = b"U"
R2_READY_BYTE = b"R"

CLOCK_SYNC_UDP_MAGIC = b"DEOSCS1!"
CLOCK_SYNC_UDP_HELLO = 1
CLOCK_SYNC_UDP_SYNC = 2
CLOCK_SYNC_UDP_DELAY_REQ = 3
CLOCK_SYNC_UDP_DELAY_RESP = 4
CLOCK_SYNC_UDP_HELLO_FORMAT = "!8sB"
CLOCK_SYNC_UDP_HELLO_SIZE = struct.calcsize(CLOCK_SYNC_UDP_HELLO_FORMAT)
CLOCK_SYNC_UDP_SYNC_FORMAT = "!8sBIQ"
CLOCK_SYNC_UDP_SYNC_SIZE = struct.calcsize(CLOCK_SYNC_UDP_SYNC_FORMAT)
CLOCK_SYNC_UDP_DELAY_REQ_FORMAT = "!8sBIQQQ"
CLOCK_SYNC_UDP_DELAY_REQ_SIZE = struct.calcsize(CLOCK_SYNC_UDP_DELAY_REQ_FORMAT)
CLOCK_SYNC_UDP_DELAY_RESP_FORMAT = "!8sBIQQQQB"
CLOCK_SYNC_UDP_DELAY_RESP_SIZE = struct.calcsize(CLOCK_SYNC_UDP_DELAY_RESP_FORMAT)

SO_TIMESTAMPNS_CANDIDATES = tuple(
    value for value in (getattr(socket, "SO_TIMESTAMPNS", None), 64, 35) if value is not None
)
SCM_TIMESTAMPNS_CANDIDATES = tuple(
    value for value in (getattr(socket, "SCM_TIMESTAMPNS", None), 64, 35) if value is not None
)


def default_json_dir(plot_dir):
    clean_dir = plot_dir.rstrip(os.sep)
    parent, base = os.path.split(clean_dir)
    if not base:
        return "json_deos"
    if base.startswith("csv"):
        json_base = "json" + base[3:]
    elif base.startswith("plots"):
        json_base = "json" + base[5:]
    else:
        json_base = base + "_json"
    return os.path.join(parent, json_base) if parent else json_base


def sudo_output_owner():
    uid = os.environ.get("SUDO_UID")
    gid = os.environ.get("SUDO_GID")
    if uid is None or gid is None:
        return None
    try:
        return int(uid), int(gid)
    except ValueError:
        return None


def chown_output_path(path):
    owner = sudo_output_owner()
    if owner is None:
        return
    try:
        os.chown(path, owner[0], owner[1])
    except OSError:
        pass


def chown_output_ancestors(path):
    owner = sudo_output_owner()
    if owner is None:
        return
    abs_path = os.path.abspath(path)
    roots = [os.path.abspath(os.getcwd()), os.path.abspath("/tmp")]
    for root in roots:
        if abs_path == root or not abs_path.startswith(root + os.sep):
            continue
        rel_path = os.path.relpath(abs_path, root)
        current = root
        for part in rel_path.split(os.sep):
            current = os.path.join(current, part)
            chown_output_path(current)
        break


def ensure_output_dir(directory):
    os.makedirs(directory, exist_ok=True)
    chown_output_ancestors(directory)


def enable_low_latency_socket(sock, sock_buf=0, busy_poll_us=0):
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass
    if hasattr(socket, "TCP_QUICKACK"):
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_QUICKACK, 1)
        except OSError:
            pass
    if sock_buf > 0:
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, int(sock_buf))
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, int(sock_buf))
        except OSError:
            pass
    if busy_poll_us > 0 and hasattr(socket, "SO_BUSY_POLL"):
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BUSY_POLL, int(busy_poll_us))
        except OSError:
            pass


def enable_kernel_timestamp_ns(sock):
    last_error = None
    for opt in SO_TIMESTAMPNS_CANDIDATES:
        try:
            sock.setsockopt(socket.SOL_SOCKET, int(opt), 1)
            return int(opt)
        except OSError as exc:
            last_error = exc
    raise OSError("SO_TIMESTAMPNS is not available") from last_error


def parse_kernel_timestamp_ns(ancdata):
    for level, cmsg_type, data in ancdata:
        if level != socket.SOL_SOCKET or cmsg_type not in SCM_TIMESTAMPNS_CANDIDATES:
            continue
        if len(data) >= 16:
            sec, nsec = struct.unpack_from("@qq", data)
            return int(sec) * 1_000_000_000 + int(nsec)
        if len(data) >= 8:
            sec, nsec = struct.unpack_from("@ll", data)
            return int(sec) * 1_000_000_000 + int(nsec)
    return None


def recvfrom_timestamped(sock, size, kernel_timestamp=False):
    if kernel_timestamp:
        cmsg_buf_size = socket.CMSG_SPACE(16) if hasattr(socket, "CMSG_SPACE") else 128
        data, ancdata, _flags, addr = sock.recvmsg(size, cmsg_buf_size)
        ts_ns = parse_kernel_timestamp_ns(ancdata)
        if ts_ns is None:
            return data, addr, time.time_ns(), False
        return data, addr, ts_ns, True
    data, addr = sock.recvfrom(size)
    return data, addr, time.time_ns(), False


def pace_wait(interval_ns, mode="sleep", spin_margin_ns=100_000):
    if interval_ns <= 0:
        return
    if mode == "sleep":
        time.sleep(interval_ns / 1_000_000_000)
        return
    deadline_ns = time.monotonic_ns() + interval_ns
    if mode == "hybrid":
        while True:
            remaining_ns = deadline_ns - time.monotonic_ns()
            if remaining_ns <= 0:
                return
            if remaining_ns > spin_margin_ns:
                time.sleep((remaining_ns - spin_margin_ns) / 1_000_000_000)
            else:
                break
    while time.monotonic_ns() < deadline_ns:
        pass


def apply_cpu_rt(cpu=None, rt_priority=None):
    if cpu is not None:
        os.sched_setaffinity(0, {int(cpu)})
    if rt_priority is not None and int(rt_priority) > 0:
        param = os.sched_param(int(rt_priority))
        os.sched_setscheduler(0, os.SCHED_FIFO, param)


def percentile(sorted_vals, p):
    if not sorted_vals:
        return 0
    idx = int((len(sorted_vals) - 1) * p)
    return sorted_vals[idx]


def percentile_inverse(sorted_vals, p):
    if not sorted_vals:
        return 0
    idx = int((len(sorted_vals) - 1) * (1.0 - p))
    return sorted_vals[idx]


def stddev(vals, mean_value):
    if not vals:
        return 0.0
    return math.sqrt(sum((v - mean_value) ** 2 for v in vals) / len(vals))


def ns_summary(vals):
    if not vals:
        return {"p50": 0, "p90": 0, "p95": 0, "p99": 0, "mean": 0, "std": 0.0, "min": 0, "max": 0}
    ordered = sorted(vals)
    mean_value = sum(vals) / len(vals)
    return {
        "p50": int(percentile(ordered, 0.50)),
        "p90": int(percentile(ordered, 0.90)),
        "p95": int(percentile(ordered, 0.95)),
        "p99": int(percentile(ordered, 0.99)),
        "mean": int(mean_value),
        "std": float(stddev(vals, mean_value)),
        "min": int(min(vals)),
        "max": int(max(vals)),
    }


def median_int(vals):
    if not vals:
        return 0
    return int(percentile(sorted(vals), 0.50))


def mad_ns(vals, median_value):
    if not vals:
        return 0
    return median_int([abs(v - median_value) for v in vals])


def empty_clock_sync_stats(method="none", warmup=0, best_ratio=0.0):
    return {
        "clock_sync_protocol": "udp",
        "clock_sync_kernel_timestamp": False,
        "clock_sync_kernel_timestamp_received": 0,
        "clock_sync_kernel_timestamp_fallback": 0,
        "clock_sync_t2_kernel_timestamp_received": 0,
        "clock_sync_t2_kernel_timestamp_fallback": 0,
        "clock_sync_t4_kernel_timestamp_received": 0,
        "clock_sync_t4_kernel_timestamp_fallback": 0,
        "clock_sync_method": method,
        "clock_sync_warmup": int(warmup),
        "clock_sync_best_ratio": float(best_ratio),
        "clock_sync_used_samples": 0,
        "clock_sync_best_path_samples": 0,
        "clock_offset_mean_ns": 0,
        "clock_offset_median_ns": 0,
        "clock_offset_best_path_median_ns": 0,
        "clock_offset_std_ns": 0.0,
        "clock_offset_mad_ns": 0,
        "clock_sync_path_delay_mean_ns": 0,
        "clock_sync_path_delay_min_ns": 0,
        "clock_sync_path_delay_median_ns": 0,
        "clock_sync_path_delay_p95_ns": 0,
        "clock_sync_path_delay_best_median_ns": 0,
    }


def build_clock_sync_stats(rows, method, warmup, best_ratio):
    used_rows = [row for row in rows if row["used_for_offset"]]
    stats = empty_clock_sync_stats(method, warmup, best_ratio)
    if not used_rows:
        return 0, 0, stats
    best_ratio = min(1.0, max(0.0, float(best_ratio)))
    best_count = max(1, int(math.ceil(len(used_rows) * best_ratio))) if best_ratio > 0 else 1
    best_rows = sorted(used_rows, key=lambda row: row["path_delay_ns"])[:best_count]
    best_ids = {row["sample_idx"] for row in best_rows}
    for row in rows:
        row["used_for_best_path"] = row["sample_idx"] in best_ids

    offsets = [row["offset_ns"] for row in used_rows]
    paths = [row["path_delay_ns"] for row in used_rows]
    best_offsets = [row["offset_ns"] for row in best_rows]
    best_paths = [row["path_delay_ns"] for row in best_rows]
    offset_mean_raw = sum(offsets) / len(offsets)
    path_mean_raw = sum(paths) / len(paths)
    offset_mean = int(offset_mean_raw)
    offset_median = median_int(offsets)
    best_offset_median = median_int(best_offsets)
    path_median = median_int(paths)
    best_path_median = median_int(best_paths)
    stats.update(
        {
            "clock_sync_best_ratio": float(best_ratio),
            "clock_sync_used_samples": int(len(used_rows)),
            "clock_sync_best_path_samples": int(len(best_rows)),
            "clock_offset_mean_ns": int(offset_mean),
            "clock_offset_median_ns": int(offset_median),
            "clock_offset_best_path_median_ns": int(best_offset_median),
            "clock_offset_std_ns": float(stddev(offsets, offset_mean_raw)),
            "clock_offset_mad_ns": int(mad_ns(offsets, offset_median)),
            "clock_sync_path_delay_mean_ns": int(path_mean_raw),
            "clock_sync_path_delay_min_ns": int(min(paths)),
            "clock_sync_path_delay_median_ns": int(path_median),
            "clock_sync_path_delay_p95_ns": int(percentile(sorted(paths), 0.95)),
            "clock_sync_path_delay_best_median_ns": int(best_path_median),
        }
    )
    if method == "mean":
        return offset_mean, int(path_mean_raw), stats
    if method == "median":
        return offset_median, path_median, stats
    return best_offset_median, best_path_median, stats


def effective_clock_sync_warmup(samples, warmup):
    sample_total = max(0, int(samples))
    if sample_total <= 0:
        return 0
    discard = int(sample_total * 0.05) if warmup is None else max(0, int(warmup))
    return min(discard, sample_total - 1)


def serve_clock_sync_udp(udp_sock, samples, kernel_timestamp=False):
    if kernel_timestamp:
        enable_kernel_timestamp_ns(udp_sock)
    hello_size = CLOCK_SYNC_UDP_HELLO_SIZE
    max_req_size = max(CLOCK_SYNC_UDP_HELLO_SIZE, CLOCK_SYNC_UDP_DELAY_REQ_SIZE)
    while True:
        data, addr, _rx_ns, _got_kernel = recvfrom_timestamped(udp_sock, max_req_size, False)
        if len(data) != hello_size:
            continue
        magic, msg_type = struct.unpack(CLOCK_SYNC_UDP_HELLO_FORMAT, data)
        if magic == CLOCK_SYNC_UDP_MAGIC and msg_type == CLOCK_SYNC_UDP_HELLO:
            break
    for sample_number in range(1, max(0, int(samples)) + 1):
        t1_ns = time.time_ns()
        udp_sock.sendto(
            struct.pack(CLOCK_SYNC_UDP_SYNC_FORMAT, CLOCK_SYNC_UDP_MAGIC, CLOCK_SYNC_UDP_SYNC, sample_number, t1_ns),
            addr,
        )
        while True:
            data, req_addr, t4_ns, t4_got_kernel = recvfrom_timestamped(
                udp_sock, CLOCK_SYNC_UDP_DELAY_REQ_SIZE, kernel_timestamp
            )
            if req_addr != addr or len(data) != CLOCK_SYNC_UDP_DELAY_REQ_SIZE:
                continue
            magic, msg_type, echoed_sample, t1_echo_ns, t2_ns, t3_ns = struct.unpack(
                CLOCK_SYNC_UDP_DELAY_REQ_FORMAT, data
            )
            if magic != CLOCK_SYNC_UDP_MAGIC or msg_type != CLOCK_SYNC_UDP_DELAY_REQ:
                continue
            if echoed_sample != sample_number or t1_echo_ns != t1_ns:
                raise ValueError("UDP PTP clock-sync request did not match the Sync timestamp")
            break
        udp_sock.sendto(
            struct.pack(
                CLOCK_SYNC_UDP_DELAY_RESP_FORMAT,
                CLOCK_SYNC_UDP_MAGIC,
                CLOCK_SYNC_UDP_DELAY_RESP,
                sample_number,
                t1_ns,
                t2_ns,
                t3_ns,
                t4_ns,
                1 if t4_got_kernel else 0,
            ),
            addr,
        )


def estimate_clock_offset_udp(
    host,
    port,
    samples,
    warmup=0,
    method="best-path-median",
    best_ratio=0.5,
    sock_buf=0,
    busy_poll_us=0,
    detect_timeout=30.0,
    detect_interval=0.05,
    kernel_timestamp=False,
):
    sample_total = max(0, int(samples))
    warmup = min(max(0, int(warmup)), max(0, sample_total - 1))
    clock_sync_samples = []
    t2_kernel_received = 0
    t2_kernel_fallback = 0
    t4_kernel_received = 0
    t4_kernel_fallback = 0
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    enable_low_latency_socket(sock, sock_buf, busy_poll_us)
    if kernel_timestamp:
        enable_kernel_timestamp_ns(sock)
    sock.bind(("", 0))
    server_addr = (host, int(port))
    timeout = max(0.001, min(0.05, max(0.001, float(detect_interval))))
    sock.settimeout(timeout)
    deadline = time.monotonic() + max(0.0, float(detect_timeout))
    try:
        sample_number = 1
        waiting_for_first_sync = True
        while sample_number <= sample_total:
            if waiting_for_first_sync:
                sock.sendto(
                    struct.pack(CLOCK_SYNC_UDP_HELLO_FORMAT, CLOCK_SYNC_UDP_MAGIC, CLOCK_SYNC_UDP_HELLO),
                    server_addr,
                )
            while True:
                if time.monotonic() >= deadline:
                    raise TimeoutError("UDP clock sync did not complete before detect-timeout expired")
                try:
                    data, addr, t2_ns, got_kernel = recvfrom_timestamped(
                        sock, CLOCK_SYNC_UDP_SYNC_SIZE, kernel_timestamp
                    )
                except socket.timeout:
                    if waiting_for_first_sync:
                        break
                    continue
                if addr != server_addr or len(data) != CLOCK_SYNC_UDP_SYNC_SIZE:
                    continue
                magic, msg_type, echoed_sample, t1_ns = struct.unpack(CLOCK_SYNC_UDP_SYNC_FORMAT, data)
                if magic != CLOCK_SYNC_UDP_MAGIC or msg_type != CLOCK_SYNC_UDP_SYNC:
                    continue
                if echoed_sample != sample_number:
                    continue
                if kernel_timestamp:
                    if got_kernel:
                        t2_kernel_received += 1
                    else:
                        t2_kernel_fallback += 1
                waiting_for_first_sync = False
                break
            else:
                continue
            if waiting_for_first_sync:
                continue
            t3_ns = time.time_ns()
            sock.sendto(
                struct.pack(
                    CLOCK_SYNC_UDP_DELAY_REQ_FORMAT,
                    CLOCK_SYNC_UDP_MAGIC,
                    CLOCK_SYNC_UDP_DELAY_REQ,
                    sample_number,
                    t1_ns,
                    t2_ns,
                    t3_ns,
                ),
                server_addr,
            )
            while True:
                if time.monotonic() >= deadline:
                    raise TimeoutError("UDP clock sync response was not received before detect-timeout expired")
                try:
                    data, addr, _rx_ns, _got_kernel = recvfrom_timestamped(
                        sock, CLOCK_SYNC_UDP_DELAY_RESP_SIZE, False
                    )
                except socket.timeout:
                    continue
                if addr != server_addr or len(data) != CLOCK_SYNC_UDP_DELAY_RESP_SIZE:
                    continue
                magic, msg_type, echoed_sample, echoed_t1_ns, echoed_t2_ns, echoed_t3_ns, t4_ns, t4_got_kernel = (
                    struct.unpack(CLOCK_SYNC_UDP_DELAY_RESP_FORMAT, data)
                )
                if magic != CLOCK_SYNC_UDP_MAGIC or msg_type != CLOCK_SYNC_UDP_DELAY_RESP:
                    continue
                if (echoed_sample, echoed_t1_ns, echoed_t2_ns, echoed_t3_ns) != (
                    sample_number,
                    t1_ns,
                    t2_ns,
                    t3_ns,
                ):
                    raise ValueError("UDP PTP clock-sync response did not match the Delay_Req timestamps")
                break
            master_to_slave_ns = t2_ns - t1_ns
            slave_to_master_ns = t4_ns - t3_ns
            mean_path_delay_ns = (master_to_slave_ns + slave_to_master_ns) // 2
            offset_ns = (slave_to_master_ns - master_to_slave_ns) // 2
            t4_got_kernel = bool(t4_got_kernel)
            if kernel_timestamp:
                if t4_got_kernel:
                    t4_kernel_received += 1
                else:
                    t4_kernel_fallback += 1
            clock_sync_samples.append(
                {
                    "sample_idx": sample_number,
                    "t1_ns": t1_ns,
                    "t2_ns": t2_ns,
                    "t3_ns": t3_ns,
                    "t4_ns": t4_ns,
                    "master_to_slave_ns": master_to_slave_ns,
                    "slave_to_master_ns": slave_to_master_ns,
                    "offset_ns": offset_ns,
                    "path_delay_ns": mean_path_delay_ns,
                    "used_for_offset": sample_number > warmup,
                    "used_for_best_path": False,
                    "t2_kernel_timestamp": bool(got_kernel) if kernel_timestamp else False,
                    "t4_kernel_timestamp": t4_got_kernel if kernel_timestamp else False,
                }
            )
            sample_number += 1
    finally:
        sock.close()
    clock_offset_ns, clock_sync_path_delay_ns, stats = build_clock_sync_stats(
        clock_sync_samples, method, warmup, best_ratio
    )
    stats.update(
        {
            "clock_sync_protocol": "udp",
            "clock_sync_kernel_timestamp": bool(kernel_timestamp),
            "clock_sync_kernel_timestamp_received": int(t2_kernel_received + t4_kernel_received),
            "clock_sync_kernel_timestamp_fallback": int(t2_kernel_fallback + t4_kernel_fallback),
            "clock_sync_t2_kernel_timestamp_received": int(t2_kernel_received),
            "clock_sync_t2_kernel_timestamp_fallback": int(t2_kernel_fallback),
            "clock_sync_t4_kernel_timestamp_received": int(t4_kernel_received),
            "clock_sync_t4_kernel_timestamp_fallback": int(t4_kernel_fallback),
        }
    )
    return clock_offset_ns, clock_sync_path_delay_ns, clock_sync_samples, stats


def connect_repeater_until_ready(host, port, connect_timeout, detect_timeout, detect_interval, sock_buf, busy_poll_us):
    deadline = time.monotonic() + max(0.0, detect_timeout)
    last_error = None
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        timeout = min(max(0.001, float(connect_timeout)), max(0.001, remaining))
        try:
            sock = socket.create_connection((host, port), timeout=timeout)
            enable_low_latency_socket(sock, sock_buf, busy_poll_us)
            return sock
        except OSError as exc:
            last_error = exc
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(max(0.0, detect_interval), remaining))
    raise TimeoutError("Repeater was not detected before detect-timeout expired") from last_error


def udp_bind_socket(host, port, sock_buf, busy_poll_us, timeout=None):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    enable_low_latency_socket(sock, sock_buf, busy_poll_us)
    if timeout is not None:
        sock.settimeout(float(timeout))
    sock.bind((host, port))
    return sock


def accept_udp_peer(control_sock, udp_sock):
    try:
        while True:
            data, addr = udp_sock.recvfrom(64)
            if data == UDP_HELLO_MAGIC:
                udp_sock.connect(addr)
                control_sock.sendall(UDP_READY_BYTE)
                return udp_sock
    except Exception:
        udp_sock.close()
        raise


def connect_udp_data(control_sock, host, port, sock_buf, busy_poll_us, detect_timeout, detect_interval):
    udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    enable_low_latency_socket(udp_sock, sock_buf, busy_poll_us)
    udp_sock.bind(("", 0))
    udp_sock.connect((host, port))
    deadline = time.monotonic() + max(0.0, detect_timeout)
    old_timeout = control_sock.gettimeout()
    try:
        control_sock.settimeout(max(0.001, min(0.05, max(0.001, detect_interval))))
        while time.monotonic() < deadline:
            try:
                udp_sock.send(UDP_HELLO_MAGIC)
            except ConnectionRefusedError:
                time.sleep(min(max(0.0, detect_interval), 0.01))
                continue
            try:
                ready = control_sock.recv(1)
                if ready == UDP_READY_BYTE:
                    return udp_sock
                if ready == b"":
                    raise ConnectionError("TCP control socket closed before UDP setup")
            except socket.timeout:
                pass
            time.sleep(min(max(0.0, detect_interval), 0.01))
    except Exception:
        udp_sock.close()
        raise
    finally:
        control_sock.settimeout(old_timeout)
    udp_sock.close()
    raise TimeoutError("Repeater did not confirm UDP setup before detect-timeout expired")


def decay_werner(base, age_ns, t1_ns):
    if t1_ns <= 0:
        raise ValueError("t1_ns must be positive")
    if age_ns < 0:
        age_ns = 0
    return float(base) * math.exp(-age_ns / float(t1_ns))


def fmt_ns(v):
    return f"{int(v)} ({int(v) / 1e9:.9f} s)"


def fmt_ts_emit(ts_ns):
    total_s = int(ts_ns // 1_000_000_000)
    ns_part = int(ts_ns % 1_000_000_000)
    tm = time.gmtime(total_s)
    return f"{tm.tm_min:02d}:{tm.tm_sec:02d}.{ns_part:09d}"

DEOS_MSG_FORMAT = "!IBIIQQQQBddd"
DEOS_MSG = struct.Struct(DEOS_MSG_FORMAT)
DEOS_MSG_SIZE = DEOS_MSG.size

STAGE_R1_SWAP = 1
STAGE_R2_SWAP = 2


def add_common(parser):
    parser.add_argument("--count", type=int, default=2000)
    parser.add_argument("--cpu", type=int, default=None)
    parser.add_argument("--rt-priority", type=int, default=50)
    parser.add_argument("--sock-buf", type=int, default=65536)
    parser.add_argument("--busy-poll-us", type=int, default=50)
    parser.add_argument("--connect-timeout", type=float, default=10.0)
    parser.add_argument("--detect-timeout", type=float, default=120.0)
    parser.add_argument("--detect-interval", type=float, default=0.02)
    parser.add_argument("--accept-timeout", type=float, default=120.0)
    parser.add_argument("--udp-ready-timeout", type=float, default=120.0)
    parser.add_argument("--udp-idle-timeout", type=float, default=5.0)
    parser.add_argument(
        "--clock-sync",
        action="store_true",
        help="Enable UDP PTP-style clock synchronization before data exchange. Disabled by default.",
    )
    parser.add_argument("--clock-sync-samples", type=int, default=264)
    parser.add_argument("--clock-sync-warmup", type=int, default=None)
    parser.add_argument(
        "--clock-sync-method",
        choices=("mean", "median", "best-path-median"),
        default="best-path-median",
    )
    parser.add_argument("--clock-sync-best-ratio", type=float, default=0.5)
    parser.add_argument(
        "--clock-sync-kernel-timestamp",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Use SO_TIMESTAMPNS RX timestamps for UDP PTP sync.",
    )
    parser.add_argument(
        "--kernel-timestamp",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Use SO_TIMESTAMPNS RX timestamps for UDP data.",
    )
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--plot", action="store_true")
    parser.add_argument("--plot-dir", default="csv_deos")
    parser.add_argument(
        "--json",
        dest="json_output",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument("--json-dir", default=None)
    parser.add_argument("--diag", action="store_true")


def add_ids(parser):
    parser.add_argument("--alice-id", type=int, default=1)
    parser.add_argument("--r1-id", type=int, default=2)
    parser.add_argument("--r2-id", type=int, default=3)
    parser.add_argument("--bob-id", type=int, default=4)


def parse_args():
    parser = argparse.ArgumentParser(
        description="DEOS: two sequential entanglement-swapping notifications over optimized UDP."
    )
    subparsers = parser.add_subparsers(dest="role", required=True)

    r1 = subparsers.add_parser("r1", help="Run repeater R1: swaps A-R1 with R1-R2.")
    add_common(r1)
    add_ids(r1)
    r1.add_argument("--listen-host-a", default="0.0.0.0")
    r1.add_argument("--listen-port-a", type=int, default=7601)
    r1.add_argument("--listen-host-r2", default="0.0.0.0")
    r1.add_argument("--listen-port-r2", type=int, default=7602)
    r1.add_argument("--listen-host-bob-sync", default="0.0.0.0")
    r1.add_argument("--listen-port-bob-sync", type=int, default=7603)
    r1.add_argument("--w12", type=float, default=1.0)
    r1.add_argument("--w34", type=float, default=1.0)
    r1.add_argument("--count-interval", type=float, default=0.00005)
    r1.add_argument("--pace-mode", choices=("sleep", "spin", "hybrid"), default="spin")
    r1.add_argument("--spin-margin-us", type=float, default=100.0)
    r1.add_argument("--plot-prefix", default="deos_r1_send_hist")

    r2 = subparsers.add_parser("r2", help="Run repeater R2: receives R1 result and swaps A-R2 with R2-B.")
    add_common(r2)
    add_ids(r2)
    r2.add_argument("--r1-host", default="127.0.0.1")
    r2.add_argument("--r1-port", type=int, default=7602)
    r2.add_argument("--listen-host-a", default="0.0.0.0")
    r2.add_argument("--listen-port-a", type=int, default=7611)
    r2.add_argument("--listen-host-b", default="0.0.0.0")
    r2.add_argument("--listen-port-b", type=int, default=7612)
    r2.add_argument("--w56", type=float, default=1.0)
    r2.add_argument("--plot-prefix", default="deos_r2_hist")

    alice = subparsers.add_parser("alice", help="Run Alice client: receives R1 intermediate and R2 final messages.")
    add_common(alice)
    add_ids(alice)
    alice.add_argument("--r1-host", default="127.0.0.1")
    alice.add_argument("--r1-port", type=int, default=7601)
    alice.add_argument("--r2-host", default="127.0.0.1")
    alice.add_argument("--r2-port", type=int, default=7611)
    alice.add_argument("--warmup", type=int, default=50)
    alice.add_argument("--t1-ns", type=float, default=1_000_000.0)
    alice.add_argument("--plot-prefix", default="deos_alice_hist")

    bob = subparsers.add_parser("bob", help="Run Bob client: receives R2 final messages and syncs to R1 for e2e.")
    add_common(bob)
    add_ids(bob)
    bob.add_argument("--r1-sync-host", default="127.0.0.1")
    bob.add_argument("--r1-sync-port", type=int, default=7603)
    bob.add_argument("--r2-host", default="127.0.0.1")
    bob.add_argument("--r2-port", type=int, default=7612)
    bob.add_argument("--warmup", type=int, default=50)
    bob.add_argument("--t1-ns", type=float, default=1_000_000.0)
    bob.add_argument("--plot-prefix", default="deos_bob_hist")

    return parser.parse_args()


def unique_output_path(directory, base, ext=".csv"):
    ensure_output_dir(directory)
    suffix = ""
    idx = 1
    while os.path.exists(os.path.join(directory, f"{base}{suffix}{ext}")):
        idx += 1
        suffix = f"_{idx}"
    return os.path.join(directory, f"{base}{suffix}{ext}"), suffix


def output_path_with_suffix(directory, base, suffix, ext=".csv"):
    ensure_output_dir(directory)
    return os.path.join(directory, f"{base}{suffix}{ext}")


def role_plot_dir(args, role_name):
    return os.path.join(args.plot_dir, role_name)


def role_json_dir(args, role_name):
    root = args.json_dir or default_json_dir(args.plot_dir)
    return os.path.join(root, role_name)


def json_path_for(args, base, suffix, role_name=None):
    directory = role_json_dir(args, role_name) if role_name else (args.json_dir or default_json_dir(args.plot_dir))
    ensure_output_dir(directory)
    return os.path.join(directory, f"{base}{suffix}.json")


def write_json(args, base, suffix, payload, role_name=None):
    if not args.json_output or not args.plot:
        return None
    path = json_path_for(args, base, suffix, role_name)
    payload = {
        **payload,
        "argv": sys.argv,
        "args": vars(args),
        "created_at_unix_ns": time.time_ns(),
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")
    chown_output_path(path)
    print(f"json=data_saved ({path})")
    return path


def write_clock_sync_csv(
    args,
    base,
    suffix,
    link_name,
    rows,
    stats,
    final_offset_ns,
    final_path_delay_ns,
    role_name=None,
):
    if not args.plot or not rows:
        return None
    directory = role_plot_dir(args, role_name) if role_name else args.plot_dir
    ensure_output_dir(directory)
    path = os.path.join(directory, f"clock_sync_{base}_{link_name}{suffix}.csv")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(
            "sample_idx,t1_ns,t2_ns,t3_ns,t4_ns,"
            "master_to_slave_ns,slave_to_master_ns,offset_ns,path_delay_ns,"
            "t2_kernel_timestamp,t4_kernel_timestamp,used_for_offset,used_for_best_path,"
            "clock_sync_method,clock_sync_protocol,clock_sync_kernel_timestamp,"
            "clock_sync_kernel_timestamp_received,clock_sync_kernel_timestamp_fallback,"
            "clock_sync_t2_kernel_timestamp_received,clock_sync_t2_kernel_timestamp_fallback,"
            "clock_sync_t4_kernel_timestamp_received,clock_sync_t4_kernel_timestamp_fallback,"
            "clock_offset_final_ns,clock_offset_mean_ns,clock_offset_median_ns,"
            "clock_offset_best_path_median_ns,clock_offset_std_ns,clock_offset_mad_ns,"
            "clock_sync_path_delay_final_ns,clock_sync_path_delay_mean_ns,"
            "clock_sync_path_delay_min_ns,clock_sync_path_delay_median_ns,"
            "clock_sync_path_delay_p95_ns,clock_sync_path_delay_best_median_ns\n"
        )
        for row in rows:
            handle.write(
                f"{row['sample_idx']},{row['t1_ns']},{row['t2_ns']},{row['t3_ns']},{row['t4_ns']},"
                f"{row['master_to_slave_ns']},{row['slave_to_master_ns']},{row['offset_ns']},{row['path_delay_ns']},"
                f"{1 if row.get('t2_kernel_timestamp') else 0},{1 if row.get('t4_kernel_timestamp') else 0},"
                f"{1 if row['used_for_offset'] else 0},{1 if row['used_for_best_path'] else 0},"
                f"{stats['clock_sync_method']},{stats['clock_sync_protocol']},"
                f"{1 if stats['clock_sync_kernel_timestamp'] else 0},"
                f"{stats['clock_sync_kernel_timestamp_received']},{stats['clock_sync_kernel_timestamp_fallback']},"
                f"{stats['clock_sync_t2_kernel_timestamp_received']},{stats['clock_sync_t2_kernel_timestamp_fallback']},"
                f"{stats['clock_sync_t4_kernel_timestamp_received']},{stats['clock_sync_t4_kernel_timestamp_fallback']},"
                f"{final_offset_ns},{stats['clock_offset_mean_ns']},{stats['clock_offset_median_ns']},"
                f"{stats['clock_offset_best_path_median_ns']},{stats['clock_offset_std_ns']:.6f},"
                f"{stats['clock_offset_mad_ns']},{final_path_delay_ns},{stats['clock_sync_path_delay_mean_ns']},"
                f"{stats['clock_sync_path_delay_min_ns']},{stats['clock_sync_path_delay_median_ns']},"
                f"{stats['clock_sync_path_delay_p95_ns']},{stats['clock_sync_path_delay_best_median_ns']}\n"
            )
    chown_output_path(path)
    print(f"clock_sync=data_saved ({path})")
    return path


def accept_control(host, port, sock_buf, busy_poll_us, timeout):
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    enable_low_latency_socket(server, sock_buf, busy_poll_us)
    server.bind((host, port))
    server.listen(1)
    server.settimeout(float(timeout))
    conn, _ = server.accept()
    server.close()
    enable_low_latency_socket(conn, sock_buf, busy_poll_us)
    return conn


def clock_warmup(args):
    return effective_clock_sync_warmup(args.clock_sync_samples, args.clock_sync_warmup)


def sync_to_master(host, port, args):
    sock = connect_repeater_until_ready(
        host,
        port,
        args.connect_timeout,
        args.detect_timeout,
        args.detect_interval,
        args.sock_buf,
        args.busy_poll_us,
    )
    if not args.clock_sync:
        stats = empty_clock_sync_stats("disabled", 0, args.clock_sync_best_ratio)
        stats.update({"clock_sync_protocol": "none"})
        return sock, 0, 0, [], stats, 0
    warmup = clock_warmup(args)
    offset_ns, path_delay_ns, rows, stats = estimate_clock_offset_udp(
        host,
        port,
        args.clock_sync_samples,
        warmup,
        args.clock_sync_method,
        args.clock_sync_best_ratio,
        args.sock_buf,
        args.busy_poll_us,
        args.detect_timeout,
        args.detect_interval,
        args.clock_sync_kernel_timestamp,
    )
    return sock, offset_ns, path_delay_ns, rows, stats, warmup


def connect_data_link(host, port, args, receive=False):
    sock, offset_ns, path_delay_ns, rows, stats, warmup = sync_to_master(host, port, args)
    data_sock = connect_udp_data(
        sock,
        host,
        port,
        args.sock_buf,
        args.busy_poll_us,
        args.detect_timeout,
        args.detect_interval,
    )
    if receive and args.kernel_timestamp:
        enable_kernel_timestamp_ns(data_sock)
    return {
        "control": sock,
        "data": data_sock,
        "offset_ns": offset_ns,
        "path_delay_ns": path_delay_ns,
        "clock_rows": rows,
        "clock_stats": stats,
        "clock_warmup": warmup,
        "kernel_timestamp": bool(receive and args.kernel_timestamp),
    }


def connect_clock_only(host, port, args):
    sock, offset_ns, path_delay_ns, rows, stats, warmup = sync_to_master(host, port, args)
    sock.close()
    return {
        "offset_ns": offset_ns,
        "path_delay_ns": path_delay_ns,
        "clock_rows": rows,
        "clock_stats": stats,
        "clock_warmup": warmup,
    }


def setup_server_links(args, specs):
    controls = {}
    udps = {}
    for name, host, port in specs:
        controls[name] = accept_control(host, port, args.sock_buf, args.busy_poll_us, args.accept_timeout)
    for name, host, port in specs:
        udps[name] = udp_bind_socket(host, port, args.sock_buf, args.busy_poll_us, args.udp_ready_timeout)
    if args.clock_sync:
        for name, _host, _port in specs:
            serve_clock_sync_udp(udps[name], args.clock_sync_samples, args.clock_sync_kernel_timestamp)
    return controls, udps


def accept_server_data_peers(controls, udps, data_names):
    data = {}
    for name in data_names:
        data[name] = accept_udp_peer(controls[name], udps[name])
    return data


def pack_msg(
    buffer,
    count_idx,
    stage,
    sender_id,
    peer_id,
    ts_origin_ns,
    ts_origin_sender_clock_ns,
    ts_emit_ns,
    ts_emit_r1_ns,
    correction_bits,
    w_left,
    w_right,
    w_result,
):
    DEOS_MSG.pack_into(
        buffer,
        0,
        int(count_idx),
        int(stage),
        int(sender_id),
        int(peer_id),
        int(ts_origin_ns),
        int(ts_origin_sender_clock_ns),
        int(ts_emit_ns),
        int(ts_emit_r1_ns),
        int(correction_bits),
        float(w_left),
        float(w_right),
        float(w_result),
    )


def unpack_msg(buffer):
    (
        count_idx,
        stage,
        sender_id,
        peer_id,
        ts_origin_ns,
        ts_origin_sender_clock_ns,
        ts_emit_ns,
        ts_emit_r1_ns,
        correction_bits,
        w_left,
        w_right,
        w_result,
    ) = DEOS_MSG.unpack_from(buffer)
    return {
        "count_idx": int(count_idx),
        "stage": int(stage),
        "sender_id": int(sender_id),
        "peer_id": int(peer_id),
        "ts_origin_ns": int(ts_origin_ns),
        "ts_origin_sender_clock_ns": int(ts_origin_sender_clock_ns),
        "ts_emit_ns": int(ts_emit_ns),
        "ts_emit_r1_ns": int(ts_emit_r1_ns),
        "correction_bits": int(correction_bits),
        "w_left": float(w_left),
        "w_right": float(w_right),
        "w_result": float(w_result),
    }


def recv_deos(sock, inbuf, kernel_timestamp):
    if kernel_timestamp:
        cmsg_buf_size = socket.CMSG_SPACE(16) if hasattr(socket, "CMSG_SPACE") else 128
        got, ancdata, _flags, _addr = sock.recvmsg_into([inbuf], cmsg_buf_size)
        ts_recv_ns = parse_kernel_timestamp_ns(ancdata)
        return got, (ts_recv_ns if ts_recv_ns is not None else time.time_ns()), ts_recv_ns is not None
    got = sock.recv_into(inbuf)
    return got, time.time_ns(), False


def summary_from_rows(rows, key):
    vals = [int(row[key]) for row in rows if row.get(key) is not None]
    return ns_summary(vals)


def werner_summary(vals):
    if not vals:
        return {"p50": 0.0, "p90": 0.0, "p95": 0.0, "p99": 0.0, "mean": 0.0, "std": 0.0}
    ordered = sorted(vals)
    mean_value = sum(vals) / len(vals)
    return {
        "p50": float(percentile_inverse(ordered, 0.50)),
        "p90": float(percentile_inverse(ordered, 0.90)),
        "p95": float(percentile_inverse(ordered, 0.95)),
        "p99": float(percentile_inverse(ordered, 0.99)),
        "mean": float(mean_value),
        "std": float(stddev(vals, mean_value)),
    }


def print_summary(label, summary):
    print("")
    print(label)
    for key in ("p50", "p90", "p95", "p99", "mean", "std", "min", "max"):
        value = summary.get(key)
        if value is None:
            continue
        if isinstance(value, float) and key == "std":
            print(f"{key}\t{fmt_ns(value)}")
        else:
            print(f"{key}\t{fmt_ns(value)}")


def print_werner_summary(label, summary):
    print("")
    print(label)
    for key in ("p50", "p90", "p95", "p99", "mean", "std"):
        value = summary.get(key)
        if value is not None:
            print(f"{key}\t{float(value):.6f}")


def run_r1(args):
    apply_cpu_rt(args.cpu, args.rt_priority)
    count = max(1, int(args.count))
    controls, udps = setup_server_links(
        args,
        [
            ("alice", args.listen_host_a, args.listen_port_a),
            ("r2", args.listen_host_r2, args.listen_port_r2),
            ("bob_sync", args.listen_host_bob_sync, args.listen_port_bob_sync),
        ],
    )
    controls["bob_sync"].close()
    udps["bob_sync"].close()
    data = accept_server_data_peers(controls, udps, ("alice", "r2"))
    controls["r2"].settimeout(float(args.accept_timeout))
    if controls["r2"].recv(1) != R2_READY_BYTE:
        raise ConnectionError("R2 did not announce that Alice/Bob output links are ready")
    send_alice = data["alice"].send
    send_r2 = data["r2"].send
    out_a = bytearray(DEOS_MSG_SIZE)
    out_r2 = bytearray(DEOS_MSG_SIZE)
    bits_samples = [random.randrange(4) for _ in range(count)]
    send_a_block = [0] * count if args.diag else []
    send_r2_block = [0] * count if args.diag else []
    send_gap = [0] * count if args.diag else []
    rows = []
    interval_ns = int(args.count_interval * 1_000_000_000)
    spin_margin_ns = int(args.spin_margin_us * 1000)
    w14 = float(args.w12) * float(args.w34)

    gc_was_enabled = gc.isenabled()
    gc.disable()
    try:
        time_ns = time.time_ns
        mono_ns = time.monotonic_ns
        for idx in range(count):
            count_idx = idx + 1
            bits = bits_samples[idx]
            ts_r1_ns = time_ns()
            pack_msg(
                out_a,
                count_idx,
                STAGE_R1_SWAP,
                args.r1_id,
                args.r2_id,
                ts_r1_ns,
                ts_r1_ns,
                ts_r1_ns,
                ts_r1_ns,
                bits,
                args.w12,
                args.w34,
                w14,
            )
            pack_msg(
                out_r2,
                count_idx,
                STAGE_R1_SWAP,
                args.r1_id,
                args.alice_id,
                ts_r1_ns,
                ts_r1_ns,
                ts_r1_ns,
                ts_r1_ns,
                bits,
                args.w12,
                args.w34,
                w14,
            )
            if args.diag:
                pre_a = mono_ns()
                send_alice(out_a)
                post_a = mono_ns()
                pre_r2 = mono_ns()
                send_r2(out_r2)
                post_r2 = mono_ns()
                send_a_block[idx] = post_a - pre_a
                send_gap[idx] = pre_r2 - pre_a
                send_r2_block[idx] = post_r2 - pre_r2
            else:
                send_alice(out_a)
                send_r2(out_r2)
            rows.append(
                {
                    "count_idx": count_idx,
                    "ts_r1_ns": ts_r1_ns,
                    "bits_r1": bits,
                    "w12": float(args.w12),
                    "w34": float(args.w34),
                    "w14": float(w14),
                }
            )
            if interval_ns > 0:
                pace_wait(interval_ns, args.pace_mode, spin_margin_ns)
    finally:
        if gc_was_enabled:
            gc.enable()
        for sock in data.values():
            sock.close()
        for conn in controls.values():
            try:
                conn.close()
            except OSError:
                pass

    suffix = ""
    csv_path = None
    if args.plot:
        if args.json_output:
            json_dir = role_json_dir(args, "r1")
            ensure_output_dir(json_dir)
            idx = 1
            while os.path.exists(os.path.join(json_dir, f"{args.plot_prefix}{suffix}.json")):
                idx += 1
                suffix = f"_{idx}"
        payload = {
            "role": "deos_r1",
            "csv_path": None,
            "csv_paths": {},
            "exchanges": count,
            "state_start": {
                "left": {"werner": float(args.w12), "peer_id": int(args.alice_id)},
                "right": {"werner": float(args.w34), "peer_id": int(args.r2_id)},
            },
            "state_out": {
                "left": {"werner": 0.0, "peer_id": None},
                "right": {"werner": 0.0, "peer_id": None},
            },
            "werner_intermediate_raw": float(w14),
            "send_stats_ns": {
                "send_alice_block_ns": ns_summary(send_a_block) if args.diag else None,
                "send_r2_block_ns": ns_summary(send_r2_block) if args.diag else None,
                "send_gap_ns": ns_summary(send_gap) if args.diag else None,
            },
            "last_origin": rows[-1] if rows else None,
        }
        write_json(args, args.plot_prefix, suffix, payload, "r1")

    print("")
    print("deos_r1_last")
    print(f"exchanges={count}")
    print(f"state_start=((w12={args.w12:.6f},peer={args.alice_id}),(w34={args.w34:.6f},peer={args.r2_id}))")
    print(f"state_out=((0.000000,None),(0.000000,None))")
    print(f"w14={w14:.6f}")
    if rows:
        print(f"last_ts_r1_ns={rows[-1]['ts_r1_ns']} ({fmt_ts_emit(rows[-1]['ts_r1_ns'])})")
    if args.diag:
        print_summary("r1_send_alice_block_ns", ns_summary(send_a_block))
        print_summary("r1_send_r2_block_ns", ns_summary(send_r2_block))
        print_summary("r1_send_gap_ns", ns_summary(send_gap))
    return 0


def run_r2(args):
    apply_cpu_rt(args.cpu, args.rt_priority)
    count = max(1, int(args.count))
    r1_link = connect_data_link(args.r1_host, args.r1_port, args, receive=True)
    controls, udps = setup_server_links(
        args,
        [
            ("alice", args.listen_host_a, args.listen_port_a),
            ("bob", args.listen_host_b, args.listen_port_b),
        ],
    )
    data = accept_server_data_peers(controls, udps, ("alice", "bob"))
    r1_link["control"].sendall(R2_READY_BYTE)
    send_alice = data["alice"].send
    send_bob = data["bob"].send
    recv_r1 = r1_link["data"]
    recv_r1.settimeout(float(args.udp_idle_timeout))
    inbuf = bytearray(DEOS_MSG_SIZE)
    out_a = bytearray(DEOS_MSG_SIZE)
    out_b = bytearray(DEOS_MSG_SIZE)
    bits_samples = [random.randrange(4) for _ in range(count)]
    rows = []
    kernel_rx = 0
    kernel_fallback = 0
    send_a_block = [0] * count if args.diag else []
    send_b_block = [0] * count if args.diag else []
    send_gap = [0] * count if args.diag else []
    offset_r1_minus_r2 = int(r1_link["offset_ns"])

    gc_was_enabled = gc.isenabled()
    gc.disable()
    try:
        time_ns = time.time_ns
        mono_ns = time.monotonic_ns
        received = 0
        seen_counts = bytearray(count + 1)
        while received < count:
            try:
                got, ts_recv_r2_ns, got_kernel = recv_deos(recv_r1, inbuf, r1_link["kernel_timestamp"])
            except socket.timeout:
                break
            if got != DEOS_MSG_SIZE:
                continue
            msg = unpack_msg(inbuf)
            if msg["stage"] != STAGE_R1_SWAP or msg["count_idx"] <= 0 or msg["count_idx"] > count:
                continue
            if seen_counts[msg["count_idx"]]:
                continue
            seen_counts[msg["count_idx"]] = 1
            if got_kernel:
                kernel_rx += 1
            elif r1_link["kernel_timestamp"]:
                kernel_fallback += 1
            received += 1
            idx = msg["count_idx"] - 1
            bits_r1 = int(msg["correction_bits"]) & 0b11
            bits_r2_local = bits_samples[idx] & 0b11
            bits_final = bits_r1 ^ bits_r2_local
            recv_r1_clock_ns = ts_recv_r2_ns + offset_r1_minus_r2
            r1_to_r2_delay_ns = recv_r1_clock_ns - msg["ts_emit_ns"]
            w14 = msg["w_result"]
            w56 = float(args.w56)
            w_final = w14 * w56
            ts_r2_ns = time_ns()
            ts_r2_r1_ns = ts_r2_ns + offset_r1_minus_r2
            ts_origin_on_r2_ns = msg["ts_origin_ns"] - offset_r1_minus_r2
            pack_msg(
                out_a,
                msg["count_idx"],
                STAGE_R2_SWAP,
                args.r2_id,
                args.bob_id,
                msg["ts_origin_ns"],
                ts_origin_on_r2_ns,
                ts_r2_ns,
                ts_r2_r1_ns,
                bits_final,
                w14,
                w56,
                w_final,
            )
            pack_msg(
                out_b,
                msg["count_idx"],
                STAGE_R2_SWAP,
                args.r2_id,
                args.alice_id,
                msg["ts_origin_ns"],
                ts_origin_on_r2_ns,
                ts_r2_ns,
                ts_r2_r1_ns,
                bits_final,
                w14,
                w56,
                w_final,
            )
            if args.diag:
                pre_a = mono_ns()
                send_alice(out_a)
                post_a = mono_ns()
                pre_b = mono_ns()
                send_bob(out_b)
                post_b = mono_ns()
                send_a_block[idx] = post_a - pre_a
                send_gap[idx] = pre_b - pre_a
                send_b_block[idx] = post_b - pre_b
            else:
                send_alice(out_a)
                send_bob(out_b)
            rows.append(
                {
                    "count_idx": msg["count_idx"],
                    "delay_ns": int(r1_to_r2_delay_ns),
                    "ts_r1_ns": msg["ts_origin_ns"],
                    "ts_r1_on_r2_ns": int(ts_origin_on_r2_ns),
                    "ts_r2_ns": int(ts_r2_ns),
                    "ts_r2_on_r1_ns": int(ts_r2_r1_ns),
                    "r2_recv_r1_msg_ns": int(ts_recv_r2_ns),
                    "r2_recv_r1_msg_on_r1_ns": int(recv_r1_clock_ns),
                    "bits_r1": bits_r1,
                    "bits_r2_local": bits_r2_local,
                    "bits_final": bits_final,
                    "w14": float(w14),
                    "w56": float(w56),
                    "w_final_raw": float(w_final),
                }
            )
    finally:
        if gc_was_enabled:
            gc.enable()
        for sock in data.values():
            sock.close()
        r1_link["data"].close()
        r1_link["control"].close()
        for conn in controls.values():
            try:
                conn.close()
            except OSError:
                pass

    rows.sort(key=lambda row: row["count_idx"])
    suffix = ""
    csv_path = None
    if args.plot:
        out_dir = role_plot_dir(args, "r2")
        csv_path, suffix = unique_output_path(out_dir, "01_r1_to_r2_aeso")
        with open(csv_path, "w", encoding="utf-8") as handle:
            handle.write(
                "count_idx,delay_ns,ts_r1_ns,ts_r1_on_r2_ns,ts_r2_ns,ts_r2_on_r1_ns,"
                "r2_recv_r1_msg_ns,r2_recv_r1_msg_on_r1_ns,bits_r1,bits_r2_local,bits_final,"
                "w14,w56,w_final_raw\n"
            )
            for row in rows:
                handle.write(
                    f"{row['count_idx']},{row['delay_ns']},{row['ts_r1_ns']},{row['ts_r1_on_r2_ns']},"
                    f"{row['ts_r2_ns']},{row['ts_r2_on_r1_ns']},{row['r2_recv_r1_msg_ns']},"
                    f"{row['r2_recv_r1_msg_on_r1_ns']},{row['bits_r1']},{row['bits_r2_local']},"
                    f"{row['bits_final']},"
                    f"{row['w14']:.12f},{row['w56']:.12f},{row['w_final_raw']:.12f}\n"
                )
        chown_output_path(csv_path)
        print(f"r2_r1_to_r2=data_saved ({csv_path})")
        write_clock_sync_csv(
            args,
            args.plot_prefix,
            suffix,
            "r1",
            r1_link["clock_rows"],
            r1_link["clock_stats"],
            r1_link["offset_ns"],
            r1_link["path_delay_ns"],
            "r2",
        )
        payload = {
            "role": "deos_r2",
            "csv_path": csv_path,
            "csv_paths": {"r1_to_r2_aeso": csv_path},
            "exchanges": count,
            "received_from_r1": len(rows),
            "udp_lost_est": max(0, count - len(rows)),
            "kernel_timestamp_received": int(kernel_rx),
            "kernel_timestamp_fallback": int(kernel_fallback),
            "clock_sync": {"r1": {**r1_link["clock_stats"], "clock_offset_final_ns": r1_link["offset_ns"], "clock_sync_path_delay_final_ns": r1_link["path_delay_ns"]}},
            "state_start": {
                "left": {"werner": float(args.w56), "peer_id": int(args.bob_id)},
                "right": {"werner": None, "peer_id": int(args.r1_id)},
            },
            "state_out": {
                "left": {"werner": 0.0, "peer_id": None},
                "right": {"werner": 0.0, "peer_id": None},
            },
            "metrics": {
                "r1_to_r2_delay_ns": summary_from_rows(rows, "delay_ns"),
                "w_final_raw": werner_summary([row["w_final_raw"] for row in rows]),
            },
        }
        write_json(args, args.plot_prefix, suffix, payload, "r2")

    print("")
    print("deos_r2_last")
    print(f"exchanges={count}")
    print(f"received_from_r1={len(rows)}")
    print(f"udp_lost_est={max(0, count - len(rows))}")
    print(f"clock_offset_r1_minus_r2_ns={offset_r1_minus_r2}")
    print(f"clock_sync_path_delay_r1_ns={r1_link['path_delay_ns']}")
    print(f"clock_offset_mad_r1_ns={r1_link['clock_stats'].get('clock_offset_mad_ns', 0)}")
    if rows:
        print(f"last_delay_r1_to_r2={fmt_ns(rows[-1]['delay_ns'])}")
        print(f"last_w_final_raw={rows[-1]['w_final_raw']:.6f}")
        print_summary("r2_r1_to_r2_delay_ns", summary_from_rows(rows, "delay_ns"))
        print_werner_summary("r2_w_final_raw", werner_summary([row["w_final_raw"] for row in rows]))
    return 0


def receive_client_messages(args, links, role_name):
    count = max(1, int(args.count))
    warmup = max(0, min(int(args.warmup), count - 1))
    inbuf = bytearray(DEOS_MSG_SIZE)
    fd_to_name = {link["data"].fileno(): name for name, link in links.items() if "data" in link}
    sockets = [link["data"] for link in links.values() if "data" in link]
    for sock in sockets:
        sock.setblocking(False)
    rows = {}
    kernel_rx = {name: 0 for name in links}
    kernel_fallback = {name: 0 for name in links}
    last_activity = time.monotonic()
    expected_final_stage = STAGE_R2_SWAP
    expect_stage1 = "data" in links.get("r1", {})

    gc_was_enabled = gc.isenabled()
    gc.disable()
    try:
        while True:
            final_seen = sum(1 for row in rows.values() if row.get("stage2_seen"))
            stage1_seen = sum(1 for row in rows.values() if row.get("stage1_seen"))
            if final_seen >= count and (not expect_stage1 or stage1_seen >= count):
                break
            timeout = max(0.0, args.udp_idle_timeout - (time.monotonic() - last_activity))
            if timeout <= 0:
                break
            ready, _, _ = select.select(sockets, [], [], timeout)
            if not ready:
                break
            for sock in ready:
                name = fd_to_name[sock.fileno()]
                link = links[name]
                try:
                    got, ts_recv_ns, got_kernel = recv_deos(sock, inbuf, link.get("kernel_timestamp", False))
                except BlockingIOError:
                    continue
                if got != DEOS_MSG_SIZE:
                    continue
                last_activity = time.monotonic()
                msg = unpack_msg(inbuf)
                count_idx = msg["count_idx"]
                if count_idx <= 0 or count_idx > count:
                    continue
                if got_kernel:
                    kernel_rx[name] = kernel_rx.get(name, 0) + 1
                elif link.get("kernel_timestamp", False):
                    kernel_fallback[name] = kernel_fallback.get(name, 0) + 1
                row = rows.setdefault(
                    count_idx,
                    {
                        "count_idx": count_idx,
                        "stage1_seen": False,
                        "stage2_seen": False,
                    },
                )
                if msg["stage"] == STAGE_R1_SWAP:
                    recv_r1_ns = ts_recv_ns + links["r1"]["offset_ns"]
                    row.update(
                        {
                            "stage1_seen": True,
                            "ts_r1_ns": msg["ts_origin_ns"],
                            "stage1_recv_local_ns": ts_recv_ns,
                            "stage1_recv_r1_ns": recv_r1_ns,
                            "stage1_delay_ns": recv_r1_ns - msg["ts_emit_ns"],
                            "bits_r1": msg["correction_bits"],
                            "w12": msg["w_left"],
                            "w34": msg["w_right"],
                            "w14": msg["w_result"],
                            "state_after_stage1_peer_id": msg["peer_id"],
                        }
                    )
                elif msg["stage"] == expected_final_stage:
                    recv_r1_ns = ts_recv_ns + links["r1"]["offset_ns"]
                    recv_r2_ns = ts_recv_ns + links["r2"]["offset_ns"]
                    final_e2e_ns = recv_r1_ns - msg["ts_origin_ns"]
                    final_e2e_via_r2_ns = recv_r2_ns - msg["ts_origin_sender_clock_ns"]
                    row.update(
                        {
                            "stage2_seen": True,
                            "ts_r1_ns": msg["ts_origin_ns"],
                            "ts_r2_ns": msg["ts_emit_ns"],
                            "ts_r2_on_r1_ns": msg["ts_emit_r1_ns"],
                            "stage2_recv_local_ns": ts_recv_ns,
                            "stage2_recv_r1_ns": recv_r1_ns,
                            "stage2_recv_r2_ns": recv_r2_ns,
                            "stage2_link_delay_ns": recv_r2_ns - msg["ts_emit_ns"],
                            "delay_ns": final_e2e_ns,
                            "final_e2e_via_r2_ns": final_e2e_via_r2_ns,
                            "bits_final": msg["correction_bits"],
                            "w14": msg["w_left"],
                            "w56": msg["w_right"],
                            "w_final_raw": msg["w_result"],
                            "state_final_peer_id": msg["peer_id"],
                        }
                    )
    finally:
        if gc_was_enabled:
            gc.enable()

    output_rows = []
    for count_idx in sorted(rows):
        row = rows[count_idx]
        if count_idx <= warmup or not row.get("stage2_seen"):
            continue
        delay_physical_ns = max(0, int(row["delay_ns"]))
        final_werner = decay_werner(row["w_final_raw"], delay_physical_ns, args.t1_ns) ** 2
        stage1_delay = row.get("stage1_delay_ns")
        stage1_werner = None
        if stage1_delay is not None:
            stage1_werner = decay_werner(row.get("w14", 0.0), max(0, int(stage1_delay)), args.t1_ns) ** 2
        output_rows.append(
            {
                **row,
                "delay_physical_ns": delay_physical_ns,
                "werner_intermediate": stage1_werner,
                "werner_final": final_werner,
            }
        )
    return output_rows, rows, kernel_rx, kernel_fallback, warmup


def write_alice_or_bob(args, role_name, rows, all_rows, links, kernel_rx, kernel_fallback, warmup):
    suffix = ""
    csv_path = None
    if args.plot:
        out_dir = role_plot_dir(args, role_name)
        sum_base = (
            "03_r1_initial_to_r2_alice_sum" if role_name == "alice" else "02_r1_initial_to_r2_bob_sum"
        )
        csv_path, suffix = unique_output_path(out_dir, sum_base)
        csv_paths = {"r1_initial_to_r2_endpoint_sum": csv_path}

        if role_name == "alice":
            stage1_path = output_path_with_suffix(out_dir, "01_r1_to_alice_aeso", suffix)
            csv_paths["r1_to_alice_aeso"] = stage1_path
            with open(stage1_path, "w", encoding="utf-8") as handle:
                handle.write(
                    "count_idx,delay_ns,delay_physical_ns,ts_r1_ns,stage1_recv_local_ns,"
                    "stage1_recv_r1_ns,bits_r1,w12,w34,w14,werner_intermediate,"
                    "state_after_stage1_peer_id\n"
                )
                for row in rows:
                    if row.get("stage1_delay_ns") is None:
                        continue
                    werner_intermediate = row.get("werner_intermediate")
                    werner_intermediate_text = (
                        "" if werner_intermediate is None else f"{werner_intermediate:.12f}"
                    )
                    stage1_delay_ns = int(row["stage1_delay_ns"])
                    handle.write(
                        f"{row['count_idx']},{stage1_delay_ns},{max(0, stage1_delay_ns)},"
                        f"{row.get('ts_r1_ns', '')},{row.get('stage1_recv_local_ns', '')},"
                        f"{row.get('stage1_recv_r1_ns', '')},{row.get('bits_r1', '')},"
                        f"{row.get('w12', 0.0):.12f},{row.get('w34', 0.0):.12f},"
                        f"{row.get('w14', 0.0):.12f},{werner_intermediate_text},"
                        f"{row.get('state_after_stage1_peer_id', '')}\n"
                    )
            chown_output_path(stage1_path)
            print(f"{role_name}_r1_to_alice=data_saved ({stage1_path})")

        stage2_base = "02_r2_to_alice_aeso" if role_name == "alice" else "01_r2_to_bob_aeso"
        stage2_path = output_path_with_suffix(out_dir, stage2_base, suffix)
        csv_paths["r2_to_endpoint_aeso"] = stage2_path
        with open(stage2_path, "w", encoding="utf-8") as handle:
            handle.write(
                "count_idx,delay_ns,delay_physical_ns,ts_r2_ns,ts_r2_on_r1_ns,"
                "stage2_recv_local_ns,stage2_recv_r1_ns,stage2_recv_r2_ns,bits_final,"
                "w14,w56,w_final_raw,state_final_peer_id\n"
            )
            for row in rows:
                stage2_delay_ns = int(row.get("stage2_link_delay_ns", 0))
                handle.write(
                    f"{row['count_idx']},{stage2_delay_ns},{max(0, stage2_delay_ns)},"
                    f"{row.get('ts_r2_ns', '')},{row.get('ts_r2_on_r1_ns', '')},"
                    f"{row.get('stage2_recv_local_ns', '')},{row.get('stage2_recv_r1_ns', '')},"
                    f"{row.get('stage2_recv_r2_ns', '')},{row.get('bits_final', '')},"
                    f"{row.get('w14', 0.0):.12f},{row.get('w56', 0.0):.12f},"
                    f"{row.get('w_final_raw', 0.0):.12f},{row.get('state_final_peer_id', '')}\n"
                )
        chown_output_path(stage2_path)
        print(f"{role_name}_r2_to_endpoint=data_saved ({stage2_path})")

        with open(csv_path, "w", encoding="utf-8") as handle:
            handle.write(
                "count_idx,delay_ns,delay_physical_ns,final_e2e_via_r2_ns,stage2_link_delay_ns,"
                "stage1_delay_ns,ts_r1_ns,ts_r2_ns,ts_r2_on_r1_ns,stage1_recv_r1_ns,"
                "stage2_recv_r1_ns,stage2_recv_r2_ns,bits_r1,bits_final,w14,w56,w_final_raw,"
                "werner_intermediate,werner_final,state_after_stage1_peer_id,state_final_peer_id\n"
            )
            for row in rows:
                werner_intermediate = row.get("werner_intermediate")
                werner_intermediate_text = (
                    "" if werner_intermediate is None else f"{werner_intermediate:.12f}"
                )
                handle.write(
                    f"{row['count_idx']},{row['delay_ns']},{row['delay_physical_ns']},"
                    f"{row.get('final_e2e_via_r2_ns', '')},{row.get('stage2_link_delay_ns', '')},"
                    f"{row.get('stage1_delay_ns', '')},{row.get('ts_r1_ns', '')},{row.get('ts_r2_ns', '')},"
                    f"{row.get('ts_r2_on_r1_ns', '')},{row.get('stage1_recv_r1_ns', '')},"
                    f"{row.get('stage2_recv_r1_ns', '')},{row.get('stage2_recv_r2_ns', '')},"
                    f"{row.get('bits_r1', '')},{row.get('bits_final', '')},"
                    f"{row.get('w14', 0.0):.12f},{row.get('w56', 0.0):.12f},{row.get('w_final_raw', 0.0):.12f},"
                    f"{werner_intermediate_text},"
                    f"{row.get('werner_final', 0.0):.12f},"
                    f"{row.get('state_after_stage1_peer_id', '')},{row.get('state_final_peer_id', '')}\n"
                )
        chown_output_path(csv_path)
        print(f"{role_name}_r1_initial_to_r2_endpoint_sum=data_saved ({csv_path})")
        for link_name, link in links.items():
            if "clock_rows" in link:
                write_clock_sync_csv(
                    args,
                    args.plot_prefix,
                    suffix,
                    link_name,
                    link["clock_rows"],
                    link["clock_stats"],
                    link["offset_ns"],
                    link["path_delay_ns"],
                    role_name,
                )
        final_werners = [row["werner_final"] for row in rows]
        payload = {
            "role": f"deos_{role_name}",
            "csv_path": csv_path,
            "csv_paths": csv_paths,
            "exchanges": int(args.count),
            "warmup": int(warmup),
            "received_final": len(rows),
            "seen_total": len(all_rows),
            "udp_lost_est_final": max(0, int(args.count) - len([row for row in all_rows.values() if row.get("stage2_seen")])),
            "kernel_timestamp_received": kernel_rx,
            "kernel_timestamp_fallback": kernel_fallback,
            "clock_sync": {
                name: {**link["clock_stats"], "clock_offset_final_ns": link["offset_ns"], "clock_sync_path_delay_final_ns": link["path_delay_ns"]}
                for name, link in links.items()
                if "clock_stats" in link
            },
            "metrics": {
                "final_e2e_delay_ns": summary_from_rows(rows, "delay_ns"),
                "stage2_link_delay_ns": summary_from_rows(rows, "stage2_link_delay_ns"),
                "stage1_delay_ns": summary_from_rows(rows, "stage1_delay_ns"),
                "werner_final": werner_summary(final_werners),
            },
            "samples": rows,
        }
        write_json(args, args.plot_prefix, suffix, payload, role_name)
    print("")
    print(f"deos_{role_name}_last")
    print(f"exchanges={args.count}")
    print(f"warmup={warmup}")
    print(f"received_final={len(rows)}")
    for link_name, link in links.items():
        if "clock_stats" not in link:
            continue
        print(f"clock_offset_{link_name}_ns={link['offset_ns']}")
        print(f"clock_sync_path_delay_{link_name}_ns={link['path_delay_ns']}")
        print(f"clock_offset_mad_{link_name}_ns={link['clock_stats'].get('clock_offset_mad_ns', 0)}")
    if rows:
        print(f"last_final_e2e={fmt_ns(rows[-1]['delay_ns'])}")
        print(f"last_stage2_link={fmt_ns(rows[-1]['stage2_link_delay_ns'])}")
        print(f"last_w_final_raw={rows[-1]['w_final_raw']:.6f}")
        print(f"last_werner_final={rows[-1]['werner_final']:.6f}")
        print_summary(f"{role_name}_final_e2e_delay_ns", summary_from_rows(rows, "delay_ns"))
        print_summary(f"{role_name}_stage2_link_delay_ns", summary_from_rows(rows, "stage2_link_delay_ns"))
        if any(row.get("stage1_delay_ns") is not None for row in rows):
            print_summary(f"{role_name}_stage1_delay_ns", summary_from_rows(rows, "stage1_delay_ns"))
        print_werner_summary(f"{role_name}_werner_final", werner_summary([row["werner_final"] for row in rows]))
    return 0


def run_alice(args):
    apply_cpu_rt(args.cpu, args.rt_priority)
    r1 = connect_data_link(args.r1_host, args.r1_port, args, receive=True)
    r2 = connect_data_link(args.r2_host, args.r2_port, args, receive=True)
    try:
        rows, all_rows, kernel_rx, kernel_fallback, warmup = receive_client_messages(
            args,
            {"r1": r1, "r2": r2},
            "alice",
        )
    finally:
        for link in (r1, r2):
            link["data"].close()
            link["control"].close()
    return write_alice_or_bob(args, "alice", rows, all_rows, {"r1": r1, "r2": r2}, kernel_rx, kernel_fallback, warmup)


def run_bob(args):
    apply_cpu_rt(args.cpu, args.rt_priority)
    r1 = connect_clock_only(args.r1_sync_host, args.r1_sync_port, args)
    r2 = connect_data_link(args.r2_host, args.r2_port, args, receive=True)
    try:
        rows, all_rows, kernel_rx, kernel_fallback, warmup = receive_client_messages(
            args,
            {"r1": r1, "r2": r2},
            "bob",
        )
    finally:
        r2["data"].close()
        r2["control"].close()
    return write_alice_or_bob(args, "bob", rows, all_rows, {"r1": r1, "r2": r2}, kernel_rx, kernel_fallback, warmup)


def main():
    args = parse_args()
    if args.role == "r1":
        return run_r1(args)
    if args.role == "r2":
        return run_r2(args)
    if args.role == "alice":
        return run_alice(args)
    if args.role == "bob":
        return run_bob(args)
    raise ValueError(f"unknown role: {args.role}")


if __name__ == "__main__":
    raise SystemExit(main())
