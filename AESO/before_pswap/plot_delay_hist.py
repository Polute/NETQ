#!/usr/bin/env python3
import argparse
import csv
import json
import statistics
import os


def sudo_output_owner():
    uid = os.environ.get("SUDO_UID")
    gid = os.environ.get("SUDO_GID")
    if uid is None or gid is None:
        return None
    try:
        return int(uid), int(gid)
    except ValueError:
        return None


def chown_output_path(path):
    owner = sudo_output_owner()
    if owner is None:
        return
    try:
        os.chown(path, owner[0], owner[1])
    except OSError:
        pass


def chown_output_ancestors(path):
    owner = sudo_output_owner()
    if owner is None:
        return
    abs_path = os.path.abspath(path)
    roots = [os.path.abspath(os.getcwd()), os.path.abspath("/tmp")]
    for root in roots:
        if abs_path == root or not abs_path.startswith(root + os.sep):
            continue
        rel_path = os.path.relpath(abs_path, root)
        current = root
        for part in rel_path.split(os.sep):
            current = os.path.join(current, part)
            chown_output_path(current)
        break


def ensure_output_dir(directory):
    os.makedirs(directory, exist_ok=True)
    chown_output_ancestors(directory)


def savefig_owned(plt, path, **kwargs):
    plt.savefig(path, **kwargs)
    chown_output_path(path)


NODE_INFO = {
    "14_1": {"name": "CAIT", "origin": "CAIT", "distance": "-", "loss": "-"},
    "223": {
        "name": "CCS-Laboratorio",
        "origin": "CCS-Laboratorio",
        "link": "Rectorado -> CCS/CEDINT",
        "distance": "23.3 km",
        "loss": "6.6 dB",
    },
    "226": {"name": "Rectorado", "origin": "Rectorado", "distance": "-", "loss": "-"},
    "227": {
        "name": "Teleco/ETSIT",
        "origin": "Teleco/ETSIT",
        "link": "Rectorado -> Teleco/ETSIT",
        "distance": "1.9 km",
        "loss": "2.3 dB",
    },
}


def load_delays(csv_path):
    values_us = []
    counts = []
    with open(csv_path, "r", encoding="utf-8") as handle:
        reader = csv.reader(handle)
        header = next(reader, None)
        if header is None:
            return counts, values_us
        for row in reader:
            if len(row) < 2:
                continue
            counts.append(int(row[0]))
            values_us.append(float(row[1]) / 1000.0)
    return counts, values_us


def load_plot_series(csv_path):
    with open(csv_path, "r", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            return []
        fieldnames = set(reader.fieldnames)
        rows = list(reader)

    if {"sample_idx", "offset_ns", "path_delay_ns"}.issubset(fieldnames):
        samples = [int(row["sample_idx"]) for row in rows]
        return [
            {
                "kind": "clock_sync",
                "suffix": "offset",
                "title": "PTP offset",
                "ylabel": "offset (us)",
                "hist_xlabel": "offset (us)",
                "x_label": "sample",
                "x": samples,
                "y": [float(row["offset_ns"]) / 1000.0 for row in rows],
            },
            {
                "kind": "clock_sync",
                "suffix": "path_delay",
                "title": "PTP path delay",
                "ylabel": "path delay (us)",
                "hist_xlabel": "path delay (us)",
                "x_label": "sample",
                "x": samples,
                "y": [float(row["path_delay_ns"]) / 1000.0 for row in rows],
            },
        ]

    if "count_idx" in fieldnames:
        file_base = os.path.splitext(os.path.basename(csv_path))[0]
        if file_base.startswith(
            (
                "03_r1_initial_to_r2_alice_sum",
                "02_r1_initial_to_r2_bob_sum",
                "03_summary_final_r1_to_alice",
                "02_summary_final_r1_to_bob",
            )
        ):
            series_defs = [("delay_ns", "", "total DEOS delay", "delay (us)", "delay (us)")]
        else:
            series_defs = [
                ("delay_ns", "", "delay", "delay (us)", "delay (us)"),
                ("send_a_block_ns", "send_a_block", "send A block", "time (us)", "time (us)"),
                ("send_b_block_ns", "send_b_block", "send B block", "time (us)", "time (us)"),
                ("send_gap_ab_ns", "send_gap_ab", "send gap A-B", "time (us)", "time (us)"),
                ("send_alice_block_ns", "send_alice_block", "send Alice block", "time (us)", "time (us)"),
                ("send_r2_block_ns", "send_r2_block", "send R2 block", "time (us)", "time (us)"),
                ("send_gap_ns", "send_gap", "send gap", "time (us)", "time (us)"),
            ]
        series_list = []
        for column, suffix, title, ylabel, hist_xlabel in series_defs:
            if column not in fieldnames:
                continue
            x_values = []
            y_values = []
            for row in rows:
                value = row.get(column)
                if value in (None, ""):
                    continue
                try:
                    x_values.append(int(row["count_idx"]))
                    y_values.append(float(value) / 1000.0)
                except (TypeError, ValueError):
                    continue
            if not x_values:
                continue
            series_list.append(
                {
                    "kind": "delay",
                    "suffix": suffix,
                    "title": title,
                    "ylabel": ylabel,
                    "hist_xlabel": hist_xlabel,
                    "x_label": "count",
                    "x": x_values,
                    "y": y_values,
                }
            )
        return series_list

    counts, delays_us = load_delays(csv_path)
    if not counts:
        return []
    return [
        {
            "kind": "delay",
            "suffix": "",
            "title": "delay",
            "ylabel": "delay (us)",
            "hist_xlabel": "delay (us)",
            "x_label": "count",
            "x": counts,
            "y": delays_us,
        }
    ]


def read_clock_offset_info(csv_path):
    if not csv_path or not os.path.exists(csv_path):
        return None
    with open(csv_path, "r", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            return None
        rows = list(reader)
    if not rows:
        return None

    mean_ns = None
    if "clock_offset_mean_ns" in reader.fieldnames:
        try:
            mean_ns = float(rows[0]["clock_offset_mean_ns"])
        except (TypeError, ValueError):
            mean_ns = None
    if mean_ns is None and "offset_ns" in reader.fieldnames:
        offsets = []
        for row in rows:
            try:
                offsets.append(float(row["offset_ns"]))
            except (TypeError, ValueError):
                pass
        if offsets:
            mean_ns = sum(offsets) / len(offsets)
    if mean_ns is None:
        return None
    return f"clock offset mean = {mean_ns / 1000.0:.3f} us"


def safe_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * fraction
    lo = int(pos)
    hi = min(lo + 1, len(ordered) - 1)
    if lo == hi:
        return ordered[lo]
    return ordered[lo] * (hi - pos) + ordered[hi] * (pos - lo)


def detect_node_info(path):
    parts = os.path.normpath(path).split(os.sep)
    for part in parts:
        if part in NODE_INFO:
            return part, NODE_INFO[part]
    return None, None


def json_dir_from_csv_root(csv_root):
    csv_root = os.path.normpath(csv_root)
    parent = os.path.dirname(csv_root)
    name = os.path.basename(csv_root)
    if name == "csv":
        json_name = "json"
    elif name.startswith("csv_"):
        json_name = "json_" + name[len("csv_") :]
    else:
        return None
    return os.path.join(parent, json_name) if parent else json_name


def equivalent_json_path(csv_path, csv_root):
    json_root = json_dir_from_csv_root(csv_root)
    if not json_root:
        return None

    rel_dir = os.path.relpath(os.path.dirname(csv_path), csv_root)
    if rel_dir == ".":
        rel_dir = ""
    name = os.path.splitext(os.path.basename(csv_path))[0]
    if name.startswith("clock_sync_client_"):
        name = "delay_hist_" + name[len("clock_sync_") :]
    elif name.startswith("clock_sync_deos_"):
        raw_name = name[len("clock_sync_") :]
        run_suffix = ""
        raw_parts = raw_name.rsplit("_", 1)
        if len(raw_parts) == 2 and raw_parts[1].isdigit():
            raw_name = raw_parts[0]
            run_suffix = "_" + raw_parts[1]
        for suffix in ("_r1", "_r2", "_alice", "_bob"):
            if raw_name.endswith(suffix):
                raw_name = raw_name[: -len(suffix)]
                break
        name = raw_name + run_suffix
    candidate = os.path.join(json_root, rel_dir, f"{name}.json")
    if os.path.exists(candidate):
        return candidate

    rel_parts = [] if not rel_dir else rel_dir.split(os.sep)
    role_name = rel_parts[0] if rel_parts else None
    deos_json_base = {
        "r1": "deos_r1_send_hist",
        "r2": "deos_r2_hist",
        "alice": "deos_alice_hist",
        "bob": "deos_bob_hist",
    }.get(role_name)
    if deos_json_base:
        suffix = ""
        for stage_prefix in (
            "00_r1_swap_start",
            "01_r1_to_r2_aeso",
            "01_r1_to_alice_aeso",
            "02_r2_to_alice_aeso",
            "01_r2_to_bob_aeso",
            "03_r1_initial_to_r2_alice_sum",
            "02_r1_initial_to_r2_bob_sum",
            "03_summary_final_r1_to_alice",
            "02_summary_final_r1_to_bob",
        ):
            if name == stage_prefix:
                suffix = ""
                break
            if name.startswith(stage_prefix + "_"):
                suffix = name[len(stage_prefix) :]
                break
        candidate = os.path.join(json_root, rel_dir, f"{deos_json_base}{suffix}.json")
        if os.path.exists(candidate):
            return candidate

    # Most current exports keep clock-sync CSVs next to delay CSVs, while the
    # JSON is stored at the json root.
    candidate = os.path.join(json_root, f"{name}.json")
    return candidate if os.path.exists(candidate) else None


def read_json_payload(path):
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None


def format_bool(value):
    if value is None:
        return None
    return "yes" if bool(value) else "no"


def format_seconds(value):
    number = safe_float(value)
    if number is None:
        return None
    if number == 0:
        return "0"
    if abs(number) < 0.001:
        return f"{number * 1_000_000:.0f} us"
    if abs(number) < 1:
        return f"{number * 1000:.3g} ms"
    return f"{number:.3g} s"


def series_summary(values):
    if not values:
        return None
    mean_value = sum(values) / len(values)
    return {
        "mean": mean_value,
        "p50": statistics.median(values),
        "p95": percentile(values, 0.95),
        "std": statistics.pstdev(values) if len(values) > 1 else 0.0,
        "n": len(values),
    }


def build_run_info_box(csv_path, csv_root, series, y_values, json_payload, extra_info=None):
    node_id, node_info = detect_node_info(csv_path)
    args = (json_payload or {}).get("args", {})
    clock_sync = (json_payload or {}).get("clock_sync", {})

    lines = []
    if node_info:
        distance_bits = []
        if node_info.get("distance") and node_info["distance"] != "-":
            distance_bits.append(node_info["distance"])
        if node_info.get("loss") and node_info["loss"] != "-":
            distance_bits.append(node_info["loss"])
        distance_text = f" ({', '.join(distance_bits)})" if distance_bits else ""
        lines.append(f"node: {node_id} {node_info['name']}{distance_text}")
        origin = node_info.get("origin") or node_info.get("link")
        if origin:
            lines.append(f"origin: {origin}")

    experiment = os.path.basename(os.path.normpath(csv_root))
    if experiment:
        if experiment.startswith("csv_"):
            experiment = experiment[len("csv_") :]
        lines.append(f"exp: {experiment}")

    role = (json_payload or {}).get("role") or args.get("role")
    client_id = (json_payload or {}).get("client_id") or args.get("client_id")
    file_base = os.path.splitext(os.path.basename(csv_path))[0]
    if client_id is None:
        for prefix in ("delay_hist_client_", "clock_sync_client_"):
            if file_base.startswith(prefix):
                client_id = file_base[len(prefix) :].split("_", 1)[0]
                role = role or "client"
                break
    if role is None and file_base.startswith("repeater_send_hist"):
        role = "repeater"

    role_line = None
    if role == "client" and client_id is not None:
        role_line = f"client {client_id}"
    elif role:
        role_line = role

    data_protocol = args.get("data_protocol") or (json_payload or {}).get("data_protocol")
    experiment_lower = (experiment or "").lower()
    if data_protocol is None:
        if "udp" in experiment_lower:
            data_protocol = "udp"
        elif "tcp" in experiment_lower:
            data_protocol = "tcp"
    clock_protocol = (
        args.get("clock_sync_protocol")
        or clock_sync.get("clock_sync_protocol")
        or ("on" if args.get("clock_sync") else None)
    )
    if clock_protocol is None:
        if "sync_udp" in experiment_lower or "kernel" in experiment_lower:
            clock_protocol = "udp"
        elif "tcp_sync" in experiment_lower:
            clock_protocol = "tcp"
    proto_line = None
    if data_protocol or clock_protocol:
        proto_line = f"data/sync={data_protocol or '-'}/{clock_protocol or '-'}"
    if role_line or proto_line:
        lines.append(" | ".join(part for part in [role_line, proto_line] if part))

    kernel_data = args.get("kernel_timestamp")
    kernel_clock = args.get("clock_sync_kernel_timestamp") if args.get("clock_sync") else None
    if kernel_data is not None or kernel_clock is not None:
        kernel_parts = []
        if kernel_data:
            kernel_parts.append("data")
        if kernel_clock:
            kernel_parts.append("sync")
        if kernel_parts:
            lines.append(f"kernel ts: {'+'.join(kernel_parts)}")
        else:
            lines.append("kernel ts: no")

    pace_mode = args.get("pace_mode")
    count_interval = args.get("count_interval")
    pace_line = None
    if pace_mode or count_interval not in (None, 0, 0.0):
        pace_line = f"pace={pace_mode or '-'}@{format_seconds(count_interval) or '-'}"

    cpu = args.get("cpu")
    sock_buf = args.get("sock_buf")
    busy_poll = args.get("busy_poll_us")
    runtime_bits = []
    if cpu is not None:
        runtime_bits.append(f"cpu={cpu}")
    if sock_buf is not None:
        runtime_bits.append(f"buf={sock_buf}")
    if busy_poll is not None:
        runtime_bits.append(f"busy={busy_poll}us")
    runtime_line = " ".join(runtime_bits) if runtime_bits else None
    if pace_line or runtime_line:
        lines.append(" | ".join(part for part in [pace_line, runtime_line] if part))

    if series["kind"] == "delay":
        stats = series_summary(y_values)
        if stats:
            lines.append(
                "delay: "
                f"mean={stats['mean']:.1f}us "
                f"p50={stats['p50']:.1f}us "
                f"p95={stats['p95']:.1f}us "
                f"std={stats['std']:.1f}us"
            )
        lost = (json_payload or {}).get("udp_lost_est")
        if lost is None:
            lost = (json_payload or {}).get("udp_lost_est_final")
        received = (json_payload or {}).get("udp_received")
        if received is None:
            received = (json_payload or {}).get("received_final")
        if received is None:
            received = (json_payload or {}).get("received_from_r1")
        sync_quality = (json_payload or {}).get("sync_quality") or clock_sync.get("sync_quality")
        if lost is not None or received is not None or sync_quality:
            lines.append(f"udp: recv={received if received is not None else '-'} lost={lost if lost is not None else '-'} | sync={sync_quality or '-'}")

    if extra_info:
        lines.append(extra_info)

    return "\n".join(line for line in lines if line)


def equivalent_clock_sync_csv(csv_path):
    name = os.path.basename(csv_path)
    if not name.startswith("delay_hist_client_") or not name.endswith(".csv"):
        return None
    suffix = name[len("delay_hist_client_") : -len(".csv")]
    candidate = os.path.join(os.path.dirname(csv_path), f"clock_sync_client_{suffix}.csv")
    return candidate if os.path.exists(candidate) else None


def confirm_overwrite(path):
    if not os.path.exists(path):
        return True
    reply = input(f"overwrite {path}? [y/N]: ").strip().lower()
    return reply == "y"


def robust_outlier_threshold(delays_us, mad_scale):
    median = statistics.median(delays_us)
    deviations = [abs(value - median) for value in delays_us]
    mad = statistics.median(deviations)
    if mad <= 0:
        return median
    return median + mad_scale * mad


def is_csv_dir(path):
    name = os.path.basename(os.path.normpath(path))
    return name == "csv" or name.startswith("csv_")


def is_plots_dir(path):
    name = os.path.basename(os.path.normpath(path))
    return name == "plots" or name.startswith("plots_")


def csv_dir_to_plots_dir(csv_dir):
    csv_dir = os.path.normpath(csv_dir)
    parent = os.path.dirname(csv_dir)
    name = os.path.basename(csv_dir)
    if name == "csv":
        plot_name = "plots"
    elif name.startswith("csv_"):
        plot_name = "plots_" + name[len("csv_") :]
    else:
        plot_name = "plots"
    return os.path.join(parent, plot_name) if parent else plot_name


def plots_dir_to_csv_dir(plots_dir):
    plots_dir = os.path.normpath(plots_dir)
    parent = os.path.dirname(plots_dir)
    name = os.path.basename(plots_dir)
    if name == "plots":
        csv_name = "csv"
    elif name.startswith("plots_"):
        csv_name = "csv_" + name[len("plots_") :]
    else:
        return None
    return os.path.join(parent, csv_name) if parent else csv_name


def find_csv_root(path):
    path = os.path.normpath(path)
    if os.path.isdir(path) and is_csv_dir(path):
        return path
    current = os.path.dirname(path) if os.path.isfile(path) else path
    while current and current != os.path.dirname(current):
        if os.path.isdir(current) and is_csv_dir(current):
            return current
        current = os.path.dirname(current)
    return "csv" if os.path.isdir("csv") else os.path.dirname(path) or "."


def iter_csv_files(csv_dir):
    for root, _, files in os.walk(csv_dir):
        for name in sorted(files):
            if name.endswith(".csv"):
                yield os.path.join(root, name)


def discover_csv_dirs():
    csv_dirs = []
    for name in sorted(os.listdir(".")):
        if os.path.isdir(name) and is_csv_dir(name):
            csv_dirs.append(name)
    for name in sorted(os.listdir(".")):
        if not (os.path.isdir(name) and is_plots_dir(name)):
            continue
        csv_dir = plots_dir_to_csv_dir(name)
        if csv_dir and os.path.isdir(csv_dir):
            csv_dirs.append(csv_dir)
    return sorted(dict.fromkeys(csv_dirs))


def collect_jobs(inputs):
    jobs = []
    if inputs:
        for input_path in inputs:
            if os.path.isdir(input_path):
                if is_csv_dir(input_path):
                    csv_root = input_path
                    plots_root = csv_dir_to_plots_dir(csv_root)
                else:
                    csv_root = input_path
                    plots_root = input_path
                for csv_path in iter_csv_files(input_path):
                    jobs.append((csv_path, csv_root, plots_root))
            else:
                csv_root = find_csv_root(input_path)
                plots_root = csv_dir_to_plots_dir(csv_root) if is_csv_dir(csv_root) else csv_root
                jobs.append((input_path, csv_root, plots_root))
        return jobs

    csv_dirs = discover_csv_dirs()
    if not csv_dirs:
        return []
    for csv_root in csv_dirs:
        plots_root = csv_dir_to_plots_dir(csv_root)
        for csv_path in iter_csv_files(csv_root):
            jobs.append((csv_path, csv_root, plots_root))
    return jobs


def output_paths(csv_path, csv_root, plots_root, base, clock=False):
    rel_dir = os.path.relpath(os.path.dirname(csv_path), csv_root)
    if rel_dir == ".":
        rel_dir = ""
    plot_base_dir = os.path.join(plots_root, rel_dir)
    if clock:
        plot_base_dir = os.path.join(plot_base_dir, "_clock")
    hist_path = os.path.join(plot_base_dir, "sec", f"{base}.png")
    seq_path = os.path.join(plot_base_dir, "counter", f"{base}_seq.png")
    filtered_hist_path = os.path.join(plot_base_dir, "sec_filtered", f"{base}_filtered.png")
    filtered_path = os.path.join(plot_base_dir, "counter_filtered", f"{base}_seq_filtered.png")
    outliers_path = os.path.join(plot_base_dir, "counter_outliers", f"{base}_seq_outliers.png")
    udp_missing_path = os.path.join(plot_base_dir, "udp_missing", f"{base}_udp_missing.png")
    return hist_path, seq_path, filtered_hist_path, filtered_path, outliers_path, udp_missing_path


def series_output_paths(csv_path, csv_root, plots_root, base, series):
    series_base = base
    if series["suffix"]:
        series_base = f"{base}_{series['suffix']}"
    return output_paths(csv_path, csv_root, plots_root, series_base, clock=series["kind"] == "clock_sync")


def should_report_udp_counts(csv_path, csv_root, plots_root):
    joined = " ".join([csv_path, csv_root, plots_root]).lower()
    return "udp" in joined


def format_missing_counts(missing, limit=20):
    if not missing:
        return "none"
    shown = ",".join(str(value) for value in missing[:limit])
    if len(missing) > limit:
        shown += f",...,+{len(missing) - limit} more"
    return shown


def udp_count_status(counts, warmup, expected_count):
    expected_start = int(warmup) + 1
    expected_end = int(expected_count) if expected_count is not None else max(counts)
    if expected_end < expected_start:
        return {
            "valid": False,
            "expected_start": expected_start,
            "expected_end": expected_end,
            "expected": [],
            "missing": [],
            "extra": [],
            "duplicates": 0,
            "received_expected": 0,
        }

    count_set = set(counts)
    expected = list(range(expected_start, expected_end + 1))
    expected_set = set(expected)
    missing = sorted(expected_set - count_set)
    extra = sorted(value for value in count_set if value < expected_start or value > expected_end)
    duplicates = len(counts) - len(count_set)
    received_expected = len(expected_set) - len(missing)
    return {
        "valid": True,
        "expected_start": expected_start,
        "expected_end": expected_end,
        "expected": expected,
        "missing": missing,
        "extra": extra,
        "duplicates": duplicates,
        "received_expected": received_expected,
    }


def print_udp_count_report(csv_path, status):
    if not status["valid"]:
        print(
            f"udp_counts {csv_path}: invalid expected range "
            f"{status['expected_start']}-{status['expected_end']}"
        )
        return

    missing = status["missing"]
    extra = status["extra"]
    duplicates = status["duplicates"]
    received_expected = status["received_expected"]
    expected_len = len(status["expected"])
    result_label = "OK" if not missing and not duplicates and not extra else "FAIL"

    print(
        f"udp_counts {result_label} {csv_path}: "
        f"received={received_expected}/{expected_len} "
        f"expected_range={status['expected_start']}-{status['expected_end']} "
        f"missing={len(missing)} duplicates={duplicates} extra={len(extra)}"
    )
    if missing:
        print(f"  missing_counts={format_missing_counts(missing)}")
    if extra:
        print(f"  extra_counts={format_missing_counts(extra)}")


def write_udp_missing_plot(plt, path, csv_path, counts, status):
    if not status["valid"]:
        return False
    ensure_output_dir(os.path.dirname(path))
    count_set = set(counts)
    expected = status["expected"]
    present_counts = [count for count in expected if count in count_set]
    missing = status["missing"]

    plt.figure(figsize=(10, 3))
    if present_counts:
        plt.scatter(present_counts, [1] * len(present_counts), s=4, color="tab:green", label="received")
    if missing:
        plt.scatter(missing, [0] * len(missing), s=10, color="tab:red", label="missing")
    else:
        mid = (status["expected_start"] + status["expected_end"]) / 2
        plt.text(mid, 0.5, "no missing UDP counts", ha="center", va="center")
    plt.yticks([0, 1], ["missing", "received"])
    plt.xlabel("count_idx")
    plt.ylabel("UDP count status")
    plt.title(f"UDP missing counts ({os.path.basename(csv_path)}): missing={len(missing)}")
    plt.ylim(-0.4, 1.4)
    if present_counts or missing:
        plt.legend(loc="upper right")
    plt.tight_layout()
    savefig_owned(plt, path, dpi=150)
    plt.close()
    return True


def add_info_box(plt, text):
    if not text:
        return
    plt.gca().text(
        0.02,
        0.98,
        text,
        transform=plt.gca().transAxes,
        va="top",
        ha="left",
        fontsize=8,
        bbox={"boxstyle": "round,pad=0.25", "facecolor": "white", "alpha": 0.75, "edgecolor": "0.8"},
    )


def add_external_header(plt, title, info_text):
    fig = plt.gcf()
    if info_text:
        fig.text(
            0.985,
            0.995,
            info_text,
            va="top",
            ha="right",
            fontsize=7.5,
            bbox={"boxstyle": "round,pad=0.16", "facecolor": "white", "alpha": 0.92, "edgecolor": "0.8"},
        )
    fig.text(0.03, 0.845, title, va="top", ha="left", fontsize=10)


def compact_plot_title_base(base):
    parts = [part for part in os.path.normpath(base).split(os.sep) if part not in ("", ".")]
    for part in parts:
        if part in {"alice", "bob", "r1", "r2"}:
            return part
    return os.path.basename(os.path.normpath(base))


def format_plot_title(series_title, x_label, base):
    return f"{series_title} per {x_label} ({compact_plot_title_base(base)})"


def format_hist_title(series_title, base):
    return f"{series_title} histogram ({compact_plot_title_base(base)})"


def finish_plot(plt, path, title, info_text=None, overlap=False):
    if overlap:
        plt.title(title)
        add_info_box(plt, info_text)
        plt.tight_layout()
    else:
        add_external_header(plt, title, info_text)
        line_count = len(info_text.splitlines()) if info_text else 0
        top = 0.80 if line_count == 0 else min(0.80, 0.74 + 0.010 * line_count)
        plt.tight_layout(rect=(0.0, 0.0, 1.0, top))
    savefig_owned(plt, path, dpi=150)
    plt.close()


def series_stats(values):
    if not values:
        return None
    return {
        "mean": sum(values) / len(values),
        "median": statistics.median(values),
        "count": len(values),
    }


def format_clock_stats(stats):
    if not stats:
        return None
    return "\n".join(
        [
            f"mean = {stats['mean']:.3f} us",
            f"median = {stats['median']:.3f} us",
            f"n = {stats['count']}",
        ]
    )


def add_reference_lines(plt, stats, orientation):
    if not stats:
        return
    if orientation == "vertical":
        if "mean" in stats:
            plt.axvline(stats["mean"], color="tab:red", linewidth=0.9, linestyle="--", label="mean")
        if "median" in stats:
            plt.axvline(stats["median"], color="tab:green", linewidth=0.9, linestyle=":", label="median")
    else:
        if "mean" in stats:
            plt.axhline(stats["mean"], color="tab:red", linewidth=0.9, linestyle="--", label="mean")
        if "median" in stats:
            plt.axhline(stats["median"], color="tab:green", linewidth=0.9, linestyle=":", label="median")


def write_basic_plots(
    plt,
    x_values,
    y_values,
    hist_path,
    seq_path,
    base,
    series,
    bins,
    info_text=None,
    reference_stats=None,
    overlap=False,
    plot_kind="all",
):
    written = []
    if plot_kind in ("all", "hist"):
        plt.figure(figsize=(8, 4 if overlap else 4.6))
        plt.hist(y_values, bins=bins)
        add_reference_lines(plt, reference_stats, "vertical")
        plt.xlabel(series["hist_xlabel"])
        plt.ylabel("count")
        if reference_stats:
            plt.legend(loc="upper right")
        finish_plot(plt, hist_path, format_hist_title(series["title"], base), info_text, overlap)
        written.append(hist_path)

    if plot_kind in ("all", "seq"):
        plt.figure(figsize=(8, 4 if overlap else 4.6))
        plt.plot(x_values, y_values, linewidth=0.6)
        add_reference_lines(plt, reference_stats, "horizontal")
        plt.xlabel(series["x_label"])
        plt.ylabel(series["ylabel"])
        if reference_stats:
            plt.legend(loc="upper right")
        finish_plot(plt, seq_path, format_plot_title(series["title"], series["x_label"], base), info_text, overlap)
        written.append(seq_path)

    return written


def main():
    parser = argparse.ArgumentParser(
        description="Plot delay histograms from CSV exports. csv* folders are written to matching plots* folders."
    )
    parser.add_argument("csv", nargs="*", help="CSV files or csv* directories produced by minimal_epr_fast.py --plot")
    parser.add_argument("--bins", type=int, default=50)
    parser.add_argument("--prefix", default=None, help="Override output prefix (no extension)")
    parser.add_argument("--last", action="store_true", help="Only plot CSVs without existing outputs.")
    parser.add_argument(
        "--filtered",
        action="store_true",
        help="Also write per-count plots with slow outliers highlighted and removed.",
    )
    parser.add_argument(
        "--filtered-only",
        action="store_true",
        help="Only write the filtered histogram and filtered/outlier per-count plots, leaving existing plots untouched.",
    )
    parser.add_argument(
        "--filter-threshold-us",
        type=float,
        default=None,
        help="Delay threshold in us for filtered plots. Defaults to median + mad-scale*MAD.",
    )
    parser.add_argument(
        "--mad-scale",
        type=float,
        default=8.0,
        help="MAD multiplier used for automatic filtered-plot threshold.",
    )
    parser.add_argument("--warmup", type=int, default=50, help="Warmup counts ignored by the CSV/client.")
    parser.add_argument("--expected-count", type=int, default=2000, help="Expected final count_idx for UDP loss reports.")
    parser.add_argument("--udp-missing", action="store_true", help="Also write UDP missing-count plots.")
    parser.add_argument("--force", action="store_true", help="Overwrite existing plot files without prompting.")
    parser.add_argument(
        "--plot-kind",
        choices=("all", "hist", "seq"),
        default="all",
        help="Limit normal plots to histogram, per-count sequence, or both.",
    )
    parser.add_argument(
        "--overlap",
        action="store_true",
        help="Draw the metadata box inside the axes, preserving the previous plot layout.",
    )
    args = parser.parse_args()
    if args.filtered_only:
        args.filtered = True

    try:
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib is required for plotting.")
        return 1

    jobs = collect_jobs(args.csv)
    if not jobs:
        print("no csv files found")
        return 1

    for csv_path, csv_root, plots_root in jobs:
        series_list = load_plot_series(csv_path)
        if not series_list:
            print(f"skip {csv_path} (no data)")
            continue

        base = os.path.splitext(os.path.basename(csv_path))[0]
        file_base = base
        if args.prefix:
            base = args.prefix

        json_payload = read_json_payload(equivalent_json_path(csv_path, csv_root))
        is_clock_sync_csv = any(series["kind"] == "clock_sync" for series in series_list)
        show_clock_stats = file_base.startswith("clock_sync")
        clock_offset_info = None
        if not is_clock_sync_csv:
            clock_offset_info = read_clock_offset_info(equivalent_clock_sync_csv(csv_path))

        udp_status = None
        if not is_clock_sync_csv and should_report_udp_counts(csv_path, csv_root, plots_root):
            counts = series_list[0]["x"]
            udp_status = udp_count_status(counts, args.warmup, args.expected_count)
            print_udp_count_report(csv_path, udp_status)
            _, _, _, _, _, udp_missing_path = output_paths(csv_path, csv_root, plots_root, base)
            if not args.udp_missing:
                pass
            elif args.force or not args.last or not os.path.exists(udp_missing_path):
                if write_udp_missing_plot(plt, udp_missing_path, csv_path, counts, udp_status):
                    print(f"wrote {udp_missing_path}")
            else:
                print(f"skip {udp_missing_path} (already plotted)")

        written = []
        for series in series_list:
            series_base = base if not series["suffix"] else f"{base}_{series['suffix']}"
            hist_path, seq_path, filtered_hist_path, filtered_path, outliers_path, _ = series_output_paths(
                csv_path, csv_root, plots_root, base, series
            )
            x_values = series["x"]
            y_values = series["y"]
            ensure_output_dir(os.path.dirname(hist_path))
            ensure_output_dir(os.path.dirname(seq_path))

            if series["kind"] == "clock_sync":
                clock_stats = series_stats(y_values) if show_clock_stats else None
                clock_info = format_clock_stats(clock_stats)
                info_text = build_run_info_box(
                    csv_path,
                    csv_root,
                    series,
                    y_values,
                    json_payload,
                    clock_info,
                )
                if args.last and not args.force and os.path.exists(hist_path) and os.path.exists(seq_path):
                    print(f"skip {csv_path} {series['suffix']} (already plotted)")
                    continue
                if not args.last and not args.force and not confirm_overwrite(hist_path):
                    print(f"skip {csv_path} {series['suffix']} (no overwrite)")
                    continue
                if not args.last and not args.force and not confirm_overwrite(seq_path):
                    print(f"skip {csv_path} {series['suffix']} (no overwrite)")
                    continue
                new_paths = write_basic_plots(
                    plt,
                    x_values,
                    y_values,
                    hist_path,
                    seq_path,
                    series_base,
                    series,
                    args.bins,
                    info_text,
                    clock_stats,
                    args.overlap,
                    args.plot_kind,
                )
                written.extend(new_paths)
                continue

            if args.filtered:
                ensure_output_dir(os.path.dirname(filtered_hist_path))
                ensure_output_dir(os.path.dirname(filtered_path))
                ensure_output_dir(os.path.dirname(outliers_path))

            if args.last and not args.force:
                if args.filtered_only:
                    existing = [filtered_hist_path, filtered_path, outliers_path]
                else:
                    existing = [hist_path, seq_path]
                    if args.filtered:
                        existing.extend([filtered_hist_path, filtered_path, outliers_path])
                if all(os.path.exists(path) for path in existing):
                    print(f"skip {csv_path} (already plotted)")
                    continue
            elif not args.filtered_only and not args.force:
                if not confirm_overwrite(hist_path) or not confirm_overwrite(seq_path):
                    print(f"skip {csv_path} (no overwrite)")
                    continue

            if not args.filtered_only:
                info_text = build_run_info_box(
                    csv_path,
                    csv_root,
                    series,
                    y_values,
                    json_payload,
                    clock_offset_info,
                )
                delay_stats = series_summary(y_values)
                delay_reference_stats = {"mean": delay_stats["mean"]} if delay_stats else None
                new_paths = write_basic_plots(
                    plt,
                    x_values,
                    y_values,
                    hist_path,
                    seq_path,
                    series_base,
                    series,
                    args.bins,
                    info_text,
                    delay_reference_stats,
                    args.overlap,
                    args.plot_kind,
                )
                written.extend(new_paths)
            else:
                info_text = build_run_info_box(
                    csv_path,
                    csv_root,
                    series,
                    y_values,
                    json_payload,
                    clock_offset_info,
                )

            if not args.filtered:
                continue

            threshold_us = args.filter_threshold_us
            if threshold_us is None:
                threshold_us = robust_outlier_threshold(y_values, args.mad_scale)
            kept = [(count, delay) for count, delay in zip(x_values, y_values) if delay <= threshold_us]
            outliers = [(count, delay) for count, delay in zip(x_values, y_values) if delay > threshold_us]
            kept_counts = [count for count, _ in kept]
            kept_delays = [delay for _, delay in kept]
            outlier_counts = [count for count, _ in outliers]
            outlier_delays = [delay for _, delay in outliers]
            filtered_stats = series_summary(kept_delays)
            filter_info_lines = []
            if clock_offset_info:
                filter_info_lines.append(clock_offset_info)
            filter_info_lines.append(
                f"filter: <= {threshold_us:.1f}us kept={len(kept_delays)}/{len(y_values)} removed={len(outliers)}"
            )
            filtered_info_text = build_run_info_box(
                csv_path,
                csv_root,
                series,
                kept_delays,
                json_payload,
                "\n".join(filter_info_lines),
            )

            plt.figure(figsize=(8, 4 if args.overlap else 4.6))
            plt.hist(kept_delays, bins=args.bins)
            if filtered_stats:
                plt.axvline(
                    filtered_stats["mean"],
                    color="tab:red",
                    linewidth=0.9,
                    linestyle="--",
                    label="filtered mean",
                )
            plt.xlabel(series["hist_xlabel"])
            plt.ylabel("count")
            if filtered_stats:
                plt.legend(loc="upper right")
            finish_plot(
                plt,
                filtered_hist_path,
                f"{series['title']} histogram filtered ({compact_plot_title_base(series_base)}, <= {threshold_us:.1f} us)",
                filtered_info_text,
                args.overlap,
            )

            plt.figure(figsize=(8, 4 if args.overlap else 4.6))
            plt.plot(kept_counts, kept_delays, linewidth=0.6)
            if filtered_stats:
                plt.axhline(
                    filtered_stats["mean"],
                    color="tab:red",
                    linewidth=0.9,
                    linestyle="--",
                    label="filtered mean",
                )
            plt.xlabel(series["x_label"])
            plt.ylabel(series["ylabel"])
            if filtered_stats:
                plt.legend(loc="upper right")
            finish_plot(
                plt,
                filtered_path,
                f"{series['title']} per {series['x_label']} filtered ({compact_plot_title_base(series_base)}, <= {threshold_us:.1f} us)",
                filtered_info_text,
                args.overlap,
            )

            plt.figure(figsize=(8, 4 if args.overlap else 4.6))
            plt.plot(x_values, y_values, linewidth=0.6, label="all samples")
            if outliers:
                plt.scatter(outlier_counts, outlier_delays, s=12, color="red", label="filtered out")
                plt.axhline(threshold_us, color="red", linewidth=0.8, linestyle="--")
            if filtered_stats:
                plt.axhline(
                    filtered_stats["mean"],
                    color="tab:red",
                    linewidth=0.9,
                    linestyle="--",
                    label="filtered mean",
                )
            plt.xlabel(series["x_label"])
            plt.ylabel(series["ylabel"])
            if outliers or filtered_stats:
                plt.legend(loc="upper right")
            finish_plot(
                plt,
                outliers_path,
                f"{series['title']} per {series['x_label']} with outliers ({compact_plot_title_base(series_base)}, > {threshold_us:.1f} us)",
                filtered_info_text,
                args.overlap,
            )
            written.extend([filtered_hist_path, filtered_path, outliers_path])
            print(f"{series_base}: filtered {len(outliers)} of {len(y_values)} samples above {threshold_us:.1f} us")

        if written:
            print("wrote " + " and ".join(written))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
