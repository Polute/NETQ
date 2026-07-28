# Minimal EPR Fast C Port

This directory contains the C implementation of `../minimal_epr_fast.py`.

The C binary is used to compare the Python implementation against a lower
runtime-overhead version while keeping the same experiment structure:

- one repeater and two clients;
- TCP control sockets;
- UDP or TCP data messages;
- PTP-like clock synchronization over TCP or UDP;
- Linux `SO_TIMESTAMPNS` kernel receive timestamps for UDP clock sync;
- Linux `SO_TIMESTAMPNS` kernel receive timestamps for UDP data;
- CSV and JSON output;
- shared swap timestamp for the two data notifications;
- paced/spin sending.

For the full experiment description and the recommended Python/C commands, see
`../README.md`.

## Build

Build from the AESO root:

```bash
cd ~/AESO_opt

gcc -O3 -Wall -Wextra -pthread \
  -o c_code/minimal_epr_fast_c \
  c_code/minimal_epr_fast.c \
  -lm
```

Build on an Ubuntu 22.04-compatible system or inside the Ubuntu 22.04 Docker
container. If the binary is compiled on a newer host, the remote Ubuntu 22.04
machines may fail with:

```text
GLIBC_2.38 not found
```

## Recommended C run

Current best C configuration:

- `--data-protocol udp`;
- `--clock-sync udp`;
- `--clock-sync-samples 264`;
- `--clock-sync-kernel-timestamp`;
- client-side `--kernel-timestamp`;
- `--sock-buf 65536`;
- `--busy-poll-us 25`;
- repeater-side `--send-mode paced`;
- repeater-side `--count-interval 0.00005`;
- repeater-side `--pace-mode spin`;
- `--shared-send-timestamp`.

See `../README.md` for the exact three-node commands.

## Permissions

When the binary is run with `sudo`, generated CSV/JSON files are changed back to
the original user when `SUDO_UID` and `SUDO_GID` are available. This keeps output
folders removable without `sudo`.
