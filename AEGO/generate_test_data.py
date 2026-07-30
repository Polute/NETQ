#!/usr/bin/env python3
"""
Generate test data for plot_tau_pgen_map.py
Creates csv_pgen_0XX and json_pgen_0XX directories with simulated data.
"""

import os
import json
import random
import numpy as np


def generate_receiver_csv(pgen, num_samples=200, output_path="receiver_timing.csv"):
    """
    Generate receiver CSV with simulated werner values.
    Higher pgen = lower werner (more decay).
    """
    # Base werner decreases with pgen
    base_werner = 0.9 - (pgen * 0.4)  # pgen 0.2 -> 0.82, pgen 0.9 -> 0.54
    
    # Add some randomness
    werner_values = []
    count_indices = []
    
    # Simulate count indices with gaps (simulating packet loss due to pgen)
    count = 50  # start after warmup
    for i in range(num_samples):
        # Higher pgen = more gaps
        if random.random() < pgen:
            count += random.randint(1, 3)
        else:
            count += 1
        
        count_indices.append(count)
        
        # Werner value with noise
        w = base_werner + random.uniform(-0.05, 0.05)
        w = max(0.0, min(1.0, w))  # clamp to [0, 1]
        werner_values.append(w)
    
    # Generate timing values
    # s2r: sender to receiver (one-way, constant relativistic)
    s2r_ns = [random.randint(190000, 195000) for _ in range(num_samples)]
    
    # r2a: receiver to Alice (return trip, varies 180-200 ns as specified)
    r2a_ns = [random.randint(180, 200) for _ in range(num_samples)]
    
    # total_ns = s2r + r2a
    total_ns = [s + r for s, r in zip(s2r_ns, r2a_ns)]
    
    # inter_success_gap: gaps between successful receptions
    inter_success_gaps = []
    prev_count = count_indices[0]
    for count in count_indices[1:]:
        gap = count - prev_count
        inter_success_gaps.append(gap)
        prev_count = count
    inter_success_gaps.insert(0, 0)  # first gap is 0
    
    # Write CSV
    with open(output_path, 'w') as f:
        f.write("count_index,s2r_ns,r2a_ns,total_ns,werner,inter_success_gap\n")
        for i in range(num_samples):
            f.write(f"{count_indices[i]},{s2r_ns[i]},{r2a_ns[i]},{total_ns[i]},{werner_values[i]:.6f},{inter_success_gaps[i]}\n")
    
    print(f"Generated {output_path} with {num_samples} samples, pgen={pgen}")


def generate_sender_json(pgen, output_path="sender_timing.json"):
    """Generate sender JSON with metadata."""
    data = {
        "mode": "sender",
        "protocol": "udp",
        "data_protocol": "udp",
        "sync_protocol": "none",
        "total_packets": 1000,
        "warmup": 50,
        "sent_count": 1000,
        "success_count": int(1000 * (1 - pgen * 0.1)),  # approximate
        "first_emit_ts_ns": 1784799683006718086,
        "last_emit_ts_ns": 1784799683377381377,
        "pgen": pgen,
        "kernel_timestamp": True,
        "args": {
            "cpu": None,
            "rt_priority": 50,
            "sock_buf": 65536,
            "busy_poll_us": 0,
            "pace_mode": "hybrid",
            "count_interval": 0.0,
            "spin_margin_us": 10.0
        },
        "samples": {
            "rtt_p50_ns": 387483,
            "rtt_p95_ns": 398772,
            "rtt_p99_ns": 406573,
            "e2r_p50_ns": 191233,
            "e2r_p95_ns": 199827
        }
    }
    
    with open(output_path, 'w') as f:
        json.dump(data, f, indent=2)
    
    print(f"Generated {output_path} with pgen={pgen}")


def generate_test_data(base_dir=".", pgens=[0.2, 0.3, 0.5, 0.7, 0.8, 0.9]):
    """Generate complete test data structure."""
    
    for pgen in pgens:
        # Create directory names
        # Format: csv_pgen0_1 for 0.1, csv_pgen0_2 for 0.2, etc.
        # Remove the decimal point and leading zero
        pgen_str = str(pgen).replace('0.', '')
        
        csv_dir_name = f"csv_pgen0_{pgen_str}"
        json_dir_name = f"json_pgen0_{pgen_str}"
        
        csv_dir = os.path.join(base_dir, csv_dir_name)
        json_dir = os.path.join(base_dir, json_dir_name)
        
        # Create directories
        os.makedirs(csv_dir, exist_ok=True)
        os.makedirs(json_dir, exist_ok=True)
        
        # Generate receiver CSVs (Alice's werner values)
        generate_receiver_csv(pgen, num_samples=200, output_path=os.path.join(csv_dir, "receiver_timing.csv"))
        generate_receiver_csv(pgen, num_samples=180, output_path=os.path.join(csv_dir, "receiver_timing_1.csv"))
        generate_receiver_csv(pgen, num_samples=190, output_path=os.path.join(csv_dir, "receiver_timing_2.csv"))
        
        # Generate sender JSONs
        generate_sender_json(pgen, output_path=os.path.join(json_dir, "sender_timing.json"))
        generate_sender_json(pgen, output_path=os.path.join(json_dir, "sender_timing_1.json"))
        generate_sender_json(pgen, output_path=os.path.join(json_dir, "sender_timing_2.json"))
        
        print(f"Created {csv_dir_name} and {json_dir_name}")


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Generate test data for tau_pgen_map plotting")
    parser.add_argument("--base-dir", default=".", help="Base directory for test data")
    parser.add_argument("--pgens", nargs='+', type=float, 
                        default=[0.2, 0.3, 0.5, 0.7, 0.8, 0.9],
                        help="List of pgen values to simulate")
    
    args = parser.parse_args()
    
    print(f"Generating test data in {args.base_dir} for pgens: {args.pgens}")
    generate_test_data(args.base_dir, args.pgens)
    print("\nDone! Now run: python plot_tau_pgen_map.py --base-dir .")
