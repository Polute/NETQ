#!/usr/bin/env python3
"""
Run one SimulaQron node in the foreground.

This helper is meant for the "one terminal per node" and "one machine per node" workflow.
It does not replace the existing demos; it only starts the local Virtual Node + local CQC
server for the node you choose, using the shared SimulaQron network JSON.
"""

import argparse
import json
import signal
import sys
import time
from pathlib import Path

from simulaqron.network import Network
from simulaqron.settings import simulaqron_settings


STOP_REQUESTED = False


def handle_stop(_signo, _stack_frame):
    """Mark that the foreground runner should stop cleanly on Ctrl+C or SIGTERM."""
    global STOP_REQUESTED
    STOP_REQUESTED = True


def parse_args():
    """Read command-line arguments for the one-node foreground launcher."""
    repo_root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(
        description=(
            "Start exactly one SimulaQron node in the foreground. "
            "Use one terminal or one physical machine per node."
        )
    )
    parser.add_argument(
        "--config",
        default=str(repo_root / "config" / "madrid_3nodes.json"),
        help="Path to the generated SimulaQron network JSON shared by all nodes.",
    )
    parser.add_argument(
        "--network-name",
        default="madrid_demo",
        help="Network name inside the SimulaQron JSON.",
    )
    parser.add_argument(
        "--node",
        required=True,
        help="Node name to start, for example Alice, Bob, or Charlie.",
    )
    parser.add_argument(
        "--backend",
        default="qutip",
        help="SimulaQron backend to use for this local node.",
    )
    parser.add_argument(
        "--log-level",
        type=int,
        default=30,
        help="Python logging level passed to SimulaQron (default: 30 = WARNING).",
    )
    parser.add_argument(
        "--noisy-qubits",
        action="store_true",
        help="Enable SimulaQron's built-in noisy-qubits mode for this node.",
    )
    parser.add_argument(
        "--t1",
        type=float,
        default=1.0,
        help="T1 value used only if noisy-qubits is enabled.",
    )
    return parser.parse_args()


def load_network_config(config_path):
    """Load the JSON file so we can validate the requested node and print useful addresses."""
    with open(config_path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def get_node_info(network_data, network_name, node_name):
    """Return the node sockets and neighbor list for one node in the selected network."""
    if network_name not in network_data:
        available = ", ".join(sorted(network_data))
        raise ValueError(f"Network '{network_name}' not found. Available networks: {available}")

    network_entry = network_data[network_name]
    nodes = network_entry.get("nodes", {})
    topology = network_entry.get("topology", {})

    if node_name not in nodes:
        available = ", ".join(sorted(nodes))
        raise ValueError(f"Node '{node_name}' not found. Available nodes: {available}")

    return nodes[node_name], topology.get(node_name, [])


def configure_simulaqron(config_path, backend, log_level, noisy_qubits, t1):
    """Point SimulaQron at the shared network JSON and configure the local backend."""
    simulaqron_settings.network_config_file = config_path
    simulaqron_settings.backend = backend
    simulaqron_settings.log_level = log_level
    simulaqron_settings.noisy_qubits = bool(noisy_qubits)
    simulaqron_settings.t1 = float(t1)


def print_startup_summary(node_name, network_name, node_info, neighbors, backend, noisy_qubits, t1, config_path):
    """Show exactly what this foreground process is about to expose to the rest of the network."""
    app_host, app_port = node_info["app_socket"]
    cqc_host, cqc_port = node_info["cqc_socket"]
    vnode_host, vnode_port = node_info["vnode_socket"]

    print(f"Starting node: {node_name}")
    print(f"Network name: {network_name}")
    print(f"Shared config: {config_path}")
    print(f"Backend: {backend}")
    print(f"Noisy qubits: {bool(noisy_qubits)}")
    print(f"T1: {t1}")
    print(f"App socket:   {app_host}:{app_port}")
    print(f"CQC socket:   {cqc_host}:{cqc_port}")
    print(f"VNode socket: {vnode_host}:{vnode_port}")
    print(f"Topology neighbors: {', '.join(neighbors) if neighbors else '(none)'}")
    print()
    print("Press Ctrl+C to stop this node.")


def main():
    """Start only the requested node and keep it alive until the user stops it."""
    args = parse_args()
    config_path = str(Path(args.config).resolve())
    network_data = load_network_config(config_path)
    node_info, neighbors = get_node_info(network_data, args.network_name, args.node)

    configure_simulaqron(
        config_path=config_path,
        backend=args.backend,
        log_level=args.log_level,
        noisy_qubits=args.noisy_qubits,
        t1=args.t1,
    )

    print_startup_summary(
        node_name=args.node,
        network_name=args.network_name,
        node_info=node_info,
        neighbors=neighbors,
        backend=args.backend,
        noisy_qubits=args.noisy_qubits,
        t1=args.t1,
        config_path=config_path,
    )

    signal.signal(signal.SIGINT, handle_stop)
    signal.signal(signal.SIGTERM, handle_stop)

    network = Network(
        name=args.network_name,
        nodes=[args.node],
        network_config_file=config_path,
        new=False,
    )

    try:
        network.start(wait_until_running=True)
        while not STOP_REQUESTED:
            time.sleep(0.5)
    finally:
        network.stop()

    return 0


if __name__ == "__main__":
    sys.exit(main())
