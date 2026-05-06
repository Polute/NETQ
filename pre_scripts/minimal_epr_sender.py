#!/usr/bin/env python3
"""
Minimal sender process for a two-node EPR state sync over raw sockets.

Protocol (binary, timestamp only):
1) Sender -> Receiver: 8-byte unsigned timestamp (unix ns) for "new EPR emitted".
2) Receiver updates local state and sends back one 8-byte timestamp for "state updated".
3) Sender receives that timestamp and updates its own local state to keep both views homogeneous.
"""

import argparse
import math
import socket
import struct
import sys
import time


TS_FORMAT = "!Q"
TS_SIZE = struct.calcsize(TS_FORMAT)


def enable_low_latency_socket(sock):
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass
    if hasattr(socket, "TCP_QUICKACK"):
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_QUICKACK, 1)
        except OSError:
            pass


def parse_args():
    parser = argparse.ArgumentParser(description="Minimal sender: emit EPR timestamp and wait for state-update timestamp.")
    parser.add_argument("--listen-host", default="0.0.0.0", help="Host/IP where sender listens for receiver ack.")
    parser.add_argument("--listen-port", type=int, default=7000, help="Unique sender TCP port.")
    parser.add_argument("--receiver-host", required=True, help="Host/IP of the receiver process.")
    parser.add_argument("--receiver-port", type=int, required=True, help="Unique receiver TCP port.")
    parser.add_argument(
        "--interactive-send",
        action="store_true",
        help="Ask in terminal whether to send the EPR after receiver readiness is detected.",
    )
    parser.add_argument(
        "--detect-timeout",
        type=float,
        default=30.0,
        help="Seconds to keep probing receiver readiness before failing.",
    )
    parser.add_argument(
        "--detect-interval",
        type=float,
        default=0.5,
        help="Seconds between readiness probes.",
    )
    parser.add_argument("--initial-werner", type=float, default=0.0, help="Initial local Werner parameter before creating EPR.")
    parser.add_argument("--werner-min", type=float, default=0.2, help="Minimum Werner floor used by the time model.")
    parser.add_argument("--t1-ns", type=float, default=1_000_000.0, help="Decay scale in ns for w(age).")
    parser.add_argument("--accept-timeout", type=float, default=30.0, help="Seconds to wait for receiver update timestamp.")
    parser.add_argument("--connect-timeout", type=float, default=10.0, help="Seconds to wait while connecting to receiver.")
    parser.add_argument("--quiet", action="store_true", help="Reduce output to key timing lines only.")
    return parser.parse_args()


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
    lines = []
    for row in matrix:
        lines.append("[" + ", ".join(f"{value:.6f}" for value in row) + "]")
    return "\n".join(lines)


def recv_exact(sock, size):
    chunks = []
    remaining = size
    while remaining > 0:
        part = sock.recv(remaining)
        if not part:
            raise ConnectionError("Socket closed before receiving full timestamp")
        chunks.append(part)
        remaining -= len(part)
    return b"".join(chunks)


def recv_timestamp(sock):
    data = recv_exact(sock, TS_SIZE)
    return struct.unpack(TS_FORMAT, data)[0]


def send_timestamp(sock, timestamp_ns):
    # use blocking sendall to avoid busy-wait loops and reduce syscall/scheduler overhead
    try:
        sock.sendall(struct.pack(TS_FORMAT, int(timestamp_ns)))
    except OSError as exc:
        raise ConnectionError("Socket closed while sending timestamp") from exc


def connect_receiver_until_ready(receiver_host, receiver_port, connect_timeout, detect_timeout, detect_interval):
    deadline = time.monotonic() + max(0.0, detect_timeout)
    last_error = None

    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        timeout = min(max(0.001, float(connect_timeout)), max(0.001, remaining))
        try:
            sock = socket.create_connection((receiver_host, receiver_port), timeout=timeout)
            enable_low_latency_socket(sock)
            return sock
        except OSError as exc:
            last_error = exc

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(max(0.0, detect_interval), remaining))

    raise TimeoutError("Receiver was not detected before detect-timeout expired") from last_error


def ask_send_confirmation():
    while True:
        raw = input("Receiver detectado. Enviar EPR ahora? [y/N]: ").strip().lower()
        if raw in ("", "n", "no"):
            return False
        if raw in ("y", "yes", "s", "si"):
            return True
        print("Respuesta invalida. Usa y/n.")


def format_ns_and_seconds(value_ns):
    value_ns = int(max(0, value_ns))
    return f"{value_ns} ({value_ns / 1e9:.9f} s)"


def main():
    args = parse_args()

    output_lines = []  # Collect all output for printing at the end

    local_werner = clamp_werner(args.initial_werner)
    
    density_before = density_matrix_from_werner_phi_plus(local_werner)

    output_lines.append("Sender started")
    output_lines.append(f"listen: {args.listen_host}:{args.listen_port}")
    output_lines.append(f"receiver target: {args.receiver_host}:{args.receiver_port}")
    output_lines.append(f"local Werner before: {local_werner:.6f}")
    output_lines.append("density before:")
    output_lines.extend(format_density(density_before).split('\n'))
    output_lines.append("")

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as ack_server:
        ack_server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        enable_low_latency_socket(ack_server)
        ack_server.bind((args.listen_host, args.listen_port))
        ack_server.listen(1)
        ack_server.settimeout(args.accept_timeout)

        with connect_receiver_until_ready(
            receiver_host=args.receiver_host,
            receiver_port=args.receiver_port,
            connect_timeout=args.connect_timeout,
            detect_timeout=args.detect_timeout,
            detect_interval=args.detect_interval,
        ) as receiver_data_socket:
            conn, addr = ack_server.accept()
            with conn:
                enable_low_latency_socket(conn)
                output_lines.append("Receiver detected and channels ready.")
                if args.interactive_send and not ask_send_confirmation():
                    output_lines.append("Send canceled by user before EPR emission.")
                    for line in output_lines:
                        print(line)
                    return 0

                ts_emit_ns = time.time_ns()
                send_start_perf_ns = time.perf_counter_ns()
                send_timestamp(receiver_data_socket, ts_emit_ns)
                send_call_duration_ns = time.perf_counter_ns() - send_start_perf_ns

                ts_remote_update_ns = recv_timestamp(conn)
                ts_sync_received_ns = time.time_ns()

    # NOW calculate W and density (after all network I/O is complete)
    age_ns = max(0, ts_remote_update_ns - ts_emit_ns)
    local_werner = werner_from_age_ns(age_ns=age_ns, werner_min=args.werner_min, t1_ns=args.t1_ns)
    density_after = density_matrix_from_werner_phi_plus(local_werner)
    base_ns = ts_emit_ns
    ts_remote_update_rel_ns = max(0, ts_remote_update_ns - base_ns)
    ts_sync_received_rel_ns = max(0, ts_sync_received_ns - base_ns)
    remote_update_to_ack_rel_ns = max(0, ts_sync_received_ns - ts_remote_update_ns)

    output_lines.append("Sender events (ns, absolute unix clock)")
    output_lines.append(f"ts_emit_new_epr_abs:        {ts_emit_ns}")
    output_lines.append(f"ts_remote_state_update_abs: {ts_remote_update_ns}")
    output_lines.append(f"ts_sender_received_ack_abs: {ts_sync_received_ns}")
    output_lines.append("")

    output_lines.append("Sender events (ns, relative to ts_emit_new_epr_abs)")
    output_lines.append(f"ts_emit_reference:      {format_ns_and_seconds(0)}")
    output_lines.append(f"ts_remote_state_update: {format_ns_and_seconds(ts_remote_update_rel_ns)}")
    output_lines.append(f"ts_sender_received_ack: {format_ns_and_seconds(ts_sync_received_rel_ns)}")
    output_lines.append(f"emit_to_remote_update:  {format_ns_and_seconds(ts_remote_update_rel_ns)}")
    output_lines.append(f"remote_update_to_ack:   {format_ns_and_seconds(remote_update_to_ack_rel_ns)}")
    output_lines.append(f"total_until_ack:        {format_ns_and_seconds(ts_sync_received_rel_ns)}")
    output_lines.append(f"local_send_call_time:   {format_ns_and_seconds(send_call_duration_ns)}")
    output_lines.append("")

    output_lines.append(f"local Werner after: {local_werner:.6f}")
    output_lines.append("density after:")
    output_lines.extend(format_density(density_after).split('\n'))
    output_lines.append("")
    output_lines.append("Sender finished.")

    if args.quiet:
        print(f"sender_werner={local_werner:.6f}")
        print(f"emit_to_remote={format_ns_and_seconds(ts_remote_update_rel_ns)}")
        print(f"round_trip={format_ns_and_seconds(ts_sync_received_rel_ns)}")
        return 0

    # Print all accumulated output after timing-critical section
    for line in output_lines:
        print(line)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Sender interrupted.")
        raise SystemExit(130)
