#!/usr/bin/env python3
import argparse
import csv
import statistics
import os


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

    if {"count_idx", "delay_ns"}.issubset(fieldnames):
        return [
            {
                "kind": "delay",
                "suffix": "",
                "title": "delay",
                "ylabel": "delay (us)",
                "hist_xlabel": "delay (us)",
                "x_label": "count",
                "x": [int(row["count_idx"]) for row in rows],
                "y": [float(row["delay_ns"]) / 1000.0 for row in rows],
            }
        ]

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
    filtered_path = os.path.join(plot_base_dir, "counter_filtered", f"{base}_seq_filtered.png")
    outliers_path = os.path.join(plot_base_dir, "counter_outliers", f"{base}_seq_outliers.png")
    udp_missing_path = os.path.join(plot_base_dir, "udp_missing", f"{base}_udp_missing.png")
    return hist_path, seq_path, filtered_path, outliers_path, udp_missing_path


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
    os.makedirs(os.path.dirname(path), exist_ok=True)
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
    plt.savefig(path, dpi=150)
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
        plt.axvline(stats["mean"], color="tab:red", linewidth=0.9, linestyle="--", label="mean")
        plt.axvline(stats["median"], color="tab:green", linewidth=0.9, linestyle=":", label="median")
    else:
        plt.axhline(stats["mean"], color="tab:red", linewidth=0.9, linestyle="--", label="mean")
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
):
    plt.figure(figsize=(8, 4))
    plt.hist(y_values, bins=bins)
    add_reference_lines(plt, reference_stats, "vertical")
    plt.xlabel(series["hist_xlabel"])
    plt.ylabel("count")
    plt.title(f"{series['title']} histogram ({base})")
    add_info_box(plt, info_text)
    if reference_stats:
        plt.legend(loc="upper right")
    plt.tight_layout()
    plt.savefig(hist_path, dpi=150)
    plt.close()

    plt.figure(figsize=(8, 4))
    plt.plot(x_values, y_values, linewidth=0.6)
    add_reference_lines(plt, reference_stats, "horizontal")
    plt.xlabel(series["x_label"])
    plt.ylabel(series["ylabel"])
    plt.title(f"{series['title']} per {series['x_label']} ({base})")
    add_info_box(plt, info_text)
    if reference_stats:
        plt.legend(loc="upper right")
    plt.tight_layout()
    plt.savefig(seq_path, dpi=150)
    plt.close()


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
        help="Only write the filtered/outlier per-count plots, leaving existing plots untouched.",
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
            _, _, _, _, udp_missing_path = output_paths(csv_path, csv_root, plots_root, base)
            if not args.udp_missing:
                pass
            elif not args.last or not os.path.exists(udp_missing_path):
                if write_udp_missing_plot(plt, udp_missing_path, csv_path, counts, udp_status):
                    print(f"wrote {udp_missing_path}")
            else:
                print(f"skip {udp_missing_path} (already plotted)")

        written = []
        for series in series_list:
            series_base = base if not series["suffix"] else f"{base}_{series['suffix']}"
            hist_path, seq_path, filtered_path, outliers_path, _ = series_output_paths(
                csv_path, csv_root, plots_root, base, series
            )
            x_values = series["x"]
            y_values = series["y"]
            os.makedirs(os.path.dirname(hist_path), exist_ok=True)
            os.makedirs(os.path.dirname(seq_path), exist_ok=True)

            if series["kind"] == "clock_sync":
                clock_stats = series_stats(y_values) if show_clock_stats else None
                clock_info = format_clock_stats(clock_stats)
                if args.last and os.path.exists(hist_path) and os.path.exists(seq_path):
                    print(f"skip {csv_path} {series['suffix']} (already plotted)")
                    continue
                if not args.last and not confirm_overwrite(hist_path):
                    print(f"skip {csv_path} {series['suffix']} (no overwrite)")
                    continue
                if not args.last and not confirm_overwrite(seq_path):
                    print(f"skip {csv_path} {series['suffix']} (no overwrite)")
                    continue
                write_basic_plots(
                    plt,
                    x_values,
                    y_values,
                    hist_path,
                    seq_path,
                    series_base,
                    series,
                    args.bins,
                    clock_info,
                    clock_stats,
                )
                written.extend([hist_path, seq_path])
                continue

            if args.filtered:
                os.makedirs(os.path.dirname(filtered_path), exist_ok=True)
                os.makedirs(os.path.dirname(outliers_path), exist_ok=True)

            if args.last:
                if args.filtered_only:
                    existing = [filtered_path, outliers_path]
                else:
                    existing = [hist_path, seq_path]
                    if args.filtered:
                        existing.extend([filtered_path, outliers_path])
                if all(os.path.exists(path) for path in existing):
                    print(f"skip {csv_path} (already plotted)")
                    continue
            elif not args.filtered_only:
                if not confirm_overwrite(hist_path) or not confirm_overwrite(seq_path):
                    print(f"skip {csv_path} (no overwrite)")
                    continue

            if not args.filtered_only:
                write_basic_plots(
                    plt,
                    x_values,
                    y_values,
                    hist_path,
                    seq_path,
                    series_base,
                    series,
                    args.bins,
                    clock_offset_info,
                )
                written.extend([hist_path, seq_path])

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

            plt.figure(figsize=(8, 4))
            plt.plot(kept_counts, kept_delays, linewidth=0.6)
            plt.xlabel(series["x_label"])
            plt.ylabel(series["ylabel"])
            plt.title(f"{series['title']} per {series['x_label']} filtered ({series_base}, <= {threshold_us:.1f} us)")
            plt.tight_layout()
            plt.savefig(filtered_path, dpi=150)
            plt.close()

            plt.figure(figsize=(8, 4))
            plt.plot(x_values, y_values, linewidth=0.6, label="all samples")
            if outliers:
                plt.scatter(outlier_counts, outlier_delays, s=12, color="red", label="filtered out")
                plt.axhline(threshold_us, color="red", linewidth=0.8, linestyle="--")
            plt.xlabel(series["x_label"])
            plt.ylabel(series["ylabel"])
            plt.title(f"{series['title']} per {series['x_label']} with outliers ({series_base}, > {threshold_us:.1f} us)")
            if outliers:
                plt.legend()
            plt.tight_layout()
            plt.savefig(outliers_path, dpi=150)
            plt.close()
            written.extend([filtered_path, outliers_path])
            print(f"{series_base}: filtered {len(outliers)} of {len(y_values)} samples above {threshold_us:.1f} us")

        if written:
            print("wrote " + " and ".join(written))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
