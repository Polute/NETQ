#!/usr/bin/env python3
import argparse
import json
from pathlib import Path


def parse_args():
    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="Build a SimulaQron network JSON from a cleaner node description.")
    parser.add_argument(
        "--input",
        default=str(repo_root / "config" / "madrid_nodes_clean.json"),
        help="Path to the clean JSON file.",
    )
    parser.add_argument(
        "--output",
        default=str(repo_root / "config" / "madrid_3nodes.json"),
        help="Path to the generated SimulaQron network JSON file.",
    )
    return parser.parse_args()


def load_json(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def build_topology(node_names, links):
    topology = {name: [] for name in node_names}
    for raw_link in links:
        if len(raw_link) != 2:
            raise ValueError(f"Each link must contain exactly 2 node names, got: {raw_link}")
        left, right = raw_link
        if left not in topology:
            raise ValueError(f"Unknown node in links: {left}")
        if right not in topology:
            raise ValueError(f"Unknown node in links: {right}")
        if right not in topology[left]:
            topology[left].append(right)
        if left not in topology[right]:
            topology[right].append(left)
    return topology


def build_network(clean_config):
    network_name = clean_config["network_name"]
    defaults = clean_config.get("defaults", {})
    default_host = defaults.get("host", "localhost")
    nodes = {}
    node_names = []

    for node in clean_config["nodes"]:
        name = node["name"]
        host = node.get("host", default_host)
        base_port = int(node["base_port"])
        node_names.append(name)
        nodes[name] = {
            "app_socket": [host, base_port],
            "cqc_socket": [host, base_port + 1],
            "vnode_socket": [host, base_port + 2],
        }

    topology = build_topology(node_names=node_names, links=clean_config.get("links", []))
    return {
        network_name: {
            "nodes": nodes,
            "topology": topology,
        }
    }


def main():
    args = parse_args()
    clean_config = load_json(args.input)
    built_config = build_network(clean_config)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as handle:
        json.dump(built_config, handle, indent=4)

    network_name = clean_config["network_name"]
    print(f"Generated {output_path}")
    print(f"Network name: {network_name}")
    print(f"Nodes: {', '.join(built_config[network_name]['nodes'].keys())}")


if __name__ == "__main__":
    main()

