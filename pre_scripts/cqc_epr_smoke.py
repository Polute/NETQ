#!/usr/bin/env python3
import argparse
import sys
import time
from importlib import metadata
from pathlib import Path

from cqc.pythonLib import CQCConnection
from simulaqron.network import Network
from simulaqron.settings import simulaqron_settings


EXPECTED_VERSIONS = {
    "simulaqron": "3.0.16",
    "qutip": "4.7.5",
    "scipy": "1.12.0",
}


def parse_args():
    """Read the command-line options for the simple high-level CQC smoke test."""
    repo_root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="Minimal smoke test using CQC createEPR/recvEPR.")
    parser.add_argument(
        "--config",
        default=str(repo_root / "config" / "madrid_3nodes.json"),
        help="Path to the SimulaQron network JSON file.",
    )
    parser.add_argument(
        "--network-name",
        default="madrid_demo",
        help="Network name inside the JSON file.",
    )
    parser.add_argument(
        "--rounds",
        type=int,
        default=10,
        help="Number of EPR rounds to test.",
    )
    parser.add_argument(
        "--sender",
        default="Alice",
        help="Node that creates the EPR pair.",
    )
    parser.add_argument(
        "--receiver",
        default="Charlie",
        help="Node that receives the EPR half.",
    )
    parser.add_argument(
        "--noise",
        action="store_true",
        help="Enable SimulaQron noisy qubits for this smoke test.",
    )
    parser.add_argument(
        "--t1",
        type=float,
        default=1.0,
        help="T1 value used when --noise is enabled.",
    )
    parser.add_argument(
        "--memory-wait",
        type=float,
        default=0.0,
        help="Seconds to wait before measurement so the time-based noise model can act.",
    )
    return parser.parse_args()


def print_versions():
    """Print package versions so the user can quickly verify the environment."""
    print("Detected versions:")
    for package, expected in EXPECTED_VERSIONS.items():
        version = metadata.version(package)
        suffix = " (expected)" if version == expected else f" (expected {expected})"
        print(f"  {package}=={version}{suffix}")


def main():
    """Start a minimal network, create EPR pairs through CQC, and verify endpoint correlations."""
    args = parse_args()
    config_path = str(Path(args.config).resolve())

    print_versions()
    print(f"Network config: {config_path}")
    print(f"Network name: {args.network_name}")
    print(f"Rounds: {args.rounds}")
    print(f"Sender: {args.sender}")
    print(f"Receiver: {args.receiver}")
    print(f"Noise enabled: {args.noise}")
    print(f"T1: {args.t1}")
    print(f"Memory wait: {args.memory_wait}")
    print()

    network = None
    try:
        simulaqron_settings.network_config_file = config_path
        simulaqron_settings.backend = "qutip"
        simulaqron_settings.noisy_qubits = bool(args.noise)
        simulaqron_settings.t1 = float(args.t1)

        network = Network(name=args.network_name, network_config_file=config_path, new=False)
        network.start()

        outcomes = []
        with CQCConnection(args.sender, network_name=args.network_name) as sender:
            with CQCConnection(args.receiver, appID=1, network_name=args.network_name) as receiver:
                for round_index in range(args.rounds):
                    # The sender creates the pair and the receiver picks up the remote half.
                    q_sender = sender.createEPR(args.receiver, remote_appID=1)
                    q_receiver = receiver.recvEPR()
                    if args.memory_wait > 0:
                        # Waiting before measurement lets the time-based memory-noise model act.
                        time.sleep(args.memory_wait)
                    m_sender = q_sender.measure()
                    m_receiver = q_receiver.measure()
                    outcomes.append((m_sender, m_receiver))
                    print(
                        f"round {round_index + 1:02d}: "
                        f"{args.sender}={m_sender} {args.receiver}={m_receiver}"
                    )

        correlated = all(a == b for a, b in outcomes)
        print()
        print(f"All rounds correlated: {correlated}")
        if args.noise and args.memory_wait <= 0:
            print("Note: noise is enabled, but memory_wait is 0, so the effect may be very small.")
        if not correlated:
            return 1
        return 0
    finally:
        if network is not None:
            network.stop()


if __name__ == "__main__":
    sys.exit(main())
