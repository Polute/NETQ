#!/usr/bin/env python3
import argparse
import itertools
import statistics
import subprocess
import sys
import time


def parse_int_list(value):
    parts = [p.strip() for p in value.split(",") if p.strip()]
    return [int(p) for p in parts]


def parse_float_list(value):
    parts = [p.strip() for p in value.split(",") if p.strip()]
    return [float(p) for p in parts]


def parse_metrics(output_text):
    metrics = {}
    for line in output_text.splitlines():
        if "=" not in line:
            continue
        key, rest = line.split("=", 1)
        key = key.strip()
        rest = rest.strip()
        num = ""
        for ch in rest:
            if ch.isdigit():
                num += ch
            else:
                break
        if num:
            metrics[key] = int(num)
    return metrics


def build_cmd(base, args, use_sudo):
    cmd = list(base) + args
    if use_sudo:
        return ["sudo"] + cmd
    return cmd


def run_pair(python_bin, args, combo):
    sock_buf, busy_poll_us, count, warmup = combo

    receiver_args = [
        "minimal_epr_fast.py",
        "receiver",
        "--listen-host",
        args.listen_host,
        "--listen-port",
        str(args.listen_port),
        "--count",
        str(count),
        "--warmup",
        str(warmup),
        "--accept-timeout",
        str(args.accept_timeout),
        "--sock-buf",
        str(sock_buf),
        "--busy-poll-us",
        str(busy_poll_us),
        "--quiet",
    ]
    if args.receiver_cpu is not None:
        receiver_args += ["--cpu", str(args.receiver_cpu)]
    if args.rt_priority is not None:
        receiver_args += ["--rt-priority", str(args.rt_priority)]

    sender_args = [
        "minimal_epr_fast.py",
        "sender",
        "--receiver-host",
        args.receiver_host,
        "--receiver-port",
        str(args.listen_port),
        "--count",
        str(count),
        "--warmup",
        str(warmup),
        "--connect-timeout",
        str(args.connect_timeout),
        "--detect-timeout",
        str(args.detect_timeout),
        "--detect-interval",
        str(args.detect_interval),
        "--sock-buf",
        str(sock_buf),
        "--busy-poll-us",
        str(busy_poll_us),
        "--quiet",
    ]
    if args.sender_cpu is not None:
        sender_args += ["--cpu", str(args.sender_cpu)]
    if args.rt_priority is not None:
        sender_args += ["--rt-priority", str(args.rt_priority)]

    receiver_cmd = build_cmd([python_bin], receiver_args, args.sudo)
    sender_cmd = build_cmd([python_bin], sender_args, args.sudo)

    receiver_proc = subprocess.Popen(
        receiver_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(args.receiver_start_delay)

    sender_run = subprocess.run(
        sender_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=args.sender_timeout,
    )

    try:
        receiver_out, receiver_err = receiver_proc.communicate(timeout=args.receiver_timeout)
    except subprocess.TimeoutExpired:
        receiver_proc.kill()
        receiver_out, receiver_err = receiver_proc.communicate()

    if sender_run.returncode != 0:
        return None, sender_run.stdout, sender_run.stderr, receiver_err
    if receiver_proc.returncode not in (0, None):
        return None, sender_run.stdout, sender_run.stderr, receiver_err

    return parse_metrics(sender_run.stdout), sender_run.stdout, sender_run.stderr, receiver_err


def mean(values):
    return sum(values) / len(values) if values else 0.0


def coef_var(values):
    if len(values) < 2:
        return 0.0
    m = mean(values)
    if m <= 0:
        return 0.0
    return statistics.pstdev(values) / m


def main():
    parser = argparse.ArgumentParser(description="Sweep sock-buf/busy-poll/count/warmup for minimal_epr_fast.py.")
    parser.add_argument("--receiver-host", default="127.0.0.1")
    parser.add_argument("--listen-host", default="0.0.0.0")
    parser.add_argument("--listen-port", type=int, default=7401)
    parser.add_argument("--sock-buf", default="0,4096,16384,65536,262144")
    parser.add_argument("--busy-poll-us", default="0,25,50,100")
    parser.add_argument("--count", default="1000,3000")
    parser.add_argument("--warmup", default="30,50")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--sender-cpu", type=int, default=None)
    parser.add_argument("--receiver-cpu", type=int, default=None)
    parser.add_argument("--rt-priority", type=int, default=None)
    parser.add_argument("--sudo", action="store_true")
    parser.add_argument("--connect-timeout", type=float, default=10.0)
    parser.add_argument("--detect-timeout", type=float, default=30.0)
    parser.add_argument("--detect-interval", type=float, default=0.05)
    parser.add_argument("--accept-timeout", type=float, default=30.0)
    parser.add_argument("--receiver-start-delay", type=float, default=0.1)
    parser.add_argument("--sender-timeout", type=float, default=60.0)
    parser.add_argument("--receiver-timeout", type=float, default=60.0)
    parser.add_argument("--metric-keys", default="")
    parser.add_argument("--cv-weight", type=float, default=1.0)
    args = parser.parse_args()

    sock_buf_list = parse_int_list(args.sock_buf)
    busy_poll_list = parse_int_list(args.busy_poll_us)
    count_list = parse_int_list(args.count)
    warmup_list = parse_int_list(args.warmup)

    if args.metric_keys:
        metric_keys = [k.strip() for k in args.metric_keys.split(",") if k.strip()]
    else:
        metric_keys = [
            "round_trip_perf_p50",
            "round_trip_perf_p95",
            "round_trip_perf_p99",
            "emit_to_remote_p50",
            "emit_to_remote_p95",
            "emit_to_remote_p99",
            "send_call_p50",
            "send_call_p95",
            "send_call_p99",
        ]

    python_bin = sys.executable
    combos = list(itertools.product(sock_buf_list, busy_poll_list, count_list, warmup_list))

    results = []
    for combo in combos:
        per_repeat = []
        for _ in range(args.repeats):
            metrics, out, err, recv_err = run_pair(python_bin, args, combo)
            if not metrics:
                print("run_failed combo=", combo)
                if err:
                    print(err.strip())
                if recv_err:
                    print(recv_err.strip())
                metrics = {}
            per_repeat.append(metrics)

        combined = {k: [] for k in metric_keys}
        for rep in per_repeat:
            for key in metric_keys:
                if key in rep:
                    combined[key].append(rep[key])

        means = {k: mean(v) for k, v in combined.items() if v}
        score_vals = [means[k] for k in metric_keys if k in means]
        score = mean(score_vals) if score_vals else float("inf")
        cv_base = combined.get("round_trip_perf_p50", [])
        cv = coef_var(cv_base)
        final_score = score * (1.0 + args.cv_weight * cv)

        results.append(
            {
                "combo": combo,
                "score": score,
                "final_score": final_score,
                "cv": cv,
                "means": means,
            }
        )

    results.sort(key=lambda r: r["final_score"])

    print("best_combo_by_final_score")
    if results:
        best = results[0]
        sock_buf, busy_poll_us, count, warmup = best["combo"]
        print(
            f"sock_buf={sock_buf} busy_poll_us={busy_poll_us} count={count} warmup={warmup} "
            f"score={best['score']:.2f} cv={best['cv']:.4f} final={best['final_score']:.2f}"
        )
        for key in metric_keys:
            if key in best["means"]:
                print(f"{key}={best['means'][key]:.2f}")

    print("\nall_results")
    for row in results:
        sock_buf, busy_poll_us, count, warmup = row["combo"]
        print(
            f"sock_buf={sock_buf} busy_poll_us={busy_poll_us} count={count} warmup={warmup} "
            f"score={row['score']:.2f} cv={row['cv']:.4f} final={row['final_score']:.2f}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
