#!/usr/bin/env python3
import argparse
import gc
import json
import math
import os
import sys
import ctypes
import random
import threading
import socket
import struct
import time

TS_FORMAT = "!Q"
TS_SIZE = struct.calcsize(TS_FORMAT)
MSG_FORMAT = "!QIBd"
MSG_SIZE = struct.calcsize(MSG_FORMAT)
UDP_MSG_FORMAT = "!IQIBd"
UDP_MSG_SIZE = struct.calcsize(UDP_MSG_FORMAT)
UDP_HELLO_MAGIC = b"AESOUDP1"
UDP_READY_BYTE = b"U"
CLOCK_SYNC_UDP_MAGIC = b"AESOCS1!"
CLOCK_SYNC_UDP_HELLO = 1
CLOCK_SYNC_UDP_SYNC = 2
CLOCK_SYNC_UDP_DELAY_REQ = 3
CLOCK_SYNC_UDP_DELAY_RESP = 4
CLOCK_SYNC_SYNC_FORMAT = "!Q"
CLOCK_SYNC_SYNC_SIZE = struct.calcsize(CLOCK_SYNC_SYNC_FORMAT)
CLOCK_SYNC_DELAY_REQ_FORMAT = "!QQQ"
CLOCK_SYNC_DELAY_REQ_SIZE = struct.calcsize(CLOCK_SYNC_DELAY_REQ_FORMAT)
CLOCK_SYNC_DELAY_RESP_FORMAT = "!QQQQ"
CLOCK_SYNC_DELAY_RESP_SIZE = struct.calcsize(CLOCK_SYNC_DELAY_RESP_FORMAT)
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


def parse_args():
    parser = argparse.ArgumentParser(
        description="Fast3 unified: repeater/client with persistent sockets and low-jitter options."
    )
    subparsers = parser.add_subparsers(dest="role", required=True)

    repeater = subparsers.add_parser("repeater", help="Run in repeater mode.")
    repeater.add_argument("--listen-host-a", default="0.0.0.0")
    repeater.add_argument("--listen-port-a", type=int, default=7401)
    repeater.add_argument("--listen-host-b", default="0.0.0.0")
    repeater.add_argument("--listen-port-b", type=int, default=7402)
    repeater.add_argument("--count", type=int, default=2000)
    repeater.add_argument("--accept-timeout", type=float, default=30.0)
    repeater.add_argument("--cpu", type=int, default=None, help="Pin this process to one CPU core.")
    repeater.add_argument("--rt-priority", type=int, default=50, help="Set SCHED_FIFO priority (1-99), usually needs sudo.")
    repeater.add_argument("--sock-buf", type=int, default=65536, help="Set both SO_SNDBUF/SO_RCVBUF if > 0.")
    repeater.add_argument("--busy-poll-us", type=int, default=25, help="Set SO_BUSY_POLL in microseconds if supported.")
    repeater.add_argument("--repeater-id", type=int, default=0)
    repeater.add_argument("--client-a-id", type=int, default=1)
    repeater.add_argument("--client-b-id", type=int, default=2)
    repeater.add_argument("--werner-ar", type=float, default=None)
    repeater.add_argument("--werner-br", type=float, default=None)
    repeater.add_argument("--parallel", action="store_true", help="Send to A/B in parallel threads.")
    repeater.add_argument("--cpu-a", type=int, default=2, help="Pin sender thread A to this CPU core.")
    repeater.add_argument("--cpu-b", type=int, default=3, help="Pin sender thread B to this CPU core.")
    repeater.add_argument(
        "--shared-send-timestamp",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Use one timestamp for both A/B data messages in non-parallel mode. PTP clock sync always uses per-client timestamps.",
    )
    repeater.add_argument("--count-interval", type=float, default=0.0, help="Sleep seconds between counts.")
    repeater.add_argument("--pace-mode", choices=("sleep", "spin", "hybrid"), default="sleep", help="How --count-interval pacing waits between counts.")
    repeater.add_argument("--spin-margin-us", type=float, default=100.0, help="Final busy-wait window used by --pace-mode hybrid.")
    repeater.add_argument("--quiet", action="store_true")
    repeater.add_argument("--plot", action="store_true", help="Write repeater send timing CSV data.")
    repeater.add_argument("--plot-prefix", default="repeater_send_hist", help="Prefix for repeater send timing CSV outputs.")
    repeater.add_argument("--plot-dir", default="csv", help="Directory where --plot CSV files are written.")
    repeater.add_argument("--json", dest="json_output", action=argparse.BooleanOptionalAction, default=True, help="Write JSON metadata when --plot is used.")
    repeater.add_argument("--json-dir", default=None, help="Directory where JSON files are written. Defaults from --plot-dir: csv_x -> json_x.")
    repeater.add_argument("--diag", action="store_true", help="Measure extra repeater send timing diagnostics.")
    repeater.add_argument(
        "--clock-sync",
        nargs="?",
        choices=("tcp", "udp"),
        const="tcp",
        default=None,
        metavar="{tcp,udp}",
        help="Enable pre-run PTP-style clock offset calibration over tcp or udp. Bare --clock-sync keeps old TCP behavior.",
    )
    repeater.add_argument("--clock-sync-samples", type=int, default=8, help="Calibration exchanges used only with --clock-sync.")
    repeater.add_argument("--clock-sync-protocol", choices=("tcp", "udp"), default=None, help=argparse.SUPPRESS)
    repeater.add_argument("--clock-sync-kernel-timestamp", action="store_true", help="Use Linux SO_TIMESTAMPNS RX timestamps for UDP clock sync.")
    repeater.add_argument("--data-protocol", choices=("udp", "tcp"), default="udp", help="Transport for swapping result messages after TCP/PTP setup.")
    repeater.add_argument("--udp-ready-timeout", type=float, default=30.0, help="Seconds to wait for each client's UDP hello.")

    client = subparsers.add_parser("client", help="Run in client mode.")
    client.add_argument("--repeater-host", default="127.0.0.1")
    client.add_argument("--repeater-port", type=int, default=7401)
    client.add_argument("--count", type=int, default=2000)
    client.add_argument("--warmup", type=int, default=50)
    client.add_argument("--connect-timeout", type=float, default=10.0)
    client.add_argument("--detect-timeout", type=float, default=30.0)
    client.add_argument("--detect-interval", type=float, default=0.05)
    client.add_argument("--cpu", type=int, default=None, help="Pin this process to one CPU core.")
    client.add_argument("--rt-priority", type=int, default=50, help="Set SCHED_FIFO priority (1-99), usually needs sudo.")
    client.add_argument("--sock-buf", type=int, default=65536, help="Set both SO_SNDBUF/SO_RCVBUF if > 0.")
    client.add_argument("--busy-poll-us", type=int, default=25, help="Set SO_BUSY_POLL in microseconds if supported.")
    client.add_argument("--client-id", type=int, default=1)
    client.add_argument("--repeater-id", type=int, default=0)
    client.add_argument("--werner-in", type=float, default=1)
    client.add_argument("--plot", action="store_true", help="Write delay histogram data and plot if matplotlib is available.")
    client.add_argument("--plot-prefix", default="delay_hist_client", help="Prefix for plot outputs.")
    client.add_argument("--plot-dir", default="csv", help="Directory where --plot CSV files are written.")
    client.add_argument("--json", dest="json_output", action=argparse.BooleanOptionalAction, default=True, help="Write JSON output when --plot is used.")
    client.add_argument("--json-dir", default=None, help="Directory where JSON files are written. Defaults from --plot-dir: csv_x -> json_x.")
    client.add_argument("--count-interval", type=float, default=0.0, help="Sleep seconds between counts.")
    client.add_argument("--quiet", action="store_true")
    client.add_argument("--t1-ns", type=float, default=1_000_000.0)
    client.add_argument("--diag", action="store_true", help="Measure extra client loop/recv timing diagnostics.")
    client.add_argument(
        "--clock-sync",
        nargs="?",
        choices=("tcp", "udp"),
        const="tcp",
        default=None,
        metavar="{tcp,udp}",
        help="Estimate repeater-clock minus client-clock offset over tcp or udp. Bare --clock-sync keeps old TCP behavior.",
    )
    client.add_argument("--clock-sync-samples", type=int, default=8, help="Calibration exchanges used only with --clock-sync.")
    client.add_argument("--clock-sync-warmup", type=int, default=None, help="Clock-sync samples discarded before offset averaging. Defaults to floor(5%% of --clock-sync-samples).")
    client.add_argument("--clock-sync-method", choices=("mean", "median", "best-path-median"), default="best-path-median", help="Estimator used for the final clock offset after warmup.")
    client.add_argument("--clock-sync-best-ratio", type=float, default=0.5, help="Fraction of lowest-path-delay samples used by --clock-sync-method best-path-median.")
    client.add_argument("--clock-sync-protocol", choices=("tcp", "udp"), default=None, help=argparse.SUPPRESS)
    client.add_argument("--clock-sync-kernel-timestamp", action="store_true", help="Use Linux SO_TIMESTAMPNS RX timestamps for UDP clock sync.")
    client.add_argument("--clock-offset-ns", type=int, default=None, help="Manual repeater-clock minus client-clock offset. Skips auto calibration and does not require --clock-sync.")
    client.add_argument("--center-delay", action="store_true", help="Center delay stats around the run median; raw signed delay stays in CSV.")
    client.add_argument("--data-protocol", choices=("udp", "tcp"), default="udp", help="Transport for swapping result messages after TCP/PTP setup.")
    client.add_argument("--udp-idle-timeout", type=float, default=5.0, help="Stop waiting for UDP data after this many idle seconds.")
    client.add_argument("--kernel-timestamp", action="store_true", help="Use Linux SO_TIMESTAMPNS receive timestamps for UDP data.")

    args = parser.parse_args()
    clock_sync_transport = getattr(args, "clock_sync", None)
    legacy_clock_sync_protocol = getattr(args, "clock_sync_protocol", None)
    if clock_sync_transport is None:
        args.clock_sync = False
        args.clock_sync_protocol = legacy_clock_sync_protocol or "tcp"
    else:
        args.clock_sync = True
        args.clock_sync_protocol = legacy_clock_sync_protocol or clock_sync_transport
    return args


def default_json_dir(plot_dir):
    clean_dir = plot_dir.rstrip(os.sep)
    parent, base = os.path.split(clean_dir)
    if not base:
        return "json"
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
    if rt_priority is not None:
        param = os.sched_param(int(rt_priority))
        os.sched_setscheduler(0, os.SCHED_FIFO, param)


def recv_exact_into(sock, buf):
    view = memoryview(buf)
    n = 0
    while n < len(buf):
        got = sock.recv_into(view[n:])
        if got <= 0:
            raise ConnectionError("Socket closed before full message")
        n += got


def recv_exact(sock, size):
    buf = bytearray(size)
    recv_exact_into(sock, buf)
    return bytes(buf)


def serve_clock_sync(sock, samples):
    for _ in range(max(0, int(samples))):
        # PTP-style exchange with the repeater as master:
        # t1: master Sync transmit, t2: slave Sync receive,
        # t3: slave Delay_Req transmit, t4: master Delay_Req receive.
        t1_ns = time.time_ns()
        sock.sendall(struct.pack(CLOCK_SYNC_SYNC_FORMAT, t1_ns))
        t1_echo_ns, t2_ns, t3_ns = struct.unpack(
            CLOCK_SYNC_DELAY_REQ_FORMAT, recv_exact(sock, CLOCK_SYNC_DELAY_REQ_SIZE)
        )
        t4_ns = time.time_ns()
        if t1_echo_ns != t1_ns:
            raise ValueError("PTP clock-sync request did not match the Sync timestamp")
        sock.sendall(struct.pack(CLOCK_SYNC_DELAY_RESP_FORMAT, t1_ns, t2_ns, t3_ns, t4_ns))


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


def effective_clock_sync_warmup(samples, warmup):
    sample_total = max(0, int(samples))
    if sample_total <= 0:
        return 0
    if warmup is None:
        discard = int(sample_total * 0.05)
    else:
        discard = max(0, int(warmup))
    return min(discard, sample_total - 1)


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
        "clock_sync_protocol": "tcp",
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


def assess_sync_quality(clock_sync_stats, signed_delays):
    negative_count = sum(1 for value in signed_delays if value < 0)
    negative_ratio = (negative_count / len(signed_delays)) if signed_delays else 0.0
    signed_p50_ns = int(percentile(sorted(signed_delays), 0.50)) if signed_delays else 0
    reasons = []
    level = 0

    path_median = int(clock_sync_stats.get("clock_sync_path_delay_median_ns", 0))
    path_p95 = int(clock_sync_stats.get("clock_sync_path_delay_p95_ns", 0))
    offset_mad = int(clock_sync_stats.get("clock_offset_mad_ns", 0))
    if path_median > 1_000_000 or path_p95 > 2_000_000:
        level = max(level, 1)
        reasons.append("high_path_delay")
    if path_median > 5_000_000 or path_p95 > 10_000_000:
        level = max(level, 2)
        reasons.append("very_high_path_delay")
    if offset_mad > 10_000:
        level = max(level, 1)
        reasons.append("high_offset_mad")
    if offset_mad > 100_000:
        level = max(level, 2)
        reasons.append("very_high_offset_mad")
    if negative_ratio > 0.20:
        level = max(level, 1)
        reasons.append("many_negative_delays")
    if negative_ratio > 0.80:
        level = max(level, 2)
        reasons.append("mostly_negative_delays")
    if signed_p50_ns < -1_000_000:
        level = max(level, 2)
        reasons.append("negative_delay_p50_ms")

    quality = ("ok", "warn", "bad")[level]
    return {
        "sync_quality": quality,
        "sync_quality_reasons": reasons,
        "clock_sync_negative_or_suspicious": bool(level > 0),
        "delay_negative_count": int(negative_count),
        "delay_negative_ratio": float(negative_ratio),
        "delay_signed_p50_ns": int(signed_p50_ns),
    }


def estimate_clock_offset(sock, samples, warmup=0, method="best-path-median", best_ratio=0.5):
    # PTP-style four-timestamp exchange with the repeater as master:
    # t1: master Sync transmit, t2: slave Sync receive,
    # t3: slave Delay_Req transmit, t4: master Delay_Req receive.
    # slave_minus_master = ((t2 - t1) - (t4 - t3)) / 2.
    # We return master_minus_slave so the client can correct as:
    # corrected_client_time = client_time + master_minus_slave.
    clock_sync_samples = []
    sample_total = max(0, int(samples))
    warmup = min(max(0, int(warmup)), max(0, sample_total - 1))
    for sample_number in range(1, sample_total + 1):
        (t1_ns,) = struct.unpack(CLOCK_SYNC_SYNC_FORMAT, recv_exact(sock, CLOCK_SYNC_SYNC_SIZE))
        t2_ns = time.time_ns()
        t3_ns = time.time_ns()
        sock.sendall(struct.pack(CLOCK_SYNC_DELAY_REQ_FORMAT, t1_ns, t2_ns, t3_ns))
        echoed_t1_ns, echoed_t2_ns, echoed_t3_ns, t4_ns = struct.unpack(
            CLOCK_SYNC_DELAY_RESP_FORMAT, recv_exact(sock, CLOCK_SYNC_DELAY_RESP_SIZE)
        )
        if (echoed_t1_ns, echoed_t2_ns, echoed_t3_ns) != (t1_ns, t2_ns, t3_ns):
            raise ValueError("PTP clock-sync response did not match the Delay_Req timestamps")
        master_to_slave_ns = t2_ns - t1_ns
        slave_to_master_ns = t4_ns - t3_ns
        mean_path_delay_ns = (master_to_slave_ns + slave_to_master_ns) // 2
        offset_ns = (slave_to_master_ns - master_to_slave_ns) // 2
        used_for_offset = sample_number > warmup
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
                "used_for_offset": used_for_offset,
                "used_for_best_path": False,
                "t2_kernel_timestamp": False,
                "t4_kernel_timestamp": False,
            }
        )
    clock_offset_ns, clock_sync_path_delay_ns, stats = build_clock_sync_stats(
        clock_sync_samples, method, warmup, best_ratio
    )
    stats.update(
        {
            "clock_sync_protocol": "tcp",
            "clock_sync_kernel_timestamp": False,
            "clock_sync_kernel_timestamp_received": 0,
            "clock_sync_kernel_timestamp_fallback": 0,
            "clock_sync_t2_kernel_timestamp_received": 0,
            "clock_sync_t2_kernel_timestamp_fallback": 0,
            "clock_sync_t4_kernel_timestamp_received": 0,
            "clock_sync_t4_kernel_timestamp_fallback": 0,
        }
    )
    return clock_offset_ns, clock_sync_path_delay_ns, clock_sync_samples, stats


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
    # UDP PTP-style exchange. t2 and t4 can be RX kernel timestamps:
    # t1: repeater user-space timestamp before sendto(Sync)
    # t2: client RX timestamp of Sync, kernel if available
    # t3: client user-space timestamp before sendto(Delay_Req)
    # t4: repeater RX timestamp of Delay_Req, kernel if enabled on repeater
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
                magic, msg_type, echoed_sample, echoed_t1_ns, echoed_t2_ns, echoed_t3_ns, t4_ns, t4_got_kernel = struct.unpack(
                    CLOCK_SYNC_UDP_DELAY_RESP_FORMAT, data
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


def decay_werner(base, age_ns, t1_ns):
    if t1_ns <= 0:
        raise ValueError("t1_ns must be positive")
    if age_ns < 0:
        # Negative one-way delay means the two host clocks are offset. Keep the
        # signed delay in the latency CSV/output, but do not abort the run or
        # apply unphysical negative decay to Werner.
        age_ns = 0
    decayed = float(base) * math.exp(-age_ns / float(t1_ns))
    return decayed


def set_thread_affinity(cpu):
    if cpu is None:
        return
    cpu = int(cpu)
    if cpu < 0:
        return
    cpu_set_t = ctypes.c_ulong * 16
    mask = cpu_set_t()
    idx = cpu // (8 * ctypes.sizeof(ctypes.c_ulong))
    bit = cpu % (8 * ctypes.sizeof(ctypes.c_ulong))
    if idx >= len(mask):
        return
    mask[idx] |= 1 << bit
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    pthread_self = libc.pthread_self
    pthread_self.restype = ctypes.c_ulong
    pthread_setaffinity_np = libc.pthread_setaffinity_np
    pthread_setaffinity_np.argtypes = [ctypes.c_ulong, ctypes.c_size_t, ctypes.POINTER(cpu_set_t)]
    pthread_setaffinity_np.restype = ctypes.c_int
    res = pthread_setaffinity_np(pthread_self(), ctypes.sizeof(mask), ctypes.byref(mask))
    if res != 0:
        err = ctypes.get_errno()
        raise OSError(err, "pthread_setaffinity_np failed")


def fmt_ns(v):
    return f"{int(v)} ({int(v) / 1e9:.9f} s)"


def fmt_ts_emit(ts_ns):
    total_s = int(ts_ns // 1_000_000_000)
    ns_part = int(ts_ns % 1_000_000_000)
    tm = time.gmtime(total_s)
    return f"{tm.tm_min:02d}:{tm.tm_sec:02d}.{ns_part:09d}"


def fmt_state(state):
    local_id, werner, peer_id = state
    return f"({local_id},{werner:.6f},{peer_id})"


def print_client_group(label, delta_ns, werner, delay_label="abs_repeater_to_client"):
    print("")
    print(f"client_{label}")
    print("metric\t\t\t\t value")
    print(f"{delay_label}\t\t {fmt_ns(delta_ns)}")
    print(f"werner\t\t\t\t {werner:.6f}")


def print_client_message_state(label, delta_ns, msg, state_out, count_idx=None, delay_label="signed_repeater_to_client"):
    print("")
    print(f"client_{label}")
    print("metric\t\t\t\t value")
    print(f"{delay_label}\t {fmt_ns(delta_ns)}")
    if count_idx is not None:
        print(f"count_idx\t\t\t {count_idx}")
    print(
        "msg=(ts_emit_ns={}, ts_emit={}, peer_id={}, bits={:02b}, w_swap={:.6f})".format(
            msg[0], fmt_ts_emit(msg[0]), msg[1], msg[2], msg[3]
        )
    )
    print(f"state_out={fmt_state(state_out)}")


def run_repeater(args):
    apply_cpu_rt(args.cpu, args.rt_priority)
    if args.clock_sync_kernel_timestamp and args.clock_sync_protocol != "udp":
        raise ValueError("--clock-sync-kernel-timestamp requires --clock-sync udp")
    count = max(1, int(args.count))
    if args.werner_ar is None:
        args.werner_ar = float(input("werner_ar: ").strip())
    if args.werner_br is None:
        args.werner_br = float(input("werner_br: ").strip())
    w_ar_init = args.werner_ar
    w_br_init = args.werner_br
    state_ar = (args.repeater_id, w_ar_init, args.client_a_id)
    state_br = (args.repeater_id, w_br_init, args.client_b_id)
    last_ar_ns = time.monotonic_ns()
    last_br_ns = time.monotonic_ns()
    data_msg_size = UDP_MSG_SIZE if args.data_protocol == "udp" else MSG_SIZE
    outbuf_a = bytearray(data_msg_size)
    outbuf_b = bytearray(data_msg_size)
    last_state_in_ar = state_ar
    last_state_in_br = state_br
    last_msg_a = (0, 0, 0, 0.0)
    last_msg_b = (0, 0, 0, 0.0)
    send_a_block_samples = [0] * count if args.diag else []
    send_b_block_samples = [0] * count if args.diag else []
    send_gap_ab_samples = [0] * count if args.diag else []
    correction_bits_samples = [random.randrange(4) for _ in range(count)]

    def accept_one(host, port):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        enable_low_latency_socket(server, args.sock_buf, args.busy_poll_us)
        server.bind((host, port))
        server.listen(1)
        server.settimeout(args.accept_timeout)
        conn, _ = server.accept()
        server.close()
        enable_low_latency_socket(conn, args.sock_buf, args.busy_poll_us)
        return conn

    conn_a = accept_one(args.listen_host_a, args.listen_port_a)
    conn_b = accept_one(args.listen_host_b, args.listen_port_b)
    needs_udp_socket = args.data_protocol == "udp" or (args.clock_sync and args.clock_sync_protocol == "udp")
    if needs_udp_socket:
        udp_a = udp_bind_socket(
            args.listen_host_a, args.listen_port_a, args.sock_buf, args.busy_poll_us, args.udp_ready_timeout
        )
        udp_b = udp_bind_socket(
            args.listen_host_b, args.listen_port_b, args.sock_buf, args.busy_poll_us, args.udp_ready_timeout
        )
    else:
        udp_a = None
        udp_b = None
    if args.clock_sync:
        if args.clock_sync_protocol == "tcp":
            serve_clock_sync(conn_a, args.clock_sync_samples)
            serve_clock_sync(conn_b, args.clock_sync_samples)
        else:
            serve_clock_sync_udp(udp_a, args.clock_sync_samples, args.clock_sync_kernel_timestamp)
            serve_clock_sync_udp(udp_b, args.clock_sync_samples, args.clock_sync_kernel_timestamp)
    if args.data_protocol == "udp":
        data_a = accept_udp_peer(conn_a, udp_a)
        data_b = accept_udp_peer(conn_b, udp_b)
    else:
        if udp_a is not None:
            udp_a.close()
        if udp_b is not None:
            udp_b.close()
        data_a = conn_a
        data_b = conn_b

    is_udp_data = args.data_protocol == "udp"
    udp_pack_into = struct.Struct(UDP_MSG_FORMAT).pack_into
    tcp_pack_into = struct.Struct(MSG_FORMAT).pack_into

    if args.parallel:
        barrier_ready = threading.Barrier(3)
        barrier_done = threading.Barrier(3)
        last_msg_a_ref = [last_msg_a]
        last_msg_b_ref = [last_msg_b]

        def sender_thread(conn, buffer_ref, cpu_pin, send_block_samples, peer_id, last_msg_ref):
            set_thread_affinity(cpu_pin)
            time_ns = time.time_ns
            for idx in range(count):
                barrier_ready.wait()
                ts_emit_ns = time_ns()
                correction_bits = correction_bits_samples[idx]
                w_swap = 1.0
                if is_udp_data:
                    udp_pack_into(buffer_ref, 0, idx + 1, ts_emit_ns, peer_id, correction_bits, w_swap)
                else:
                    tcp_pack_into(buffer_ref, 0, ts_emit_ns, peer_id, correction_bits, w_swap)
                if args.diag:
                    pre_send_ns = time.monotonic_ns()
                    if is_udp_data:
                        conn.send(buffer_ref)
                    else:
                        conn.sendall(buffer_ref)
                    send_block_samples[idx] = time.monotonic_ns() - pre_send_ns
                else:
                    if is_udp_data:
                        conn.send(buffer_ref)
                    else:
                        conn.sendall(buffer_ref)
                last_msg_ref[0] = (ts_emit_ns, peer_id, correction_bits, w_swap)
                barrier_done.wait()

        gc_was_enabled = gc.isenabled()
        gc.disable()
        try:
            with conn_a, conn_b, data_a, data_b:
                t_a = threading.Thread(
                    target=sender_thread,
                    args=(data_a, outbuf_a, args.cpu_a, send_a_block_samples, args.client_b_id, last_msg_a_ref),
                    daemon=True,
                )
                t_b = threading.Thread(
                    target=sender_thread,
                    args=(data_b, outbuf_b, args.cpu_b, send_b_block_samples, args.client_a_id, last_msg_b_ref),
                    daemon=True,
                )
                t_a.start()
                t_b.start()
                for idx in range(count):
                    last_state_in_ar = state_ar
                    last_state_in_br = state_br
                    barrier_ready.wait()
                    barrier_done.wait()
                    last_msg_a = last_msg_a_ref[0]
                    last_msg_b = last_msg_b_ref[0]
                    if args.diag:
                        send_gap_ab_samples[idx] = 0
                    if args.count_interval > 0:
                        pace_wait(
                            int(args.count_interval * 1_000_000_000),
                            args.pace_mode,
                            int(args.spin_margin_us * 1000),
                        )
                t_a.join()
                t_b.join()
        finally:
            if gc_was_enabled:
                gc.enable()
    else:
        gc_was_enabled = gc.isenabled()
        gc.disable()
        try:
            with conn_a, conn_b, data_a, data_b:
                send_a = data_a.send if is_udp_data else data_a.sendall
                send_b = data_b.send if is_udp_data else data_b.sendall
                pack_into = udp_pack_into if is_udp_data else tcp_pack_into
                time_ns = time.time_ns
                mono_ns = time.monotonic_ns
                sleep = time.sleep
                diag = args.diag
                count_interval = args.count_interval
                pace_interval_ns = int(count_interval * 1_000_000_000)
                pace_mode = args.pace_mode
                spin_margin_ns = int(args.spin_margin_us * 1000)
                peer_a_id = args.client_b_id
                peer_b_id = args.client_a_id
                shared_send_timestamp = args.shared_send_timestamp
                w_swap = 1.0
                for idx in range(count):
                    correction_bits = correction_bits_samples[idx]
                    count_idx = idx + 1
                    ts_emit_a_ns = time_ns()
                    if is_udp_data:
                        pack_into(outbuf_a, 0, count_idx, ts_emit_a_ns, peer_a_id, correction_bits, w_swap)
                    else:
                        pack_into(outbuf_a, 0, ts_emit_a_ns, peer_a_id, correction_bits, w_swap)
                    if diag:
                        pre_send_a_ns = mono_ns()
                        send_a(outbuf_a)
                        post_send_a_ns = mono_ns()
                        send_a_block_samples[idx] = post_send_a_ns - pre_send_a_ns
                    else:
                        send_a(outbuf_a)
                    last_msg_a = (ts_emit_a_ns, peer_a_id, correction_bits, w_swap)
                    ts_emit_b_ns = ts_emit_a_ns if shared_send_timestamp else time_ns()
                    if is_udp_data:
                        pack_into(outbuf_b, 0, count_idx, ts_emit_b_ns, peer_b_id, correction_bits, w_swap)
                    else:
                        pack_into(outbuf_b, 0, ts_emit_b_ns, peer_b_id, correction_bits, w_swap)
                    if diag:
                        pre_send_b_ns = mono_ns()
                        send_gap_ab_samples[idx] = pre_send_b_ns - pre_send_a_ns
                        send_b(outbuf_b)
                        send_b_block_samples[idx] = mono_ns() - pre_send_b_ns
                    else:
                        send_b(outbuf_b)
                    last_msg_b = (ts_emit_b_ns, peer_b_id, correction_bits, w_swap)
                    if pace_interval_ns > 0:
                        if pace_mode == "sleep":
                            sleep(count_interval)
                        else:
                            pace_wait(pace_interval_ns, pace_mode, spin_margin_ns)
        finally:
            if gc_was_enabled:
                gc.enable()

    if args.plot:
        ensure_output_dir(args.plot_dir)
        base = args.plot_prefix
        suffix = ""
        idx = 1
        while os.path.exists(os.path.join(args.plot_dir, f"{base}{suffix}.csv")):
            idx += 1
            suffix = f"_{idx}"
        csv_path = os.path.join(args.plot_dir, f"{base}{suffix}.csv")
        with open(csv_path, "w", encoding="utf-8") as handle:
            if args.diag:
                handle.write("count_idx,send_a_block_ns,send_b_block_ns,send_gap_ab_ns\n")
                for count_idx, (send_a_ns, send_b_ns, send_gap_ab_ns) in enumerate(
                    zip(send_a_block_samples, send_b_block_samples, send_gap_ab_samples), start=1
                ):
                    handle.write(f"{count_idx},{send_a_ns},{send_b_ns},{send_gap_ab_ns}\n")
            else:
                handle.write("count_idx\n")
                for count_idx in range(1, count + 1):
                    handle.write(f"{count_idx}\n")
        chown_output_path(csv_path)
        print(f"repeater_plot=data_saved ({csv_path})")
        if args.json_output:
            json_dir = args.json_dir or default_json_dir(args.plot_dir)
            ensure_output_dir(json_dir)
            json_path = os.path.join(json_dir, f"{base}{suffix}.json")
            payload = {
                "role": "repeater",
                "argv": sys.argv,
                "args": vars(args),
                "csv_path": csv_path,
                "created_at_unix_ns": time.time_ns(),
                "exchanges": int(count),
                "data_protocol": args.data_protocol,
                "state_ar_start": {
                    "local_id": int(args.repeater_id),
                    "werner": float(w_ar_init),
                    "peer_id": int(args.client_a_id),
                },
                "state_br_start": {
                    "local_id": int(args.repeater_id),
                    "werner": float(w_br_init),
                    "peer_id": int(args.client_b_id),
                },
                "last_msg_a": {
                    "ts_emit_ns": int(last_msg_a[0]),
                    "peer_id": int(last_msg_a[1]),
                    "correction_bits": int(last_msg_a[2]),
                    "w_swap": float(last_msg_a[3]),
                },
                "last_msg_b": {
                    "ts_emit_ns": int(last_msg_b[0]),
                    "peer_id": int(last_msg_b[1]),
                    "correction_bits": int(last_msg_b[2]),
                    "w_swap": float(last_msg_b[3]),
                },
                "send_summary_ns": None,
                "samples": [],
            }
            if args.diag:
                payload["send_summary_ns"] = {
                    "send_a_block_ns": ns_summary(send_a_block_samples),
                    "send_b_block_ns": ns_summary(send_b_block_samples),
                    "send_gap_ab_ns": ns_summary(send_gap_ab_samples),
                }
                payload["samples"] = [
                    {
                        "count_idx": int(count_idx),
                        "send_a_block_ns": int(send_a_ns),
                        "send_b_block_ns": int(send_b_ns),
                        "send_gap_ab_ns": int(send_gap_ab_ns),
                    }
                    for count_idx, (send_a_ns, send_b_ns, send_gap_ab_ns) in enumerate(
                        zip(send_a_block_samples, send_b_block_samples, send_gap_ab_samples), start=1
                    )
                ]
            with open(json_path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2)
                handle.write("\n")
            chown_output_path(json_path)
            print(f"repeater_json=data_saved ({json_path})")

    if not args.quiet:
        print("repeater_mode=fast3")
        print(f"exchanges={count}")
        print(f"repeater_id={args.repeater_id}")
        print(f"data_protocol={args.data_protocol}")
    print("")
    print(f"state_ar_start={fmt_state((args.repeater_id, w_ar_init, args.client_a_id))}")
    print(f"state_br_start={fmt_state((args.repeater_id, w_br_init, args.client_b_id))}")
    print("")
    print("repeater_last")
    print("msg_a=(ts_emit_ns={}, ts_emit={}, peer_id={}, bits={:02b}, w_swap={:.6f})".format(
        last_msg_a[0], fmt_ts_emit(last_msg_a[0]), last_msg_a[1], last_msg_a[2], last_msg_a[3]
    ))
    print("msg_b=(ts_emit_ns={}, ts_emit={}, peer_id={}, bits={:02b}, w_swap={:.6f})".format(
        last_msg_b[0], fmt_ts_emit(last_msg_b[0]), last_msg_b[1], last_msg_b[2], last_msg_b[3]
    ))
    print("")
    print(f"state_in_ar={fmt_state(last_state_in_ar)}")
    print(f"state_in_br={fmt_state(last_state_in_br)}")
    print("")
    print(f"state_out_ar={fmt_state((args.repeater_id, 0.0, None))}")
    print(f"state_out_br={fmt_state((args.repeater_id, 0.0, None))}")
    return 0


def run_client(args):
    apply_cpu_rt(args.cpu, args.rt_priority)
    if args.clock_sync_kernel_timestamp and args.clock_sync_protocol != "udp":
        raise ValueError("--clock-sync-kernel-timestamp requires --clock-sync udp")
    count = max(1, int(args.count))
    warmup = max(0, min(int(args.warmup), count - 1))
    sample_count = count - warmup
    delta_samples = [0] * sample_count
    werner_samples = [0.0] * sample_count
    werner_raw_samples = [0.0] * sample_count
    sample_msgs = [None] * sample_count
    delta_record_counts = [0] * sample_count
    loop_gap_samples = [0] * sample_count if args.diag else []
    recv_block_samples = [0] * sample_count if args.diag else []
    last_delta = 0
    last_werner = 0.0
    last_msg = (0, 0, 0, 0.0)
    last_raw_msg = (0, 0, 0, 0.0)
    last_ts_emit_ns = 0
    last_peer_id = 0
    last_correction_bits = 0
    last_w_swap_raw = 0.0
    last_state_out = (args.client_id, 0.0, None)
    data_msg_size = UDP_MSG_SIZE if args.data_protocol == "udp" else MSG_SIZE
    inbuf = bytearray(data_msg_size)
    is_udp_data = args.data_protocol == "udp"
    udp_unpack_from = struct.Struct(UDP_MSG_FORMAT).unpack_from
    tcp_unpack_from = struct.Struct(MSG_FORMAT).unpack_from
    sample_idx = 0
    udp_received = 0
    udp_lost_est = 0
    udp_seen_counts = bytearray(count + 1)
    udp_seen_total = 0
    kernel_timestamp_received = 0
    kernel_timestamp_fallback = 0
    clock_sync_sample_rows = []
    clock_sync_warmup = 0
    clock_sync_stats = empty_clock_sync_stats("manual" if args.clock_offset_ns is not None else "none")

    with connect_repeater_until_ready(
        args.repeater_host,
        args.repeater_port,
        args.connect_timeout,
        args.detect_timeout,
        args.detect_interval,
        args.sock_buf,
        args.busy_poll_us,
    ) as sock:
        if args.clock_offset_ns is not None:
            clock_offset_ns = int(args.clock_offset_ns)
            clock_sync_path_delay_ns = 0
        elif args.clock_sync:
            clock_sync_warmup = effective_clock_sync_warmup(args.clock_sync_samples, args.clock_sync_warmup)
            if args.clock_sync_protocol == "tcp":
                clock_offset_ns, clock_sync_path_delay_ns, clock_sync_sample_rows, clock_sync_stats = estimate_clock_offset(
                    sock,
                    args.clock_sync_samples,
                    clock_sync_warmup,
                    args.clock_sync_method,
                    args.clock_sync_best_ratio,
                )
            else:
                clock_offset_ns, clock_sync_path_delay_ns, clock_sync_sample_rows, clock_sync_stats = estimate_clock_offset_udp(
                    args.repeater_host,
                    args.repeater_port,
                    args.clock_sync_samples,
                    clock_sync_warmup,
                    args.clock_sync_method,
                    args.clock_sync_best_ratio,
                    args.sock_buf,
                    args.busy_poll_us,
                    args.detect_timeout,
                    args.detect_interval,
                    args.clock_sync_kernel_timestamp,
                )
        else:
            clock_offset_ns = 0
            clock_sync_path_delay_ns = 0

        kernel_timestamp_enabled = False
        if is_udp_data:
            data_sock = connect_udp_data(
                sock,
                args.repeater_host,
                args.repeater_port,
                args.sock_buf,
                args.busy_poll_us,
                args.detect_timeout,
                args.detect_interval,
            )
            data_sock.settimeout(float(args.udp_idle_timeout))
            if args.kernel_timestamp:
                enable_kernel_timestamp_ns(data_sock)
                kernel_timestamp_enabled = True
        else:
            if args.kernel_timestamp:
                raise ValueError("--kernel-timestamp is only supported with --data-protocol udp")
            data_sock = sock

        gc_was_enabled = gc.isenabled()
        gc.disable()
        try:
            diag = args.diag
            recv_into = data_sock.recv_into
            recv_exact = recv_exact_into
            time_ns = time.time_ns
            mono_ns = time.monotonic_ns
            sleep = time.sleep
            count_interval = args.count_interval
            recvmsg_into = data_sock.recvmsg_into if kernel_timestamp_enabled else None
            cmsg_buf_size = socket.CMSG_SPACE(16) if hasattr(socket, "CMSG_SPACE") else 128
            prev_loop_ns = mono_ns() if diag else 0
            if is_udp_data:
                while True:
                    if diag:
                        loop_now_ns = mono_ns()
                        loop_gap_ns = loop_now_ns - prev_loop_ns
                        prev_loop_ns = loop_now_ns
                        pre_recv_ns = mono_ns()
                    try:
                        if kernel_timestamp_enabled:
                            got, ancdata, _flags, _addr = recvmsg_into([inbuf], cmsg_buf_size)
                            ts_recv_ns = parse_kernel_timestamp_ns(ancdata)
                            if ts_recv_ns is None:
                                ts_recv_ns = time_ns()
                                kernel_timestamp_fallback += 1
                            else:
                                kernel_timestamp_received += 1
                        else:
                            got = recv_into(inbuf)
                            ts_recv_ns = time_ns()
                    except socket.timeout:
                        break
                    if diag:
                        recv_block_ns = mono_ns() - pre_recv_ns
                    if got != UDP_MSG_SIZE:
                        continue
                    count_idx, ts_emit_ns, peer_id, correction_bits, w_swap = udp_unpack_from(inbuf)
                    if count_idx <= 0 or count_idx > count:
                        continue
                    udp_received += 1
                    if not udp_seen_counts[count_idx]:
                        udp_seen_counts[count_idx] = 1
                        udp_seen_total += 1
                    w_swap_raw = w_swap
                    last_ts_emit_ns = ts_emit_ns
                    last_peer_id = peer_id
                    last_correction_bits = correction_bits
                    last_w_swap_raw = w_swap_raw
                    last_delta = (ts_recv_ns + clock_offset_ns) - ts_emit_ns
                    if count_idx > warmup and sample_idx < sample_count:
                        raw_msg = (ts_emit_ns, peer_id, correction_bits, w_swap_raw)
                        delta_samples[sample_idx] = last_delta
                        werner_raw_samples[sample_idx] = w_swap_raw
                        sample_msgs[sample_idx] = (last_delta, count_idx, raw_msg, None)
                        delta_record_counts[sample_idx] = count_idx
                        if diag:
                            loop_gap_samples[sample_idx] = loop_gap_ns
                            recv_block_samples[sample_idx] = recv_block_ns
                        sample_idx += 1
                    if count_idx >= count:
                        break
                    if count_interval > 0:
                        sleep(count_interval)
            else:
                for i in range(count):
                    if diag:
                        loop_now_ns = mono_ns()
                        loop_gap_ns = loop_now_ns - prev_loop_ns
                        prev_loop_ns = loop_now_ns
                        pre_recv_ns = mono_ns()
                        recv_exact(data_sock, inbuf)
                        recv_block_ns = mono_ns() - pre_recv_ns
                    else:
                        recv_exact(data_sock, inbuf)
                    ts_emit_ns, peer_id, correction_bits, w_swap = tcp_unpack_from(inbuf)
                    ts_recv_ns = time_ns()
                    w_swap_raw = w_swap
                    last_ts_emit_ns = ts_emit_ns
                    last_peer_id = peer_id
                    last_correction_bits = correction_bits
                    last_w_swap_raw = w_swap_raw
                    last_delta = (ts_recv_ns + clock_offset_ns) - ts_emit_ns
                    if i >= warmup:
                        raw_msg = (ts_emit_ns, peer_id, correction_bits, w_swap_raw)
                        delta_samples[sample_idx] = last_delta
                        werner_raw_samples[sample_idx] = w_swap_raw
                        sample_msgs[sample_idx] = (last_delta, i + 1, raw_msg, None)
                        delta_record_counts[sample_idx] = i + 1
                        if diag:
                            loop_gap_samples[sample_idx] = loop_gap_ns
                            recv_block_samples[sample_idx] = recv_block_ns
                        sample_idx += 1
                    if count_interval > 0:
                        sleep(count_interval)
        finally:
            if gc_was_enabled:
                gc.enable()
            if args.data_protocol == "udp":
                data_sock.close()

    if is_udp_data:
        udp_lost_est = max(0, count - udp_seen_total)

    delta_samples = delta_samples[:sample_idx]
    werner_raw_samples = werner_raw_samples[:sample_idx]
    sample_msgs = sample_msgs[:sample_idx]
    delta_record_counts = delta_record_counts[:sample_idx]
    if args.diag:
        loop_gap_samples = loop_gap_samples[:sample_idx]
        recv_block_samples = recv_block_samples[:sample_idx]

    delay_center_ns = percentile(sorted(delta_samples), 0.50) if args.center_delay and delta_samples else 0
    delay_stat_samples = [delay_ns - delay_center_ns for delay_ns in delta_samples]
    delay_physical_samples = [max(0, delay_ns) for delay_ns in delay_stat_samples]
    clock_sync_stats.update(assess_sync_quality(clock_sync_stats, delta_samples))
    last_raw_msg = (last_ts_emit_ns, last_peer_id, last_correction_bits, last_w_swap_raw)

    werner_samples = [
        decay_werner(w_swap_raw, max(0, delay_ns), args.t1_ns) ** 2
        for w_swap_raw, delay_ns in zip(werner_raw_samples, delay_stat_samples)
    ]
    sample_msgs = [
        (
            delta_ns - delay_center_ns,
            count_idx,
            (msg[0], msg[1], msg[2], werner),
            (args.client_id, werner, msg[1]),
        )
        for (delta_ns, count_idx, msg, _), werner in zip(sample_msgs, werner_samples)
    ]
    last_stat_delta = last_delta - delay_center_ns
    last_werner = decay_werner(last_raw_msg[3], max(0, last_stat_delta), args.t1_ns) ** 2
    last_msg = (last_raw_msg[0], last_raw_msg[1], last_raw_msg[2], last_werner)
    last_state_out = (args.client_id, last_werner, last_raw_msg[1])

    delay_abs_samples = [abs(delay_ns) for delay_ns in delay_stat_samples]
    delta_sorted = sorted(delay_abs_samples)
    w_sorted = sorted(werner_samples)
    mean_delay_raw = sum(delay_abs_samples) / len(delay_abs_samples) if delay_abs_samples else 0.0
    mean_delay = int(mean_delay_raw)
    mean_werner = sum(werner_samples) / len(werner_samples) if werner_samples else 0.0
    std_delay = stddev(delay_abs_samples, mean_delay_raw)
    std_werner = stddev(werner_samples, mean_werner)

    def pick_by_delta(samples, want_max=False):
        if not samples:
            return (0, 0, (0, 0, 0, 0.0), (args.client_id, 0.0, None))
        key = lambda item: abs(item[0])
        return max(samples, key=key) if want_max else min(samples, key=key)

    min_sample = pick_by_delta(sample_msgs, want_max=False)
    max_sample = pick_by_delta(sample_msgs, want_max=True)
    abs_delay_label = "abs_centered_repeater_to_client" if args.center_delay else "abs_repeater_to_client"
    signed_delay_label = "signed_centered_repeater_to_client" if args.center_delay else "signed_repeater_to_client"

    if args.plot:
        ensure_output_dir(args.plot_dir)
        base = f"{args.plot_prefix}_{args.client_id}"
        suffix = ""
        idx = 1
        while os.path.exists(os.path.join(args.plot_dir, f"{base}{suffix}.csv")):
            idx += 1
            suffix = f"_{idx}"
        csv_path = os.path.join(args.plot_dir, f"{base}{suffix}.csv")
        with open(csv_path, "w", encoding="utf-8") as handle:
            if args.diag:
                handle.write("count_idx,delay_ns,delay_center_ns,delay_centered_ns,delay_physical_ns,clock_offset_ns,clock_sync_path_delay_ns,loop_gap_ns,recv_block_ns\n")
                for idx, delay_ns, delay_centered_ns, delay_physical_ns, loop_gap_ns, recv_block_ns in zip(
                    delta_record_counts, delta_samples, delay_stat_samples, delay_physical_samples, loop_gap_samples, recv_block_samples
                ):
                    handle.write(f"{idx},{delay_ns},{delay_center_ns},{delay_centered_ns},{delay_physical_ns},{clock_offset_ns},{clock_sync_path_delay_ns},{loop_gap_ns},{recv_block_ns}\n")
            else:
                handle.write("count_idx,delay_ns,delay_center_ns,delay_centered_ns,delay_physical_ns,clock_offset_ns,clock_sync_path_delay_ns\n")
                for idx, delay_ns, delay_centered_ns, delay_physical_ns in zip(delta_record_counts, delta_samples, delay_stat_samples, delay_physical_samples):
                    handle.write(f"{idx},{delay_ns},{delay_center_ns},{delay_centered_ns},{delay_physical_ns},{clock_offset_ns},{clock_sync_path_delay_ns}\n")
        chown_output_path(csv_path)
        print(f"plot=data_saved ({csv_path})")
        if clock_sync_sample_rows:
            clock_sync_base = f"clock_sync_client_{args.client_id}"
            clock_sync_path = os.path.join(args.plot_dir, f"{clock_sync_base}{suffix}.csv")
            with open(clock_sync_path, "w", encoding="utf-8") as handle:
                handle.write(
                    "sample_idx,t1_ns,t2_ns,t3_ns,t4_ns,"
                    "master_to_slave_ns,slave_to_master_ns,offset_ns,path_delay_ns,"
                    "t2_kernel_timestamp,t4_kernel_timestamp,"
                    "used_for_offset,used_for_best_path,clock_sync_method,"
                    "clock_sync_protocol,clock_sync_kernel_timestamp,"
                    "clock_sync_kernel_timestamp_received,clock_sync_kernel_timestamp_fallback,"
                    "clock_sync_t2_kernel_timestamp_received,clock_sync_t2_kernel_timestamp_fallback,"
                    "clock_sync_t4_kernel_timestamp_received,clock_sync_t4_kernel_timestamp_fallback,"
                    "clock_offset_final_ns,clock_offset_mean_ns,clock_offset_median_ns,"
                    "clock_offset_best_path_median_ns,clock_offset_std_ns,clock_offset_mad_ns,"
                    "clock_sync_path_delay_final_ns,clock_sync_path_delay_mean_ns,"
                    "clock_sync_path_delay_min_ns,clock_sync_path_delay_median_ns,"
                    "clock_sync_path_delay_p95_ns,clock_sync_path_delay_best_median_ns,"
                    "clock_sync_negative_or_suspicious,sync_quality\n"
                )
                for row in clock_sync_sample_rows:
                    handle.write(
                        f"{row['sample_idx']},{row['t1_ns']},{row['t2_ns']},{row['t3_ns']},{row['t4_ns']},"
                        f"{row['master_to_slave_ns']},{row['slave_to_master_ns']},{row['offset_ns']},{row['path_delay_ns']},"
                        f"{1 if row.get('t2_kernel_timestamp') else 0},{1 if row.get('t4_kernel_timestamp') else 0},"
                        f"{1 if row['used_for_offset'] else 0},{1 if row['used_for_best_path'] else 0},"
                        f"{clock_sync_stats['clock_sync_method']},"
                        f"{clock_sync_stats['clock_sync_protocol']},"
                        f"{1 if clock_sync_stats['clock_sync_kernel_timestamp'] else 0},"
                        f"{clock_sync_stats['clock_sync_kernel_timestamp_received']},"
                        f"{clock_sync_stats['clock_sync_kernel_timestamp_fallback']},"
                        f"{clock_sync_stats['clock_sync_t2_kernel_timestamp_received']},"
                        f"{clock_sync_stats['clock_sync_t2_kernel_timestamp_fallback']},"
                        f"{clock_sync_stats['clock_sync_t4_kernel_timestamp_received']},"
                        f"{clock_sync_stats['clock_sync_t4_kernel_timestamp_fallback']},"
                        f"{clock_offset_ns},{clock_sync_stats['clock_offset_mean_ns']},"
                        f"{clock_sync_stats['clock_offset_median_ns']},"
                        f"{clock_sync_stats['clock_offset_best_path_median_ns']},"
                        f"{clock_sync_stats['clock_offset_std_ns']:.6f},"
                        f"{clock_sync_stats['clock_offset_mad_ns']},"
                        f"{clock_sync_path_delay_ns},{clock_sync_stats['clock_sync_path_delay_mean_ns']},"
                        f"{clock_sync_stats['clock_sync_path_delay_min_ns']},"
                        f"{clock_sync_stats['clock_sync_path_delay_median_ns']},"
                        f"{clock_sync_stats['clock_sync_path_delay_p95_ns']},"
                        f"{clock_sync_stats['clock_sync_path_delay_best_median_ns']},"
                        f"{1 if clock_sync_stats['clock_sync_negative_or_suspicious'] else 0},"
                        f"{clock_sync_stats['sync_quality']}\n"
                    )
            chown_output_path(clock_sync_path)
            print(f"clock_sync=data_saved ({clock_sync_path})")
        if args.json_output:
            json_dir = args.json_dir or default_json_dir(args.plot_dir)
            ensure_output_dir(json_dir)
            json_path = os.path.join(json_dir, f"{base}{suffix}.json")
            json_samples = []
            for count_idx, delay_ns, delay_centered_ns, delay_physical_ns, w_swap_raw, werner, sample in zip(
                delta_record_counts,
                delta_samples,
                delay_stat_samples,
                delay_physical_samples,
                werner_raw_samples,
                werner_samples,
                sample_msgs,
            ):
                msg = sample[2] if sample else (0, 0, 0, 0.0)
                json_samples.append(
                    {
                        "count_idx": int(count_idx),
                        "delay_ns": int(delay_ns),
                        "delay_center_ns": int(delay_center_ns),
                        "delay_centered_ns": int(delay_centered_ns),
                        "delay_physical_ns": int(delay_physical_ns),
                        "clock_offset_ns": int(clock_offset_ns),
                        "clock_sync_path_delay_ns": int(clock_sync_path_delay_ns),
                        "ts_emit_ns": int(msg[0]),
                        "peer_id": int(msg[1]),
                        "correction_bits": int(msg[2]),
                        "w_swap_raw": float(w_swap_raw),
                        "werner": float(werner),
                    }
                )
            payload = {
                "role": "client",
                "argv": sys.argv,
                "args": vars(args),
                "client_id": int(args.client_id),
                "repeater_id": int(args.repeater_id),
                "exchanges": int(count),
                "warmup": int(warmup),
                "data_protocol": args.data_protocol,
                "udp_received": int(udp_received),
                "udp_lost_est": int(udp_lost_est),
                "clock_offset_ns": int(clock_offset_ns),
                "clock_sync_path_delay_ns": int(clock_sync_path_delay_ns),
                "clock_sync_warmup": int(clock_sync_warmup),
                "clock_sync": {
                    **clock_sync_stats,
                    "clock_offset_final_ns": int(clock_offset_ns),
                    "clock_sync_path_delay_final_ns": int(clock_sync_path_delay_ns),
                },
                "sync_quality": clock_sync_stats["sync_quality"],
                "kernel_timestamp": bool(kernel_timestamp_enabled),
                "kernel_timestamp_received": int(kernel_timestamp_received),
                "kernel_timestamp_fallback": int(kernel_timestamp_fallback),
                "delay_center_ns": int(delay_center_ns),
                "summary": {
                    "abs_delay_ns": {
                        "p50": int(percentile(delta_sorted, 0.50)),
                        "p90": int(percentile(delta_sorted, 0.90)),
                        "p95": int(percentile(delta_sorted, 0.95)),
                        "p99": int(percentile(delta_sorted, 0.99)),
                        "mean": int(mean_delay),
                        "std": float(std_delay),
                    },
                    "werner": {
                        "p50": float(percentile_inverse(w_sorted, 0.50)),
                        "p90": float(percentile_inverse(w_sorted, 0.90)),
                        "p95": float(percentile_inverse(w_sorted, 0.95)),
                        "p99": float(percentile_inverse(w_sorted, 0.99)),
                        "mean": float(mean_werner),
                        "std": float(std_werner),
                    },
                },
                "samples": json_samples,
            }
            with open(json_path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2)
                handle.write("\n")
            chown_output_path(json_path)
            print(f"json=data_saved ({json_path})")

    if args.quiet:
        print(f"exchanges={count}")
        print(f"warmup={warmup}")
        print(f"data_protocol={args.data_protocol}")
        print(f"kernel_timestamp={kernel_timestamp_enabled}")
        if kernel_timestamp_enabled:
            print(f"kernel_timestamp_received={kernel_timestamp_received}")
            print(f"kernel_timestamp_fallback={kernel_timestamp_fallback}")
        if args.data_protocol == "udp":
            print(f"udp_received={udp_received}")
            print(f"udp_lost_est={udp_lost_est}")
        print(f"clock_offset_ns={clock_offset_ns}")
        print(f"clock_sync_path_delay_ns={clock_sync_path_delay_ns}")
        print(f"clock_sync_warmup={clock_sync_warmup}")
        print(f"clock_sync_method={clock_sync_stats['clock_sync_method']}")
        print(f"clock_sync_protocol={clock_sync_stats['clock_sync_protocol']}")
        print(f"clock_sync_kernel_timestamp={clock_sync_stats['clock_sync_kernel_timestamp']}")
        print(f"clock_sync_kernel_timestamp_received={clock_sync_stats['clock_sync_kernel_timestamp_received']}")
        print(f"clock_sync_kernel_timestamp_fallback={clock_sync_stats['clock_sync_kernel_timestamp_fallback']}")
        print(f"clock_sync_t2_kernel_timestamp_received={clock_sync_stats['clock_sync_t2_kernel_timestamp_received']}")
        print(f"clock_sync_t2_kernel_timestamp_fallback={clock_sync_stats['clock_sync_t2_kernel_timestamp_fallback']}")
        print(f"clock_sync_t4_kernel_timestamp_received={clock_sync_stats['clock_sync_t4_kernel_timestamp_received']}")
        print(f"clock_sync_t4_kernel_timestamp_fallback={clock_sync_stats['clock_sync_t4_kernel_timestamp_fallback']}")
        print(f"sync_quality={clock_sync_stats['sync_quality']}")
        print(f"delay_center_ns={delay_center_ns}")
        print(f"state_in={fmt_state((args.client_id, args.werner_in, args.repeater_id))}")
        print_client_group("p50", percentile(delta_sorted, 0.50), percentile_inverse(w_sorted, 0.50), abs_delay_label)
        print_client_group("p90", percentile(delta_sorted, 0.90), percentile_inverse(w_sorted, 0.90), abs_delay_label)
        print_client_group("p95", percentile(delta_sorted, 0.95), percentile_inverse(w_sorted, 0.95), abs_delay_label)
        print_client_group("p99", percentile(delta_sorted, 0.99), percentile_inverse(w_sorted, 0.99), abs_delay_label)
        print_client_group("mean", mean_delay, mean_werner, abs_delay_label)
        print_client_group("std", std_delay, std_werner, abs_delay_label)
        print_client_message_state("min", min_sample[0], min_sample[2], min_sample[3], min_sample[1], signed_delay_label)
        print_client_message_state("max", max_sample[0], max_sample[2], max_sample[3], max_sample[1], signed_delay_label)
        print_client_message_state("last", last_stat_delta, last_msg, last_state_out, delay_label=signed_delay_label)
        return 0

    print("client_mode=fast3")
    print(f"exchanges={count} warmup={warmup}")
    print(f"client_id={args.client_id} repeater_id={args.repeater_id}")
    print(f"data_protocol={args.data_protocol}")
    print(f"kernel_timestamp={kernel_timestamp_enabled}")
    if kernel_timestamp_enabled:
        print(f"kernel_timestamp_received={kernel_timestamp_received}")
        print(f"kernel_timestamp_fallback={kernel_timestamp_fallback}")
    if args.data_protocol == "udp":
        print(f"udp_received={udp_received}")
        print(f"udp_lost_est={udp_lost_est}")
    print(f"clock_offset_ns={clock_offset_ns}")
    print(f"clock_sync_path_delay_ns={clock_sync_path_delay_ns}")
    print(f"clock_sync_warmup={clock_sync_warmup}")
    print(f"clock_sync_method={clock_sync_stats['clock_sync_method']}")
    print(f"clock_sync_protocol={clock_sync_stats['clock_sync_protocol']}")
    print(f"clock_sync_kernel_timestamp={clock_sync_stats['clock_sync_kernel_timestamp']}")
    print(f"clock_sync_kernel_timestamp_received={clock_sync_stats['clock_sync_kernel_timestamp_received']}")
    print(f"clock_sync_kernel_timestamp_fallback={clock_sync_stats['clock_sync_kernel_timestamp_fallback']}")
    print(f"clock_sync_t2_kernel_timestamp_received={clock_sync_stats['clock_sync_t2_kernel_timestamp_received']}")
    print(f"clock_sync_t2_kernel_timestamp_fallback={clock_sync_stats['clock_sync_t2_kernel_timestamp_fallback']}")
    print(f"clock_sync_t4_kernel_timestamp_received={clock_sync_stats['clock_sync_t4_kernel_timestamp_received']}")
    print(f"clock_sync_t4_kernel_timestamp_fallback={clock_sync_stats['clock_sync_t4_kernel_timestamp_fallback']}")
    print(f"sync_quality={clock_sync_stats['sync_quality']}")
    print(f"delay_center_ns={delay_center_ns}")
    print(f"state_in={fmt_state((args.client_id, args.werner_in, args.repeater_id))}")
    print_client_group("p50", percentile(delta_sorted, 0.50), percentile_inverse(w_sorted, 0.50), abs_delay_label)
    print_client_group("p95", percentile(delta_sorted, 0.95), percentile_inverse(w_sorted, 0.95), abs_delay_label)
    print_client_group("p90", percentile(delta_sorted, 0.90), percentile_inverse(w_sorted, 0.90), abs_delay_label)
    print_client_group("p99", percentile(delta_sorted, 0.99), percentile_inverse(w_sorted, 0.99), abs_delay_label)
    print_client_group("mean", mean_delay, mean_werner, abs_delay_label)
    print_client_group("std", std_delay, std_werner, abs_delay_label)
    print_client_message_state("min", min_sample[0], min_sample[2], min_sample[3], min_sample[1], signed_delay_label)
    print_client_message_state("max", max_sample[0], max_sample[2], max_sample[3], max_sample[1], signed_delay_label)
    print_client_message_state("last", last_stat_delta, last_msg, last_state_out, delay_label=signed_delay_label)
    return 0


def main():
    args = parse_args()
    if args.role == "repeater":
        return run_repeater(args)
    if args.role == "client":
        return run_client(args)
    raise ValueError(f"Unknown role: {args.role}")


if __name__ == "__main__":
    raise SystemExit(main())
