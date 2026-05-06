#!/usr/bin/env python3
import argparse
import shlex
import subprocess
import time


def parse_args():
    p = argparse.ArgumentParser(description="Run fast benchmark with receiver on remote VM via SSH jump host.")
    p.add_argument("--jump-user", required=True)
    p.add_argument("--jump-host", required=True)
    p.add_argument("--vm-user", required=True)
    p.add_argument("--vm-host", required=True)
    p.add_argument("--vm-workdir", default="~/NETQ")
    p.add_argument("--receiver-bind-ip", required=True, help="IP address on VM for receiver bind.")
    p.add_argument("--receiver-port", type=int, default=7401)
    p.add_argument("--count", type=int, default=3000)
    p.add_argument("--warmup", type=int, default=30)
    p.add_argument("--busy-poll-us", type=int, default=25)
    p.add_argument("--receiver-cpu", type=int, default=3)
    p.add_argument("--sender-cpu", type=int, default=2)
    p.add_argument("--rt-priority", type=int, default=50)
    p.add_argument("--no-rt", action="store_true")
    p.add_argument("--sender-local-ip", default="")
    return p.parse_args()


def main():
    args = parse_args()
    rt_flag = "" if args.no_rt else f"--rt-priority {args.rt_priority}"

    remote_cmd = (
        f"cd {shlex.quote(args.vm_workdir)} && "
        f"sudo taskset -c {args.receiver_cpu} "
        f"python minimal_epr_receiver_fast_remote.py "
        f"--bind-ip {shlex.quote(args.receiver_bind_ip)} "
        f"--listen-port {args.receiver_port} "
        f"--count {args.count} --warmup {args.warmup} "
        f"--busy-poll-us {args.busy_poll_us} {rt_flag} --quiet"
    ).strip()

    ssh_target = f"{args.vm_user}@{args.vm_host}"
    jump_target = f"{args.jump_user}@{args.jump_host}"
    ssh_cmd = ["ssh", "-J", jump_target, ssh_target, remote_cmd]

    print("Starting remote receiver over SSH (password prompts handled by ssh/sudo)...")
    rx_proc = subprocess.Popen(ssh_cmd)
    time.sleep(1.0)

    sender_cmd = [
        "sudo",
        "taskset",
        "-c",
        str(args.sender_cpu),
        "python",
        "minimal_epr_sender_fast_remote.py",
        "--receiver-ip",
        args.receiver_bind_ip,
        "--receiver-port",
        str(args.receiver_port),
        "--count",
        str(args.count),
        "--warmup",
        str(args.warmup),
        "--busy-poll-us",
        str(args.busy_poll_us),
        "--quiet",
        "--show-arrows",
    ]
    if not args.no_rt:
        sender_cmd.extend(["--rt-priority", str(args.rt_priority)])
    if args.sender_local_ip:
        sender_cmd.extend(["--local-ip", args.sender_local_ip])

    try:
        subprocess.run(sender_cmd, check=True)
    finally:
        rx_proc.wait(timeout=120)


if __name__ == "__main__":
    raise SystemExit(main())
