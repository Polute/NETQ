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
    p = argparse.ArgumentParser(description="Sender fast4 remote: same metrics, explicit remote-ready args.")
    p.add_argument("--receiver-ip", required=True, help="Receiver machine IP.")
    p.add_argument("--receiver-port", type=int, required=True)
    p.add_argument("--local-ip", default="", help="Optional local source IP to bind before connect.")
    p.add_argument("--count", type=int, default=1000)
    p.add_argument("--warmup", type=int, default=100)
    p.add_argument("--connect-timeout", type=float, default=10.0)
    p.add_argument("--detect-timeout", type=float, default=30.0)
    p.add_argument("--detect-interval", type=float, default=0.05)
    p.add_argument("--cpu", type=int, default=None)
    p.add_argument("--rt-priority", type=int, default=None)
    p.add_argument("--busy-poll-us", type=int, default=25)
    p.add_argument("--initial-werner", type=float, default=0.0)
    p.add_argument("--werner-min", type=float, default=0.2)
    p.add_argument("--t1-ns", type=float, default=1_000_000.0)
    p.add_argument("--show-arrows", action="store_true")
    p.add_argument("--quiet", action="store_true")
    return p.parse_args()


def enable_low_latency_socket(sock, busy_poll_us=0):
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass
    if hasattr(socket, "TCP_QUICKACK"):
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_QUICKACK, 1)
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
        os.sched_setscheduler(0, os.SCHED_FIFO, os.sched_param(int(rt_priority)))


def recv_exact_into(sock, buf):
    view = memoryview(buf)
    n = 0
    while n < len(buf):
        got = sock.recv_into(view[n:])
        if got <= 0:
            raise ConnectionError("Socket closed before full timestamp")
        n += got


def connect_receiver_until_ready(receiver_ip, receiver_port, local_ip, connect_timeout, detect_timeout, detect_interval, busy_poll_us):
    deadline = time.monotonic() + max(0.0, detect_timeout)
    last_error = None
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        timeout = min(max(0.001, float(connect_timeout)), max(0.001, remaining))
        try:
            if local_ip:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(timeout)
                sock.bind((local_ip, 0))
                sock.connect((receiver_ip, receiver_port))
            else:
                sock = socket.create_connection((receiver_ip, receiver_port), timeout=timeout)
            enable_low_latency_socket(sock, busy_poll_us)
            return sock
        except OSError as exc:
            last_error = exc
        time.sleep(min(max(0.0, detect_interval), max(0.0, deadline - time.monotonic())))
    raise TimeoutError("Receiver was not detected before detect-timeout expired") from last_error


def percentile(sorted_vals, p):
    if not sorted_vals:
        return 0
    return sorted_vals[int((len(sorted_vals) - 1) * p)]


def clamp_werner(value):
    return max(0.0, min(1.0, float(value)))


def werner_from_age_ns(age_ns, werner_min, t1_ns):
    w_min = clamp_werner(werner_min)
    age_ns = max(0.0, float(age_ns))
    if t1_ns <= 0:
        return w_min
    dynamic = math.exp(-age_ns / float(t1_ns))
    return clamp_werner(w_min + (1.0 - w_min) * dynamic)


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
    rtt_samples = []
    e2r_samples = []
    send_samples = []
    w_samples = []
    last_e2r = 0
    last_rtt = 0
    last_send = 0
    outbuf = bytearray(TS_SIZE)
    inbuf = bytearray(TS_SIZE)

    with connect_receiver_until_ready(
        args.receiver_ip,
        args.receiver_port,
        args.local_ip,
        args.connect_timeout,
        args.detect_timeout,
        args.detect_interval,
        args.busy_poll_us,
    ) as sock:
        for i in range(count):
            ts_emit_ns = time.time_ns()
            struct.pack_into(TS_FORMAT, outbuf, 0, ts_emit_ns)
            t0 = time.perf_counter_ns()
            t_send0 = time.perf_counter_ns()
            sock.sendall(outbuf)
            t_send1 = time.perf_counter_ns()
            recv_exact_into(sock, inbuf)
            t1 = time.perf_counter_ns()
            ts_remote_update_ns = struct.unpack(TS_FORMAT, inbuf)[0]
            last_e2r = max(0, ts_remote_update_ns - ts_emit_ns)
            last_rtt = max(0, t1 - t0)
            last_send = max(0, t_send1 - t_send0)
            last_w = werner_from_age_ns(last_e2r, args.werner_min, args.t1_ns)
            if i >= warmup:
                rtt_samples.append(last_rtt)
                e2r_samples.append(last_e2r)
                send_samples.append(last_send)
                w_samples.append(last_w)

    rtt = sorted(rtt_samples)
    e2r = sorted(e2r_samples)
    snd = sorted(send_samples)
    w = sorted(w_samples)
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
    print(f"sender_werner_p50={percentile(w, 0.50):.6f}")
    print(f"sender_werner_p95={percentile(w, 0.95):.6f}")
    print(f"sender_werner_p99={percentile(w, 0.99):.6f}")
    print(f"sender_werner_last={last_w:.6f}")
    if args.show_arrows:
        print_arrow_table(percentile(e2r, 0.50), percentile(snd, 0.50), percentile(rtt, 0.50), "p50")
        print_arrow_table(last_e2r, last_send, last_rtt, "last")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
