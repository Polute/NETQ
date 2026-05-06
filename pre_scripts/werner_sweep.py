#!/usr/bin/env python3
import argparse
import sys
from pathlib import Path

from epr_qutip_demo import (
    DEFAULT_NETWORK_NAME,
    EXPECTED_VERSIONS,
    package_versions,
    werner_fit,
    werner_is_entangled,
    werner_parameter_from_time,
    werner_state,
)


def parse_waits(raw_value):
    """Convert a comma-separated string of wait times into a list of floats."""
    waits = []
    for chunk in raw_value.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        waits.append(float(chunk))
    if not waits:
        raise ValueError("At least one wait value is required")
    return waits


def print_versions():
    """Print package versions so the sweep output is tied to a known software stack."""
    versions = package_versions()
    print("Detected versions:")
    for package, version in versions.items():
        expected = EXPECTED_VERSIONS[package]
        suffix = " (expected)" if version == expected else f" (expected {expected})"
        print(f"  {package}=={version}{suffix}")


def build_wait_point(wait_seconds, noise, t1):
    """Compute the direct-link and swapped Werner parameters for one storage-time point."""
    w_ac = werner_parameter_from_time(wait_seconds, t1, noise)
    w_cb = werner_parameter_from_time(wait_seconds, t1, noise)
    w_swap = w_ac * w_cb

    ac_werner = werner_fit(werner_state("phi_plus", w_ac), label="phi_plus")
    cb_werner = werner_fit(werner_state("phi_plus", w_cb), label="phi_plus")
    swap_werner = werner_fit(werner_state("phi_plus", w_swap), label="phi_plus")

    return {
        "wait_seconds": wait_seconds,
        "ac_werner": ac_werner,
        "cb_werner": cb_werner,
        "swap_werner": swap_werner,
        "predicted_swap": w_ac * w_cb,
        "prediction_error": swap_werner["parameter"] - (w_ac * w_cb),
        "swap_can_create_entanglement": werner_is_entangled(w_ac) and werner_is_entangled(w_cb),
    }


def parse_args():
    """Read command-line arguments for the deterministic Werner-time sweep tool."""
    repo_root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(
        description=(
            "Sweep a deterministic Werner-time decay model for the 3-node Alice-Charlie-Bob setup. "
            "At each wait value, w1 and w2 are evaluated at the instant of swapping and the script reports "
            "the ideal prediction w_swap = w1 * w2."
        )
    )
    parser.add_argument(
        "--config",
        default=str(repo_root / "config" / "madrid_3nodes.json"),
        help="Unused in the deterministic sweep, kept only for CLI compatibility.",
    )
    parser.add_argument(
        "--network-name",
        default=DEFAULT_NETWORK_NAME,
        help="Unused in the deterministic sweep, kept only for CLI compatibility.",
    )
    parser.add_argument(
        "--waits",
        default="0.0,0.1,0.3,0.5,0.7",
        help="Comma-separated storage times in seconds evaluated at the moment of swapping.",
    )
    parser.add_argument(
        "--shots",
        type=int,
        default=1,
        help="Unused in the deterministic sweep, kept only for CLI compatibility.",
    )
    parser.add_argument(
        "--noise",
        action="store_true",
        help="Enable deterministic Werner-time decay. If omitted, every point stays at w=1.",
    )
    parser.add_argument(
        "--t1",
        type=float,
        default=1.0,
        help="Time constant of the deterministic Werner decay model.",
    )
    return parser.parse_args()


def print_summary_table(results):
    """Print the sweep results in a compact table that is easy to compare across wait times."""
    print("wait_s  w_ac      ent_ac  w_cb      ent_cb  w_swap    ent_sw  w1*w2     delta")
    for point in results:
        ac = point["ac_werner"]
        cb = point["cb_werner"]
        swap = point["swap_werner"]
        print(
            f"{point['wait_seconds']:>6.3f}  "
            f"{ac['parameter']:>8.5f}  "
            f"{str(ac['is_entangled']):>6}  "
            f"{cb['parameter']:>8.5f}  "
            f"{str(cb['is_entangled']):>6}  "
            f"{swap['parameter']:>8.5f}  "
            f"{str(swap['is_entangled']):>6}  "
            f"{point['predicted_swap']:>8.5f}  "
            f"{point['prediction_error']:>8.5f}"
        )


def main():
    """Run the deterministic time sweep and print the resulting Werner-parameter table."""
    args = parse_args()
    waits = parse_waits(args.waits)

    print_versions()
    print(f"Noise enabled: {args.noise}")
    print(f"T1: {args.t1}")
    print(f"Wait sweep: {', '.join(f'{wait:.3f}' for wait in waits)}")
    if args.shots != 1:
        print(f"Shots per point: {args.shots} (ignored in deterministic Werner mode)")
    print()

    results = [build_wait_point(wait_seconds=wait, noise=args.noise, t1=args.t1) for wait in waits]

    print_summary_table(results)
    print()
    print("Interpretation:")
    print("  w_ac    : Alice-Charlie Werner parameter at the instant of swapping")
    print("  w_cb    : Charlie-Bob Werner parameter at the instant of swapping")
    print("  w_swap  : swapped Alice-Bob Werner parameter")
    print("  w1*w2   : ideal swapped prediction using w_swap = w1 * w2")
    print("  delta   : observed swapped w minus the ideal prediction")
    print("  ent_*   : True only when the fitted Werner state has w > 1/3")
    print("  If one input link drops to w <= 1/3, the swapped output is also no longer entangled.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
