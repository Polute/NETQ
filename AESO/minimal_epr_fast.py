#!/usr/bin/env python3
import argparse
import math
import os
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
    repeater.add_argument("--count", type=int, default=1000)
    repeater.add_argument("--accept-timeout", type=float, default=30.0)
    repeater.add_argument("--cpu", type=int, default=None, help="Pin this process to one CPU core.")
    repeater.add_argument("--rt-priority", type=int, default=50, help="Set SCHED_FIFO priority (1-99), usually needs sudo.")
    repeater.add_argument("--sock-buf", type=int, default=4096, help="Set both SO_SNDBUF/SO_RCVBUF if > 0.")
    repeater.add_argument("--busy-poll-us", type=int, default=25, help="Set SO_BUSY_POLL in microseconds if supported.")
    repeater.add_argument("--repeater-id", type=int, default=0)
    repeater.add_argument("--client-a-id", type=int, default=1)
    repeater.add_argument("--client-b-id", type=int, default=2)
    repeater.add_argument("--werner-ar", type=float, default=None)
    repeater.add_argument("--werner-br", type=float, default=None)
    repeater.add_argument("--t1-ns", type=float, default=1_000_000.0)
    repeater.add_argument("--parallel", action="store_true", help="Send to A/B in parallel threads.")
    repeater.add_argument("--cpu-a", type=int, default=2, help="Pin sender thread A to this CPU core.")
    repeater.add_argument("--cpu-b", type=int, default=3, help="Pin sender thread B to this CPU core.")
    repeater.add_argument("--count-interval", type=float, default=0.0, help="Sleep seconds between counts.")
    repeater.add_argument("--quiet", action="store_true")

    client = subparsers.add_parser("client", help="Run in client mode.")
    client.add_argument("--repeater-host", default="127.0.0.1")
    client.add_argument("--repeater-port", type=int, default=7401)
    client.add_argument("--count", type=int, default=1000)
    client.add_argument("--warmup", type=int, default=50)
    client.add_argument("--connect-timeout", type=float, default=10.0)
    client.add_argument("--detect-timeout", type=float, default=30.0)
    client.add_argument("--detect-interval", type=float, default=0.05)
    client.add_argument("--cpu", type=int, default=5, help="Pin this process to one CPU core.")
    client.add_argument("--rt-priority", type=int, default=50, help="Set SCHED_FIFO priority (1-99), usually needs sudo.")
    client.add_argument("--sock-buf", type=int, default=4096, help="Set both SO_SNDBUF/SO_RCVBUF if > 0.")
    client.add_argument("--busy-poll-us", type=int, default=25, help="Set SO_BUSY_POLL in microseconds if supported.")
    client.add_argument("--client-id", type=int, default=1)
    client.add_argument("--repeater-id", type=int, default=0)
    client.add_argument("--werner-in", type=float, default=1)
    client.add_argument("--plot", action="store_true", help="Write delay histogram data and plot if matplotlib is available.")
    client.add_argument("--plot-prefix", default="delay_hist_client", help="Prefix for plot outputs.")
    client.add_argument("--count-interval", type=float, default=0.0, help="Sleep seconds between counts.")
    client.add_argument("--quiet", action="store_true")

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
            raise ConnectionError("Socket closed before full message")
        n += got


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


def decay_werner(base, age_ns, t1_ns):
    if t1_ns <= 0:
        return clamp_werner(base)
    age_ns = max(0.0, float(age_ns))
    decayed = float(base) * math.exp(-age_ns / float(t1_ns))
    return clamp_werner(decayed)


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


def print_client_group(label, delta_ns, werner):
    print("")
    print(f"client_{label}")
    print("metric\t\t\t\t value")
    print(f"repeater_to_client\t\t {fmt_ns(delta_ns)}")
    print(f"werner\t\t\t\t {werner:.6f}")


def print_client_message_state(label, delta_ns, msg, state_out, count_idx=None):
    print("")
    print(f"client_{label}")
    print("metric\t\t\t\t value")
    print(f"repeater_to_client\t\t {fmt_ns(delta_ns)}")
    if count_idx is not None:
        print(f"count_idx\t\t\t {count_idx}")
    print(
        "msg=(ts_emit_ns={}, ts_emit={}, peer_id={}, bits={:02b}, w_swap={:.6f})".format(
            msg[0], fmt_ts_emit(msg[0]), msg[1], msg[2], msg[3]
        )
    )
    print(f"state_out=({state_out[0]:.6f},{state_out[1]})")


def run_repeater(args):
    apply_cpu_rt(args.cpu, args.rt_priority)
    count = max(1, int(args.count))
    if args.werner_ar is None:
        args.werner_ar = float(input("werner_ar: ").strip())
    if args.werner_br is None:
        args.werner_br = float(input("werner_br: ").strip())
    w_ar_init = clamp_werner(args.werner_ar)
    w_br_init = clamp_werner(args.werner_br)
    state_ar = (w_ar_init, args.client_a_id)
    state_br = (w_br_init, args.client_b_id)
    last_ar_ns = time.monotonic_ns()
    last_br_ns = time.monotonic_ns()
    outbuf = bytearray(MSG_SIZE)
    outbuf_a = bytearray(MSG_SIZE)
    outbuf_b = bytearray(MSG_SIZE)
    last_state_in_ar = (w_ar_init, args.client_a_id)
    last_state_in_br = (w_br_init, args.client_b_id)
    last_msg_a = (0, 0, 0, 0.0)
    last_msg_b = (0, 0, 0, 0.0)

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

    def update_round_state(now_ns):
        w_ar = decay_werner(w_ar_init, now_ns - last_ar_ns, args.t1_ns)
        w_br = decay_werner(w_br_init, now_ns - last_br_ns, args.t1_ns)
        state_ar_local = (w_ar, args.client_a_id)
        state_br_local = (w_br, args.client_b_id)
        ts_emit_ns_local = time.time_ns()
        correction_bits_local = random.randint(0, 3)
        w_swap_local = clamp_werner(state_ar_local[0] * state_br_local[0])
        return state_ar_local, state_br_local, ts_emit_ns_local, correction_bits_local, w_swap_local

    if args.parallel:
        barrier_ready = threading.Barrier(3)
        barrier_done = threading.Barrier(3)

        def sender_thread(conn, buffer_ref, cpu_pin):
            set_thread_affinity(cpu_pin)
            for _ in range(count):
                barrier_ready.wait()
                conn.sendall(buffer_ref)
                barrier_done.wait()

        with conn_a, conn_b:
            t_a = threading.Thread(target=sender_thread, args=(conn_a, outbuf_a, args.cpu_a), daemon=True)
            t_b = threading.Thread(target=sender_thread, args=(conn_b, outbuf_b, args.cpu_b), daemon=True)
            t_a.start()
            t_b.start()
            for _ in range(count):
                now_ns = time.monotonic_ns()
                state_ar, state_br, ts_emit_ns, correction_bits, w_swap = update_round_state(now_ns)
                last_state_in_ar = state_ar
                last_state_in_br = state_br
                last_ar_ns = now_ns
                last_br_ns = now_ns
                struct.pack_into(MSG_FORMAT, outbuf_a, 0, ts_emit_ns, args.client_b_id, correction_bits, w_swap)
                struct.pack_into(MSG_FORMAT, outbuf_b, 0, ts_emit_ns, args.client_a_id, correction_bits, w_swap)
                last_msg_a = (ts_emit_ns, args.client_b_id, correction_bits, w_swap)
                last_msg_b = (ts_emit_ns, args.client_a_id, correction_bits, w_swap)
                barrier_ready.wait()
                barrier_done.wait()
                if args.count_interval > 0:
                    time.sleep(args.count_interval)
            t_a.join()
            t_b.join()
    else:
        with conn_a, conn_b:
            for _ in range(count):
                now_ns = time.monotonic_ns()
                state_ar, state_br, ts_emit_ns, correction_bits, w_swap = update_round_state(now_ns)
                last_state_in_ar = state_ar
                last_state_in_br = state_br
                last_ar_ns = now_ns
                last_br_ns = now_ns
                struct.pack_into(MSG_FORMAT, outbuf, 0, ts_emit_ns, args.client_b_id, correction_bits, w_swap)
                conn_a.sendall(outbuf)
                last_msg_a = (ts_emit_ns, args.client_b_id, correction_bits, w_swap)
                struct.pack_into(MSG_FORMAT, outbuf, 0, ts_emit_ns, args.client_a_id, correction_bits, w_swap)
                conn_b.sendall(outbuf)
                last_msg_b = (ts_emit_ns, args.client_a_id, correction_bits, w_swap)
                if args.count_interval > 0:
                    time.sleep(args.count_interval)

    if not args.quiet:
        print("repeater_mode=fast3")
        print(f"exchanges={count}")
        print(f"repeater_id={args.repeater_id}")
    print("")
    print(f"state_ar_start=({w_ar_init:.6f},{args.client_a_id})")
    print(f"state_br_start=({w_br_init:.6f},{args.client_b_id})")
    print("")
    print("repeater_last")
    print("msg_a=(ts_emit_ns={}, ts_emit={}, peer_id={}, bits={:02b}, w_swap={:.6f})".format(
        last_msg_a[0], fmt_ts_emit(last_msg_a[0]), last_msg_a[1], last_msg_a[2], last_msg_a[3]
    ))
    print("msg_b=(ts_emit_ns={}, ts_emit={}, peer_id={}, bits={:02b}, w_swap={:.6f})".format(
        last_msg_b[0], fmt_ts_emit(last_msg_b[0]), last_msg_b[1], last_msg_b[2], last_msg_b[3]
    ))
    print("")
    print(f"state_in_ar=({last_state_in_ar[0]:.6f},{last_state_in_ar[1]})")
    print(f"state_in_br=({last_state_in_br[0]:.6f},{last_state_in_br[1]})")
    print("")
    print("state_out_ar=(0.000000,None)")
    print("state_out_br=(0.000000,None)")
    return 0


def run_client(args):
    apply_cpu_rt(args.cpu, args.rt_priority)
    count = max(1, int(args.count))
    warmup = max(0, min(int(args.warmup), count - 1))
    delta_samples = []
    werner_samples = []
    sample_msgs = []
    delta_records = []
    last_delta = 0
    last_werner = 0.0
    last_msg = (0, 0, 0, 0.0)
    last_state_out = (0.0, 0)
    inbuf = bytearray(MSG_SIZE)

    with connect_repeater_until_ready(
        args.repeater_host,
        args.repeater_port,
        args.connect_timeout,
        args.detect_timeout,
        args.detect_interval,
        args.sock_buf,
        args.busy_poll_us,
    ) as sock:
        for i in range(count):
            recv_exact_into(sock, inbuf)
            ts_emit_ns, peer_id, correction_bits, w_swap = struct.unpack(MSG_FORMAT, inbuf)
            ts_recv_ns = time.time_ns()
            w_swap = clamp_werner(w_swap)
            peer_id = int(peer_id)
            correction_bits = int(correction_bits)
            last_msg = (ts_emit_ns, peer_id, correction_bits, w_swap)
            last_state_out = (w_swap, peer_id)
            last_delta = max(0, ts_recv_ns - ts_emit_ns)
            last_werner = w_swap
            if i >= warmup:
                delta_samples.append(last_delta)
                werner_samples.append(last_werner)
                sample_msgs.append((last_delta, i + 1, last_msg, last_state_out))
                delta_records.append((i + 1, last_delta))
            if args.count_interval > 0:
                time.sleep(args.count_interval)

    delta_sorted = sorted(delta_samples)
    w_sorted = sorted(werner_samples)
    mean_delay = int(sum(delta_samples) / len(delta_samples)) if delta_samples else 0
    mean_werner = sum(werner_samples) / len(werner_samples) if werner_samples else 0.0

    def pick_by_delta(samples, want_max=False):
        if not samples:
            return (0, 0, (0, 0, 0, 0.0), (0.0, 0))
        return max(samples, key=lambda item: item[0]) if want_max else min(samples, key=lambda item: item[0])

    min_sample = pick_by_delta(sample_msgs, want_max=False)
    max_sample = pick_by_delta(sample_msgs, want_max=True)

    if args.plot:
        os.makedirs("csv", exist_ok=True)
        base = f"{args.plot_prefix}_{args.client_id}"
        suffix = ""
        idx = 1
        while os.path.exists(os.path.join("csv", f"{base}{suffix}.csv")):
            idx += 1
            suffix = f"_{idx}"
        csv_path = os.path.join("csv", f"{base}{suffix}.csv")
        with open(csv_path, "w", encoding="utf-8") as handle:
            handle.write("count_idx,delay_ns\n")
            for count_idx, delay_ns in delta_records:
                handle.write(f"{count_idx},{delay_ns}\n")
        print(f"plot=data_saved ({csv_path})")

    if args.quiet:
        print(f"exchanges={count}")
        print(f"warmup={warmup}")
        print(f"state_in=({clamp_werner(args.werner_in):.6f},{args.repeater_id})")
        print_client_group("p50", percentile(delta_sorted, 0.50), percentile_inverse(w_sorted, 0.50))
        print_client_group("p95", percentile(delta_sorted, 0.95), percentile_inverse(w_sorted, 0.95))
        print_client_group("p90", percentile(delta_sorted, 0.90), percentile_inverse(w_sorted, 0.90))
        print_client_group("p99", percentile(delta_sorted, 0.99), percentile_inverse(w_sorted, 0.99))
        print_client_group("mean", mean_delay, mean_werner)
        print_client_message_state("min", min_sample[0], min_sample[2], min_sample[3], min_sample[1])
        print_client_message_state("max", max_sample[0], max_sample[2], max_sample[3], max_sample[1])
        print_client_message_state("last", last_delta, last_msg, last_state_out)
        return 0

    print("client_mode=fast3")
    print(f"exchanges={count} warmup={warmup}")
    print(f"client_id={args.client_id} repeater_id={args.repeater_id}")
    print(f"state_in=({clamp_werner(args.werner_in):.6f},{args.repeater_id})")
    print_client_group("p50", percentile(delta_sorted, 0.50), percentile_inverse(w_sorted, 0.50))
    print_client_group("p95", percentile(delta_sorted, 0.95), percentile_inverse(w_sorted, 0.95))
    print_client_group("p90", percentile(delta_sorted, 0.90), percentile_inverse(w_sorted, 0.90))
    print_client_group("p99", percentile(delta_sorted, 0.99), percentile_inverse(w_sorted, 0.99))
    print_client_group("mean", mean_delay, mean_werner)
    print_client_message_state("min", min_sample[0], min_sample[2], min_sample[3], min_sample[1])
    print_client_message_state("max", max_sample[0], max_sample[2], max_sample[3], max_sample[1])
    print_client_message_state("last", last_delta, last_msg, last_state_out)
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
