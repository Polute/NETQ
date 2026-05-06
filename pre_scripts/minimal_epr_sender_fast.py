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
    p = argparse.ArgumentParser(description="Sender fast4: persistent one-socket with low-jitter options.")
    p.add_argument("--receiver-host", required=True)
    p.add_argument("--receiver-port", type=int, required=True)
    p.add_argument("--count", type=int, default=1000)
    p.add_argument("--warmup", type=int, default=50)
    p.add_argument("--connect-timeout", type=float, default=10.0)
    p.add_argument("--detect-timeout", type=float, default=30.0)
    p.add_argument("--detect-interval", type=float, default=0.05)
    p.add_argument("--cpu", type=int, default=None, help="Pin this process to one CPU core.")
    p.add_argument("--rt-priority", type=int, default=None, help="Set SCHED_FIFO priority (1-99), usually needs sudo.")
    p.add_argument("--sock-buf", type=int, default=0, help="Set both SO_SNDBUF/SO_RCVBUF if > 0.")
    p.add_argument("--busy-poll-us", type=int, default=25, help="Set SO_BUSY_POLL in microseconds if supported.")
    p.add_argument("--show-arrows", action="store_true", help="Print per-arrow timing table at the end.")
    p.add_argument("--initial-werner", type=float, default=0.0)
    p.add_argument("--werner-min", type=float, default=0.2)
    p.add_argument("--t1-ns", type=float, default=1_000_000.0)
    p.add_argument("--quiet", action="store_true")
    return p.parse_args()


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


def clamp_werner(value):
    return max(0.0, min(1.0, float(value)))


def werner_from_age_ns(age_ns, werner_min, t1_ns):
    w_min = clamp_werner(werner_min)
    age_ns = max(0.0, float(age_ns))
    if t1_ns <= 0:
        return w_min
    dynamic = math.exp(-age_ns / float(t1_ns))
    return clamp_werner(w_min + (1.0 - w_min) * dynamic)


def density_matrix_from_werner_phi_plus(werner):
    w = clamp_werner(werner)
    a = 0.25 + 0.25 * w
    b = 0.25 - 0.25 * w
    c = 0.5 * w
    return [
        [a, 0.0, 0.0, c],
        [0.0, b, 0.0, 0.0],
        [0.0, 0.0, b, 0.0],
        [c, 0.0, 0.0, a],
    ]


def format_density(matrix):
    return "\n".join("[" + ", ".join(f"{value:.6f}" for value in row) + "]" for row in matrix)


def fmt_ns(v):
    return f"{int(v)} ({int(v) / 1e9:.9f} s)"


def print_arrow_table(emit_to_remote_ns, send_call_ns, round_trip_ns, label):
    remote_to_sender_ns = max(0, round_trip_ns - emit_to_remote_ns)
    print("")
    print(f"timing_arrows_{label}")
    print("segment                         ns")
    print(f"sender_to_receiver              {int(emit_to_remote_ns)}")
    print(f"sender_local_send_call          {int(send_call_ns)}")
    print(f"receiver_to_sender_back         {int(remote_to_sender_ns)}")
    print(f"total_round_trip_perf           {int(round_trip_ns)}")


def main():
    args = parse_args()
    apply_cpu_rt(args.cpu, args.rt_priority)
    count = max(1, int(args.count))
    warmup = max(0, min(int(args.warmup), count - 1))
    rtt_perf_samples = []
    emit_to_remote_samples = []
    send_call_samples = []
    werner_samples = []
    last_emit_to_remote = 0
    last_round_trip_perf = 0
    last_send_call = 0
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
            last_send_call = max(0, send_call_ns)
            last_werner = werner_from_age_ns(last_emit_to_remote, args.werner_min, args.t1_ns)
            if i >= warmup:
                rtt_perf_samples.append(last_round_trip_perf)
                emit_to_remote_samples.append(last_emit_to_remote)
                send_call_samples.append(last_send_call)
                werner_samples.append(last_werner)

    rtt = sorted(rtt_perf_samples)
    e2r = sorted(emit_to_remote_samples)
    snd = sorted(send_call_samples)
    w_sorted = sorted(werner_samples)
    local_werner = werner_from_age_ns(last_emit_to_remote, args.werner_min, args.t1_ns)
    density_after = density_matrix_from_werner_phi_plus(local_werner)
    if args.quiet:
        print(f"exchanges={count}")
        print(f"warmup={warmup}")
        print(f"round_trip_perf_p50={fmt_ns(percentile(rtt, 0.50))}")
        print(f"round_trip_perf_p95={fmt_ns(percentile(rtt, 0.95))}")
        print(f"round_trip_perf_p99={fmt_ns(percentile(rtt, 0.99))}")
        print(f"emit_to_remote_p50={fmt_ns(percentile(e2r, 0.50))}")
        print(f"emit_to_remote_p95={fmt_ns(percentile(e2r, 0.95))}")
        print(f"emit_to_remote_p99={fmt_ns(percentile(e2r, 0.99))}")
        print(f"send_call_p50={fmt_ns(percentile(snd, 0.50))}")
        print(f"send_call_p95={fmt_ns(percentile(snd, 0.95))}")
        print(f"send_call_p99={fmt_ns(percentile(snd, 0.99))}")
        print(f"sender_werner_p50={percentile(w_sorted, 0.50):.6f}")
        print(f"sender_werner_p95={percentile(w_sorted, 0.95):.6f}")
        print(f"sender_werner_p99={percentile(w_sorted, 0.99):.6f}")
        print(f"sender_werner_last={local_werner:.6f}")
        if args.show_arrows:
            print_arrow_table(
                percentile(e2r, 0.50),
                percentile(snd, 0.50),
                percentile(rtt, 0.50),
                "p50",
            )
            print_arrow_table(last_emit_to_remote, last_send_call, last_round_trip_perf, "last")
        return 0

    print("sender_mode=fast4")
    print(f"exchanges={count} warmup={warmup}")
    print(f"round_trip_perf_p50={fmt_ns(percentile(rtt, 0.50))}")
    print(f"round_trip_perf_p95={fmt_ns(percentile(rtt, 0.95))}")
    print(f"round_trip_perf_p99={fmt_ns(percentile(rtt, 0.99))}")
    print(f"emit_to_remote_p50={fmt_ns(percentile(e2r, 0.50))}")
    print(f"emit_to_remote_p95={fmt_ns(percentile(e2r, 0.95))}")
    print(f"emit_to_remote_p99={fmt_ns(percentile(e2r, 0.99))}")
    print(f"send_call_p50={fmt_ns(percentile(snd, 0.50))}")
    print(f"send_call_p95={fmt_ns(percentile(snd, 0.95))}")
    print(f"send_call_p99={fmt_ns(percentile(snd, 0.99))}")
    print(f"sender_werner_p50={percentile(w_sorted, 0.50):.6f}")
    print(f"sender_werner_p95={percentile(w_sorted, 0.95):.6f}")
    print(f"sender_werner_p99={percentile(w_sorted, 0.99):.6f}")
    print(f"sender_werner_last={local_werner:.6f}")
    print("sender_density_after:")
    print(format_density(density_after))
    if args.show_arrows:
        print_arrow_table(
            percentile(e2r, 0.50),
            percentile(snd, 0.50),
            percentile(rtt, 0.50),
            "p50",
        )
        print_arrow_table(last_emit_to_remote, last_send_call, last_round_trip_perf, "last")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
