#!/usr/bin/env python3
import argparse
import subprocess
import re
import sys
import time
import atexit
import json
import os
import statistics
from collections import deque

_ptp_process = None

def cleanup_ptp():
    """Ensures ptp4l stops cleanly if the daemon exits (Ctrl+C)."""
    global _ptp_process
    print("\n[PTP Daemon] Safely shutting down ptp4l daemon...")
    if _ptp_process:
        try:
            _ptp_process.terminate()
            _ptp_process.wait(timeout=2)
        except:
            pass
        _ptp_process = None
    
    # Failsafe killall for orphaned processes
    subprocess.run(["sudo", "killall", "-q", "ptp4l"], stderr=subprocess.DEVNULL)
    
    # Clean up the temporary status file
    if os.path.exists("/tmp/ptp_status.json"):
        try:
            os.remove("/tmp/ptp_status.json")
        except:
            pass
    print("[PTP Daemon] Synchronization system stopped and CPU freed.")

def main():
    parser = argparse.ArgumentParser(description="Standalone Unicast PTP synchronization daemon for AEGO.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--master", action="store_true", help="Act as PTP Grandmaster.")
    group.add_argument("--slave", type=str, help="IP of the PTP Master.")
    parser.add_argument("--interface", default="enp6s18", help="Network interface (default: enp6s18).")
    parser.add_argument("--target-offset", type=int, default=10000, help="Target offset in nanoseconds (default: 10000 ns).")
    args = parser.parse_args()

    # Register cleanup function on exit
    atexit.register(cleanup_ptp)

    # 1. Initial cleanup of any previous processes
    subprocess.run(["sudo", "killall", "-q", "ptp4l"], stderr=subprocess.DEVNULL)

    # 2. Dynamically generate the Unicast configuration file
    config_file = "/tmp/ptp4l_unicast.conf"
    with open(config_file, "w") as f:
        f.write("[global]\n")
        f.write("time_stamping software\n")
        f.write("delay_mechanism E2E\n")
        f.write("network_transport UDPv4\n")
        f.write("clock_servo linreg\n")
        f.write("logSyncInterval -3\n")
        f.write("logMinDelayReqInterval -3\n")
        
        if not args.master:
            f.write("unicast_req_duration 300\n") 
        
        f.write(f"\n[{args.interface}]\n")
        if args.master:
            f.write("unicast_listen 1\n")        
        else:
            f.write("unicast_master_table 1\n")
            f.write("\n[unicast_master_table]\n")
            f.write("table_id 1\n")
            f.write("logQueryInterval -3\n")
            f.write(f"UDPv4 {args.slave}\n")

    role_str = "MASTER" if args.master else f"SLAVE (Target IP: {args.slave})"
    print(f"=== Starting Standalone PTP Daemon [{role_str}] ===")
    print(f"[*] Interface: {args.interface} | Target: <{args.target_offset} ns")

    # 3. Launch ptp4l in the background capturing standard output
    # Note: -S flag prevents clock adjustment (only reports offset)
    ptp4l_cmd = ["sudo", "ptp4l", "-i", args.interface, "-f", config_file, "-m", "-S"]
    if not args.master:
        ptp4l_cmd.append("-s")

    global _ptp_process
    _ptp_process = subprocess.Popen(
        ptp4l_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
    )

    # If Master, it has no offset to calculate. Keeps service active and sleeps efficiently.
    if args.master:
        print("[PTP Grandmaster] Active and distributing time. Press Ctrl+C to stop.")
        status_payload = {
            "ptp_role": "master",
            "ptp_status": "grandmaster_active",
            "timestamp_unix_ns": time.time_ns()
        }
        with open("/tmp/ptp_status.json", "w") as sf:
            json.dump(status_payload, sf, indent=2)
        
        while True:
            time.sleep(3600)

    # If Slave, we continuously monitor the clock offset
    offset_regex = re.compile(r"master offset\s+(-?\d+)")
    has_reached_target = False

    # Circular memory to store the last 50 offsets
    recent_offsets = deque(maxlen=50)
    
    # Variables for JSON update throttling
    last_update_time = 0.0
    UPDATE_INTERVAL_SEC = 1.0  # Only write JSON and calculate math once per second

    print("[PTP Slave] Monitoring clock offset (no clock adjustment due to -S flag)...")
    
    while True:
        line = _ptp_process.stdout.readline()
        if not line:
            if _ptp_process.poll() is not None:
                print("[ERROR] The ptp4l process died unexpectedly.")
                sys.exit(1)
            time.sleep(0.05)
            continue
        
        # CPU OPTIMIZATION: Quick string check before running regex
        if "master offset" not in line:
            continue
            
        match = offset_regex.search(line)
        if match:
            # Save the offset keeping its sign (positive or negative)
            raw_offset = int(match.group(1))
            current_offset_abs = abs(raw_offset)
            recent_offsets.append(raw_offset)
            
            # Print current state inline
            if has_reached_target == False:
                print(f" -> Instantaneous Offset: {raw_offset} ns      ", end="\r")
            
            current_time = time.time()
            
            # When it reaches the desired absolute precision for the first time
            if current_offset_abs <= args.target_offset and not has_reached_target:
                has_reached_target = True
                
                jitter_p2p = max(recent_offsets) - min(recent_offsets) if len(recent_offsets) > 1 else 0
                std_dev = statistics.stdev(recent_offsets) if len(recent_offsets) > 1 else 0
                
                msg = f"[PTP SUCCESS] Clock offset stable! P2P Precision: {jitter_p2p} ns."
                print(f"\n\n{msg}")
                print("[*] Keeping synchronization active in background. You may launch AEGO.")
                
                status_payload = {
                    "ptp_role": "slave",
                    "ptp_status": "synchronized",
                    "instantaneous_offset_ns": raw_offset,
                    "precision_p2p_ns": jitter_p2p,
                    "precision_stddev_ns": round(std_dev, 2),
                    "sync_message": msg,
                    "timestamp_unix_ns": time.time_ns()
                }
                with open("/tmp/ptp_status.json", "w") as sf:
                    json.dump(status_payload, sf, indent=2)
                
                last_update_time = current_time
            
            # Throttle background updates to 1 Hz to save CPU cycles
            elif has_reached_target and (current_time - last_update_time) >= UPDATE_INTERVAL_SEC:
                try:
                    jitter_p2p = max(recent_offsets) - min(recent_offsets)
                    std_dev = statistics.stdev(recent_offsets)
                    
                    status_payload = {
                        "ptp_role": "slave",
                        "ptp_status": "synchronized",
                        "instantaneous_offset_ns": raw_offset,
                        "precision_p2p_ns": jitter_p2p,
                        "precision_stddev_ns": round(std_dev, 2),
                        "sync_message": f"[PTP ALIVE] P2P Jitter: {jitter_p2p} ns | StdDev: {round(std_dev, 2)} ns",
                        "timestamp_unix_ns": time.time_ns()
                    }
                    with open("/tmp/ptp_status.json", "w") as sf:
                        json.dump(status_payload, sf, indent=2)
                        
                    last_update_time = current_time
                except Exception:
                    pass

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[PTP Daemon] User interrupt received.")
        sys.exit(0)
