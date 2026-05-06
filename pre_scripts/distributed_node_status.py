#!/usr/bin/env python3
"""
Check whether SimulaQron nodes appear to be already running.

This script does not start anything. It only inspects the configured sockets and
tries a lightweight CQC connection.
"""

import argparse
import json
import socket
import sys
from pathlib import Path

from cqc.pythonLib import CQCConnection
from simulaqron.settings import simulaqron_settings


def parse_args():
    """Read options for the node-status checker."""
    repo_root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="Check whether configured SimulaQron nodes are already up.")
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
        "--node",
        default=None,
        help="Optional single node to check. If omitted, all nodes in the network are checked.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=1.0,
        help="Socket timeout in seconds for raw port checks.",
    )
    return parser.parse_args()


def load_network(config_path, network_name):
    """Load one network entry from the JSON file."""
    with open(config_path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if network_name not in data:
        available = ", ".join(sorted(data))
        raise ValueError(f"Network '{network_name}' not found. Available networks: {available}")
    return data[network_name]


def socket_check(host, port, timeout):
    """Return True if a TCP socket accepts a connection."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def cqc_check(node_name, network_name):
    """Return True if a lightweight CQC connection can be established to the node."""
    try:
        connection = CQCConnection(node_name, appID=999, retry_connection=False, network_name=network_name)
    except Exception:
        return False
    else:
        connection.close()
        return True


def main():
    """Print per-node status for app, CQC, vnode, and a real CQC handshake."""
    args = parse_args()
    config_path = str(Path(args.config).resolve())
    simulaqron_settings.network_config_file = config_path

    network = load_network(config_path, args.network_name)
    nodes = network["nodes"]
    node_names = [args.node] if args.node else sorted(nodes)

    print(f"Shared config: {config_path}")
    print(f"Network name: {args.network_name}")
    print()

    all_ok = True
    for node_name in node_names:
        if node_name not in nodes:
            raise ValueError(f"Node '{node_name}' not present in network '{args.network_name}'")

        app_host, app_port = nodes[node_name]["app_socket"]
        cqc_host, cqc_port = nodes[node_name]["cqc_socket"]
        vnode_host, vnode_port = nodes[node_name]["vnode_socket"]

        app_ok = socket_check(app_host, app_port, args.timeout)
        cqc_port_ok = socket_check(cqc_host, cqc_port, args.timeout)
        vnode_ok = socket_check(vnode_host, vnode_port, args.timeout)
        cqc_protocol_ok = cqc_check(node_name, args.network_name)

        node_ok = cqc_port_ok and vnode_ok and cqc_protocol_ok
        all_ok = all_ok and node_ok

        print(node_name)
        print(f"  app socket reachable:   {app_ok} ({app_host}:{app_port})")
        print(f"  cqc socket reachable:   {cqc_port_ok} ({cqc_host}:{cqc_port})")
        print(f"  vnode socket reachable: {vnode_ok} ({vnode_host}:{vnode_port})")
        print(f"  CQC protocol handshake: {cqc_protocol_ok}")
        print(f"  node ready:             {node_ok}")
        print()

    print("Note:")
    print("  This script can tell you whether the nodes are already running and reachable.")
    print("  It cannot reliably tell whether they were started by separate terminals or by one shell script.")
    print("  For that distinction you would need OS-level process-tree inspection or your own PID/metadata tracking.")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
