#!/usr/bin/env python3
import csv
import json
import math
import os
import statistics
from collections import defaultdict

TIMEOUT_THRESHOLD_NS = 4_500_000_000  # Umbral para detectar timeouts de socket de 5.0 s
T_COH_LIST_US = [100.0, 250.0, 500.0, 1000.0, 1_000_000.0]


def pct(sorted_vals, p):
    if not sorted_vals:
        return 0.0
    idx = int((len(sorted_vals) - 1) * p)
    return sorted_vals[idx]


def calc_en(t_exp_us, t_coh_us, w0=1.0):
    if t_coh_us <= 0:
        return 0.0
    w = w0 * math.exp(-t_exp_us / t_coh_us)
    if w <= 1.0 / 3.0:
        return 0.0
    return math.log2((1.0 + 3.0 * w) / 2.0)


def find_matching_json(csv_path):
    base = os.path.splitext(os.path.basename(csv_path))[0]
    parent = os.path.dirname(csv_path)
    grandparent = os.path.dirname(parent)
    dir_name = os.path.basename(parent)

    candidates = [os.path.join(parent, base + ".json")]
    if dir_name.startswith("csv"):
        json_dir = os.path.join(grandparent, "json" + dir_name[3:])
        candidates.append(os.path.join(json_dir, base + ".json"))
    candidates.append(os.path.join(grandparent, "json", base + ".json"))

    for c in candidates:
        if os.path.exists(c):
            try:
                with open(c, "r") as f:
                    return json.load(f)
            except Exception:
                pass
    return None


def infer_pgen(csv_path, meta):
    if meta and "pgen" in meta:
        return round(float(meta["pgen"]), 2)
    name = csv_path.lower()
    for token in name.replace("/", "_").replace("-", "_").split("_"):
        if token.startswith("pgen"):
            val = token[4:]
            try:
                fval = float(val)
                return round(fval / 10.0 if fval > 1.0 else fval, 2)
            except ValueError:
                pass
    return -1.0


def scan_workspace(root="."):
    aego_sender_runs = defaultdict(list)
    aego_receiver_runs = defaultdict(list)
    aeso_client_runs = defaultdict(list)

    for dirpath, _, filenames in os.walk(root):
        for fname in sorted(filenames):
            if not fname.endswith(".csv"):
                continue
            fpath = os.path.join(dirpath, fname)
            try:
                with open(fpath, "r", newline="") as f:
                    reader = csv.reader(f)
                    header = next(reader, None)
                    if not header:
                        continue
                    rows = list(reader)
            except Exception:
                continue

            if not rows:
                continue

            meta = find_matching_json(fpath)

            # 1. AEGO Sender CSV: count_index,emit_ts_ns,rtt_ns,e2r_ns,werner,success_bit
            if header[:4] == ["count_index", "emit_ts_ns", "rtt_ns", "e2r_ns"]:
                pgen = infer_pgen(fpath, meta)
                aego_sender_runs[pgen].append((fpath, rows, meta))

            # 2. AEGO Receiver CSV: count_index,s2r_ns,r2a_ns,total_ns,werner,inter_success_gap
            elif header[:4] == ["count_index", "s2r_ns", "r2a_ns", "total_ns"]:
                pgen = infer_pgen(fpath, meta)
                aego_receiver_runs[pgen].append((fpath, rows, meta))

            # 3. AESO Client CSV: count_index,ts_swap,swap_to_recv_ns,werner,success_bit
            elif header[:3] == ["count_index", "ts_swap", "swap_to_recv_ns"]:
                aeso_client_runs[os.path.basename(dirpath)].append((fpath, rows))

    return aego_sender_runs, aego_receiver_runs, aeso_client_runs


def analyze_aego(sender_runs, receiver_runs):
    print("==========================================================================")
    print(" 1. AEGO: BLOQUEO SECUENCIAL, TIMEOUTS (5.0 s) Y COLAS DE LATENCIA")
    print("==========================================================================")
    pgens = sorted(set(sender_runs.keys()) | set(receiver_runs.keys()))

    for pgen in pgens:
        s_runs = sender_runs.get(pgen, [])
        r_runs = receiver_runs.get(pgen, [])

        all_rtt_us = []
        all_e2r_us = []
        total_udp_losses = 0
        total_stall_sec = 0.0
        early_closures = 0

        # Tiempos acumulados hasta cada exito (para T_purge y tasa con/sin stalls)
        success_cycle_us_no_stall = []
        success_cycle_us_with_stall = []
        success_rtt_us = []

        for fpath, rows, meta in s_runs:
            prev_idx = None
            prev_emit_ns = None
            acc_no_stall_us = 0.0
            acc_with_stall_us = 0.0

            for r in rows:
                if len(r) < 6:
                    continue
                idx = int(r[0])
                emit_ns = int(r[1])
                rtt_us = float(r[2]) / 1000.0
                e2r_us = float(r[3]) / 1000.0
                succ = int(r[5])

                all_rtt_us.append(rtt_us)
                all_e2r_us.append(e2r_us)

                # Detectar paquetes perdidos / timeouts de 5.0 s entre filas registradas
                lost_between = 0
                stall_us = 0.0
                if prev_idx is not None and idx > prev_idx + 1:
                    lost_between = idx - prev_idx - 1
                if prev_emit_ns is not None and (emit_ns - prev_emit_ns) >= TIMEOUT_THRESHOLD_NS:
                    gap_s = (emit_ns - prev_emit_ns) / 1e9
                    lost_between = max(lost_between, int(round(gap_s / 5.0)))
                    stall_us = gap_s * 1e6
                elif lost_between > 0:
                    stall_us = lost_between * 5_000_000.0

                total_udp_losses += lost_between
                total_stall_sec += stall_us / 1e6

                acc_no_stall_us += rtt_us
                acc_with_stall_us += rtt_us + stall_us

                if succ == 1:
                    success_cycle_us_no_stall.append(acc_no_stall_us)
                    success_cycle_us_with_stall.append(acc_with_stall_us)
                    success_rtt_us.append(rtt_us)
                    acc_no_stall_us = 0.0
                    acc_with_stall_us = 0.0

                prev_idx = idx
                prev_emit_ns = emit_ns

        all_gaps = []
        for fpath, rows, meta in r_runs:
            if meta and meta.get("recv_count", meta.get("total_packets", 0)) < meta.get("total_packets", 0):
                early_closures += 1
            for r in rows:
                if len(r) >= 6:
                    all_gaps.append(int(r[5]))

        rtt_sorted = sorted(all_rtt_us)
        e2r_sorted = sorted(all_e2r_us)
        if not rtt_sorted:
            continue

        print(f"\n[p_gen = {pgen:.2f}] Runs(S/R)={len(s_runs)}/{len(r_runs)} | "
              f"Muestras={len(rtt_sorted)} | Exitos={len(success_rtt_us)} | "
              f"Perdidas_UDP={total_udp_losses} | T_stall_total={total_stall_sec:.2f}s | "
              f"Cierres_tempranos_RX={early_closures}")
        print(f"  RTT [us]: mean={statistics.mean(rtt_sorted):.2f}, p50={pct(rtt_sorted,0.5):.2f}, "
              f"p95={pct(rtt_sorted,0.95):.2f}, p99={pct(rtt_sorted,0.99):.2f}, max={rtt_sorted[-1]:.2f}")
        print(f"  E2R [us]: mean={statistics.mean(e2r_sorted):.2f}, p95={pct(e2r_sorted,0.95):.2f}, "
              f"p99={pct(e2r_sorted,0.99):.2f}")
        if all_gaps:
            print(f"  inter_success_gap: mean={statistics.mean(all_gaps):.2f}, max={max(all_gaps)}")

        # Evaluacion por regimen de coherencia T_coh (IV-F)
        for t_coh in T_COH_LIST_US:
            purged = 0
            rates_no_stall = []
            rates_with_stall = []
            for rtt_u, cyc_ns, cyc_ws in zip(success_rtt_us, success_cycle_us_no_stall, success_cycle_us_with_stall):
                en = calc_en(rtt_u, t_coh, w0=1.0)
                if en == 0.0:
                    purged += 1
                rates_no_stall.append(en / (cyc_ns / 1e6))
                rates_with_stall.append(en / (cyc_ws / 1e6))

            purge_pct = (100.0 * purged / len(success_rtt_us)) if success_rtt_us else 0.0
            r_clean = statistics.mean(rates_no_stall) if rates_no_stall else 0.0
            r_stall = statistics.mean(rates_with_stall) if rates_with_stall else 0.0
            print(f"    T_coh={t_coh:9.1f} us -> Purgados(W<=1/3)={purge_pct:5.1f}% | "
                  f"R_indiv(sin_stall)={r_clean:8.2f} eb/s | R_indiv(con_Tstall)={r_stall:8.2f} eb/s")


def analyze_aeso(aeso_runs):
    if not aeso_runs:
        return
    print("\n==========================================================================")
    print(" 2. AESO: AUDITORIA DE PERDIDAS UDP EN DISEMINACION ABIERTA")
    print("==========================================================================")
    for folder, runs in sorted(aeso_runs.items()):
        total_rx = 0
        total_expected = 0
        for fpath, rows in runs:
            indices = [int(r[0]) for r in rows]
            total_rx += len(indices)
            total_expected += (max(indices) - min(indices) + 1) if indices else 0
        lost = total_expected - total_rx
        print(f"  {folder}: archivos={len(runs)}, recibidos={total_rx}/{total_expected}, perdidos_UDP={lost}")


if __name__ == "__main__":
    s_runs, r_runs, a_runs = scan_workspace(".")
    analyze_aego(s_runs, r_runs)
    analyze_aeso(a_runs)