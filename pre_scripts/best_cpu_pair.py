#!/usr/bin/env python3
import argparse
import os
from collections import Counter


def parse_args():
    p = argparse.ArgumentParser(description="Suggest best sender/receiver CPU pair for fast4 benchmark.")
    p.add_argument("--top", type=int, default=5, help="How many candidate pairs to print.")
    return p.parse_args()


def read_text(path):
    try:
        with open(path, "r", encoding="ascii", errors="ignore") as f:
            return f.read().strip()
    except OSError:
        return None


def parse_cpu_list(s):
    out = set()
    if not s:
        return out
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.update(range(int(a), int(b) + 1))
        else:
            out.add(int(part))
    return out


def online_cpus():
    return sorted(parse_cpu_list(read_text("/sys/devices/system/cpu/online")))


def siblings_of(cpu):
    s = read_text(f"/sys/devices/system/cpu/cpu{cpu}/topology/thread_siblings_list")
    return parse_cpu_list(s)


def cpu_process_load():
    # Count processes currently scheduled on each CPU (lightweight proxy).
    counts = Counter()
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        stat = read_text(f"/proc/{pid}/stat")
        if not stat:
            continue
        parts = stat.split()
        if len(parts) < 39:
            continue
        try:
            cpu = int(parts[38])
        except ValueError:
            continue
        counts[cpu] += 1
    return counts


def main():
    args = parse_args()
    cpus = online_cpus()
    if len(cpus) < 2:
        print("error: fewer than 2 online CPUs")
        return 1

    load = cpu_process_load()
    sib_map = {c: siblings_of(c) for c in cpus}

    candidates = []
    for rx in cpus:
        for tx in cpus:
            if tx == rx:
                continue
            # Prefer non-sibling pairs
            sibling_penalty = 1 if tx in sib_map.get(rx, set()) else 0
            score = (
                sibling_penalty * 1_000_000
                + load.get(rx, 0) * 1000
                + load.get(tx, 0)
            )
            candidates.append((score, rx, tx, sibling_penalty, load.get(rx, 0), load.get(tx, 0)))

    candidates.sort(key=lambda x: x[0])
    topn = max(1, int(args.top))

    print("best_cpu_pair")
    best = candidates[0]
    _, best_rx, best_tx, best_pen, best_rx_load, best_tx_load = best
    print(f"recommended_receiver_cpu={best_rx}")
    print(f"recommended_sender_cpu={best_tx}")
    print(f"non_sibling={'yes' if best_pen == 0 else 'no'}")
    print(f"receiver_load={best_rx_load}")
    print(f"sender_load={best_tx_load}")
    print("")

    print("top_candidates")
    print("rank receiver sender non_sibling receiver_load sender_load score")
    for i, row in enumerate(candidates[:topn], start=1):
        score, rx, tx, pen, rx_load, tx_load = row
        print(
            f"{i:>4} {rx:>8} {tx:>6} "
            f"{('yes' if pen == 0 else 'no'):>11} {rx_load:>13} {tx_load:>11} {score}"
        )

    print("")
    print("commands")
    print(
        f"sudo taskset -c {best_rx} python pre_scripts/minimal_epr_receiver_fast.py "
        "--listen-port 7401 --count 1000 --warmup 100 --rt-priority 50 --quiet"
    )
    print(
        f"sudo taskset -c {best_tx} python pre_scripts/minimal_epr_sender_fast.py "
        "--receiver-host 127.0.0.1 --receiver-port 7401 "
        "--count 1000 --warmup 100 --rt-priority 50 --quiet --show-arrows"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
