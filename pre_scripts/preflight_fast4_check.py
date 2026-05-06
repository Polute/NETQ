#!/usr/bin/env python3
import argparse
import glob
import os
from collections import defaultdict


def parse_args():
    p = argparse.ArgumentParser(description="Pre-flight CPU isolation checks for fast4 benchmarks.")
    p.add_argument("--sender-cpu", type=int, default=3)
    p.add_argument("--receiver-cpu", type=int, default=2)
    return p.parse_args()


def read_text(path):
    try:
        with open(path, "r", encoding="ascii", errors="ignore") as f:
            return f.read().strip()
    except OSError:
        return None


def parse_cpu_list(s):
    cpus = set()
    if not s:
        return cpus
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            cpus.update(range(int(a), int(b) + 1))
        else:
            cpus.add(int(part))
    return cpus


def format_cpu_set(cpus):
    if not cpus:
        return "-"
    return ",".join(str(c) for c in sorted(cpus))


def get_siblings(cpu):
    p = f"/sys/devices/system/cpu/cpu{cpu}/topology/thread_siblings_list"
    return parse_cpu_list(read_text(p))


def get_governor(cpu):
    p = f"/sys/devices/system/cpu/cpu{cpu}/cpufreq/scaling_governor"
    return read_text(p) or "unknown"


def get_online():
    return parse_cpu_list(read_text("/sys/devices/system/cpu/online"))


def check_boot_isolation():
    cmdline = read_text("/proc/cmdline") or ""
    keys = ["isolcpus=", "nohz_full=", "rcu_nocbs="]
    found = {k[:-1]: None for k in keys}
    for k in keys:
        i = cmdline.find(k)
        if i >= 0:
            j = cmdline.find(" ", i)
            if j < 0:
                j = len(cmdline)
            found[k[:-1]] = cmdline[i + len(k) : j]
    return found


def get_irq_hits_for_cpus(target_cpus):
    irq_names = {}
    for p in glob.glob("/proc/irq/[0-9]*/smp_affinity_list"):
        irq = p.split("/")[-2]
        irq_names[irq] = parse_cpu_list(read_text(p))

    stat = read_text("/proc/interrupts") or ""
    lines = stat.splitlines()
    cpu_header = []
    if lines:
        cpu_header = [x for x in lines[0].split() if x.startswith("CPU")]
    cpu_count = len(cpu_header)
    cpu_cols = {i: 0 for i in range(cpu_count)}
    irq_rows = defaultdict(int)
    for line in lines[1:]:
        parts = line.split()
        if not parts or ":" not in parts[0]:
            continue
        irq = parts[0].rstrip(":")
        counts = []
        for i in range(1, min(1 + cpu_count, len(parts))):
            try:
                counts.append(int(parts[i]))
            except ValueError:
                counts.append(0)
        for c in target_cpus:
            if c < len(counts):
                cpu_cols[c] += counts[c]
        if irq in irq_names and (irq_names[irq] & target_cpus):
            irq_rows[irq] = sum(counts[c] for c in target_cpus if c < len(counts))
    return cpu_cols, irq_rows, irq_names


def list_processes_on_cpus(target_cpus):
    rows = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        stat = read_text(f"/proc/{pid}/stat")
        comm = read_text(f"/proc/{pid}/comm")
        if not stat or not comm:
            continue
        parts = stat.split()
        if len(parts) < 39:
            continue
        try:
            cpu = int(parts[38])
        except ValueError:
            continue
        if cpu in target_cpus:
            rows.append((int(pid), comm, cpu))
    rows.sort(key=lambda x: (x[2], x[1], x[0]))
    return rows


def main():
    args = parse_args()
    sender_cpu = int(args.sender_cpu)
    receiver_cpu = int(args.receiver_cpu)
    target = {sender_cpu, receiver_cpu}

    online = get_online()
    print("fast4_preflight")
    print(f"target_sender_cpu={sender_cpu}")
    print(f"target_receiver_cpu={receiver_cpu}")
    print(f"online_cpus={format_cpu_set(online)}")
    print("")

    for cpu, role in [(sender_cpu, "sender"), (receiver_cpu, "receiver")]:
        sib = get_siblings(cpu)
        gov = get_governor(cpu)
        print(f"{role}_cpu={cpu} siblings={format_cpu_set(sib)} governor={gov}")
        if len(sib) > 1:
            print(f"warn_{role}_smt_shared=yes")
        else:
            print(f"warn_{role}_smt_shared=no")
    print("")

    iso = check_boot_isolation()
    print(f"boot_isolcpus={iso['isolcpus'] or '-'}")
    print(f"boot_nohz_full={iso['nohz_full'] or '-'}")
    print(f"boot_rcu_nocbs={iso['rcu_nocbs'] or '-'}")
    print("")

    cpu_cols, irq_rows, irq_aff = get_irq_hits_for_cpus(target)
    print("interrupt_totals_on_target_cpus")
    for c in sorted(target):
        print(f"cpu{c}_total_interrupt_count={cpu_cols.get(c, 0)}")
    print("")

    top_irqs = sorted(irq_rows.items(), key=lambda kv: kv[1], reverse=True)[:10]
    print("top_irq_lines_affined_to_target_cpus")
    if not top_irqs:
        print("-")
    else:
        for irq, hits in top_irqs:
            aff = format_cpu_set(irq_aff.get(irq, set()))
            print(f"irq={irq} affinity={aff} hits_on_target={hits}")
    print("")

    procs = list_processes_on_cpus(target)
    print("processes_currently_running_on_target_cpus")
    if not procs:
        print("-")
    else:
        for pid, comm, cpu in procs[:40]:
            print(f"pid={pid} cpu={cpu} comm={comm}")
        if len(procs) > 40:
            print(f"... ({len(procs)-40} more)")


if __name__ == "__main__":
    raise SystemExit(main())
