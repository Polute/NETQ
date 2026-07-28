#!/usr/bin/env python3
import argparse
import gc
from http import server
from itertools import count
import json
import math
import os
import random
import socket
import struct
import time

TS_FORMAT = "!QB"
TS_SIZE = struct.calcsize(TS_FORMAT)


def read_ptp_offset():
    """Read current PTP offset from /tmp/ptp_status.json if available.
    
    Returns:
        int: Clock offset in nanoseconds (0 if not available)
    """
    try:
        with open("/tmp/ptp_status.json", "r") as f:
            data = json.load(f)
            return data.get("instantaneous_offset_ns", 0)
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        return 0


# --- UDP CLOCK SYNC (Native DEOS-style) ---
CLOCK_SYNC_UDP_MAGIC = b"AEGOCS1!"
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

def serve_clock_sync_udp(udp_sock, samples=5, kernel_timestamp=False):
    if kernel_timestamp:
        enable_kernel_timestamp_ns(udp_sock)

    hello_size = CLOCK_SYNC_UDP_HELLO_SIZE
    
    # CORRECCIÓN 1: Usar un tamaño máximo para vaciar siempre el buffer UDP sin truncar paquetes
    max_req_size = max(
        CLOCK_SYNC_UDP_HELLO_SIZE, 
        CLOCK_SYNC_UDP_SYNC_SIZE,
        CLOCK_SYNC_UDP_DELAY_REQ_SIZE,
        CLOCK_SYNC_UDP_DELAY_RESP_SIZE
    )

    # Timeout de seguridad para no congelar el proceso si la red pierde paquetes
    udp_sock.settimeout(2.0)

    # 1. Espera el HELLO inicial del cliente (AEGOCS1!)
    while True:
        try:
            data, addr, _rx_ns, _got_kernel = recvfrom_timestamped(
                udp_sock, max_req_size, False
            )
        except socket.timeout:
            continue

        if len(data) == hello_size:
            magic, msg_type = struct.unpack(CLOCK_SYNC_UDP_HELLO_FORMAT, data)
            if (
                magic == CLOCK_SYNC_UDP_MAGIC
                and msg_type == CLOCK_SYNC_UDP_HELLO
            ):
                break

    # 2. Ráfaga rápida de sincronización AEGO
    for sample_number in range(1, max(0, int(samples)) + 1):
        t1_ns = time.time_ns()
        udp_sock.sendto(
            struct.pack(
                CLOCK_SYNC_UDP_SYNC_FORMAT,
                CLOCK_SYNC_UDP_MAGIC,
                CLOCK_SYNC_UDP_SYNC,
                sample_number,
                t1_ns,
            ),
            addr,
        )

        sample_matched = False
        while True:
            try:
                # CORRECCIÓN 2: Leer con max_req_size en vez de CLOCK_SYNC_UDP_DELAY_REQ_SIZE
                data, req_addr, t4_ns, t4_got_kernel = recvfrom_timestamped(
                    udp_sock, max_req_size, kernel_timestamp
                )
            except socket.timeout:
                # Si se pierde la solicitud, descartamos la muestra actual en vez de congelar el socket
                break

            if req_addr != addr:
                continue
                
            if len(data) == CLOCK_SYNC_UDP_DELAY_REQ_SIZE:
                (
                    magic,
                    msg_type,
                    echoed_sample,
                    t1_echo_ns,
                    t2_ns,
                    t3_ns,
                ) = struct.unpack(CLOCK_SYNC_UDP_DELAY_REQ_FORMAT, data)
    
                if (
                    magic == CLOCK_SYNC_UDP_MAGIC
                    and msg_type == CLOCK_SYNC_UDP_DELAY_REQ
                    and echoed_sample == sample_number
                ):
                    sample_matched = True
                    break

        if not sample_matched:
            continue

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
    warmup,
    method="best-path-median",
    best_ratio=0.5,
    sock_buf=65536,
    busy_poll_us=0,
    detect_timeout=1.0,
    detect_interval=0.001,
    kernel_timestamp=False,
):
    """Estimate clock offset against UDP sync server (Client side)."""
    sample_total = max(1, int(samples))
    clock_sync_samples = []

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    if sock_buf > 0:
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, int(sock_buf))
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, int(sock_buf))
        except OSError:
            pass

    if kernel_timestamp:
        enable_kernel_timestamp_ns(sock)

    sock.bind(("", 0))
    server_addr = (host, int(port))

    sock.settimeout(max(0.05, float(detect_interval))) # Timeout ágil
    
    # CORRECCIÓN 3: Refrescaremos este deadline si hay progreso para evitar crasheos prematuros
    base_timeout = max(2.0, float(detect_timeout))
    deadline = time.monotonic() + base_timeout

    max_req_size = max(
        CLOCK_SYNC_UDP_HELLO_SIZE, 
        CLOCK_SYNC_UDP_SYNC_SIZE,
        CLOCK_SYNC_UDP_DELAY_REQ_SIZE,
        CLOCK_SYNC_UDP_DELAY_RESP_SIZE
    )

    try:
        sample_number = 1
        waiting_for_first_sync = True

        while sample_number <= sample_total:
            if time.monotonic() >= deadline:
                # CORRECCIÓN 4: Si superamos el límite global por red caída, rompemos el bucle
                # en lugar de lanzar TimeoutError, y devolvemos lo que hayamos logrado.
                break

            if waiting_for_first_sync:
                sock.sendto(
                    struct.pack(
                        CLOCK_SYNC_UDP_HELLO_FORMAT,
                        CLOCK_SYNC_UDP_MAGIC,
                        CLOCK_SYNC_UDP_HELLO,
                    ),
                    server_addr,
                )

            # Receive SYNC packet
            sync_received = False
            while True:
                if time.monotonic() >= deadline:
                    break
                try:
                    data, addr, t2_ns, got_kernel = recvfrom_timestamped(
                        sock, max_req_size, kernel_timestamp
                    )
                except socket.timeout:
                    if waiting_for_first_sync:
                        break # Salir para volver a mandar HELLO
                    # Salir del bucle interno para evaluar si el proceso ha muerto
                    break

                if addr != server_addr:
                    continue
                    
                if len(data) == CLOCK_SYNC_UDP_SYNC_SIZE:
                    magic, msg_type, echoed_sample, t1_ns = struct.unpack(
                        CLOCK_SYNC_UDP_SYNC_FORMAT, data
                    )
                    if (
                        magic == CLOCK_SYNC_UDP_MAGIC
                        and msg_type == CLOCK_SYNC_UDP_SYNC
                        and echoed_sample >= sample_number  # CORRECCIÓN 5: ¡Fast-Forward! Aceptamos si el servidor avanzó la muestra
                    ):
                        sample_number = echoed_sample
                        waiting_for_first_sync = False
                        sync_received = True
                        deadline = time.monotonic() + base_timeout # Hubo progreso, refrescamos vida
                        break

            if time.monotonic() >= deadline:
                break

            if waiting_for_first_sync or not sync_received:
                continue

            # Send DELAY_REQ packet
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

            # Receive DELAY_RESP packet
            resp_received = False
            while True:
                if time.monotonic() >= deadline:
                    break
                try:
                    data, addr, _rx_ns, _got_kernel = recvfrom_timestamped(
                        sock, max_req_size, False
                    )
                except socket.timeout:
                    # CORRECCIÓN 6: Romper el bucle infinito si se pierde la respuesta
                    break

                if addr != server_addr:
                    continue

                if len(data) == CLOCK_SYNC_UDP_DELAY_RESP_SIZE:
                    (
                        magic,
                        msg_type,
                        echoed_sample,
                        echoed_t1_ns,
                        echoed_t2_ns,
                        echoed_t3_ns,
                        t4_ns,
                        t4_got_kernel,
                    ) = struct.unpack(CLOCK_SYNC_UDP_DELAY_RESP_FORMAT, data)

                    if (
                        magic == CLOCK_SYNC_UDP_MAGIC
                        and msg_type == CLOCK_SYNC_UDP_DELAY_RESP
                        and echoed_sample == sample_number
                    ):
                        resp_received = True
                        deadline = time.monotonic() + base_timeout # Hubo progreso, refrescamos vida
                        break

            if not resp_received:
                # Si falló, simplemente repetimos el bucle principal. 
                # El servidor mandará un nuevo SYNC y el Fast-Forward (>=) actualizará sample_number.
                continue

            # --- CÁLCULO DE OFFSET ---
            master_to_slave_ns = t2_ns - t1_ns
            slave_to_master_ns = t4_ns - t3_ns
            mean_path_delay_ns = (master_to_slave_ns + slave_to_master_ns) // 2
            offset_ns = (master_to_slave_ns - slave_to_master_ns) // 2

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
                    "used_for_offset": True,
                    "used_for_best_path": False,
                }
            )
            sample_number += 1
    finally:
        sock.close()

    if not clock_sync_samples:
        # Si fallaron todas, no petamos la aplicación, devolvemos 0
        return 0, 0, []

    best_count = max(
        1, int(math.ceil(len(clock_sync_samples) * float(best_ratio)))
    )
    best_rows = sorted(
        clock_sync_samples, key=lambda row: row["path_delay_ns"]
    )[:best_count]

    best_offsets = [row["offset_ns"] for row in best_rows]
    best_paths = [row["path_delay_ns"] for row in best_rows]

    clock_offset_ns = int(percentile(sorted(best_offsets), 0.50))
    clock_sync_path_delay_ns = int(percentile(sorted(best_paths), 0.50))

    return clock_offset_ns, clock_sync_path_delay_ns, clock_sync_samples

def parse_args():
    parser = argparse.ArgumentParser(
        description="Fast4 unified: sender/receiver with persistent one-socket and low-jitter options."
    )
    subparsers = parser.add_subparsers(dest="role", required=True)

    sender = subparsers.add_parser("sender", help="Run in sender mode.")
    sender.add_argument("--receiver-host", default="127.0.0.1")
    sender.add_argument("--receiver-port", type=int, default=7401)
    sender.add_argument("--count", type=int, default=1000)
    sender.add_argument("--warmup", type=int, default=50)
    sender.add_argument("--connect-timeout", type=float, default=10.0)
    sender.add_argument("--detect-timeout", type=float, default=30.0)
    sender.add_argument("--detect-interval", type=float, default=0.05)
    sender.add_argument("--cpu", type=int, default=None, help="Pin this process to one CPU core.")
    sender.add_argument("--rt-priority", type=int, default=50, help="Set SCHED_FIFO priority (1-99), usually needs sudo.")
    sender.add_argument("--sock-buf", type=int, default=65536, help="Set both SO_SNDBUF/SO_RCVBUF if > 0.")
    sender.add_argument("--busy-poll-us", type=int, default=0, help="Set SO_BUSY_POLL in microseconds if supported.")
    sender.add_argument("--kernel-timestamp", action="store_true", help="Enable kernel timestamping (SO_TIMESTAMPNS).")
    sender.add_argument("--show-arrows", action="store_true", help="Print per-arrow timing table at the end.")
    sender.add_argument("--werner-min", type=float, default=0.2)
    sender.add_argument("--t1-ns", type=float, default=1_000_000.0)
    sender.add_argument("--count-interval", type=float, default=0.0, help="Sleep seconds between counts.")
    sender.add_argument("--pace-mode", choices=("sleep", "spin", "hybrid"), default="hybrid", help="How --count-interval pacing waits between counts.")
    sender.add_argument("--spin-margin-us", type=float, default=10.0, help="Final busy-wait window used by --pace-mode hybrid.")
    sender.add_argument("--diag", action="store_true", help="Measure extra sender timing diagnostics.")
    sender.add_argument("--plot", action="store_true", help="Write sender timing CSV data.")
    sender.add_argument("--plot-prefix", default="sender_timing", help="Prefix for sender timing CSV outputs.")
    sender.add_argument("--plot-dir", default="csv", help="Directory where --plot CSV files are written.")
    sender.add_argument("--json", dest="json_output", action=argparse.BooleanOptionalAction, default=True, help="Write JSON metadata when --plot is used.")
    sender.add_argument("--json-dir", default=None, help="Directory where JSON files are written. Defaults from --plot-dir: csv_x -> json_x.")
    sender.add_argument("--pgen", type=float, default=1.0, help="Probability of generating/sending each packet (0.0-1.0).")
    sender.add_argument("--quiet", action="store_true")
    
    sync_group = sender.add_mutually_exclusive_group()
    sync_group.add_argument("--clock-sync", action="store_true", help="Enable native UDP clock synchronization.")
    sender.add_argument("--clock-sync-samples", type=int, default=264, help="Number of UDP clock sync samples (default: 264).")
    sender.add_argument("--clock-sync-warmup", type=int, default=10, help="Warmup samples for UDP clock sync.")
    sender.add_argument("--clock-sync-method", choices=("mean", "median", "best-path-median"), default="best-path-median", help="UDP clock sync method.")
    sender.add_argument("--clock-sync-best-ratio", type=float, default=0.5, help="Best ratio for UDP clock sync.")
    sender.add_argument("--clock-sync-kernel-timestamp", action=argparse.BooleanOptionalAction, default=True, help="Use SO_TIMESTAMPNS for UDP clock sync.")
    sender.add_argument("--clock-sync-port", type=int, default=7501, help="UDP port for clock sync (default: 7501).")

    receiver = subparsers.add_parser("receiver", help="Run in receiver mode.")
    receiver.add_argument("--listen-host", default="0.0.0.0")
    receiver.add_argument("--listen-port", type=int, default=7401)
    receiver.add_argument("--count", type=int, default=1000)
    receiver.add_argument("--warmup", type=int, default=50)
    receiver.add_argument("--accept-timeout", type=float, default=30.0)
    receiver.add_argument("--cpu", type=int, default=None, help="Pin this process to one CPU core.")
    receiver.add_argument("--rt-priority", type=int, default=50, help="Set SCHED_FIFO priority (1-99), usually needs sudo.")
    receiver.add_argument("--sock-buf", type=int, default=65536, help="Set both SO_SNDBUF/SO_RCVBUF if > 0.")
    receiver.add_argument("--busy-poll-us", type=int, default=0, help="Set SO_BUSY_POLL in microseconds if supported.")
    receiver.add_argument("--kernel-timestamp", action="store_true", help="Enable kernel timestamping (SO_TIMESTAMPNS).")
    receiver.add_argument("--werner-min", type=float, default=0.2)
    receiver.add_argument("--t1-ns", type=float, default=1_000_000.0)
    receiver.add_argument("--count-interval", type=float, default=0.0, help="Sleep seconds between counts.")
    receiver.add_argument("--pace-mode", choices=("sleep", "spin", "hybrid"), default="hybrid", help="How --count-interval pacing waits between counts.")
    receiver.add_argument("--spin-margin-us", type=float, default=10.0, help="Final busy-wait window used by --pace-mode hybrid.")
    receiver.add_argument("--diag", action="store_true", help="Measure extra receiver timing diagnostics.")
    receiver.add_argument("--plot", action="store_true", help="Write receiver timing CSV data.")
    receiver.add_argument("--plot-prefix", default="receiver_timing", help="Prefix for receiver timing CSV outputs.")
    receiver.add_argument("--plot-dir", default="csv", help="Directory where --plot CSV files are written.")
    receiver.add_argument("--json", dest="json_output", action=argparse.BooleanOptionalAction, default=True, help="Write JSON metadata when --plot is used.")
    receiver.add_argument("--json-dir", default=None, help="Directory where JSON files are written. Defaults from --plot-dir: csv_x -> json_x.")
    receiver.add_argument("--pgen", type=float, default=1.0, help="Probability of success for each packet (0.0-1.0). Bob decides success/failure.")
    receiver.add_argument("--show-arrows", action="store_true", help="Print receiver timing table at the end.")
    receiver.add_argument("--quiet", action="store_true")
    
    sync_group = receiver.add_mutually_exclusive_group()
    sync_group.add_argument("--clock-sync", action="store_true", help="Enable native UDP clock synchronization.")
    receiver.add_argument("--clock-sync-samples", type=int, default=264, help="Number of UDP clock sync samples (default: 264).")
    receiver.add_argument("--clock-sync-warmup", type=int, default=10, help="Warmup samples for UDP clock sync.")
    receiver.add_argument("--clock-sync-method", choices=("mean", "median", "best-path-median"), default="best-path-median", help="UDP clock sync method.")
    receiver.add_argument("--clock-sync-best-ratio", type=float, default=0.5, help="Best ratio for UDP clock sync.")
    receiver.add_argument("--clock-sync-kernel-timestamp", action=argparse.BooleanOptionalAction, default=True, help="Use SO_TIMESTAMPNS for UDP clock sync.")
    receiver.add_argument("--clock-sync-port", type=int, default=7501, help="UDP port for clock sync (default: 7501).")

    return parser.parse_args()


def enable_low_latency_socket(sock, sock_buf=0, busy_poll_us=0, kernel_timestamp=False):
    """Apply low-latency options to UDP socket safely."""
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
    if kernel_timestamp and hasattr(socket, "SO_TIMESTAMPNS"):
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_TIMESTAMPNS, 1)
        except OSError:
            pass


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


def get_available_filename(base_path, extension):
    candidate = base_path + extension
    counter = 1
    while os.path.exists(candidate):
        candidate = f"{base_path}_{counter}{extension}"
        counter += 1
    return candidate


def pgen_hook(count_idx, ts_ns, outbuf, args):
    return True


def connect_receiver_until_ready_udp(host, port, connect_timeout, detect_timeout, detect_interval, sock_buf, busy_poll_us, kernel_timestamp=False):
    """Probe UDP receiver via ping handshake before commencing timing measurements."""
    deadline = time.monotonic() + max(0.0, float(detect_timeout))
    server_addr = (host, int(port))
    last_error = None
    
    while time.monotonic() < deadline:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        enable_low_latency_socket(sock, sock_buf, busy_poll_us, kernel_timestamp)
        sock.settimeout(max(0.001, float(connect_timeout)))
        try:
            sock.sendto(b"AEGOPING", server_addr)
            data, _ = sock.recvfrom(16)
            if b"AEGOPONG" in data:
                sock.connect(server_addr)
                return sock
        except OSError as exc:
            last_error = exc
            sock.close()
            
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(max(0.0, float(detect_interval)), remaining))
        
    raise TimeoutError("UDP Receiver was not detected before detect-timeout expired") from last_error


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


def print_sender_group(label, round_trip_ns, emit_to_remote_ns, werner):
    print("")
    print(f"sender_{label}")
    print("segment\t\t\t\t\t ns (s)")
    print(f"t_recv_ns\t\t\t\t{fmt_ns(emit_to_remote_ns)}")
    print(f"total_round_trip_perf\t\t\t{fmt_ns(round_trip_ns)}")
    print(f"werner\t\t\t\t\t{werner:.6f}")
    print("")


def print_receiver_group(label, sender_to_receiver_ns, recv_to_ack_ns, total_view_ns, werner):
    print("")
    print(f"receiver_{label}")
    print("segment\t\t\t\t\t ns (s)")
    print(f"sender_to_receiver\t\t\t{fmt_ns(sender_to_receiver_ns)}")
    print(f"receiver_to_ack_send\t\t\t{fmt_ns(recv_to_ack_ns)}")
    print(f"total_receiver_view\t\t\t{fmt_ns(total_view_ns)}")
    print(f"werner\t\t\t\t\t{werner:.6f}")
    print("")


def run_sender(args):
    ptp_offset_ns = read_ptp_offset()
    if ptp_offset_ns != 0:
        print(f"[PTP] Using offset from daemon: {ptp_offset_ns} ns")
    
    clock_offset_ns = 0
    if getattr(args, 'clock_sync', False):
        clock_sync_host = args.receiver_host
        clock_sync_port = getattr(args, 'clock_sync_port', 7501)
        clock_sync_samples = getattr(args, 'clock_sync_samples', 264)
        clock_sync_warmup = getattr(args, 'clock_sync_warmup', 10)
        clock_sync_method = getattr(args, 'clock_sync_method', 'best-path-median')
        clock_sync_best_ratio = getattr(args, 'clock_sync_best_ratio', 0.5)
        clock_sync_kernel_ts = getattr(args, 'clock_sync_kernel_timestamp', True)
        
        print(f"[Clock Sync] Synchronizing with {clock_sync_host}:{clock_sync_port}...")
        try:
            clock_offset_ns, path_delay_ns, _ = estimate_clock_offset_udp(
                host=clock_sync_host,
                port=clock_sync_port,
                samples=clock_sync_samples,
                warmup=clock_sync_warmup,
                method=clock_sync_method,
                best_ratio=clock_sync_best_ratio,
                sock_buf=args.sock_buf,
                busy_poll_us=args.busy_poll_us,
                detect_timeout=args.detect_timeout,
                detect_interval=args.detect_interval,
                kernel_timestamp=clock_sync_kernel_ts,
            )
            print(f"[Clock Sync] Offset: {clock_offset_ns} ns, Path delay: {path_delay_ns} ns")
            
        except Exception as e:
            print(f"[Clock Sync] Failed: {e}")
            print(f"[Clock Sync] Continuing without clock sync...")
    
    apply_cpu_rt(args.cpu, args.rt_priority)
    count = max(1, int(args.count))
    warmup = max(0, min(int(args.warmup), count - 1))
    rtt_perf_samples = []
    emit_to_remote_samples = []
    sent_count = 0
    success_count = 0
    indices = []
    emit_ts_samples = []
    success_bits = []
    last_emit_to_remote = 0
    last_round_trip_perf = 0
    outbuf = bytearray(TS_SIZE)
    inbuf = bytearray(TS_SIZE)
    
    send_timings = [] if args.diag else None
    recv_timings = [] if args.diag else None
    
    count_interval_ns = int(float(args.count_interval) * 1_000_000_000)
    spin_margin_ns = int(float(args.spin_margin_us) * 1_000)

    gc_was_enabled = gc.isenabled()
    gc.disable()

    try:
        sock = connect_receiver_until_ready_udp(
            args.receiver_host,
            args.receiver_port,
            args.connect_timeout,
            args.detect_timeout,
            args.detect_interval,
            args.sock_buf,
            args.busy_poll_us,
            getattr(args, 'kernel_timestamp', False),
        )
        sock.settimeout(5.0)
        with sock:
            for i in range(count):
                ts_emit_ns = time.time_ns()
                struct.pack_into(TS_FORMAT, outbuf, 0, ts_emit_ns-clock_offset_ns, 1)
                
                if not pgen_hook(i, ts_emit_ns, outbuf, args):
                    continue
                
                sent_count += 1
                if args.diag:
                    t_send_pre = time.perf_counter_ns()
                
                t_rtt0 = time.perf_counter_ns()
                sock.send(outbuf)
                try:
                    nbytes, _ = sock.recvfrom_into(inbuf)
                except socket.timeout:
                    continue  # UDP Packet lost or timed out
                t_rtt1 = time.perf_counter_ns()
                
                if args.diag:
                    t_send_post = time.perf_counter_ns()
                    send_timings.append(t_send_post - t_send_pre)
                    recv_timings.append(t_rtt1 - t_rtt0)
                
                ts_remote_update_ns, success_bit = struct.unpack(TS_FORMAT, inbuf[:TS_SIZE])
                ts_remote_corrected_ns = ts_remote_update_ns + clock_offset_ns

                last_emit_to_remote = max(0, abs(ts_remote_corrected_ns - ts_emit_ns))
                last_round_trip_perf = max(0, t_rtt1 - t_rtt0)
                
                if i >= warmup:
                    rtt_perf_samples.append(last_round_trip_perf)
                    emit_to_remote_samples.append(last_emit_to_remote)
                    emit_ts_samples.append(ts_emit_ns)
                    success_count += 1
                    indices.append(i)
                    success_bits.append(success_bit)
                
                if count_interval_ns > 0:
                    pace_wait(count_interval_ns, args.pace_mode, spin_margin_ns)
    finally:
        if gc_was_enabled:
            gc.enable()

    rtt = sorted(rtt_perf_samples)
    e2r = sorted(emit_to_remote_samples)
    werner_samples = [
        werner_from_age_ns(age_ns, args.werner_min, args.t1_ns)
        for age_ns in emit_to_remote_samples
    ]
    w_sorted = sorted(werner_samples)
    local_werner = werner_from_age_ns(last_emit_to_remote, args.werner_min, args.t1_ns)
    max_werner_sample = max(werner_samples) if w_sorted else 0.0

    if args.plot:
        ensure_output_dir(args.plot_dir)
        csv_base = os.path.join(args.plot_dir, args.plot_prefix)
        csv_path = get_available_filename(csv_base, ".csv")
        json_dir = args.json_dir or default_json_dir(args.plot_dir)
        json_base = os.path.join(json_dir, args.plot_prefix)
        json_path = get_available_filename(json_base, ".json")
        
        with open(csv_path, "w") as f:
            f.write("count_index,emit_ts_ns,rtt_ns,e2r_ns,werner,success_bit\n")
            for count_idx, emit_ts_ns, rtt_val, e2r_val, w_val, success_bit in zip(indices, emit_ts_samples, rtt_perf_samples, emit_to_remote_samples, werner_samples, success_bits):
                f.write(f"{count_idx},{int(emit_ts_ns)},{int(rtt_val)},{int(e2r_val)},{w_val:.6f},{int(success_bit)}\n")
        
        if args.json_output:
            ensure_output_dir(json_dir)
            metadata = {
                "mode": "sender",
                "protocol": "udp",
                "data_protocol": "udp",
                "sync_protocol": "udp" if getattr(args, 'clock_sync', False) else "none",
                "total_packets": count,
                "warmup": warmup,
                "sent_count": sent_count,
                "success_count": success_count,
                "first_emit_ts_ns": int(emit_ts_samples[0]) if emit_ts_samples else None,
                "last_emit_ts_ns": int(emit_ts_samples[-1]) if emit_ts_samples else None,
                "pgen": float(getattr(args, 'pgen', 1.0)),
                "kernel_timestamp": getattr(args, 'kernel_timestamp', False),
                "args": {
                    "cpu": args.cpu,
                    "rt_priority": args.rt_priority,
                    "sock_buf": args.sock_buf,
                    "busy_poll_us": args.busy_poll_us,
                    "pace_mode": args.pace_mode,
                    "count_interval": args.count_interval,
                    "spin_margin_us": args.spin_margin_us,
                },
                "samples": {
                    "rtt_p50_ns": int(percentile(rtt, 0.50)),
                    "rtt_p95_ns": int(percentile(rtt, 0.95)),
                    "rtt_p99_ns": int(percentile(rtt, 0.99)),
                    "e2r_p50_ns": int(percentile(e2r, 0.50)),
                    "e2r_p95_ns": int(percentile(e2r, 0.95)),
                },
            }
            if args.diag and send_timings:
                metadata["send_timings_ns"] = send_timings[:10]
                metadata["recv_timings_ns"] = recv_timings[:10]
            with open(json_path, "w") as f:
                json.dump(metadata, f, indent=2)

    if args.quiet:
        print(f"exchanges={count}")
        print(f"warmup={warmup}")
        print(f"sent={sent_count}")
        print(f"success={success_count}")
        print_sender_group("p50", percentile(rtt, 0.50), percentile(e2r, 0.50), percentile_inverse(w_sorted, 0.50))
        print_sender_group("p95", percentile(rtt, 0.95), percentile(e2r, 0.95), percentile_inverse(w_sorted, 0.95))
        print_sender_group("p99", percentile(rtt, 0.99), percentile(e2r, 0.99), percentile_inverse(w_sorted, 0.99))
        print_sender_group("min", min(rtt) if rtt else 0, min(e2r) if e2r else 0, max_werner_sample)
        print_sender_group("max", max(rtt) if rtt else 0, max(e2r) if e2r else 0, min(w_sorted) if w_sorted else 0.0)
        print_sender_group("last", last_round_trip_perf, last_emit_to_remote, local_werner)
        print(f"sender_werner_max={max_werner_sample:.6f}")
        return 0

    print("sender_mode=fast4")
    print(f"exchanges={count} warmup={warmup} sent={sent_count} success={success_count}")
    print_sender_group("p50", percentile(rtt, 0.50), percentile(e2r, 0.50), percentile_inverse(w_sorted, 0.50))
    print_sender_group("p95", percentile(rtt, 0.95), percentile(e2r, 0.95), percentile_inverse(w_sorted, 0.95))
    print_sender_group("p99", percentile(rtt, 0.99), percentile(e2r, 0.99), percentile_inverse(w_sorted, 0.99))
    print_sender_group("min", min(rtt) if rtt else 0, min(e2r) if e2r else 0, max_werner_sample)
    print_sender_group("max", max(rtt) if rtt else 0, max(e2r) if e2r else 0, min(w_sorted) if w_sorted else 0.0)
    print_sender_group("last", last_round_trip_perf, last_emit_to_remote, local_werner)
    print(f"sender_werner_max={max_werner_sample:.6f}")
    return 0


def run_receiver(args):
    ptp_offset_ns = read_ptp_offset()
    if ptp_offset_ns != 0:
        print(f"[PTP] Using offset from daemon: {ptp_offset_ns} ns")
    
    if getattr(args, 'clock_sync', False):
        clock_sync_port = getattr(args, 'clock_sync_port', 7501)
        clock_sync_samples = getattr(args, 'clock_sync_samples', 264)
        clock_sync_kernel_ts = getattr(args, 'clock_sync_kernel_timestamp', True)
        
        print(f"[Clock Sync AEGO] Starting UDP clock sync server on port {clock_sync_port}...")
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp_sock:
                udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                udp_sock.bind(("0.0.0.0", clock_sync_port))
                
                serve_clock_sync_udp(udp_sock, clock_sync_samples, clock_sync_kernel_ts)
                
            print("[Clock Sync AEGO] UDP clock sync server completed. Starting experiments...")
        except Exception as e:
            print(f"[Clock Sync AEGO] Failed: {e}")
            print("[Clock Sync AEGO] Continuing to experiments without sync...")
    
    apply_cpu_rt(args.cpu, args.rt_priority)
    count = max(1, int(args.count))
    warmup = max(0, min(int(args.warmup), count - 1))
    recv_to_ack_samples = []
    total_view_samples = []
    sender_to_receiver_samples = []
    recv_count = 0
    closed_early = False
    last_sender_to_receiver = 0
    inbuf = bytearray(TS_SIZE)
    outbuf = bytearray(TS_SIZE)
    
    recv_timings = [] if args.diag else None
    ack_timings = [] if args.diag else None
    
    count_interval_ns = int(float(args.count_interval) * 1_000_000_000)
    spin_margin_ns = int(float(args.spin_margin_us) * 1_000)
    pgen = min(1.0, max(0.0, float(getattr(args, 'pgen', 1.0))))
    
    success_indices = []
    inter_success_gaps = []
    last_success_count_idx = None
    failure_start_count_idx = None
    
    gc_was_enabled = gc.isenabled()
    gc.disable()

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as server:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            enable_low_latency_socket(server, args.sock_buf, args.busy_poll_us, getattr(args, 'kernel_timestamp', False))
            server.bind((args.listen_host, args.listen_port))
            server.settimeout(args.accept_timeout)
            
            first_pkt_data = None
            client_addr = None
            
            while True:
                try:
                    data, addr = server.recvfrom(128)
                except socket.timeout:
                    raise RuntimeError(f"Receiver timeout waiting for UDP connection on {args.listen_host}:{args.listen_port} after {args.accept_timeout}s.")
                if data == b"AEGOPING":
                    server.sendto(b"AEGOPONG", addr)
                    continue
                if len(data) >= TS_SIZE:
                    first_pkt_data = data[:TS_SIZE]
                    client_addr = addr
                    break
            
            server.settimeout(5.0)
            for i in range(count):
                if args.diag:
                    t_recv_pre = time.perf_counter_ns()

                if i == 0 and first_pkt_data:
                    inbuf[:TS_SIZE] = first_pkt_data
                else:
                    try:
                        nbytes, client_addr = server.recvfrom_into(inbuf)
                    except (socket.timeout, OSError):
                        closed_early = True
                        break
                recv_count += 1
                
                if args.diag:
                    t_recv_post = time.perf_counter_ns()
                    recv_timings.append(t_recv_post - t_recv_pre)
                
                # 1. Unpack the timestamp and the client flag
                ts_emit_ns, sender_flag = struct.unpack(TS_FORMAT, inbuf[:TS_SIZE])
                ts_recv_ns = time.time_ns()
                
                # 2. Determine success/failure before packing the response
                is_success = True
                if pgen < 1.0 and i >= warmup:
                    is_success = random.random() <= pgen
                
                # 3. Pack the receive timestamp and the success bit (1 or 0)
                struct.pack_into(TS_FORMAT, outbuf, 0, ts_recv_ns, 1 if is_success else 0)
                
                if args.diag:
                    t_ack_pre = time.perf_counter_ns()
                
                ts_ack_sent_ns = time.time_ns()
                
                if args.diag:
                    t_ack_post = time.perf_counter_ns()
                    ack_timings.append(t_ack_post - t_ack_pre)
                
                server.sendto(outbuf, client_addr)
                last_sender_to_receiver = max(0, abs(ts_recv_ns - ts_emit_ns))
                last_recv_to_ack = max(0, ts_ack_sent_ns - ts_recv_ns)
                
                if i >= warmup:
                    if is_success:
                        sender_to_receiver_samples.append(last_sender_to_receiver)
                        recv_to_ack_samples.append(last_recv_to_ack)
                        total_view_samples.append(last_sender_to_receiver + last_recv_to_ack)
                        success_indices.append(i)
                        
                        if last_success_count_idx is None:
                            inter_success_gap = 0
                        else:
                            inter_success_gap = i - last_success_count_idx
                        inter_success_gaps.append(inter_success_gap)
                        last_success_count_idx = i
                        failure_start_count_idx = None
                    else:
                        if failure_start_count_idx is None:
                            failure_start_count_idx = i
                
                if count_interval_ns > 0:
                    pace_wait(count_interval_ns, args.pace_mode, spin_margin_ns)
    finally:
        if gc_was_enabled:
            gc.enable()

    s2r = sorted(sender_to_receiver_samples)
    r2a = sorted(recv_to_ack_samples)
    total_view = sorted(total_view_samples)
    werner_samples = [
        werner_from_age_ns(age_ns, args.werner_min, args.t1_ns)
        for age_ns in sender_to_receiver_samples
    ]
    w_sorted = sorted(werner_samples)
    local_werner = werner_from_age_ns(last_sender_to_receiver, args.werner_min, args.t1_ns)
    max_werner_sample = max(werner_samples) if w_sorted else 0.0

    last_total_view = max(0, last_sender_to_receiver + last_recv_to_ack)
    s2r_min = min(s2r) if s2r else 0
    r2a_min = min(r2a) if r2a else 0
    total_view_min = min(total_view) if total_view else 0

    if args.plot:
        ensure_output_dir(args.plot_dir)
        csv_base = os.path.join(args.plot_dir, args.plot_prefix)
        csv_path = get_available_filename(csv_base, ".csv")
        json_dir = args.json_dir or default_json_dir(args.plot_dir)
        json_base = os.path.join(json_dir, args.plot_prefix)
        json_path = get_available_filename(json_base, ".json")
        
        with open(csv_path, "w") as f:
            f.write("count_index,s2r_ns,r2a_ns,total_ns,werner,inter_success_gap\n")
            for idx, (count_idx, s2r_val, r2a_val, total_val, w_val) in enumerate(
                zip(success_indices, sender_to_receiver_samples, recv_to_ack_samples, total_view_samples, werner_samples)
            ):
                inter_gap = inter_success_gaps[idx] if idx < len(inter_success_gaps) else 0
                f.write(f"{count_idx},{int(s2r_val)},{int(r2a_val)},{int(total_val)},{w_val:.6f},{inter_gap}\n")
        
        if args.json_output:
            ensure_output_dir(json_dir)
            metadata = {
                "mode": "receiver",
                "protocol": "udp",
                "data_protocol": "udp",
                "sync_protocol": "udp" if getattr(args, 'clock_sync', False) else "none",
                "total_packets": count,
                "warmup": warmup,
                "recv_count": recv_count,
                "success_count": len(success_indices),
                "pgen": pgen,
                "kernel_timestamp": getattr(args, 'kernel_timestamp', False),
                "args": {
                    "cpu": args.cpu,
                    "rt_priority": args.rt_priority,
                    "sock_buf": args.sock_buf,
                    "busy_poll_us": args.busy_poll_us,
                    "pace_mode": args.pace_mode,
                    "count_interval": args.count_interval,
                    "spin_margin_us": args.spin_margin_us,
                },
                "samples": {
                    "s2r_p50_ns": int(percentile(s2r, 0.50)),
                    "s2r_p95_ns": int(percentile(s2r, 0.95)),
                    "s2r_p99_ns": int(percentile(s2r, 0.99)),
                    "r2a_p50_ns": int(percentile(r2a, 0.50)),
                    "r2a_p95_ns": int(percentile(r2a, 0.95)),
                },
            }
            if args.diag and recv_timings:
                metadata["recv_timings_ns"] = recv_timings[:10]
                metadata["ack_timings_ns"] = ack_timings[:10]
            with open(json_path, "w") as f:
                json.dump(metadata, f, indent=2)

    if args.quiet:
        print(f"exchanges={count}")
        print(f"warmup={warmup}")
        print(f"received={recv_count}")
        if closed_early:
            print("receiver_stream_closed=1")
        print_receiver_group("p50", percentile(s2r, 0.50), percentile(r2a, 0.50), percentile(total_view, 0.50), percentile_inverse(w_sorted, 0.50))
        print_receiver_group("p95", percentile(s2r, 0.95), percentile(r2a, 0.95), percentile(total_view, 0.95), percentile_inverse(w_sorted, 0.95))
        print_receiver_group("p99", percentile(s2r, 0.99), percentile(r2a, 0.99), percentile(total_view, 0.99), percentile_inverse(w_sorted, 0.99))
        print_receiver_group("min", s2r_min, r2a_min, total_view_min, max_werner_sample)
        print_receiver_group("max", max(s2r) if s2r else 0, max(r2a) if r2a else 0, max(total_view) if total_view else 0, min(w_sorted) if w_sorted else 0.0)
        print_receiver_group("last", last_sender_to_receiver, last_recv_to_ack, last_total_view, local_werner)
        print(f"receiver_werner_max={max_werner_sample:.6f}")
        return 0

    print("receiver_mode=fast4")
    print(f"exchanges={count} warmup={warmup} received={recv_count}")
    if closed_early:
        print("receiver_stream_closed=1")
    print_receiver_group("p50", percentile(s2r, 0.50), percentile(r2a, 0.50), percentile(total_view, 0.50), percentile_inverse(w_sorted, 0.50))
    print_receiver_group("p95", percentile(s2r, 0.95), percentile(r2a, 0.95), percentile(total_view, 0.95), percentile_inverse(w_sorted, 0.95))
    print_receiver_group("p99", percentile(s2r, 0.99), percentile(r2a, 0.99), percentile(total_view, 0.99), percentile_inverse(w_sorted, 0.99))
    print_receiver_group("min", s2r_min, r2a_min, total_view_min, max_werner_sample)
    print_receiver_group("max", max(s2r) if s2r else 0, max(r2a) if r2a else 0, max(total_view) if total_view else 0, min(w_sorted) if w_sorted else 0.0)
    print_receiver_group("last", last_sender_to_receiver, last_recv_to_ack, last_total_view, local_werner)
    print(f"receiver_werner_max={max_werner_sample:.6f}")
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