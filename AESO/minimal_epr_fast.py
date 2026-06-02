#!/usr/bin/env python3
import argparse
import gc
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
UDP_MSG_FORMAT = "!IQIBd"
UDP_MSG_SIZE = struct.calcsize(UDP_MSG_FORMAT)
UDP_HELLO_MAGIC = b"AESOUDP1"
UDP_READY_BYTE = b"U"
CLOCK_SYNC_SYNC_FORMAT = "!Q"
CLOCK_SYNC_SYNC_SIZE = struct.calcsize(CLOCK_SYNC_SYNC_FORMAT)
CLOCK_SYNC_DELAY_REQ_FORMAT = "!QQQ"
CLOCK_SYNC_DELAY_REQ_SIZE = struct.calcsize(CLOCK_SYNC_DELAY_REQ_FORMAT)
CLOCK_SYNC_DELAY_RESP_FORMAT = "!QQQQ"
CLOCK_SYNC_DELAY_RESP_SIZE = struct.calcsize(CLOCK_SYNC_DELAY_RESP_FORMAT)


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
    repeater.add_argument("--count-interval", type=float, default=0.0, help="Sleep seconds between counts.")
    repeater.add_argument("--quiet", action="store_true")
    repeater.add_argument("--plot", action="store_true", help="Write repeater send timing CSV data.")
    repeater.add_argument("--plot-prefix", default="repeater_send_hist", help="Prefix for repeater send timing CSV outputs.")
    repeater.add_argument("--plot-dir", default="csv", help="Directory where --plot CSV files are written.")
    repeater.add_argument("--diag", action="store_true", help="Measure extra repeater send timing diagnostics.")
    repeater.add_argument("--clock-sync", action="store_true", help="Enable pre-run PTP-style clock offset calibration. Enable it on both clients too.")
    repeater.add_argument("--clock-sync-samples", type=int, default=8, help="Calibration exchanges used only with --clock-sync.")
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
    client.add_argument("--sock-buf", type=int, default=4096, help="Set both SO_SNDBUF/SO_RCVBUF if > 0.")
    client.add_argument("--busy-poll-us", type=int, default=25, help="Set SO_BUSY_POLL in microseconds if supported.")
    client.add_argument("--client-id", type=int, default=1)
    client.add_argument("--repeater-id", type=int, default=0)
    client.add_argument("--werner-in", type=float, default=1)
    client.add_argument("--plot", action="store_true", help="Write delay histogram data and plot if matplotlib is available.")
    client.add_argument("--plot-prefix", default="delay_hist_client", help="Prefix for plot outputs.")
    client.add_argument("--plot-dir", default="csv", help="Directory where --plot CSV files are written.")
    client.add_argument("--count-interval", type=float, default=0.0, help="Sleep seconds between counts.")
    client.add_argument("--quiet", action="store_true")
    client.add_argument("--t1-ns", type=float, default=1_000_000.0)
    client.add_argument("--diag", action="store_true", help="Measure extra client loop/recv timing diagnostics.")
    client.add_argument("--clock-sync", action="store_true", help="Estimate repeater-clock minus client-clock offset with a PTP-style exchange before the data loop. Repeater and both clients must enable it.")
    client.add_argument("--clock-sync-samples", type=int, default=8, help="Calibration exchanges used only with --clock-sync.")
    client.add_argument("--clock-offset-ns", type=int, default=None, help="Manual repeater-clock minus client-clock offset. Skips auto calibration and does not require --clock-sync.")
    client.add_argument("--center-delay", action="store_true", help="Center delay stats around the run median; raw signed delay stays in CSV.")
    client.add_argument("--data-protocol", choices=("udp", "tcp"), default="udp", help="Transport for swapping result messages after TCP/PTP setup.")
    client.add_argument("--udp-idle-timeout", type=float, default=5.0, help="Stop waiting for UDP data after this many idle seconds.")

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


def estimate_clock_offset(sock, samples):
    # PTP-style four-timestamp exchange with the repeater as master:
    # t1: master Sync transmit, t2: slave Sync receive,
    # t3: slave Delay_Req transmit, t4: master Delay_Req receive.
    # slave_minus_master = ((t2 - t1) - (t4 - t3)) / 2.
    # We return master_minus_slave so the client can correct as:
    # corrected_client_time = client_time + master_minus_slave.
    offset_total_ns = 0
    path_delay_total_ns = 0
    sample_count = 0
    clock_sync_samples = []
    for _ in range(max(0, int(samples))):
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
        offset_total_ns += offset_ns
        path_delay_total_ns += mean_path_delay_ns
        sample_count += 1
        clock_sync_samples.append(
            (
                sample_count,
                t1_ns,
                t2_ns,
                t3_ns,
                t4_ns,
                master_to_slave_ns,
                slave_to_master_ns,
                offset_ns,
                mean_path_delay_ns,
            )
        )
    if sample_count == 0:
        return 0, 0, clock_sync_samples
    return offset_total_ns // sample_count, path_delay_total_ns // sample_count, clock_sync_samples


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
    if args.data_protocol == "udp":
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
        serve_clock_sync(conn_a, args.clock_sync_samples)
        serve_clock_sync(conn_b, args.clock_sync_samples)
    if args.data_protocol == "udp":
        data_a = accept_udp_peer(conn_a, udp_a)
        data_b = accept_udp_peer(conn_b, udp_b)
    else:
        data_a = conn_a
        data_b = conn_b

    def update_round_state(now_ns, correction_bits):
        state_ar_local = (args.repeater_id, w_ar_init, args.client_a_id)
        state_br_local = (args.repeater_id, w_br_init, args.client_b_id)
        ts_emit_ns_local = time.time_ns()
        w_swap_local = 1
        return state_ar_local, state_br_local, ts_emit_ns_local, correction_bits, w_swap_local

    if args.parallel:
        barrier_ready = threading.Barrier(3)
        barrier_done = threading.Barrier(3)
        last_msg_a_ref = [last_msg_a]
        last_msg_b_ref = [last_msg_b]

        def sender_thread(conn, buffer_ref, cpu_pin, send_block_samples, peer_id, last_msg_ref):
            set_thread_affinity(cpu_pin)
            for idx in range(count):
                barrier_ready.wait()
                ts_emit_ns = time.time_ns()
                correction_bits = correction_bits_samples[idx]
                w_swap = 1.0
                if args.data_protocol == "udp":
                    struct.pack_into(UDP_MSG_FORMAT, buffer_ref, 0, idx + 1, ts_emit_ns, peer_id, correction_bits, w_swap)
                else:
                    struct.pack_into(MSG_FORMAT, buffer_ref, 0, ts_emit_ns, peer_id, correction_bits, w_swap)
                if args.diag:
                    pre_send_ns = time.monotonic_ns()
                    if args.data_protocol == "udp":
                        conn.send(buffer_ref)
                    else:
                        conn.sendall(buffer_ref)
                    send_block_samples[idx] = time.monotonic_ns() - pre_send_ns
                else:
                    if args.data_protocol == "udp":
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
                    now_ns = time.monotonic_ns()
                    state_ar, state_br, _ts_emit_ns, correction_bits, w_swap = update_round_state(
                        now_ns, correction_bits_samples[idx]
                    )
                    last_state_in_ar = state_ar
                    last_state_in_br = state_br
                    last_ar_ns = now_ns
                    last_br_ns = now_ns
                    barrier_ready.wait()
                    barrier_done.wait()
                    last_msg_a = last_msg_a_ref[0]
                    last_msg_b = last_msg_b_ref[0]
                    if args.diag:
                        send_gap_ab_samples[idx] = 0
                    if args.count_interval > 0:
                        time.sleep(args.count_interval)
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
                for idx in range(count):
                    now_ns = time.monotonic_ns()
                    state_ar, state_br, _ts_emit_ns, correction_bits, w_swap = update_round_state(
                        now_ns, correction_bits_samples[idx]
                    )
                    last_state_in_ar = state_ar
                    last_state_in_br = state_br
                    last_ar_ns = now_ns
                    last_br_ns = now_ns
                    ts_emit_a_ns = time.time_ns()
                    if args.data_protocol == "udp":
                        struct.pack_into(
                            UDP_MSG_FORMAT, outbuf_a, 0, idx + 1, ts_emit_a_ns, args.client_b_id, correction_bits, w_swap
                        )
                    else:
                        struct.pack_into(MSG_FORMAT, outbuf_a, 0, ts_emit_a_ns, args.client_b_id, correction_bits, w_swap)
                    if args.diag:
                        pre_send_a_ns = time.monotonic_ns()
                        if args.data_protocol == "udp":
                            data_a.send(outbuf_a)
                        else:
                            data_a.sendall(outbuf_a)
                        post_send_a_ns = time.monotonic_ns()
                        send_a_block_samples[idx] = post_send_a_ns - pre_send_a_ns
                    else:
                        if args.data_protocol == "udp":
                            data_a.send(outbuf_a)
                        else:
                            data_a.sendall(outbuf_a)
                    last_msg_a = (ts_emit_a_ns, args.client_b_id, correction_bits, w_swap)
                    ts_emit_b_ns = time.time_ns()
                    if args.data_protocol == "udp":
                        struct.pack_into(
                            UDP_MSG_FORMAT, outbuf_b, 0, idx + 1, ts_emit_b_ns, args.client_a_id, correction_bits, w_swap
                        )
                    else:
                        struct.pack_into(MSG_FORMAT, outbuf_b, 0, ts_emit_b_ns, args.client_a_id, correction_bits, w_swap)
                    if args.diag:
                        pre_send_b_ns = time.monotonic_ns()
                        send_gap_ab_samples[idx] = pre_send_b_ns - pre_send_a_ns
                        if args.data_protocol == "udp":
                            data_b.send(outbuf_b)
                        else:
                            data_b.sendall(outbuf_b)
                        send_b_block_samples[idx] = time.monotonic_ns() - pre_send_b_ns
                    else:
                        if args.data_protocol == "udp":
                            data_b.send(outbuf_b)
                        else:
                            data_b.sendall(outbuf_b)
                    last_msg_b = (ts_emit_b_ns, args.client_a_id, correction_bits, w_swap)
                    if args.count_interval > 0:
                        time.sleep(args.count_interval)
        finally:
            if gc_was_enabled:
                gc.enable()

    if args.plot:
        os.makedirs(args.plot_dir, exist_ok=True)
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
        print(f"repeater_plot=data_saved ({csv_path})")

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
    last_state_out = (args.client_id, 0.0, None)
    data_msg_size = UDP_MSG_SIZE if args.data_protocol == "udp" else MSG_SIZE
    inbuf = bytearray(data_msg_size)
    sample_idx = 0
    udp_received = 0
    udp_lost_est = 0
    clock_sync_sample_rows = []

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
            clock_offset_ns, clock_sync_path_delay_ns, clock_sync_sample_rows = estimate_clock_offset(
                sock, args.clock_sync_samples
            )
        else:
            clock_offset_ns = 0
            clock_sync_path_delay_ns = 0

        if args.data_protocol == "udp":
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
        else:
            data_sock = sock

        gc_was_enabled = gc.isenabled()
        gc.disable()
        try:
            prev_loop_ns = time.monotonic_ns() if args.diag else 0
            if args.data_protocol == "udp":
                last_seq = 0
                while True:
                    if args.diag:
                        loop_now_ns = time.monotonic_ns()
                        loop_gap_ns = loop_now_ns - prev_loop_ns
                        prev_loop_ns = loop_now_ns
                        pre_recv_ns = time.monotonic_ns()
                    try:
                        got = data_sock.recv_into(inbuf)
                    except socket.timeout:
                        break
                    if args.diag:
                        recv_block_ns = time.monotonic_ns() - pre_recv_ns
                    if got != UDP_MSG_SIZE:
                        continue
                    count_idx, ts_emit_ns, peer_id, correction_bits, w_swap = struct.unpack(UDP_MSG_FORMAT, inbuf)
                    ts_recv_ns = time.time_ns()
                    count_idx = int(count_idx)
                    if count_idx <= 0 or count_idx > count:
                        continue
                    udp_received += 1
                    if count_idx > last_seq:
                        udp_lost_est += max(0, count_idx - last_seq - 1)
                        last_seq = count_idx
                    w_swap_raw = float(w_swap)
                    peer_id = int(peer_id)
                    correction_bits = int(correction_bits)
                    last_raw_msg = (ts_emit_ns, peer_id, correction_bits, w_swap_raw)
                    last_delta = (ts_recv_ns + clock_offset_ns) - ts_emit_ns
                    if count_idx > warmup and sample_idx < sample_count:
                        delta_samples[sample_idx] = last_delta
                        werner_raw_samples[sample_idx] = w_swap_raw
                        sample_msgs[sample_idx] = (last_delta, count_idx, last_raw_msg, None)
                        delta_record_counts[sample_idx] = count_idx
                        if args.diag:
                            loop_gap_samples[sample_idx] = loop_gap_ns
                            recv_block_samples[sample_idx] = recv_block_ns
                        sample_idx += 1
                    if count_idx >= count:
                        break
                    if args.count_interval > 0:
                        time.sleep(args.count_interval)
            else:
                for i in range(count):
                    if args.diag:
                        loop_now_ns = time.monotonic_ns()
                        loop_gap_ns = loop_now_ns - prev_loop_ns
                        prev_loop_ns = loop_now_ns
                        pre_recv_ns = time.monotonic_ns()
                        recv_exact_into(data_sock, inbuf)
                        recv_block_ns = time.monotonic_ns() - pre_recv_ns
                    else:
                        recv_exact_into(data_sock, inbuf)
                    ts_emit_ns, peer_id, correction_bits, w_swap = struct.unpack(MSG_FORMAT, inbuf)
                    ts_recv_ns = time.time_ns()
                    w_swap_raw = float(w_swap)
                    peer_id = int(peer_id)
                    correction_bits = int(correction_bits)
                    last_raw_msg = (ts_emit_ns, peer_id, correction_bits, w_swap_raw)
                    last_delta = (ts_recv_ns + clock_offset_ns) - ts_emit_ns
                    if i >= warmup:
                        delta_samples[sample_idx] = last_delta
                        werner_raw_samples[sample_idx] = w_swap_raw
                        sample_msgs[sample_idx] = (last_delta, i + 1, last_raw_msg, None)
                        delta_record_counts[sample_idx] = i + 1
                        if args.diag:
                            loop_gap_samples[sample_idx] = loop_gap_ns
                            recv_block_samples[sample_idx] = recv_block_ns
                        sample_idx += 1
                    if args.count_interval > 0:
                        time.sleep(args.count_interval)
        finally:
            if gc_was_enabled:
                gc.enable()
            if args.data_protocol == "udp":
                data_sock.close()

    delta_samples = delta_samples[:sample_idx]
    werner_raw_samples = werner_raw_samples[:sample_idx]
    sample_msgs = sample_msgs[:sample_idx]
    delta_record_counts = delta_record_counts[:sample_idx]
    if args.diag:
        loop_gap_samples = loop_gap_samples[:sample_idx]
        recv_block_samples = recv_block_samples[:sample_idx]

    delay_center_ns = percentile(sorted(delta_samples), 0.50) if args.center_delay and delta_samples else 0
    delay_stat_samples = [delay_ns - delay_center_ns for delay_ns in delta_samples]

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
        os.makedirs(args.plot_dir, exist_ok=True)
        base = f"{args.plot_prefix}_{args.client_id}"
        suffix = ""
        idx = 1
        while os.path.exists(os.path.join(args.plot_dir, f"{base}{suffix}.csv")):
            idx += 1
            suffix = f"_{idx}"
        csv_path = os.path.join(args.plot_dir, f"{base}{suffix}.csv")
        with open(csv_path, "w", encoding="utf-8") as handle:
            if args.diag:
                handle.write("count_idx,delay_ns,delay_center_ns,delay_centered_ns,clock_offset_ns,clock_sync_path_delay_ns,loop_gap_ns,recv_block_ns\n")
                for idx, delay_ns, delay_centered_ns, loop_gap_ns, recv_block_ns in zip(
                    delta_record_counts, delta_samples, delay_stat_samples, loop_gap_samples, recv_block_samples
                ):
                    handle.write(f"{idx},{delay_ns},{delay_center_ns},{delay_centered_ns},{clock_offset_ns},{clock_sync_path_delay_ns},{loop_gap_ns},{recv_block_ns}\n")
            else:
                handle.write("count_idx,delay_ns,delay_center_ns,delay_centered_ns,clock_offset_ns,clock_sync_path_delay_ns\n")
                for idx, delay_ns, delay_centered_ns in zip(delta_record_counts, delta_samples, delay_stat_samples):
                    handle.write(f"{idx},{delay_ns},{delay_center_ns},{delay_centered_ns},{clock_offset_ns},{clock_sync_path_delay_ns}\n")
        print(f"plot=data_saved ({csv_path})")
        if clock_sync_sample_rows:
            clock_sync_base = f"clock_sync_client_{args.client_id}"
            clock_sync_path = os.path.join(args.plot_dir, f"{clock_sync_base}{suffix}.csv")
            with open(clock_sync_path, "w", encoding="utf-8") as handle:
                handle.write(
                    "sample_idx,t1_ns,t2_ns,t3_ns,t4_ns,"
                    "master_to_slave_ns,slave_to_master_ns,offset_ns,path_delay_ns,"
                    "clock_offset_mean_ns,clock_sync_path_delay_mean_ns\n"
                )
                for (
                    sample_number,
                    t1_ns,
                    t2_ns,
                    t3_ns,
                    t4_ns,
                    master_to_slave_ns,
                    slave_to_master_ns,
                    offset_ns,
                    mean_path_delay_ns,
                ) in clock_sync_sample_rows:
                    handle.write(
                        f"{sample_number},{t1_ns},{t2_ns},{t3_ns},{t4_ns},"
                        f"{master_to_slave_ns},{slave_to_master_ns},{offset_ns},{mean_path_delay_ns},"
                        f"{clock_offset_ns},{clock_sync_path_delay_ns}\n"
                    )
            print(f"clock_sync=data_saved ({clock_sync_path})")

    if args.quiet:
        print(f"exchanges={count}")
        print(f"warmup={warmup}")
        print(f"data_protocol={args.data_protocol}")
        if args.data_protocol == "udp":
            print(f"udp_received={udp_received}")
            print(f"udp_lost_est={udp_lost_est}")
        print(f"clock_offset_ns={clock_offset_ns}")
        print(f"clock_sync_path_delay_ns={clock_sync_path_delay_ns}")
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
    if args.data_protocol == "udp":
        print(f"udp_received={udp_received}")
        print(f"udp_lost_est={udp_lost_est}")
    print(f"clock_offset_ns={clock_offset_ns}")
    print(f"clock_sync_path_delay_ns={clock_sync_path_delay_ns}")
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
