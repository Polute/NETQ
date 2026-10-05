#!/usr/bin/env python3
"""
aegso_Latt.py - Atomic Entanglement Generation and Swapping Operation
3-Node Quantum Repeater Simulator: A --- R --- B

Generation succeeds independently on each optical link with:
p_eff = pgen * exp(-L / Latt)
"""

import argparse
import gc
import json
import math
import os
import random
import selectors
import signal
import socket
import struct
import time

# ---- Message Formats ----
TS_FORMAT = "!QB"          # Generation / ACK (ts_emit, success_bit)
TS_SIZE = struct.calcsize(TS_FORMAT)

PAYLOAD_FORMAT = "!QdI"    # Swap Result (ts_swap, werner_result, success_bit)
PAYLOAD_SIZE = struct.calcsize(PAYLOAD_FORMAT)

REG_FORMAT = "!I"          # Client Registration (client_id)
REG_SIZE = struct.calcsize(REG_FORMAT)

TS_PACK = struct.Struct(TS_FORMAT).pack_into
TS_UNPACK = struct.Struct(TS_FORMAT).unpack_from
PAYLOAD_PACK = struct.Struct(PAYLOAD_FORMAT).pack_into
PAYLOAD_UNPACK = struct.Struct(PAYLOAD_FORMAT).unpack_from
REG_PACK = struct.pack

SO_TIMESTAMPNS_CANDIDATES = tuple(
    v for v in (getattr(socket, "SO_TIMESTAMPNS", None), 64, 35) if v is not None
)

SCM_TIMESTAMPNS_CANDIDATES = tuple(
    v for v in (getattr(socket, "SCM_TIMESTAMPNS", None), 64, 35) if v is not None
)

# Physical optical-link lengths and pre-calculated L_att in km from dB/km losses.
# Link A: 23.3 km, 6.6 dB -> Latt = (4.3429 * 23.3) / 6.6 ≈ 15.3315 km
# Link B: 1.9 km, 2.3 dB  -> Latt = (4.3429 * 1.9) / 2.3  ≈ 3.5920 km
LINK_CONFIG = {
    "A": {"length_km": 23.3, "latt_km": (4.342944819032518 * 23.3) / 6.6},
    "B": {"length_km": 1.9, "latt_km": (4.342944819032518 * 1.9) / 2.3},
}


def read_ptp_offset():
    try:
        with open("/tmp/ptp_status.json", "r") as f:
            return json.load(f).get("instantaneous_offset_ns", 0)
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        return 0


def enable_kernel_timestamp_ns(sock):
    for opt in SO_TIMESTAMPNS_CANDIDATES:
        try:
            sock.setsockopt(socket.SOL_SOCKET, int(opt), 1)
            return
        except OSError:
            pass


def parse_kernel_timestamp_ns(ancdata):
    """Extract kernel RX timestamp (ns) from ancillary data."""
    for level, cmsg_type, data in ancdata:
        if level != socket.SOL_SOCKET:
            continue
        if cmsg_type not in SCM_TIMESTAMPNS_CANDIDATES:
            continue
        if len(data) >= 16:
            sec, nsec = struct.unpack_from("@qq", data)
            return sec * 1_000_000_000 + nsec
        elif len(data) >= 8:
            sec, nsec = struct.unpack_from("@ll", data)
            return sec * 1_000_000_000 + nsec
    return None


def enable_low_latency_socket(sock, sock_buf=0, busy_poll_us=0, kernel_timestamp=False):
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


def chown_output_path(path):
    uid = os.environ.get("SUDO_UID")
    gid = os.environ.get("SUDO_GID")
    if uid is None or gid is None:
        return
    try:
        os.chown(path, int(uid), int(gid))
    except (OSError, ValueError):
        pass


def chown_output_ancestors(path):
    uid = os.environ.get("SUDO_UID")
    gid = os.environ.get("SUDO_GID")
    if uid is None or gid is None:
        return
    try:
        uid_i, gid_i = int(uid), int(gid)
    except ValueError:
        return
    abs_path = os.path.abspath(path)
    cwd = os.path.abspath(os.getcwd())
    if abs_path == cwd:
        chown_output_path(abs_path)
        return
    if not abs_path.startswith(cwd + os.sep):
        chown_output_path(abs_path)
        return
    rel = os.path.relpath(abs_path, cwd)
    current = cwd
    for part in rel.split(os.sep):
        current = os.path.join(current, part)
        try:
            os.chown(current, uid_i, gid_i)
        except OSError:
            pass


def get_unique_filepath(folder, prefix, extension=".csv"):
    os.makedirs(folder, exist_ok=True)
    chown_output_ancestors(folder)
    n = 1
    while True:
        fp = os.path.join(folder, f"{prefix}_{n}{extension}")
        if not os.path.exists(fp):
            return fp
        n += 1


def resource_werner_ns(age_ns, t1_ns, w0):
    age_ns = max(0.0, float(age_ns))
    if t1_ns <= 0:
        return 0.0 if age_ns > 0 else w0
    return w0 * math.exp(-age_ns / float(t1_ns))


def effective_generation_probability(pgen, length_km, latt_km):
    """Calcula p_eff aplicando únicamente la atenuacion del canal de fibra."""
    return min(1.0, max(0.0, float(pgen) * math.exp(-float(length_km) / float(latt_km))))


# ───────────────────────────── Repeater (R) ─────────────────────────────

class _Link:
    __slots__ = ("addr", "ready", "epr_ts_ns", "w0", "attempts", "successes")

    def __init__(self, w0):
        self.addr = None
        self.ready = False
        self.epr_ts_ns = 0
        self.w0 = w0
        self.attempts = 0
        self.successes = 0


def run_repeater(args):
    # Formato de visualización de Rol del Repeater
    role_str_map = {
        "none": "ReceiverA-ReceiverB + Repeater",
        "both": "SenderA-SenderB + Repeater",
        "A": "SenderA-ReceiverB + Repeater",
        "B": "ReceiverA-SenderB + Repeater"
    }
    print(f"[ROL]: {role_str_map.get(args.emit_to, 'Repeater')}")

    folder = (f"aegso_report_pswap{str(args.pswap).replace('.', '_')}"
              f"/pgen{str(args.pgen).replace('.', '_')}")
    out_path = get_unique_filepath(folder, "repeater_swap")
    out_gen_path = get_unique_filepath(folder, "repeater_gen")

    sock_a = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock_a.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    enable_low_latency_socket(sock_a, args.sock_buf, args.busy_poll_us, False)
    sock_a.bind((args.listen_host_a, args.listen_port_a))

    sock_b = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock_b.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    enable_low_latency_socket(sock_b, args.sock_buf, args.busy_poll_us, False)
    sock_b.bind((args.listen_host_b, args.listen_port_b))

    links = {"A": _Link(args.werner_a0), "B": _Link(args.werner_b0)}
    sockets = {"A": sock_a, "B": sock_b}

    try:
        sock_a.settimeout(args.register_timeout)
        sock_b.settimeout(args.register_timeout)
        print(f"[Repeater] Waiting for A on port {args.listen_port_a}...")
        data, addr = sock_a.recvfrom(64)
        links["A"].addr = addr
        print(f"[Repeater] A registered from {addr}")

        print(f"[Repeater] Waiting for B on port {args.listen_port_b}...")
        data, addr = sock_b.recvfrom(64)
        links["B"].addr = addr
        print(f"[Repeater] B registered from {addr}")
    except socket.timeout:
        print("[Repeater] Timeout awaiting clients. Aborting.")
        return

    # ── READY handshake ──
    READY_SIG = b"\x01"
    sock_a.sendto(READY_SIG, links["A"].addr)
    sock_b.sendto(READY_SIG, links["B"].addr)
    print(f"[Repeater] READY sent. Starting swaps.")

    sock_a.setblocking(False)
    sock_b.setblocking(False)

    sel = selectors.DefaultSelector()
    sel.register(sock_a, selectors.EVENT_READ, "A")
    sel.register(sock_b, selectors.EVENT_READ, "B")

    apply_cpu_rt(args.cpu, args.rt_priority)
    running = True

    def _stop(signum, frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, _stop)

    pgen = args.pgen
    effective_pgen = {
        side: effective_generation_probability(pgen, cfg["length_km"], cfg["latt_km"])
        for side, cfg in LINK_CONFIG.items()
    }
    
    # Mapeo por pantalla de la transformación de PGEN
    print(f"[PGEN TRANSFORM Link A]: {pgen:.6g} -> {effective_pgen['A']:.6g} (L={LINK_CONFIG['A']['length_km']}km, Latt={LINK_CONFIG['A']['latt_km']:.2f}km)")
    print(f"[PGEN TRANSFORM Link B]: {pgen:.6g} -> {effective_pgen['B']:.6g} (L={LINK_CONFIG['B']['length_km']}km, Latt={LINK_CONFIG['B']['latt_km']:.2f}km)")

    pswap = args.pswap
    threshold = args.werner_threshold
    t1_ns = max(1.0, float(args.coherence_ns))

    time_ns = time.time_ns
    rnd = random.random
    in_buf = bytearray(TS_SIZE + 64)
    ack_buf = bytearray(TS_SIZE)
    gen_buf = bytearray(TS_SIZE)
    swap_buf = bytearray(PAYLOAD_SIZE)
    records = []
    gen_records = []

    gc_was_enabled = gc.isenabled()
    gc.disable()

    round_idx = 0
    total_gen_attempts = 0
    try:
        while running and round_idx < args.count:
            # Emisión proactiva del Repeater según --emit-to
            for side in ("A", "B"):
                should_emit = (args.emit_to == "both") or (args.emit_to == side)
                if should_emit and not links[side].ready:
                    total_gen_attempts += 1
                    links[side].attempts += 1
                    ts_emit = time_ns()
                    TS_PACK(gen_buf, 0, ts_emit, 1)
                    sockets[side].sendto(gen_buf, links[side].addr)

            for key, _ in sel.select(timeout=0.005):
                side = key.data
                sock = sockets[side]
                link = links[side]
                try:
                    nbytes, addr = sock.recvfrom_into(in_buf)
                except (BlockingIOError, OSError):
                    continue

                if nbytes != TS_SIZE:
                    continue

                ts_recv_ns = time_ns()
                ts_emit, pgen_remote_bit = TS_UNPACK(in_buf, 0)
                e2r_ns = max(0, ts_recv_ns - ts_emit)

                is_emitting = (args.emit_to == "both") or (args.emit_to == side)

                if is_emitting:
                    # El cliente actuó como receptor y le responde al repetidor el resultado del éxito
                    is_success = bool(pgen_remote_bit)
                    pgen_link = effective_pgen[side]
                else:
                    # Modo tradicional: cliente emite, el repetidor decide pgen
                    link.attempts += 1
                    total_gen_attempts += 1
                    pgen_link = effective_pgen[side]
                    is_success = rnd() <= pgen_link
                    TS_PACK(ack_buf, 0, ts_recv_ns, 1 if is_success else 0)
                    sock.sendto(ack_buf, addr)

                w_recv = link.w0
                gen_records.append((
                    total_gen_attempts,
                    side,
                    ts_recv_ns,
                    e2r_ns,
                    w_recv,
                    pgen_link,
                    1 if is_success else 0,
                ))

                if is_success:
                    link.ready = True
                    link.epr_ts_ns = ts_recv_ns
                    link.successes += 1

            if links["A"].ready and links["B"].ready:
                now_ns = time_ns()
                la, lb = links["A"], links["B"]
                w_a = resource_werner_ns(now_ns - la.epr_ts_ns, t1_ns, la.w0)
                w_b = resource_werner_ns(now_ns - lb.epr_ts_ns, t1_ns, lb.w0)

                expired_a = w_a < threshold
                expired_b = w_b < threshold

                if expired_a:
                    la.ready = False
                if expired_b:
                    lb.ready = False

                if not expired_a and not expired_b:
                    success_bit = 1 if (pswap >= 1.0 or rnd() <= pswap) else 0
                    w_result = (w_a * w_b) if success_bit else 0.0

                    PAYLOAD_PACK(swap_buf, 0, now_ns, w_result, success_bit)
                    sock_a.sendto(swap_buf, la.addr)
                    sock_b.sendto(swap_buf, lb.addr)

                    records.append((round_idx, now_ns, w_a, w_b, w_result, success_bit))
                    round_idx += 1
                    la.ready = False
                    lb.ready = False
    finally:
        if gc_was_enabled:
            gc.enable()
        sel.close()
        sock_a.close()
        sock_b.close()

    with open(out_path, "w") as f:
            f.write("round,ts_swap_ns,werner_a,werner_b,werner_result,pswap_success_bit\n")
            for r in records:
                f.write(f"{r[0]},{r[1]},{r[2]:.6f},{r[3]:.6f},{r[4]:.6f},{r[5]}\n")
    chown_output_path(out_path)

    with open(out_gen_path, "w") as f:
            f.write("count,link,ts_recv,e2r_ns,w_recv,pgen_effective,bit_success\n")
            for r in gen_records:
                f.write(f"{r[0]},{r[1]},{r[2]},{r[3]},{r[4]:.6f},{r[5]:.12g},{r[6]}\n")
    chown_output_path(out_gen_path)

    print(f"[Repeater] Saved {len(records)} swaps -> {out_path}")
    print(f"[Repeater] Saved {len(gen_records)} gen attempts -> {out_gen_path}")


# ───────────────────────────── Client (A/B) ─────────────────────────────

def run_client(args):
    role_str = "Receiver" if args.is_receiver else "Sender"
    print(f"[ROL]: {role_str}")

    ptp_offset_ns = read_ptp_offset()
    if ptp_offset_ns:
        print(f"[Client {args.node_id}] PTP Offset: {ptp_offset_ns} ns")

    folder = (f"aegso_report_pswap{str(args.pswap).replace('.', '_')}"
              f"/pgen{str(args.pgen).replace('.', '_')}")
    
    prefix = "client_lab" if args.node_id == "A" else "client_teleco"
    out_path = get_unique_filepath(folder, prefix)
    
    gen_prefix = f"{prefix}_gen"
    out_gen_path = get_unique_filepath(folder, gen_prefix)

    # ── Socket: kernel timestamping enabled ──
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    enable_low_latency_socket(
        sock, args.sock_buf, args.busy_poll_us, kernel_timestamp=True
    )

    server_addr = (args.repeater_host, args.repeater_port)
    client_id = 1 if args.node_id == "A" else 2

    # ── Register + wait for READY ──
    sock.settimeout(5.0)
    ready = False
    for _reg in range(200):
        sock.sendto(REG_PACK(REG_FORMAT, client_id), server_addr)
        try:
            data, _ = sock.recvfrom(64)
            if len(data) == 1 and data[0] == 0x01:
                ready = True
                break
        except (socket.timeout, OSError):
            time.sleep(0.05)

    if not ready:
        print(f"[Client {args.node_id}] ERROR: No READY signal. Aborting.")
        sock.close()
        return

    print(f"[Client {args.node_id}] READY. Starting {args.count} swap rounds...")
    sock.settimeout(args.attempt_timeout)

    apply_cpu_rt(args.cpu, args.rt_priority)
    running = True

    def _stop(signum, frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, _stop)

    gen_buf = bytearray(TS_SIZE)
    ack_buf = bytearray(TS_SIZE)
    rx_buf = bytearray(PAYLOAD_SIZE + 64)
    records = []
    gen_records = []

    time_ns = time.time_ns
    perf_ns = time.perf_counter_ns
    rnd = random.random
    recvmsg_into = sock.recvmsg_into
    anc_size = socket.CMSG_SPACE(48) if hasattr(socket, "CMSG_SPACE") else 128

    cfg = LINK_CONFIG[args.node_id]
    effective_pgen = effective_generation_probability(args.pgen, cfg["length_km"], cfg["latt_km"])

    # Impresión en pantalla del cambio en PGEN
    print(f"[PGEN TRANSFORM]: {args.pgen:.6g} -> {effective_pgen:.6g} (Link {args.node_id}: {cfg['length_km']}km, Latt={cfg['latt_km']:.2f}km)")

    gc_was_enabled = gc.isenabled()
    gc.disable()

    round_idx = 0
    total_gen_attempts = 0
    try:
        while running and round_idx < args.count:
            attempts = 0
            gen_rtt_ns = 0
            pgen_ok = False

            # ── PHASE 1: Generation Loop ──
            t_gen_start_ns = time_ns()
            while running:
                attempts += 1
                total_gen_attempts += 1
                ts_emit = time_ns()

                t0 = perf_ns()

                if not args.is_receiver:
                    # Modo Sender: El cliente emite el intento
                    TS_PACK(gen_buf, 0, ts_emit, 1)
                    sock.sendto(gen_buf, server_addr)
                    try:
                        n, ancdata, _flags, _addr = recvmsg_into([rx_buf], anc_size)
                    except (socket.timeout, OSError):
                        w_rtt = 1.0
                        gen_records.append((total_gen_attempts, ts_emit, 0, w_rtt, 0))
                        continue

                    if n != TS_SIZE:
                        continue
                    gen_rtt_ns = perf_ns() - t0

                    _, pgen_bit = TS_UNPACK(rx_buf, 0)
                    gen_records.append((total_gen_attempts, ts_emit, gen_rtt_ns, 1.0, 1 if pgen_bit else 0))
                    if pgen_bit:
                        pgen_ok = True
                        ts_ack_kernel = parse_kernel_timestamp_ns(ancdata)
                        ts_gen_success_ns = ts_ack_kernel if ts_ack_kernel else time_ns()
                        t_gen_total_ns = ts_gen_success_ns - t_gen_start_ns
                        break
                else:
                    # Modo Receiver: El Repeater emite el intento
                    try:
                        n, ancdata, _flags, _addr = recvmsg_into([rx_buf], anc_size)
                    except (socket.timeout, OSError):
                        continue

                    if n != TS_SIZE:
                        continue

                    gen_rtt_ns = perf_ns() - t0
                    # El cliente Receiver decide la pgen aplicando únicamente la atenuación del tramo
                    is_success = rnd() <= effective_pgen
                    TS_PACK(ack_buf, 0, time_ns(), 1 if is_success else 0)
                    sock.sendto(ack_buf, server_addr)

                    pgen_bit = 1 if is_success else 0
                    gen_records.append((total_gen_attempts, ts_emit, gen_rtt_ns, 1.0, pgen_bit))

                    if is_success:
                        pgen_ok = True
                        ts_ack_kernel = parse_kernel_timestamp_ns(ancdata)
                        ts_gen_success_ns = ts_ack_kernel if ts_ack_kernel else time_ns()
                        t_gen_total_ns = ts_gen_success_ns - t_gen_start_ns
                        break

            if not pgen_ok:
                break

            # ── PHASE 2: Wait for Swap ──
            sock.settimeout(args.swap_wait_timeout)
            try:
                n, ancdata, _flags, _addr = recvmsg_into([rx_buf], anc_size)
            except (socket.timeout, OSError):
                continue  # EPR expired → regenerate

            if n != PAYLOAD_SIZE:
                continue

            ts_swap_kernel = parse_kernel_timestamp_ns(ancdata)
            ts_recv_ns = ts_swap_kernel if ts_swap_kernel else time_ns()

            ts_swap_ns, werner, pswap_bit = PAYLOAD_UNPACK(rx_buf, 0)
            swap_to_recv_ns = max(0, ts_recv_ns - ts_swap_ns)
            t_exp_ns = max(0, ts_swap_ns - ts_gen_success_ns)

            records.append((
                round_idx,
                attempts,
                t_gen_total_ns,
                gen_rtt_ns,
                ts_gen_success_ns,
                ts_swap_ns,
                swap_to_recv_ns,
                t_exp_ns,
                werner,
                pgen_bit,
                pswap_bit
            ))
            round_idx += 1
            sock.settimeout(args.attempt_timeout)

    finally:
        sock.close()
        if gc_was_enabled:
            gc.enable()

    with open(out_path, "w") as f:
        f.write(
            "round,gen_attempts,t_gen_total_ns,gen_rtt_ns,"
            "ts_gen_success_ns,ts_swap_ns,swap_to_recv_ns,t_exp_ns,"
            "werner,pgen_success_bit,pswap_success_bit\n"
        )
        for r in records:
            f.write(
                f"{r[0]},{r[1]},{r[2]},{r[3]},{r[4]},{r[5]},{r[6]},{r[7]},"
                f"{r[8]:.6f},{r[9]},{r[10]}\n"
            )
    chown_output_path(out_path)

    with open(out_gen_path, "w") as f:
        f.write("count,ts_gen,ts_rtt,w_rtt,bit_success_gen\n")
        for r in gen_records:
            f.write(f"{r[0]},{r[1]},{r[2]},{r[3]:.6f},{r[4]}\n")
    chown_output_path(out_gen_path)

    ok = sum(1 for r in records if r[10])
    print(f"[Client {args.node_id}] {len(records)} rounds "
          f"({ok} pswap successful). CSV -> {out_path}")
    print(f"[Client {args.node_id}] {len(gen_records)} total gen attempts -> {out_gen_path}")


# ─────────────────────────────────── CLI ───────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="aegso_Latt: Fast generation + swapping protocol with link attenuation."
    )
    sub = p.add_subparsers(dest="role", required=True)

    cli = sub.add_parser("client")
    cli.add_argument("--node-id", choices=("A", "B"), required=True)
    cli.add_argument("--repeater-host", required=True)
    cli.add_argument("--repeater-port", type=int, required=True)
    cli.add_argument("--count", type=int, default=2000)
    cli.add_argument("--pswap", type=float, default=1.00)
    cli.add_argument("--pgen", type=float, default=1.0)
    cli.add_argument("--is-receiver", action="store_true",
                     help="Set if the client acts as a receiver (repeater emits EPRs to this node).")
    cli.add_argument("--attempt-timeout", type=float, default=0.5)
    cli.add_argument("--swap-wait-timeout", type=float, default=3.0)
    cli.add_argument("--cpu", type=int, default=None)
    cli.add_argument("--rt-priority", type=int, default=50)
    cli.add_argument("--sock-buf", type=int, default=65536)
    cli.add_argument("--busy-poll-us", type=int, default=50)

    rep = sub.add_parser("repeater")
    rep.add_argument("--listen-host-a", default="0.0.0.0")
    rep.add_argument("--listen-port-a", type=int, default=7401)
    rep.add_argument("--listen-host-b", default="0.0.0.0")
    rep.add_argument("--listen-port-b", type=int, default=7402)
    rep.add_argument("--count", type=int, default=2000)
    rep.add_argument("--pgen", type=float, default=1.0)
    rep.add_argument("--emit-to", choices=("none", "both", "A", "B"), default="none",
                     help="Configure direction where Repeater acts as transmitter ('none', 'both', 'A', 'B').")
    rep.add_argument("--pswap", type=float, default=1.00)
    rep.add_argument("--werner-a0", type=float, default=1.0)
    rep.add_argument("--werner-b0", type=float, default=1.0)
    rep.add_argument("--werner-threshold", type=float, default=1 / 3)
    rep.add_argument("--coherence-ns", type=float, default=10000000.0)
    rep.add_argument("--register-timeout", type=float, default=60.0)
    rep.add_argument("--cpu", type=int, default=None)
    rep.add_argument("--rt-priority", type=int, default=50)
    rep.add_argument("--sock-buf", type=int, default=65536)
    rep.add_argument("--busy-poll-us", type=int, default=50)

    return p.parse_args()


def main():
    args = parse_args()
    if args.role == "client":
        run_client(args)
    elif args.role == "repeater":
        run_repeater(args)


if __name__ == "__main__":
    main()