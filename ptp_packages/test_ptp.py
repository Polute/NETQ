import argparse
import subprocess
import time
import re
import atexit
import sys

# GLOBAL CONFIGURATION
_ptp_process = None
INTERFACE = "enp6s18"

def cleanup_ptp():
    """Cierra ptp4l al salir."""
    global _ptp_process
    if _ptp_process:
        print("\n[Cleanup] Stopping ptp4l software process...")
        try:
            _ptp_process.terminate()
            _ptp_process.wait(timeout=2)
        except:
            pass
        subprocess.run(["sudo", "killall", "-q", "ptp4l"], stderr=subprocess.DEVNULL)

def start_ptp_unicast(is_master, master_ip=None, target_offset_ns=10000):
    global _ptp_process
    
    subprocess.run(["sudo", "killall", "-q", "ptp4l"], stderr=subprocess.DEVNULL)
    atexit.register(cleanup_ptp)

    # 2. Generar archivo UNICAST con sintaxis de tabla corregida
    config_file = "/tmp/ptp4l_unicast.conf"
    with open(config_file, "w") as f:
        # --- SECCIÓN GLOBAL ---
        f.write("[global]\n")
        f.write("time_stamping software\n")
        f.write("delay_mechanism E2E\n")
        f.write("network_transport UDPv4\n") # Forzar UDP explícito
        if not is_master:
            f.write("unicast_req_duration 300\n") 
        
        # --- SECCIÓN DE INTERFAZ ---
        f.write(f"\n[{INTERFACE}]\n")
        if is_master:
            f.write("unicast_listen 1\n")        
        else:
            f.write("unicast_master_table 1\n")
            
            # --- SECCIÓN DE TABLA UNICAST ---
            f.write("\n[unicast_master_table]\n")
            f.write("table_id 1\n")
            f.write("logQueryInterval 2\n")
            # ¡LA CORRECCIÓN CRÍTICA ESTÁ AQUÍ! Protocolo + IP
            f.write(f"UDPv4 {master_ip}\n")

    role_str = "MASTER" if is_master else f"SLAVE (Target IP: {master_ip})"
    print(f"--- Starting PTP in SOFTWARE UNICAST mode ({role_str}) on {INTERFACE} ---")

    ptp4l_cmd = ["sudo", "ptp4l", "-i", INTERFACE, "-f", config_file, "-m"]
    if not is_master:
        ptp4l_cmd.append("-s")

    _ptp_process = subprocess.Popen(
        ptp4l_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
    )

    offset_regex = re.compile(r"master offset\s+(-?\d+)")
    print("[INFO] Reading live unicast logs from ptp4l daemon:")
    print("-" * 60)
    
    try:
        while True:
            line = _ptp_process.stdout.readline()
            if not line:
                if _ptp_process.poll() is not None:
                    print("\n[ERROR] ptp4l process died unexpectedly!")
                    break
                time.sleep(0.05)
                continue
            
            print(f"[ptp4l] {line.strip()}")
            
            if not is_master:
                match = offset_regex.search(line)
                if match:
                    current_offset = abs(int(match.group(1)))
                    if current_offset <= target_offset_ns:
                        print(f"\n[SUCCESS] Clock synced via Unicast! Precision: {current_offset} ns.")
                        break
    except KeyboardInterrupt:
        print("\nInterrupted by user.")
        sys.exit(0)

def main():
    parser = argparse.ArgumentParser(description="PTP Software Unicast Script")
    parser.add_argument("--master", action="store_true", help="Run as PTP Grandmaster")
    parser.add_argument("--master_ip", type=str, help="IP del Master (Requerida para esclavos)")
    args = parser.parse_args()

    if not args.master and not args.master_ip:
        print("[ERROR] Slaves require the Master's IP address. Use: --master_ip X.X.X.X")
        sys.exit(1)

    start_ptp_unicast(is_master=args.master, master_ip=args.master_ip)

    if not args.master:
        print("\n--- Monitoring Synchronized System Time (Every 5s) ---")
        try:
            while True:
                now_ns = time.clock_gettime_ns(time.CLOCK_REALTIME)
                print(f"System Time (ns): {now_ns}")
                time.sleep(5)
        except KeyboardInterrupt:
            print("\nExiting...")

if __name__ == "__main__":
    main()