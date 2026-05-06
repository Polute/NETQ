#!/usr/bin/env python3
import argparse
import math
import os
import socket
import struct
import time

TS_FORMAT = "!Q"
TS_SIZE = struct.calcsize(TS_FORMAT)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Fast4 unified: sender/receiver with persistent one-socket and low-jitter options."
    )
    subparsers = parser.add_subparsers(dest="role", required=True)

    sender = subparsers.add_parser("sender", help="Run in sender mode.")
    sender.add_argument("--receiver-host", required=True)
    sender.add_argument("--receiver-port", type=int, required=True)
    sender.add_argument("--count", type=int, default=1000)
    sender.add_argument("--warmup", type=int, default=50)
    sender.add_argument("--connect-timeout", type=float, default=10.0)
    sender.add_argument("--detect-timeout", type=float, default=30.0)
    sender.add_argument("--detect-interval", type=float, default=0.05)
    sender.add_argument("--cpu", type=int, default=None, help="Pin this process to one CPU core.")
    sender.add_argument("--rt-priority", type=int, default=None, help="Set SCHED_FIFO priority (1-99), usually needs sudo.")
    sender.add_argument("--sock-buf", type=int, default=0, help="Set both SO_SNDBUF/SO_RCVBUF if > 0.")
    sender.add_argument("--busy-poll-us", type=int, default=25, help="Set SO_BUSY_POLL in microseconds if supported.")
    sender.add_argument("--show-arrows", action="store_true", help="Print per-arrow timing table at the end.")
    sender.add_argument("--werner-min", type=float, default=0.2)
    sender.add_argument("--t1-ns", type=float, default=1_000_000.0)
    sender.add_argument("--quiet", action="store_true")

    receiver = subparsers.add_parser("receiver", help="Run in receiver mode.")
    receiver.add_argument("--listen-host", default="0.0.0.0")
    receiver.add_argument("--listen-port", type=int, default=7401)
    receiver.add_argument("--count", type=int, default=1000)
    receiver.add_argument("--warmup", type=int, default=50)
    receiver.add_argument("--accept-timeout", type=float, default=30.0)
    receiver.add_argument("--cpu", type=int, default=None, help="Pin this process to one CPU core.")
    receiver.add_argument("--rt-priority", type=int, default=None, help="Set SCHED_FIFO priority (1-99), usually needs sudo.")
    receiver.add_argument("--sock-buf", type=int, default=0, help="Set both SO_SNDBUF/SO_RCVBUF if > 0.")
    receiver.add_argument("--busy-poll-us", type=int, default=25, help="Set SO_BUSY_POLL in microseconds if supported.")
    receiver.add_argument("--werner-min", type=float, default=0.2)
    receiver.add_argument("--t1-ns", type=float, default=1_000_000.0)
    receiver.add_argument("--show-arrows", action="store_true", help="Print receiver timing table at the end.")
    receiver.add_argument("--quiet", action="store_true")

    return parser.parse_args()


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
            raise ConnectionError("Socket closed before full timestamp")
        n += got


def connect_receiver_until_ready(host, port, connect_timeout, detect_timeout, detect_interval, sock_buf, busy_poll_us):
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
    raise TimeoutError("Receiver was not detected before detect-timeout expired") from last_error


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


def clamp_werner(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(value):
        return 0.0
    return max(0.0, min(1.0, value))


def werner_from_age_ns(age_ns, werner_min, t1_ns):
    w_min = clamp_werner(werner_min)
    age_ns = max(0.0, float(age_ns))
    if t1_ns <= 0:
        return w_min
    dynamic = math.exp(-age_ns / float(t1_ns))
    return clamp_werner(w_min + (1.0 - w_min) * dynamic)


def fmt_ns(v):
    return f"{int(v)} ({int(v) / 1e9:.9f} s)"


def print_sender_total_table(round_trip_ns, label):
    print("")
    print(f"timing_arrows_{label}")
    print("segment                         ns")
    print(f"total_round_trip_perf           {int(round_trip_ns)}")


def print_receiver_table(sender_to_receiver_ns, recv_to_ack_ns, label):
    total_view_ns = max(0, sender_to_receiver_ns + recv_to_ack_ns)
    print("")
    print(f"receiver_timing_{label}")
    print("segment                         ns")
    print(f"sender_to_receiver              {fmt_ns(sender_to_receiver_ns)}")
    print(f"receiver_to_ack_send            {fmt_ns(recv_to_ack_ns)}")
    print(f"total_receiver_view             {fmt_ns(total_view_ns)}")


def print_receiver_group(label, sender_to_receiver_ns, recv_to_ack_ns, total_view_ns, werner):
    print("")
    print(f"receiver_{label}")
    print("segment                         ns (s)")
    print(f"sender_to_receiver              {fmt_ns(sender_to_receiver_ns)}")
    print(f"receiver_to_ack_send            {fmt_ns(recv_to_ack_ns)}")
    print(f"total_receiver_view             {fmt_ns(total_view_ns)}")
    print(f"werner                           {werner:.6f}")
    print("")


def print_sender_group(label, round_trip_ns, emit_to_remote_ns, werner):
    print("")
    print(f"sender_{label}")
    print("segment                         ns (s)")
    print(f"t_recv_ns                      {fmt_ns(emit_to_remote_ns)}")
    print(f"total_round_trip_perf           {fmt_ns(round_trip_ns)}")
    print(f"werner                           {werner:.6f}")
    print("")


def run_sender(args):
    apply_cpu_rt(args.cpu, args.rt_priority)
    count = max(1, int(args.count))
    warmup = max(0, min(int(args.warmup), count - 1))
    rtt_perf_samples = []
    emit_to_remote_samples = []
    werner_samples = []
    sample_tuples = []
    last_emit_to_remote = 0
    last_round_trip_perf = 0
    outbuf = bytearray(TS_SIZE)
    inbuf = bytearray(TS_SIZE)

    with connect_receiver_until_ready(
        args.receiver_host,
        args.receiver_port,
        args.connect_timeout,
        args.detect_timeout,
        args.detect_interval,
        args.sock_buf,
        args.busy_poll_us,
    ) as sock:
        for i in range(count):
            ts_emit_ns = time.time_ns()
            struct.pack_into(TS_FORMAT, outbuf, 0, ts_emit_ns)
            t_rtt0 = time.perf_counter_ns()
            t_send0 = time.perf_counter_ns()
            sock.sendall(outbuf)
            send_call_ns = time.perf_counter_ns() - t_send0
            recv_exact_into(sock, inbuf)
            t_rtt1 = time.perf_counter_ns()
            ts_remote_update_ns = struct.unpack(TS_FORMAT, inbuf)[0]
            last_emit_to_remote = max(0, ts_remote_update_ns - ts_emit_ns)
            last_round_trip_perf = max(0, t_rtt1 - t_rtt0)
            last_werner = werner_from_age_ns(last_emit_to_remote, args.werner_min, args.t1_ns)
            if i >= warmup:
                rtt_perf_samples.append(last_round_trip_perf)
                emit_to_remote_samples.append(last_emit_to_remote)
                werner_samples.append(last_werner)
                sample_tuples.append(
                    (last_round_trip_perf, last_emit_to_remote, last_werner)
                )

    rtt = sorted(rtt_perf_samples)
    e2r = sorted(emit_to_remote_samples)
    w_sorted = sorted(werner_samples)
    local_werner = werner_from_age_ns(last_emit_to_remote, args.werner_min, args.t1_ns)
    if sample_tuples:
        min_s2r_sample = min(sample_tuples, key=lambda x: x[1])
        max_werner_sample = max(sample_tuples, key=lambda x: x[2])
    else:
        min_s2r_sample = (0, 0, 0.0)
        max_werner_sample = (0, 0, 0.0)

    if args.quiet:
        print(f"exchanges={count}")
        print(f"warmup={warmup}")
        print_sender_group(
            "p50",
            percentile(rtt, 0.50),
            percentile(e2r, 0.50),
            percentile_inverse(w_sorted, 0.50),
        )
        print_sender_group(
            "p95",
            percentile(rtt, 0.95),
            percentile(e2r, 0.95),
            percentile_inverse(w_sorted, 0.95),
        )
        print_sender_group(
            "p99",
            percentile(rtt, 0.99),
            percentile(e2r, 0.99),
            percentile_inverse(w_sorted, 0.99),
        )
        print_sender_group(
            "min",
            min(rtt) if rtt else 0,
            min(e2r) if e2r else 0,
            max_werner_sample[2],
        )
        print_sender_group(
            "max",
            max(rtt) if rtt else 0,
            max(e2r) if e2r else 0,
            min(w_sorted) if w_sorted else 0.0,
        )
        print_sender_group("last", last_round_trip_perf, last_emit_to_remote, local_werner)
        print(f"sender_werner_max={max_werner_sample[2]:.6f}")
        # Timing arrows omitted to avoid redundant output with sender_* groups.
        return 0

    print("sender_mode=fast4")
    print(f"exchanges={count} warmup={warmup}")
    print_sender_group(
        "p50",
        percentile(rtt, 0.50),
        percentile(e2r, 0.50),
        percentile_inverse(w_sorted, 0.50),
    )
    print_sender_group(
        "p95",
        percentile(rtt, 0.95),
        percentile(e2r, 0.95),
        percentile_inverse(w_sorted, 0.95),
    )
    print_sender_group(
        "p99",
        percentile(rtt, 0.99),
        percentile(e2r, 0.99),
        percentile_inverse(w_sorted, 0.99),
    )
    print_sender_group(
        "min",
        min(rtt) if rtt else 0,
        min(e2r) if e2r else 0,
        max_werner_sample[2],
    )
    print_sender_group(
        "max",
        max(rtt) if rtt else 0,
        max(e2r) if e2r else 0,
        min(w_sorted) if w_sorted else 0.0,
    )
    print_sender_group("last", last_round_trip_perf, last_emit_to_remote, local_werner)
    print(f"sender_werner_max={max_werner_sample[2]:.6f}")
    # Timing arrows omitted to avoid redundant output with sender_* groups.
    return 0


def run_receiver(args):
    apply_cpu_rt(args.cpu, args.rt_priority)
    count = max(1, int(args.count))
    warmup = max(0, min(int(args.warmup), count - 1))
    recv_to_ack_samples = []
    total_view_samples = []
    sender_to_receiver_samples = []
    werner_samples = []
    sample_tuples = []
    last_sender_to_receiver = 0
    inbuf = bytearray(TS_SIZE)
    outbuf = bytearray(TS_SIZE)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        enable_low_latency_socket(server, args.sock_buf, args.busy_poll_us)
        server.bind((args.listen_host, args.listen_port))
        server.listen(1)
        server.settimeout(args.accept_timeout)
        conn, _ = server.accept()
        with conn:
            enable_low_latency_socket(conn, args.sock_buf, args.busy_poll_us)
            for i in range(count):
                recv_exact_into(conn, inbuf)
                ts_emit_ns = struct.unpack(TS_FORMAT, inbuf)[0]
                ts_recv_ns = time.time_ns()
                struct.pack_into(TS_FORMAT, outbuf, 0, ts_recv_ns)
                ts_ack_sent_ns = time.time_ns()
                t0 = time.perf_counter_ns()
                conn.sendall(outbuf)
                _ack_send_ns = time.perf_counter_ns() - t0
                last_sender_to_receiver = max(0, ts_recv_ns - ts_emit_ns)
                last_recv_to_ack = max(0, ts_ack_sent_ns - ts_recv_ns)
                last_werner = werner_from_age_ns(last_sender_to_receiver, args.werner_min, args.t1_ns)
                if i >= warmup:
                    sender_to_receiver_samples.append(last_sender_to_receiver)
                    recv_to_ack_samples.append(last_recv_to_ack)
                    total_view_samples.append(last_sender_to_receiver + last_recv_to_ack)
                    werner_samples.append(last_werner)
                    sample_tuples.append((last_sender_to_receiver, last_recv_to_ack, last_werner))

    s2r = sorted(sender_to_receiver_samples)
    r2a = sorted(recv_to_ack_samples)
    total_view = sorted(total_view_samples)
    w_sorted = sorted(werner_samples)
    local_werner = werner_from_age_ns(last_sender_to_receiver, args.werner_min, args.t1_ns)
    if sample_tuples:
        max_werner_sample = max(sample_tuples, key=lambda x: x[2])
    else:
        max_werner_sample = (0, 0, 0.0)

    last_total_view = max(0, last_sender_to_receiver + last_recv_to_ack)
    s2r_min = min(s2r) if s2r else 0
    r2a_min = min(r2a) if r2a else 0
    total_view_min = min(total_view) if total_view else 0

    if args.quiet:
        print(f"exchanges={count}")
        print(f"warmup={warmup}")
        print_receiver_group(
            "p50",
            percentile(s2r, 0.50),
            percentile(r2a, 0.50),
            percentile(total_view, 0.50),
            percentile_inverse(w_sorted, 0.50),
        )
        print_receiver_group(
            "p95",
            percentile(s2r, 0.95),
            percentile(r2a, 0.95),
            percentile(total_view, 0.95),
            percentile_inverse(w_sorted, 0.95),
        )
        print_receiver_group(
            "p99",
            percentile(s2r, 0.99),
            percentile(r2a, 0.99),
            percentile(total_view, 0.99),
            percentile_inverse(w_sorted, 0.99),
        )
        print_receiver_group(
            "min",
            s2r_min,
            r2a_min,
            total_view_min,
            max_werner_sample[2],
        )
        print_receiver_group(
            "max",
            max(s2r) if s2r else 0,
            max(r2a) if r2a else 0,
            max(total_view) if total_view else 0,
            min(w_sorted) if w_sorted else 0.0,
        )
        print_receiver_group(
            "last",
            last_sender_to_receiver,
            last_recv_to_ack,
            last_total_view,
            local_werner,
        )
        print(f"receiver_werner_max={max_werner_sample[2]:.6f}")
        return 0

    print("receiver_mode=fast4")
    print(f"exchanges={count} warmup={warmup}")
    print_receiver_group(
        "p50",
        percentile(s2r, 0.50),
        percentile(r2a, 0.50),
        percentile(total_view, 0.50),
        percentile_inverse(w_sorted, 0.50),
    )
    print_receiver_group(
        "p95",
        percentile(s2r, 0.95),
        percentile(r2a, 0.95),
        percentile(total_view, 0.95),
        percentile_inverse(w_sorted, 0.95),
    )
    print_receiver_group(
        "p99",
        percentile(s2r, 0.99),
        percentile(r2a, 0.99),
        percentile(total_view, 0.99),
        percentile_inverse(w_sorted, 0.99),
    )
    print_receiver_group(
        "min",
        s2r_min,
        r2a_min,
        total_view_min,
        max_werner_sample[2],
    )
    print_receiver_group(
        "max",
        max(s2r) if s2r else 0,
        max(r2a) if r2a else 0,
        max(total_view) if total_view else 0,
        min(w_sorted) if w_sorted else 0.0,
    )
    print_receiver_group(
        "last",
        last_sender_to_receiver,
        last_recv_to_ack,
        last_total_view,
        local_werner,
    )
    print(f"receiver_werner_max={max_werner_sample[2]:.6f}")
    return 0


def main():
    args = parse_args()
    if args.role == "sender":
        return run_sender(args)
    if args.role == "receiver":
        return run_receiver(args)
    raise ValueError(f"Unknown role: {args.role}")


if __name__ == "__main__":
    raise SystemExit(main())
