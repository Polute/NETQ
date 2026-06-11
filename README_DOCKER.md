# Docker Environment For DEOS/AESO

This file keeps the Docker setup needed to reproduce the Ubuntu 22.04 / Python
3.10 environment and reserve one CPU per terminal.

The recommended four-CPU layout for DEOS is:

- R1 on CPU `3`.
- Alice on CPU `5`.
- R2 on CPU `7`.
- Bob on CPU `11`.

## Install Docker

On Ubuntu/Debian hosts:

```bash
sudo apt update
sudo apt install -y docker.io
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"
```

Log out and back in, or run:

```bash
newgrp docker
```

Check that Docker works:

```bash
docker --version
docker run --rm hello-world
```

## Build The Ubuntu 22.04 / Python 3.10 Image

Run from the repository root:

```bash
cd /home/giicc/NETQ
docker build -f Dockerfile.ubuntu2204-py310 -t netq-ubuntu2204-py310 .
```

The image is based on Ubuntu 22.04 and uses the Ubuntu 22.04 Python 3.10 line.

## Open Four Reserved-CPU Terminals

Open four host terminals. Each Docker container is pinned to exactly one host
CPU using `--cpuset-cpus`.

Use the same CPU number inside the DEOS/AESO command with `--cpu`.

### Terminal 1: R1 on CPU 3

```bash
cd /home/giicc/NETQ
docker run --rm -it \
  --network host \
  --cpuset-cpus="3" \
  --cap-add SYS_NICE \
  --cap-add NET_ADMIN \
  --ulimit rtprio=99 \
  --ulimit memlock=-1 \
  -v "$PWD":/work \
  -w /work/DEOS \
  netq-ubuntu2204-py310
```

### Terminal 2: Alice on CPU 5

```bash
cd /home/giicc/NETQ
docker run --rm -it \
  --network host \
  --cpuset-cpus="5" \
  --cap-add SYS_NICE \
  --cap-add NET_ADMIN \
  --ulimit rtprio=99 \
  --ulimit memlock=-1 \
  -v "$PWD":/work \
  -w /work/DEOS \
  netq-ubuntu2204-py310
```

### Terminal 3: R2 on CPU 7

```bash
cd /home/giicc/NETQ
docker run --rm -it \
  --network host \
  --cpuset-cpus="7" \
  --cap-add SYS_NICE \
  --cap-add NET_ADMIN \
  --ulimit rtprio=99 \
  --ulimit memlock=-1 \
  -v "$PWD":/work \
  -w /work/DEOS \
  netq-ubuntu2204-py310
```

### Terminal 4: Bob on CPU 11

```bash
cd /home/giicc/NETQ
docker run --rm -it \
  --network host \
  --cpuset-cpus="11" \
  --cap-add SYS_NICE \
  --cap-add NET_ADMIN \
  --ulimit rtprio=99 \
  --ulimit memlock=-1 \
  -v "$PWD":/work \
  -w /work/DEOS \
  netq-ubuntu2204-py310
```

## Verify CPU Pinning Inside Each Container

Inside each container:

```bash
taskset -pc $$
nproc
```

The affinity mask should contain only the CPU assigned to that terminal.

## Notes

- `--network host` avoids Docker NAT and keeps the timing path closer to the
  host network stack.
- `--cap-add SYS_NICE` and `--ulimit rtprio=99` allow real-time scheduling when
  the program is run with `sudo` inside the container.
- `--cap-add NET_ADMIN` allows low-level network options used during tests.
- `--ulimit memlock=-1` avoids memory-locking limits if later tests use locked
  memory or real-time tuning.
- For AESO instead of DEOS, keep the same `docker run` options and change
  `-w /work/DEOS` to `-w /work/AESO`.
