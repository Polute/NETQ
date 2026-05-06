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
    p = argparse.ArgumentParser(description="Receiver fast4: persistent one-socket with low-jitter options.")
    p.add_argument("--listen-host", default="0.0.0.0")
    p.add_argument("--listen-port", type=int, default=7401)
    p.add_argument("--count", type=int, default=1000)
    p.add_argument("--warmup", type=int, default=50)
    p.add_argument("--accept-timeout", type=float, default=30.0)
    p.add_argument("--cpu", type=int, default=None, help="Pin this process to one CPU core.")
    p.add_argument("--rt-priority", type=int, default=None, help="Set SCHED_FIFO priority (1-99), usually needs sudo.")
    p.add_argument("--sock-buf", type=int, default=0, help="Set both SO_SNDBUF/SO_RCVBUF if > 0.")
    p.add_argument("--busy-poll-us", type=int, default=25, help="Set SO_BUSY_POLL in microseconds if supported.")
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


def main():
    args = parse_args()
    apply_cpu_rt(args.cpu, args.rt_priority)
    count = max(1, int(args.count))
    warmup = max(0, min(int(args.warmup), count - 1))
    ack_send_samples = []
    sender_to_receiver_samples = []
    werner_samples = []
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
                t0 = time.perf_counter_ns()
                conn.sendall(outbuf)
                ack_send_ns = time.perf_counter_ns() - t0
                if i >= warmup:
                    last_sender_to_receiver = max(0, ts_recv_ns - ts_emit_ns)
                    sender_to_receiver_samples.append(last_sender_to_receiver)
                    ack_send_samples.append(max(0, ack_send_ns))
                    werner_samples.append(werner_from_age_ns(last_sender_to_receiver, args.werner_min, args.t1_ns))
                else:
                    last_sender_to_receiver = max(0, ts_recv_ns - ts_emit_ns)

    s2r = sorted(sender_to_receiver_samples)
    ack = sorted(ack_send_samples)
    w_sorted = sorted(werner_samples)
    local_werner = werner_from_age_ns(last_sender_to_receiver, args.werner_min, args.t1_ns)
    density_after = density_matrix_from_werner_phi_plus(local_werner)
    if args.quiet:
        print(f"exchanges={count}")
        print(f"warmup={warmup}")
        print(f"sender_to_receiver_p50={fmt_ns(percentile(s2r, 0.50))}")
        print(f"sender_to_receiver_p95={fmt_ns(percentile(s2r, 0.95))}")
        print(f"sender_to_receiver_p99={fmt_ns(percentile(s2r, 0.99))}")
        print(f"ack_send_call_p50={fmt_ns(percentile(ack, 0.50))}")
        print(f"ack_send_call_p95={fmt_ns(percentile(ack, 0.95))}")
        print(f"ack_send_call_p99={fmt_ns(percentile(ack, 0.99))}")
        print(f"receiver_werner_p50={percentile(w_sorted, 0.50):.6f}")
        print(f"receiver_werner_p95={percentile(w_sorted, 0.95):.6f}")
        print(f"receiver_werner_p99={percentile(w_sorted, 0.99):.6f}")
        print(f"receiver_werner_last={local_werner:.6f}")
        return 0

    print("receiver_mode=fast4")
    print(f"exchanges={count} warmup={warmup}")
    print(f"sender_to_receiver_p50={fmt_ns(percentile(s2r, 0.50))}")
    print(f"sender_to_receiver_p95={fmt_ns(percentile(s2r, 0.95))}")
    print(f"sender_to_receiver_p99={fmt_ns(percentile(s2r, 0.99))}")
    print(f"ack_send_call_p50={fmt_ns(percentile(ack, 0.50))}")
    print(f"ack_send_call_p95={fmt_ns(percentile(ack, 0.95))}")
    print(f"ack_send_call_p99={fmt_ns(percentile(ack, 0.99))}")
    print(f"receiver_werner_p50={percentile(w_sorted, 0.50):.6f}")
    print(f"receiver_werner_p95={percentile(w_sorted, 0.95):.6f}")
    print(f"receiver_werner_p99={percentile(w_sorted, 0.99):.6f}")
    print(f"receiver_werner_last={local_werner:.6f}")
    print("receiver_density_after:")
    print(format_density(density_after))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
