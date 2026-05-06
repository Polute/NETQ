# NETQ

Minimal SimulaQron-based testbed for a 3-node quantum network, focused on one thing: validating a small `Alice`-`Charlie`-`Bob` setup with the `qutip` backend so we can inspect density matrices for direct EPR distribution and entanglement swapping.

## Purpose

This repository is a small validation environment for:

- running `SimulaQron 3.0.16`
- switching the simulator backend from `stabilizer` to `qutip`
- working with exactly 3 nodes: `Alice`, `Bob`, and `Charlie`
- distributing an EPR pair across one link
- using `Charlie` as the middle node for entanglement swapping
- exposing exact density matrices through the `qutip` backend
- expressing the 2-qubit quality indicators in Bell-centered Werner-state terms
- testing optional SimulaQron noise in a controlled way

At this stage, the goal is not a full distributed deployment yet. The goal is a minimal local 3-node lab that we can trust before moving to physical machines.

## Verified Version Combination

The current working combination is:

- `simulaqron==3.0.16`
- `qutip==4.7.5`
- `scipy==1.12.0`
- `numpy==1.26.4`
- `matplotlib==3.8.0`

This matters because newer `scipy` versions can break the `qutip` backend used by SimulaQron `3.0.16`.

## Repository Layout

- `requirements.txt`
  Fixed dependency versions.
- `config/madrid_nodes_clean.json`
  Cleaner user-facing network description.
- `config/madrid_3nodes.json`
  Generated SimulaQron network configuration.
- `scripts/build_simulaqron_network.py`
  Converts the clean JSON into SimulaQron's network format.
- `scripts/setup_simulaqron_qutip.sh`
  Creates the virtual environment, installs dependencies, builds the network config, and configures SimulaQron.
- `scripts/run_epr_qutip_demo.sh`
  Runs the main qutip density-matrix demo.
- `scripts/run_minimal_qutip_network.sh`
  Runs the standalone qutip-only demo without SimulaQron services.
- `scripts/run_werner_sweep.sh`
  Runs a deterministic time sweep and reports Werner parameters for the two links and for swapping.
- `scripts/run_cqc_epr_smoke.sh`
  Optional high-level CQC smoke test.
- `epr_qutip_demo.py`
  Main 3-node demo for direct EPR distribution and swapping with density matrices.
- `minimal_qutip_network.py`
  Standalone in-process 3-node demo with custom `createEPR()/recvEPR()` and direct density-matrix access.
- `werner_sweep.py`
  Deterministic Werner-time sweep for link decay and swapping predictions.
- `cqc_epr_smoke.py`
  Optional high-level API smoke test using `createEPR()` and `recvEPR()`.

## Quick Start

Run:

```bash
bash scripts/setup_simulaqron_qutip.sh
bash scripts/run_epr_qutip_demo.sh --scenario direct
bash scripts/run_epr_qutip_demo.sh --scenario swap
```

What these commands do:

1. `setup_simulaqron_qutip.sh`
   Creates `.venv`, installs dependencies, builds the 3-node SimulaQron config, and configures the backend as `qutip`.
2. `run_epr_qutip_demo.sh --scenario direct`
   Runs the direct EPR distribution path and prints the exact density matrices.
3. `run_epr_qutip_demo.sh --scenario swap`
   Runs the 3-node entanglement-swapping path and prints the relevant density matrices before and after the swap.

If you want both scenarios in one run:

```bash
bash scripts/run_epr_qutip_demo.sh --scenario all
```

If you want the faster standalone version that does not start SimulaQron services:

```bash
bash scripts/run_minimal_qutip_network.sh --scenario all
```

If you want to study the time evolution statistically instead of looking at a single run:

```bash
bash scripts/run_werner_sweep.sh --noise --t1 0.1 --waits 0.0,0.1,0.3,0.5,0.7 --shots 16
```

## Main Demo

The main script is:

- `epr_qutip_demo.py`

It supports:

- `--scenario direct`
- `--scenario swap`
- `--scenario all`
- `--noise`
- `--noise-wait`
- `--t1`
- `--swap-wait-ac`
- `--swap-wait-cb`

The terminal output also includes a small timeline in nanoseconds for the key events of each scenario:

- `createEPR()` completion
- `recvEPR()` completion
- Bell measurement for swapping
- deterministic Werner decay application
- Pauli correction labeling

Short meaning of each argument:

- `--scenario`
  Chooses which experiment to run. `direct` tests one EPR link, `swap` tests entanglement swapping, and `all` runs both.
- `--noise`
  Turns on the deterministic Werner-time decay model used by this demo.
- `--noise-wait`
  Additional storage time in seconds. The demo adds the measured `createEPR()->recvEPR()` runtime on top of this.
- `--t1`
  Decay timescale of the deterministic Werner model. Smaller `t1` means faster loss of entanglement.
- `--swap-wait-ac`
  Additional storage time of the `Alice-Charlie` link. The demo adds the measured EPR age up to the swap time.
- `--swap-wait-cb`
  Additional storage time of the `Charlie-Bob` link. The demo adds the measured EPR age up to the swap time.

Example:

```bash
bash scripts/run_epr_qutip_demo.sh --scenario all --noise --noise-wait 0.5 --t1 0.1
```

## Standalone Qutip Demo

The standalone script is:

- `minimal_qutip_network.py`

It is useful when you want:

- direct density-matrix access without SimulaQron runtime overhead
- a custom in-process `createEPR(receiver)` / `recvEPR()` model
- the same 3-node `Alice`-`Charlie`-`Bob` logic
- deterministic Werner-time degradation
- a minimal swapping example that runs very fast

Run it with:

```bash
bash scripts/run_minimal_qutip_network.sh --scenario all
```

Or with time-based Werner degradation:

```bash
bash scripts/run_minimal_qutip_network.sh --scenario all --noise --noise-wait 0.1 --t1 1
```

What it does:

- loads the same 3-node JSON topology
- builds a tiny in-process network object instead of starting SimulaQron services
- implements custom `createEPR()` and `recvEPR()` primitives
- prints exact density matrices for direct EPR distribution and swapping
- measures the local runtime of EPR creation and reception
- adds those measured times to the configured waits before computing `w(t)`

Important note about the swap correction:

- Charlie's Bell-measurement outcome can be one of four Bell labels
- the Pauli correction is not arbitrary; it is determined by that Bell label
- in this script the Bell outcome is sampled from a pseudorandom generator
- because the default `--seed` is fixed to `7`, the first swap result is reproducible across runs
- that is why repeated separate runs often show the same correction, for example `Z`
- in the output you shared, `Z` maps `phi_minus` back to `phi_plus`
- it is not trying to reach `psi_plus` in that case

## Scenario 1: Direct EPR Distribution

Command:

```bash
bash scripts/run_epr_qutip_demo.sh --scenario direct
```

What it does:

- creates the link with `createEPR("Charlie")` and `recvEPR()`
- prints a nanosecond timeline for `createEPR()`, `recvEPR()`, and Werner decay
- prints the ideal EPR-pair density matrix associated with the protocol
- prints the time-degraded 2-qubit density matrix after adding measured runtime plus configured wait
- prints the reduced density matrix seen by `Alice`
- prints the reduced density matrix seen by `Charlie`
- prints the closest Bell-centered Werner label, Werner parameter `w`, and Werner-fit residual

Expected ideal result:

- the full distributed state is `|Phi+><Phi+|`
- the reduced local state at `Alice` is `I/2`
- the reduced local state at `Charlie` is `I/2`
- the reported Werner parameter is `w = 1`

That is the correct signature of an EPR pair: locally mixed, globally entangled.

## Scenario 2: Entanglement Swapping

Command:

```bash
bash scripts/run_epr_qutip_demo.sh --scenario swap
```

What it does:

- creates one EPR pair between `Alice` and `Charlie` with `createEPR()/recvEPR()`
- creates one EPR pair between `Charlie` and `Bob` with `createEPR()/recvEPR()`
- keeps the two middle qubits at `Charlie`
- performs Charlie's Bell-basis measurement
- prints a nanosecond timeline for AC `createEPR()/recvEPR()`, CB `createEPR()/recvEPR()`, swap, Werner decay, and Pauli correction
- evaluates `w1` and `w2` at the instant of swapping using measured EPR age plus configured wait
- sets the swapped Werner parameter to `w_swap = w1 * w2`
- prints the `Alice`-`Bob` reduced density matrix before the swap
- prints the `Alice`-`Bob` density matrix immediately after the Bell measurement
- prints the `Alice`-`Bob` density matrix after the Pauli correction step used by the script
- prints the reduced local density matrices at `Alice` and `Bob`
- prints Werner labels, Werner parameters, and Werner-fit residuals before and after the correction

Why this is useful:

- before the Bell measurement, the `Alice`-`Bob` reduced state should be maximally mixed (`I/4`)
- after swapping, the corrected Alice-Bob Werner parameter is computed as `w_swap = w1 * w2`
- the printed matrices make that Bell-state mapping visible instead of hiding it behind only measurement statistics
- for this local density-matrix demo, the script identifies the ideal post-swap Bell label from the exact density matrix and then applies the deterministic Werner decay on top
- in Werner terms, the ideal cases look like `w = 0` for `I/4` and `w = 1` for a pure Bell state

## Noise Model

The main qutip demo now uses a deterministic time-dependent Werner model instead of SimulaQron's random Pauli trigger.
It creates links with `createEPR()/recvEPR()`, then reconstructs the displayed density matrices analytically from the protocol flow and the measured timing.

That means:

- `--noise` enables deterministic Werner decay
- `--noise-wait` sets extra direct-link storage time beyond the measured `createEPR()->recvEPR()` runtime
- `--t1` controls how fast the Werner parameter decays with time
- `--swap-wait-ac` and `--swap-wait-cb` add extra waiting on top of the measured age of each EPR when the swap is performed

Practical interpretation:

- if `--noise` is not passed, the demo runs ideally
- if `--noise` is passed but `--noise-wait 0`, the direct-link still degrades by the measured `createEPR()->recvEPR()` runtime
- if `--noise-wait` is increased, the Werner parameter decreases
- if `--t1` is reduced, the same waiting time produces stronger degradation
- a common test choice is `--noise --noise-wait 0.5 --t1 0.1`

Example:

```bash
bash scripts/run_epr_qutip_demo.sh --scenario direct --noise --noise-wait 0.5 --t1 0.1
```

Important note:

- local reduced density matrices can still look like `I/2` even when the global entangled state has degraded
- because of that, the best signal to watch is the global density matrix together with the reported Werner parameter
- in the swapping case, if one input link reaches `w <= 1/3`, the swapped output is also no longer entangled

In this repository, the 2-qubit quality reporting is done with Bell-centered Werner states:

- `rho_W = w |B><B| + (1 - w) I/4`
- `B` is the closest Bell state found from the density matrix
- `w = 1` means a pure Bell state
- `w = 0` means maximally mixed `I/4`
- in this repository we clamp `w` to the physical range `0 <= w <= 1`
- the entanglement threshold is `w > 1/3`
- when `w <= 1/3`, the Werner state is treated as no longer entangled
- the Werner-fit residual tells you how close the actual state is to that Werner family

## Werner Sweep

The sweep script now evaluates the deterministic Werner-time model directly.
It treats each wait value as the storage time of both input links at the instant of swapping and reports the ideal prediction `w_swap = w1 * w2`.

Command:

```bash
bash scripts/run_werner_sweep.sh --noise --t1 0.1 --waits 0.0,0.1,0.3,0.5,0.7 --shots 16
```

What it does:

- computes the `Alice-Charlie` Werner parameter at each wait value
- computes the `Charlie-Bob` Werner parameter at each wait value
- computes the swapped Werner parameter using `w_swap = w1 * w2`
- marks whether each Werner state is still entangled using the `w > 1/3` rule

Meaning of the sweep arguments:

- `--waits`
  Comma-separated storage times in seconds, evaluated at the moment of swapping.
- `--shots`
  Legacy argument kept only for CLI compatibility. It is ignored by the deterministic sweep.
- `--noise`
  Enables deterministic Werner-time decay.
- `--t1`
  Decay timescale. Smaller values mean stronger decoherence at the same wait.

Meaning of the printed columns:

- `w_ac`
  Werner parameter of the `Alice-Charlie` link at the instant of swapping.
- `w_cb`
  Werner parameter of the `Charlie-Bob` link at the instant of swapping.
- `w_swap`
  Werner parameter of the swapped `Alice-Bob` state.
- `w1*w2`
  Ideal swapped Werner parameter using `w_swap = w1 * w2`.
- `delta`
  Difference between `w_swap` and `w1*w2`. In this deterministic model it should be `0`.
- `ent_ac`, `ent_cb`, `ent_sw`
  Whether the corresponding Werner state still satisfies `w > 1/3`.

Werner interpretation used here:

- `w = 1` means ideal Bell-quality entanglement
- `w = 1/3` is the separability boundary
- `w < 1/3` means the fitted Werner state is no longer entangled
- `w = 0` is the fully mixed limit

## Why The Demo Uses CQC Plus Reconstruction

The main purpose of this repository is to test EPR distribution and swapping through SimulaQron's public EPR API:

- `createEPR(receiver_node)`
- `recvEPR()`

The qutip backend is still used underneath, but the displayed density matrices in `epr_qutip_demo.py` are reconstructed analytically from:

- the ideal Bell/EPR protocol flow
- Charlie's Bell-measurement bits during swapping
- the deterministic Werner-time model

So the split is:

- `epr_qutip_demo.py`: CQC EPR workflow plus density-matrix reconstruction
- `cqc_epr_smoke.py`: optional lighter API sanity check

## Optional CQC Smoke Test

If you want a small high-level API check as well, run:

```bash
bash scripts/run_cqc_epr_smoke.sh --rounds 10
```

This validates:

- `CQCConnection.createEPR()`
- `CQCConnection.recvEPR()`

It does not print exact density matrices.

By default, this smoke test now uses the direct edge:

- `Alice -> Charlie`

That matches the minimal path topology:

- `Alice <-> Charlie <-> Bob`

If you want a different direct edge, you can choose it explicitly:

```bash
bash scripts/run_cqc_epr_smoke.sh --sender Charlie --receiver Bob --rounds 10
```

Short meaning of the smoke-test arguments:

- `--rounds`
  Number of EPR pairs generated and measured in the test loop.
- `--sender`
  Node that calls `createEPR()`.
- `--receiver`
  Node that calls `recvEPR()`.
- `--noise`
  Enables SimulaQron's optional time-based memory noise for that smoke test.
- `--memory-wait`
  Wait time before measurement, so the noise has time to appear.
- `--t1`
  Noise timescale for the memory model. Smaller means noisier for the same wait.

If you want to make the optional time-based noise more visible there:

```bash
bash scripts/run_cqc_epr_smoke.sh --rounds 10 --noise --t1 0.1 --memory-wait 0.5
```

## Clean Network Configuration

The recommended file to edit is:

- `config/madrid_nodes_clean.json`

Example:

```json
{
    "network_name": "madrid_demo",
    "defaults": {
        "host": "localhost"
    },
    "nodes": [
        {
            "name": "Alice",
            "host": "localhost",
            "base_port": 9100
        },
        {
            "name": "Bob",
            "host": "localhost",
            "base_port": 9110
        },
        {
            "name": "Charlie",
            "host": "localhost",
            "base_port": 9120
        }
    ],
    "links": [
        ["Alice", "Charlie"],
        ["Charlie", "Bob"]
    ]
}
```

Rules:

- `network_name`
  Name used internally by SimulaQron to identify this network definition.
- `defaults.host`
  Default host/IP used for nodes that do not set their own `host`.
- `nodes`
  List of node definitions.
- `nodes[].name`
  Logical node name used in SimulaQron, for example `Alice` or `Charlie`.
- `nodes[].host`
  Hostname or IP address where that node will listen.
- `base_port` defines the 3 ports for a node
- `nodes[].base_port`
  Starting port for that node. The script derives the other two ports from it.
- `app_socket = base_port`
- `cqc_socket = base_port + 1`
- `vnode_socket = base_port + 2`
- `links`
  Undirected edges of the network topology, written as pairs of node names

For this minimal swapping testbed, the recommended topology is the path:

- `Alice <-> Charlie <-> Bob`

After editing the clean file, regenerate and reconfigure with:

```bash
bash scripts/setup_simulaqron_qutip.sh
```

## Using Real IP Addresses Later

When you move from local testing to real machines, edit `config/madrid_nodes_clean.json` and replace `localhost` with the real IP address for each node.

Example:

```json
{
    "name": "Alice",
    "host": "192.168.1.10",
    "base_port": 9100
}
```

Then run:

```bash
bash scripts/setup_simulaqron_qutip.sh
```

This rebuilds `config/madrid_3nodes.json` with the updated addresses.

## Manual Commands

If you want to run the individual steps manually:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
python scripts/build_simulaqron_network.py --input config/madrid_nodes_clean.json --output config/madrid_3nodes.json
simulaqron set backend qutip
simulaqron set network-config-file "$(pwd)/config/madrid_3nodes.json"
simulaqron set noisy-qubits off
simulaqron set t1 1.0
python epr_qutip_demo.py --scenario direct
python epr_qutip_demo.py --scenario swap
```

## Current Status

What is already in place:

- a minimal 3-node local SimulaQron network
- `qutip` as backend
- exact density-matrix exposure for the direct EPR path
- exact density-matrix exposure for the 3-node swapping path
- optional time-based noise controls for both scenarios

What this repository is intentionally not doing yet:

- GUI
- deployment across physical machines
- a larger Madrid-wide network
- a full production control plane

That will come later. Right now the focus is the smallest possible 3-node setup that lets us test EPR distribution, swapping, noise, and density matrices with as little moving machinery as possible.
