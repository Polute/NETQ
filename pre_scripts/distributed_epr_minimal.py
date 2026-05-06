#!/usr/bin/env python3
"""
Minimal EPR example for an already running distributed SimulaQron network.

This script does not start or stop nodes. It assumes Alice/Charlie/Bob (or the
chosen nodes) are already running somewhere and reachable through the shared
network JSON.
"""

import argparse
import sys
import threading
import time
from pathlib import Path

from cqc.pythonLib import CQCConnection
from simulaqron.settings import simulaqron_settings


def parse_args():
    """Read command-line options for the already-running minimal EPR test."""
    repo_root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(
        description="Minimal createEPR()/recvEPR() example for nodes that are already running."
    )
    parser.add_argument(
        "--config",
        default=str(repo_root / "config" / "madrid_3nodes.json"),
        help="Path to the shared SimulaQron network JSON.",
    )
    parser.add_argument(
        "--network-name",
        default="madrid_demo",
        help="Network name inside the JSON.",
    )
    parser.add_argument(
        "--sender",
        default="Alice",
        help="Node that calls createEPR(receiver).",
    )
    parser.add_argument(
        "--receiver",
        default="Charlie",
        help="Node that calls recvEPR().",
    )
    parser.add_argument(
        "--sender-app-id",
        type=int,
        default=0,
        help="App ID used by the sender CQCConnection.",
    )
    parser.add_argument(
        "--receiver-app-id",
        type=int,
        default=1,
        help="App ID used by the receiver CQCConnection.",
    )
    parser.add_argument(
        "--rounds",
        type=int,
        default=1,
        help="How many EPR rounds to run.",
    )
    parser.add_argument(
        "--measure-wait",
        type=float,
        default=0.0,
        help="Optional wait in seconds before measuring both EPR halves.",
    )
    return parser.parse_args()


def recv_epr_worker(node_name, network_name, app_id, ready_event, measure_event, result):
    """Receive and measure the remote EPR half inside the same CQC connection and thread."""
    try:
        with CQCConnection(node_name, appID=app_id, network_name=network_name) as receiver:
            q_receiver = receiver.recvEPR()
            result["received_at_ns"] = time.perf_counter_ns()
            ready_event.set()
            if not measure_event.wait(timeout=10.0):
                raise TimeoutError(f"{node_name} did not receive the measurement signal in time")
            result["receiver_measurement"] = q_receiver.measure()
            result["receiver_measured_at_ns"] = time.perf_counter_ns()
    except Exception as exc:
        result["error"] = exc
        ready_event.set()


def configure_simulaqron(config_path):
    """Point the CQC library at the shared network JSON."""
    simulaqron_settings.network_config_file = config_path


def ns_with_seconds(value_ns):
    """Format one nanosecond duration together with its value in seconds."""
    return f"{value_ns} ns ({value_ns / 1e9:.9f} s)"


def run_round(sender_name, receiver_name, network_name, sender_app_id, receiver_app_id, measure_wait):
    """Run one createEPR/recvEPR round against already running nodes."""
    receiver_result = {}
    receiver_ready = threading.Event()
    measure_event = threading.Event()
    receiver_thread = threading.Thread(
        target=recv_epr_worker,
        args=(receiver_name, network_name, receiver_app_id, receiver_ready, measure_event, receiver_result),
        daemon=True,
    )
    receiver_thread.start()

    with CQCConnection(sender_name, appID=sender_app_id, network_name=network_name) as sender:
        created_at_ns = time.perf_counter_ns()
        q_sender = sender.createEPR(receiver_name, remote_appID=receiver_app_id)
        create_completed_at_ns = time.perf_counter_ns()

        if not receiver_ready.wait(timeout=10.0):
            raise TimeoutError(f"{receiver_name}.recvEPR() did not complete in time")
        if "error" in receiver_result:
            raise receiver_result["error"]

        recv_completed_at_ns = receiver_result["received_at_ns"]

        if measure_wait > 0:
            time.sleep(measure_wait)

        measure_event.set()
        m_sender = q_sender.measure()
        sender_measured_at_ns = time.perf_counter_ns()

        receiver_thread.join(timeout=10.0)
        if receiver_thread.is_alive():
            raise TimeoutError(f"{receiver_name} did not finish measurement in time")
        if "error" in receiver_result:
            raise receiver_result["error"]

    return {
        "created_at_ns": created_at_ns,
        "create_completed_at_ns": create_completed_at_ns,
        "recv_completed_at_ns": recv_completed_at_ns,
        "sender_measured_at_ns": sender_measured_at_ns,
        "receiver_measured_at_ns": receiver_result["receiver_measured_at_ns"],
        "create_call_ns": create_completed_at_ns - created_at_ns,
        "create_to_recv_ns": recv_completed_at_ns - create_completed_at_ns,
        "total_until_recv_ns": recv_completed_at_ns - created_at_ns,
        "sender_measurement": m_sender,
        "receiver_measurement": receiver_result["receiver_measurement"],
    }


def main():
    """Run the minimal already-running EPR example and print timing plus outcomes."""
    args = parse_args()
    config_path = str(Path(args.config).resolve())
    configure_simulaqron(config_path)

    print(f"Shared config: {config_path}")
    print(f"Network name: {args.network_name}")
    print(f"Sender: {args.sender} (appID={args.sender_app_id})")
    print(f"Receiver: {args.receiver} (appID={args.receiver_app_id})")
    print(f"Rounds: {args.rounds}")
    print(f"Measure wait: {args.measure_wait}")
    print()

    outcomes = []
    for round_index in range(args.rounds):
        result = run_round(
            sender_name=args.sender,
            receiver_name=args.receiver,
            network_name=args.network_name,
            sender_app_id=args.sender_app_id,
            receiver_app_id=args.receiver_app_id,
            measure_wait=args.measure_wait,
        )
        outcomes.append(result)
        print(f"Round {round_index + 1:02d}")
        print(f"  sender bit: {result['sender_measurement']}")
        print(f"  receiver bit: {result['receiver_measurement']}")
        print(f"  createEPR() local call time: {ns_with_seconds(result['create_call_ns'])}")
        print(f"  createEPR()->recvEPR() delta: {ns_with_seconds(result['create_to_recv_ns'])}")
        print(f"  total until recvEPR(): {ns_with_seconds(result['total_until_recv_ns'])}")
        print()

    correlated = all(item["sender_measurement"] == item["receiver_measurement"] for item in outcomes)
    print(f"All rounds correlated: {correlated}")
    return 0 if correlated else 1


if __name__ == "__main__":
    sys.exit(main())
