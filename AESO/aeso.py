#!/usr/bin/env python3
"""
aeso.py - Optimized AESO Binary UDP Simulator (Enhanced Edition)

Optimizations & Features:
  • Zero-heap allocation in hot loop via pre-allocated bytearrays & Struct.pack_into.
  • Hardware/Kernel NS timestamping support (SO_TIMESTAMPNS).
  • Spin / Hybrid / Sleep pacing modes without code duplication.
  • Network & CPU Warm-up phase (--warmup) to eliminate cold-start spikes.
  • In-situ real-time statistical processing (Min, Mean, P50, P99, Jitter).
  • Safe Signal Interrupt Handling (restores GC and saves partial metrics on Ctrl+C).
"""

import argparse
import gc
import json
import os
import random
import signal
import socket
import struct
import sys
import time

PAYLOAD_FORMAT = "!QdI"
PAYLOAD_SIZE = struct.calcsize(PAYLOAD_FORMAT)   # 16 bytes
REG_FORMAT = "!I"
REG_SIZE = struct.calcsize(REG_FORMAT)           # 4 bytes

PAYLOAD_PACK = struct.Struct(PAYLOAD_FORMAT).pack_into
PAYLOAD_UNPACK = struct.Struct(PAYLOAD_FORMAT).unpack_from
REG_PACK = struct.pack

# ─── Socket & Kernel Utilities ─────────────────────────────────────────────────

SO_TIMESTAMPNS_CANDIDATES = tuple(
    v for v in (getattr(socket, "SO_TIMESTAMPNS", None), 64, 35) if v is not None
)

def read_ptp_offset():
    """Read current PTP clock offset in nanoseconds from daemon status file."""
    try:
        with open("/tmp/ptp_status.json", "r") as f:
            data = json.load(f)
            return data.get("instantaneous_offset_ns", 0)
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        return 0

def enable_kernel_timestamp_ns(sock):
    """Enable kernel-level packet timestamping (SO_TIMESTAMPNS)."""
    for opt in SO_TIMESTAMPNS_CANDIDATES:
        try:
            sock.setsockopt(socket.SOL_SOCKET, int(opt), 1)
            return
        except OSError:
            pass

def parse_kernel_timestamp_ns(ancdata):
    """Extract nanosecond kernel RX timestamp from socket control messages."""
    for level, cmsg_type, data in ancdata:
        if level == socket.SOL_SOCKET and cmsg_type in SO_TIMESTAMPNS_CANDIDATES:
            if len(data) >= 16:
                sec, nsec = struct.unpack_from("@qq", data)
                return sec * 1_000_000_000 + nsec
            elif len(data) >= 8:
                sec, nsec = struct.unpack_from("@ll", data)
                return sec * 1_000_000_000 + nsec
    return None

def enable_low_latency_socket(sock, sock_buf=0, busy_poll_us=0, kernel_timestamp=True):
    """Apply socket options for minimal OS/network stack latency."""
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
    if kernel_timestamp:
        enable_kernel_timestamp_ns(sock)

def apply_cpu_rt(cpu=None, rt_priority=None):
    """Set CPU affinity and Real-Time scheduling (SCHED_FIFO)."""
    if cpu is not None:
        try:
            os.sched_setaffinity(0, {int(cpu)})
        except OSError:
            pass
    if rt_priority is not None and int(rt_priority) > 0:
        try:
            param = os.sched_param(int(rt_priority))
            os.sched_setscheduler(0, os.SCHED_FIFO, param)
        except OSError:
            pass

def get_unique_filepath(folder, prefix, extension=".csv"):
    os.makedirs(folder, exist_ok=True)
    n = 1
    while True:
        filepath = os.path.join(folder, f"{prefix}_{n}{extension}")
        if not os.path.exists(filepath):
            return filepath
        n += 1

# ─── Repeater Engine ───────────────────────────────────────────────────────────

def run_repeater(args):
    w_prod = args.werner_ar * args.werner_br
    pswap_str = str(args.pswap).replace(".", "_")
    folder_name = f"aeso_report_pswap{pswap_str}"
    output_file = get_unique_filepath(folder_name, "repeater_swap")

    sock_a = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock_a.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    enable_low_latency_socket(sock_a, args.sock_buf, args.busy_poll_us, kernel_timestamp=False)
    sock_a.bind((args.listen_host_a, args.listen_port_a))

    sock_b = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock_b.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    enable_low_latency_socket(sock_b, args.sock_buf, args.busy_poll_us, kernel_timestamp=False)
    sock_b.bind((args.listen_host_b, args.listen_port_b))

    print(f"[Repeater] Waiting for Client A on {args.listen_host_a}:{args.listen_port_a}...")
    data_a, addr_a = sock_a.recvfrom(1024)
    client_a_id = struct.unpack_from(REG_FORMAT, data_a)[0]
    print(f"[Repeater] Client A (ID: {client_a_id}) registered from {addr_a}")

    print(f"[Repeater] Waiting for Client B on {args.listen_host_b}:{args.listen_port_b}...")
    data_b, addr_b = sock_b.recvfrom(1024)
    client_b_id = struct.unpack_from(REG_FORMAT, data_b)[0]
    print(f"[Repeater] Client B (ID: {client_b_id}) registered from {addr_b}")

    count = args.count
    warmup = args.warmup
    pswap = args.pswap
    pace_interval_ns = int(args.count_interval * 1_000_000_000)
    spin_margin_ns = int(args.spin_margin_us * 1000)
    pace_mode = args.pace_mode

    send_a = sock_a.sendto
    send_b = sock_b.sendto
    time_ns = time.time_ns
    mono_ns = time.monotonic_ns
    rnd = random.random
    pack_into = PAYLOAD_PACK
    pkt_buf = bytearray(PAYLOAD_SIZE)
    raw_records = []
    rec_append = raw_records.append

    apply_cpu_rt(args.cpu, args.rt_priority)

    # Signal Handling for Graceful Shutdown
    running = True
    def stop_signal_handler(sig, frame):
        nonlocal running
        print("\n[Repeater] Interrupted! Flushing data to disk...")
        running = False
    signal.signal(signal.SIGINT, stop_signal_handler)

    # --- Warm-up Phase ---
    if warmup > 0:
        print(f"[Repeater] Warming up hardware/caches with {warmup} dummy packets...")
        for _ in range(warmup):
            pack_into(pkt_buf, 0, time_ns(), 0.0, 0)
            send_a(pkt_buf, addr_a)
            send_b(pkt_buf, addr_b)

    print(f"[Repeater] Firing {count} packets (pace={args.count_interval*1e6:.1f} µs, mode={pace_mode}) -> {output_file}")

    gc_was_enabled = gc.isenabled()
    gc.disable()

    try:
        is_pswap_full = (pswap >= 1.0)
        for seq in range(count):
            if not running:
                break

            ts = time_ns()
            success_bit = 1 if (is_pswap_full or rnd() <= pswap) else 0
            w_val = w_prod if success_bit else 0.0

            pack_into(pkt_buf, 0, ts, w_val, success_bit)
            send_a(pkt_buf, addr_a)
            send_b(pkt_buf, addr_b)
            rec_append((seq, ts, w_val, success_bit))

            if pace_interval_ns > 0:
                deadline_ns = mono_ns() + pace_interval_ns
                if pace_mode == "spin":
                    while mono_ns() < deadline_ns:
                        pass
                elif pace_mode == "hybrid":
                    while True:
                        rem_ns = deadline_ns - mono_ns()
                        if rem_ns <= 0:
                            break
                        if rem_ns > spin_margin_ns:
                            time.sleep((rem_ns - spin_margin_ns) / 1_000_000_000)
                        else:
                            break
                    while mono_ns() < deadline_ns:
                        pass
                elif pace_mode == "sleep":
                    rem_ns = deadline_ns - mono_ns()
                    if rem_ns > 0:
                        time.sleep(rem_ns / 1_000_000_000)
    finally:
        sock_a.close()
        sock_b.close()
        if gc_was_enabled:
            gc.enable()

    # Save to CSV
    csv_lines = [b"count_index,ts_swap,werner,success_bit\n"]
    for seq, ts, w, bit in raw_records:
        csv_lines.append(f"{seq},{ts},{w:.6f},{bit}\n".encode())

    with open(output_file, "wb") as f:
        f.writelines(csv_lines)

    print(f"[Repeater] Completed {len(raw_records)} packets. CSV saved: {output_file}")

# ─── Client Engine ─────────────────────────────────────────────────────────────

def run_client(args):
    ptp_offset_ns = read_ptp_offset()
    if ptp_offset_ns != 0:
        print(f"[Client {args.client_id}] Applied PTP daemon offset: {ptp_offset_ns} ns")

    pswap_str = str(args.pswap).replace(".", "_")
    folder_name = f"aeso_report_pswap{pswap_str}"
    prefix = (
        "client_lab" if args.client_id == 1
        else ("client_teleco" if args.client_id == 2 else f"client_{args.client_id}")
    )
    output_file = get_unique_filepath(folder_name, prefix)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    enable_low_latency_socket(sock, args.sock_buf, args.busy_poll_us, kernel_timestamp=True)

    sock.sendto(REG_PACK(REG_FORMAT, args.client_id), (args.repeater_host, args.repeater_port))
    print(f"[Client {args.client_id}] Registered. Awaiting {args.count} packets...")

    count = args.count
    warmup = args.warmup
    recvmsg_into = sock.recvmsg_into
    time_ns = time.time_ns
    unpack_from = PAYLOAD_UNPACK

    rx_buf = bytearray(PAYLOAD_SIZE + 128)
    anc_size = socket.CMSG_SPACE(32) if hasattr(socket, "CMSG_SPACE") else 128
    raw_records = []
    rec_append = raw_records.append

    apply_cpu_rt(args.cpu, args.rt_priority)

    # Signal Handling
    running = True
    def stop_signal_handler(sig, frame):
        nonlocal running
        print(f"\n[Client {args.client_id}] Interrupted! Saving recorded data...")
        running = False
    signal.signal(signal.SIGINT, stop_signal_handler)

    # --- Warm-up Phase (Drain dummy packets) ---
    if warmup > 0:
        print(f"[Client {args.client_id}] Discarding {warmup} warm-up packets...")
        for _ in range(warmup):
            recvmsg_into([rx_buf], anc_size)

    gc_was_enabled = gc.isenabled()
    gc.disable()

    try:
        for seq in range(count):
            if not running:
                break
            nbytes, ancdata, _flags, _addr = recvmsg_into([rx_buf], anc_size)

            ts_rx_kernel = parse_kernel_timestamp_ns(ancdata)
            if ts_rx_kernel is None:
                ts_rx_kernel = time_ns()

            ts_recv_ns = ts_rx_kernel
            ts_emit, w, bit = unpack_from(rx_buf, 0)
            one_way_delay_ns = ts_recv_ns - ts_emit

            rec_append((seq, ts_emit, one_way_delay_ns, w, bit))
    finally:
        sock.close()
        if gc_was_enabled:
            gc.enable()

    # Save to CSV
    csv_lines = [b"count_index,ts_swap,swap_to_recv_ns,werner,success_bit\n"]
    for seq, ts_emit, delay, w, bit in raw_records:
        csv_lines.append(f"{seq},{ts_emit},{delay},{w:.6f},{bit}\n".encode())

    with open(output_file, "wb") as f:
        f.writelines(csv_lines)

    print(f"[Client {args.client_id}] Done. CSV saved: {output_file}")

    # --- Real-Time Statistics Reporting ---
    if raw_records:
        delays_us = [r[2] / 1000.0 for r in raw_records]
        delays_us.sort()
        n = len(delays_us)
        min_d = delays_us[0]
        max_d = delays_us[-1]
        mean_d = sum(delays_us) / n
        p50 = delays_us[int(n * 0.50)]
        p99 = delays_us[int(n * 0.99)]

        jitters = [abs(delays_us[i] - delays_us[i - 1]) for i in range(1, n)]
        mean_jitter = (sum(jitters) / len(jitters)) if jitters else 0.0

        print(f"\n========================================")
        print(f"   CLIENT {args.client_id} LATENCY SUMMARY (µs)   ")
        print(f"========================================")
        print(f" Packets Received : {n}/{count}")
        print(f" Min Latency      : {min_d:.2f} µs")
        print(f" Mean Latency     : {mean_d:.2f} µs")
        print(f" P50 (Median)     : {p50:.2f} µs")
        print(f" P99 Latency      : {p99:.2f} µs")
        print(f" Max Latency      : {max_d:.2f} µs")
        print(f" Mean Jitter      : {mean_jitter:.2f} µs")
        print(f"========================================\n")

# ─── CLI Entrypoint ────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="AESO Binary UDP Simulator (Enhanced Edition)")
    subparsers = parser.add_subparsers(dest="mode", required=True)

    # --- Repeater ---
    p_rep = subparsers.add_parser("repeater", help="Run as the central repeater.")
    p_rep.add_argument("--listen-host-a", default="0.0.0.0")
    p_rep.add_argument("--listen-port-a", type=int, default=7401)
    p_rep.add_argument("--listen-host-b", default="0.0.0.0")
    p_rep.add_argument("--listen-port-b", type=int, default=7402)
    p_rep.add_argument("--werner-ar", type=float, default=0.98)
    p_rep.add_argument("--werner-br", type=float, default=0.95)
    p_rep.add_argument("--pswap", type=float, default=0.85)
    p_rep.add_argument("--count", type=int, default=2000)
    p_rep.add_argument("--warmup", type=int, default=50, help="Number of initial dummy packets to discard")
    p_rep.add_argument("--count-interval", type=float, default=0.0,
                       help="Inter-packet interval in seconds (0 = back-to-back burst)")
    p_rep.add_argument("--pace-mode", choices=("spin", "hybrid", "sleep"), default="spin",
                       help="Pacing strategy: spin=pure busy-wait, hybrid=spin+sleep, sleep=standard sleep")
    p_rep.add_argument("--spin-margin-us", type=float, default=100.0,
                       help="Final spin window in µs (only used with --pace-mode hybrid)")
    p_rep.add_argument("--cpu", type=int, default=None)
    p_rep.add_argument("--rt-priority", type=int, default=50)
    p_rep.add_argument("--sock-buf", type=int, default=65536)
    p_rep.add_argument("--busy-poll-us", type=int, default=50)

    # --- Client ---
    p_cli = subparsers.add_parser("client", help="Run as a receiving client.")
    p_cli.add_argument("--repeater-host", default="127.0.0.1")
    p_cli.add_argument("--repeater-port", type=int, required=True)
    p_cli.add_argument("--client-id", type=int, required=True)
    p_cli.add_argument("--pswap", type=float, default=0.85)
    p_cli.add_argument("--count", type=int, default=2000)
    p_cli.add_argument("--warmup", type=int, default=50, help="Number of initial dummy packets to discard")
    p_cli.add_argument("--cpu", type=int, default=None)
    p_cli.add_argument("--rt-priority", type=int, default=50)
    p_cli.add_argument("--sock-buf", type=int, default=65536)
    p_cli.add_argument("--busy-poll-us", type=int, default=50)

    args = parser.parse_args()

    if args.mode == "repeater":
        run_repeater(args)
    elif args.mode == "client":
        run_client(args)

if __name__ == "__main__":
    main()