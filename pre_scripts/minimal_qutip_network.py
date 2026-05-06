#!/usr/bin/env python3
"""
Minimal 3-node EPR and swapping demo without SimulaQron runtime.

This script keeps the same project idea as the SimulaQron-based demos, but it
implements its own tiny in-process network model:

- `createEPR(receiver)` creates an ideal EPR pair locally
- `recvEPR()` picks up the queued remote EPR half
- density matrices are available directly
- time-based noise can be modeled either with Bell-centered Werner states or
  with a Lindblad master equation

The goal is to make direct EPR distribution and swapping easy to inspect while
removing the runtime overhead of the SimulaQron services.
"""

import argparse
import json
import random
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path

import numpy as np
import qutip as qt


EXPECTED_VERSIONS = {
    "qutip": "4.7.5",
    "scipy": "1.12.0",
    "numpy": "1.26.4",
}


def package_versions():
    """Return the versions of the numerical packages used by this standalone model."""
    versions = {}
    for package in EXPECTED_VERSIONS:
        versions[package] = metadata.version(package)
    return versions


def format_complex(value):
    """Format one complex number so printed density matrices are easy to inspect."""
    return f"{value.real: .6f}{value.imag:+.6f}j"


def format_matrix(matrix):
    """Render a matrix as a multi-line string."""
    return "\n".join("[" + ", ".join(format_complex(value) for value in row) + "]" for row in matrix)


def ns_with_seconds(value_ns):
    """Show one duration in both nanoseconds and seconds."""
    return f"{value_ns} ns ({value_ns / 1e9:.9f} s)"


def format_timeline(timeline):
    """Render the event timeline using both ns and seconds."""
    lines = []
    for key, value in timeline.items():
        label = key.replace("_", " ")
        lines.append(f"  {label}: t={value} ns ({value / 1e9:.9f} s)")
    return "\n".join(lines)


def matrix_to_array(matrix):
    """Convert a nested list to a NumPy complex array."""
    return np.array(matrix, dtype=complex)


def tensor_product_matrices(left_matrix, right_matrix):
    """Build the tensor product of two density matrices represented as nested lists."""
    return np.kron(matrix_to_array(left_matrix), matrix_to_array(right_matrix)).tolist()


def bell_state_vector(label="phi_plus"):
    """Return one Bell state vector."""
    scale = 1 / np.sqrt(2)
    if label == "phi_plus":
        return np.array([scale, 0, 0, scale], dtype=complex)
    if label == "phi_minus":
        return np.array([scale, 0, 0, -scale], dtype=complex)
    if label == "psi_plus":
        return np.array([0, scale, scale, 0], dtype=complex)
    if label == "psi_minus":
        return np.array([0, scale, -scale, 0], dtype=complex)
    raise ValueError(f"Unknown Bell state label: {label}")


def bell_state_density_qobj(label="phi_plus"):
    """Return one Bell-state density matrix as a QuTiP object."""
    return qt.Qobj(matrix_to_array(BELL_MATRICES[label]), dims=[[2, 2], [2, 2]])


def density_from_statevector(vector):
    """Build a density matrix from a pure state vector."""
    return np.outer(vector, np.conjugate(vector)).tolist()


BELL_MATRICES = {
    "phi_plus": density_from_statevector(bell_state_vector("phi_plus")),
    "phi_minus": density_from_statevector(bell_state_vector("phi_minus")),
    "psi_plus": density_from_statevector(bell_state_vector("psi_plus")),
    "psi_minus": density_from_statevector(bell_state_vector("psi_minus")),
}


def maximally_mixed_qubit():
    """Return I/2."""
    return [
        [0.5 + 0j, 0j],
        [0j, 0.5 + 0j],
    ]


def maximally_mixed_two_qubits():
    """Return I/4."""
    return [
        [0.25 + 0j, 0j, 0j, 0j],
        [0j, 0.25 + 0j, 0j, 0j],
        [0j, 0j, 0.25 + 0j, 0j],
        [0j, 0j, 0j, 0.25 + 0j],
    ]


def matrix_close(actual, expected, atol=1e-6):
    """Check matrix equality up to numerical tolerance."""
    return bool(np.allclose(matrix_to_array(actual), matrix_to_array(expected), atol=atol))


def partial_trace(matrix, keep, total_qubits):
    """Trace out all qubits except those listed in `keep`."""
    rho = qt.Qobj(matrix_to_array(matrix), dims=[[2] * total_qubits, [2] * total_qubits])
    return rho.ptrace(keep).full().tolist()


def reduced_pair_states(matrix):
    """Return the 1-qubit reduced states of a 2-qubit state."""
    return (
        partial_trace(matrix, [0], total_qubits=2),
        partial_trace(matrix, [1], total_qubits=2),
    )


def bell_fidelity(matrix, label="phi_plus"):
    """Overlap with one Bell state."""
    rho = matrix_to_array(matrix)
    bell = bell_state_vector(label)
    return float(np.real(np.conjugate(bell) @ rho @ bell))


def identify_bell_state(matrix):
    """Pick the Bell label with highest overlap."""
    scores = [(label, bell_fidelity(matrix, label=label)) for label in BELL_MATRICES]
    scores.sort(key=lambda item: item[1], reverse=True)
    return scores[0]


def werner_parameter_from_fidelity(fidelity):
    """Convert Bell fidelity to the Werner parameter and clamp it to 0..1."""
    parameter = (4.0 * fidelity - 1.0) / 3.0
    return max(0.0, min(1.0, parameter))


def werner_parameter_from_time(wait_seconds, t1, enabled):
    """Exponential time decay used by the standalone model."""
    if not enabled or wait_seconds <= 0:
        return 1.0
    if t1 <= 0:
        raise ValueError("t1 must be positive when the deterministic Werner-time model is enabled")
    return float(np.exp(-wait_seconds / t1))


def werner_is_entangled(parameter, atol=1e-9):
    """A Bell-centered Werner state is entangled iff w > 1/3."""
    return parameter > (1.0 / 3.0 + atol)


def werner_state(label, parameter):
    """Construct rho_W = w |B><B| + (1-w) I/4."""
    bell = matrix_to_array(BELL_MATRICES[label])
    mixed = np.eye(4, dtype=complex) / 4.0
    return (parameter * bell + (1.0 - parameter) * mixed).tolist()


def werner_fit(matrix, label=None):
    """Fit a matrix to the closest Bell-centered Werner family member."""
    if label is None:
        label, fidelity = identify_bell_state(matrix)
    else:
        fidelity = bell_fidelity(matrix, label=label)
    parameter = werner_parameter_from_fidelity(fidelity)
    fitted_state = werner_state(label, parameter)
    residual = float(np.linalg.norm(matrix_to_array(matrix) - matrix_to_array(fitted_state)))
    return {
        "label": label,
        "parameter": parameter,
        "state": fitted_state,
        "residual": residual,
        "is_entangled": werner_is_entangled(parameter),
        "matches_exactly": matrix_close(matrix, fitted_state),
    }


def single_qubit_operator(single_op, qubit_index, total_qubits):
    """Lift a 1-qubit operator to the specified position in an N-qubit register."""
    factors = [qt.qeye(2) for _ in range(total_qubits)]
    factors[qubit_index] = single_op
    return qt.tensor(factors)


def cnot_operator(control, target, total_qubits):
    """Construct a CNOT gate acting on arbitrary qubit positions."""
    proj0 = qt.basis(2, 0) * qt.basis(2, 0).dag()
    proj1 = qt.basis(2, 1) * qt.basis(2, 1).dag()
    x_gate = qt.sigmax()
    return (
        single_qubit_operator(proj0, control, total_qubits)
        + single_qubit_operator(proj1, control, total_qubits) * single_qubit_operator(x_gate, target, total_qubits)
    )


def hadamard_operator(qubit_index, total_qubits):
    """Construct a Hadamard gate acting on one qubit in an N-qubit register."""
    hadamard = qt.Qobj(
        (1 / np.sqrt(2)) * np.array([[1, 1], [1, -1]], dtype=complex),
        dims=[[2], [2]],
    )
    return single_qubit_operator(hadamard, qubit_index, total_qubits)


def projector_on_bit(qubit_index, bit_value, total_qubits):
    """Project one qubit of an N-qubit register onto |0> or |1>."""
    projector = qt.basis(2, bit_value) * qt.basis(2, bit_value).dag()
    return single_qubit_operator(projector, qubit_index, total_qubits)


def build_local_lindblad_operators(total_qubits, gamma_relax_map=None, gamma_dephase_map=None):
    """Build local Lindblad jump operators for amplitude damping and dephasing."""
    gamma_relax_map = gamma_relax_map or {}
    gamma_dephase_map = gamma_dephase_map or {}

    operators = []
    sigma_minus = qt.sigmam()
    sigma_z = qt.sigmaz()

    for qubit_index, gamma_value in gamma_relax_map.items():
        if gamma_value > 0:
            operators.append(np.sqrt(gamma_value) * single_qubit_operator(sigma_minus, qubit_index, total_qubits))

    for qubit_index, gamma_value in gamma_dephase_map.items():
        if gamma_value > 0:
            operators.append(np.sqrt(gamma_value) * single_qubit_operator(sigma_z, qubit_index, total_qubits))

    return operators


def apply_lindblad_noise(rho0, duration, collapse_operators):
    """Evolve a density matrix with a Lindblad master equation for a fixed duration."""
    if duration <= 0 or not collapse_operators:
        return rho0

    zero_hamiltonian = 0 * rho0
    times = np.linspace(0.0, float(duration), 2)
    result = qt.mesolve(zero_hamiltonian, rho0, times, c_ops=collapse_operators, e_ops=[])
    return result.states[-1]


def lindblad_direct_epr_state(duration, gamma_relax, gamma_dephase):
    """Apply local Lindblad noise to a direct 2-qubit EPR pair."""
    rho0 = bell_state_density_qobj("phi_plus")
    collapse_operators = build_local_lindblad_operators(
        total_qubits=2,
        gamma_relax_map={0: gamma_relax, 1: gamma_relax},
        gamma_dephase_map={0: gamma_dephase, 1: gamma_dephase},
    )
    return apply_lindblad_noise(rho0, duration, collapse_operators)


def lindblad_swap_input_state(duration_ac, duration_cb, gamma_relax, gamma_dephase):
    """Build the 4-qubit swap input state from two independently evolved noisy EPR links."""
    rho_ac = lindblad_direct_epr_state(duration_ac, gamma_relax, gamma_dephase)
    rho_cb = lindblad_direct_epr_state(duration_cb, gamma_relax, gamma_dephase)
    return qt.tensor(rho_ac, rho_cb)


def lindblad_swap_measurement(rho_before_measurement, rng):
    """Apply Charlie's Bell measurement to a 4-qubit state and return the post-measurement Alice-Bob state."""
    total_qubits = 4
    measurement_unitary = hadamard_operator(1, total_qubits) * cnot_operator(1, 2, total_qubits)
    measured_register = measurement_unitary * rho_before_measurement * measurement_unitary.dag()

    outcomes = [(0, 0), (0, 1), (1, 0), (1, 1)]
    probabilities = []
    projectors = {}

    for bits in outcomes:
        projector = projector_on_bit(1, bits[0], total_qubits) * projector_on_bit(2, bits[1], total_qubits)
        probability = float(np.real((projector * measured_register).tr()))
        projectors[bits] = projector
        probabilities.append(max(0.0, probability))

    probability_sum = sum(probabilities)
    if probability_sum <= 0:
        raise RuntimeError("The Bell-measurement probabilities vanished unexpectedly")

    threshold = rng.random() * probability_sum
    cumulative = 0.0
    chosen_bits = outcomes[-1]
    for bits, probability in zip(outcomes, probabilities):
        cumulative += probability
        if threshold <= cumulative:
            chosen_bits = bits
            break

    projector = projectors[chosen_bits]
    probability = float(np.real((projector * measured_register).tr()))
    post_measurement = (projector * measured_register * projector) / probability
    alice_bob_state = post_measurement.ptrace([0, 3])
    return chosen_bits, alice_bob_state


def apply_bob_pauli_correction(rho_ab, correction_name):
    """Apply Bob's logical Pauli correction to a 2-qubit Alice-Bob density matrix."""
    pauli_i = qt.qeye(2)
    pauli_x = qt.sigmax()
    pauli_z = qt.sigmaz()

    if correction_name == "I":
        correction = pauli_i
    elif correction_name == "X":
        correction = pauli_x
    elif correction_name == "Z":
        correction = pauli_z
    elif correction_name == "XZ":
        correction = pauli_x * pauli_z
    else:
        raise ValueError(f"Unknown Pauli correction: {correction_name}")

    operator = qt.tensor(pauli_i, correction)
    return operator * rho_ab * operator.dag()


def bob_pauli_correction_from_label(raw_label):
    """Map the raw swapped Bell state back to |Phi+> with one logical Pauli."""
    if raw_label == "phi_plus":
        return "I"
    if raw_label == "phi_minus":
        return "Z"
    if raw_label == "psi_plus":
        return "X"
    if raw_label == "psi_minus":
        return "XZ"
    raise ValueError(f"Unknown Bell state label: {raw_label}")


def bell_label_from_swap_bits(measurement_first, measurement_second):
    """Bell-measurement bits of Charlie determine which Bell state Alice-Bob share."""
    mapping = {
        (0, 0): "phi_plus",
        (0, 1): "psi_plus",
        (1, 0): "phi_minus",
        (1, 1): "psi_minus",
    }
    return mapping[(measurement_first, measurement_second)]


def normalize_network_description(path, requested_network_name=None):
    """Load either the clean JSON or the generated SimulaQron JSON and normalize it."""
    with open(path, "r", encoding="utf-8") as handle:
        raw = json.load(handle)

    if "network_name" in raw and "nodes" in raw:
        network_name = raw["network_name"]
        if requested_network_name and requested_network_name != network_name:
            raise ValueError(
                f"Requested network '{requested_network_name}' does not match clean config network '{network_name}'"
            )
        links = [tuple(link) for link in raw.get("links", [])]
        return {
            "network_name": network_name,
            "nodes": raw["nodes"],
            "links": links,
        }

    if requested_network_name is None:
        if len(raw) != 1:
            available = ", ".join(sorted(raw))
            raise ValueError(
                "The generated JSON contains multiple networks. "
                f"Please pass --network-name explicitly. Available: {available}"
            )
        requested_network_name = next(iter(raw))

    if requested_network_name not in raw:
        available = ", ".join(sorted(raw))
        raise ValueError(f"Network '{requested_network_name}' not found. Available networks: {available}")

    entry = raw[requested_network_name]
    nodes = []
    for name, sockets in entry["nodes"].items():
        host, app_port = sockets["app_socket"]
        nodes.append({
            "name": name,
            "host": host,
            "base_port": int(app_port),
        })

    links = []
    seen = set()
    for left, neighbors in entry.get("topology", {}).items():
        for right in neighbors:
            pair = tuple(sorted((left, right)))
            if pair not in seen:
                seen.add(pair)
                links.append(pair)

    return {
        "network_name": requested_network_name,
        "nodes": nodes,
        "links": links,
    }


@dataclass
class LocalEPRHalf:
    """One local handle to a shared EPR pair inside the minimal model."""

    pair_id: int
    owner: str
    remote_owner: str
    created_at_ns: int
    create_completed_at_ns: int = 0
    recv_completed_at_ns: int = 0
    active: bool = True


@dataclass
class MinimalQutipNode:
    """Tiny node object with an incoming EPR queue and local handle registry."""

    name: str
    host: str
    base_port: int
    incoming_eprs: deque = field(default_factory=deque)
    local_halves: dict = field(default_factory=dict)

    def createEPR(self, receiver_name, network):
        """Create a new EPR pair and queue the remote half for the receiver."""
        return network.create_epr(self.name, receiver_name)

    def recvEPR(self):
        """Receive the next queued EPR half."""
        if not self.incoming_eprs:
            raise RuntimeError(f"No queued EPR half available for node {self.name}")
        half = self.incoming_eprs.popleft()
        self.local_halves[half.pair_id] = half
        return half


class MinimalQutipNetwork:
    """Minimal in-process 3-node network with direct EPR creation and swapping support."""

    def __init__(self, description, seed=None):
        self.network_name = description["network_name"]
        self.links = {frozenset(link) for link in description["links"]}
        self.nodes = {
            node["name"]: MinimalQutipNode(
                name=node["name"],
                host=node.get("host", "localhost"),
                base_port=int(node["base_port"]),
            )
            for node in description["nodes"]
        }
        self._next_pair_id = 1
        self._rng = random.Random(seed)

    def ensure_node(self, name):
        """Raise a clear error if the named node does not exist."""
        if name not in self.nodes:
            available = ", ".join(sorted(self.nodes))
            raise ValueError(f"Unknown node '{name}'. Available nodes: {available}")

    def ensure_adjacent(self, left, right):
        """Require that two nodes are adjacent in the configured topology."""
        if frozenset((left, right)) not in self.links:
            raise ValueError(f"Nodes {left} and {right} are not adjacent in the configured topology")

    def create_epr(self, sender_name, receiver_name):
        """Implement the local `createEPR(receiver)` primitive."""
        self.ensure_node(sender_name)
        self.ensure_node(receiver_name)
        self.ensure_adjacent(sender_name, receiver_name)

        created_at_ns = time.perf_counter_ns()
        pair_id = self._next_pair_id
        self._next_pair_id += 1

        sender_half = LocalEPRHalf(
            pair_id=pair_id,
            owner=sender_name,
            remote_owner=receiver_name,
            created_at_ns=created_at_ns,
        )
        receiver_half = LocalEPRHalf(
            pair_id=pair_id,
            owner=receiver_name,
            remote_owner=sender_name,
            created_at_ns=created_at_ns,
        )

        self.nodes[sender_name].local_halves[pair_id] = sender_half
        self.nodes[receiver_name].incoming_eprs.append(receiver_half)
        return sender_half

    def perform_swap_bell_measurement(self, left_half, right_half):
        """Consume Charlie's two halves and produce the Bell outcome bits for swapping."""
        if not left_half.active or not right_half.active:
            raise RuntimeError("Charlie cannot swap with an inactive EPR half")

        outcomes = [(0, 0), (0, 1), (1, 0), (1, 1)]
        measurement = outcomes[self._rng.randrange(len(outcomes))]
        left_half.active = False
        right_half.active = False
        self.nodes[left_half.owner].local_halves.pop(left_half.pair_id, None)
        self.nodes[right_half.owner].local_halves.pop(right_half.pair_id, None)
        return measurement


def mark_time_ns(start_ns):
    """Return elapsed time since an experiment started."""
    return time.perf_counter_ns() - start_ns


def parse_args():
    """Read command-line arguments for the standalone qutip-only demo."""
    repo_root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(
        description=(
            "Standalone qutip-only 3-node demo with custom createEPR()/recvEPR(), "
            "exact density matrices, and selectable Werner or Lindblad noise."
        )
    )
    parser.add_argument(
        "--config",
        default=str(repo_root / "config" / "madrid_nodes_clean.json"),
        help="Path to the clean 3-node JSON, or to the generated SimulaQron JSON.",
    )
    parser.add_argument(
        "--network-name",
        default=None,
        help="Optional network name when the input JSON contains more than one network.",
    )
    parser.add_argument(
        "--scenario",
        choices=["direct", "swap", "all"],
        default="all",
        help="Which experiment to run.",
    )
    parser.add_argument(
        "--noise",
        action="store_true",
        help="Enable the selected noise model.",
    )
    parser.add_argument(
        "--noise-model",
        choices=["werner", "lindblad"],
        default="werner",
        help="Noise model to use when --noise is enabled.",
    )
    parser.add_argument(
        "--noise-wait",
        type=float,
        default=0.0,
        help="Additional wait in seconds added to the measured direct-link runtime.",
    )
    parser.add_argument(
        "--t1",
        type=float,
        default=1.0,
        help=(
            "Time constant of the Werner decay model. When Lindblad noise is selected and "
            "--gamma-dephase is omitted, the default dephasing rate is 1/T1."
        ),
    )
    parser.add_argument(
        "--gamma-relax",
        type=float,
        default=None,
        help="Local amplitude-damping rate used only by the Lindblad noise model.",
    )
    parser.add_argument(
        "--gamma-dephase",
        type=float,
        default=None,
        help="Local pure-dephasing rate used only by the Lindblad noise model.",
    )
    parser.add_argument(
        "--swap-wait-ac",
        type=float,
        default=None,
        help="Additional wait in seconds added to the measured Alice-Charlie age at swap time.",
    )
    parser.add_argument(
        "--swap-wait-cb",
        type=float,
        default=None,
        help="Additional wait in seconds added to the measured Charlie-Bob age at swap time.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=7,
        help="Random seed used only for Charlie's Bell-measurement outcomes in the swap demo.",
    )
    return parser.parse_args()


def print_versions():
    """Print the numerical stack used by the standalone simulator."""
    versions = package_versions()
    print("Detected versions:")
    for package, version in versions.items():
        expected = EXPECTED_VERSIONS[package]
        suffix = " (expected)" if version == expected else f" (expected {expected})"
        print(f"  {package}=={version}{suffix}")


def resolved_lindblad_rates(noise_model, t1, gamma_relax_arg, gamma_dephase_arg):
    """Resolve the Lindblad rates, keeping the CLI ergonomic for quick experiments."""
    if gamma_relax_arg is not None and gamma_relax_arg < 0:
        raise ValueError("--gamma-relax must be non-negative")
    if gamma_dephase_arg is not None and gamma_dephase_arg < 0:
        raise ValueError("--gamma-dephase must be non-negative")

    if noise_model != "lindblad":
        return 0.0, 0.0

    gamma_relax = 0.0 if gamma_relax_arg is None else gamma_relax_arg
    if gamma_dephase_arg is not None:
        gamma_dephase = gamma_dephase_arg
    else:
        if t1 <= 0:
            raise ValueError("t1 must be positive when Lindblad noise derives its default dephasing rate from T1")
        gamma_dephase = 1.0 / t1
    return gamma_relax, gamma_dephase


def run_direct_distribution(network, noisy, noise_wait, t1, noise_model, gamma_relax, gamma_dephase):
    """Run the direct Alice -> Charlie EPR flow on the standalone network."""
    sender_name = "Alice"
    receiver_name = "Charlie"
    sender = network.nodes[sender_name]
    receiver = network.nodes[receiver_name]

    start_ns = time.perf_counter_ns()
    timeline = {"experiment_started": 0}

    create_started_abs_ns = time.perf_counter_ns()
    sender_half = sender.createEPR(receiver_name, network)
    create_completed_abs_ns = time.perf_counter_ns()
    sender_half.create_completed_at_ns = create_completed_abs_ns
    timeline["createEPR_completed"] = create_completed_abs_ns - start_ns

    receiver_half = receiver.recvEPR()
    recv_completed_abs_ns = time.perf_counter_ns()
    receiver_half.create_completed_at_ns = create_completed_abs_ns
    receiver_half.recv_completed_at_ns = recv_completed_abs_ns
    timeline["recvEPR_completed"] = recv_completed_abs_ns - start_ns

    runtime_create_to_recv = max(0.0, (recv_completed_abs_ns - create_completed_abs_ns) / 1e9)
    effective_wait = noise_wait + runtime_create_to_recv
    if not noisy:
        full_after_distribution = BELL_MATRICES["phi_plus"]
        timeline["ideal_state_recorded"] = mark_time_ns(start_ns)
    elif noise_model == "lindblad":
        full_after_distribution = lindblad_direct_epr_state(
            effective_wait,
            gamma_relax=gamma_relax,
            gamma_dephase=gamma_dephase,
        ).full().tolist()
        timeline["lindblad_evolution_applied"] = mark_time_ns(start_ns)
    else:
        link_parameter = werner_parameter_from_time(effective_wait, t1, noisy)
        full_after_distribution = werner_state("phi_plus", link_parameter)
        timeline["werner_decay_applied"] = mark_time_ns(start_ns)
    sender_reduced, receiver_reduced = reduced_pair_states(full_after_distribution)
    closest_werner = werner_fit(full_after_distribution, label="phi_plus")

    sender_half.active = False
    receiver_half.active = False
    sender.local_halves.pop(sender_half.pair_id, None)
    receiver.local_halves.pop(receiver_half.pair_id, None)

    return {
        "sender_name": sender_name,
        "receiver_name": receiver_name,
        "configured_wait": noise_wait,
        "runtime_create_call_ns": create_completed_abs_ns - create_started_abs_ns,
        "runtime_create_to_recv": runtime_create_to_recv,
        "effective_wait": effective_wait,
        "noise_model": noise_model,
        "gamma_relax": gamma_relax,
        "gamma_dephase": gamma_dephase,
        "timeline_ns": timeline,
        "full_before_send": BELL_MATRICES["phi_plus"],
        "full_after_distribution": full_after_distribution,
        "sender_reduced": sender_reduced,
        "receiver_reduced": receiver_reduced,
        "closest_werner": closest_werner,
        "matches_bell": matrix_close(full_after_distribution, BELL_MATRICES["phi_plus"]),
        "sender_local_ok": matrix_close(sender_reduced, maximally_mixed_qubit()),
        "receiver_local_ok": matrix_close(receiver_reduced, maximally_mixed_qubit()),
    }


def run_entanglement_swapping(network, noisy, link_wait_ac, link_wait_cb, t1, noise_model, gamma_relax, gamma_dephase):
    """Run Alice-Charlie-Bob entanglement swapping on the standalone network."""
    alice = network.nodes["Alice"]
    bob = network.nodes["Bob"]
    charlie = network.nodes["Charlie"]

    start_ns = time.perf_counter_ns()
    timeline = {"experiment_started": 0}

    ac_create_started_abs_ns = time.perf_counter_ns()
    q_charlie_left = charlie.createEPR("Alice", network)
    ac_create_completed_abs_ns = time.perf_counter_ns()
    q_charlie_left.create_completed_at_ns = ac_create_completed_abs_ns
    timeline["alice_charlie_createEPR_completed"] = ac_create_completed_abs_ns - start_ns

    q_alice = alice.recvEPR()
    alice_recv_abs_ns = time.perf_counter_ns()
    q_alice.create_completed_at_ns = ac_create_completed_abs_ns
    q_alice.recv_completed_at_ns = alice_recv_abs_ns
    timeline["alice_recvEPR_completed"] = alice_recv_abs_ns - start_ns

    cb_create_started_abs_ns = time.perf_counter_ns()
    q_charlie_right = charlie.createEPR("Bob", network)
    cb_create_completed_abs_ns = time.perf_counter_ns()
    q_charlie_right.create_completed_at_ns = cb_create_completed_abs_ns
    timeline["charlie_bob_createEPR_completed"] = cb_create_completed_abs_ns - start_ns

    q_bob = bob.recvEPR()
    bob_recv_abs_ns = time.perf_counter_ns()
    q_bob.create_completed_at_ns = cb_create_completed_abs_ns
    q_bob.recv_completed_at_ns = bob_recv_abs_ns
    timeline["bob_recvEPR_completed"] = bob_recv_abs_ns - start_ns

    runtime_create_to_recv_ac = max(0.0, (alice_recv_abs_ns - ac_create_completed_abs_ns) / 1e9)
    runtime_create_to_recv_cb = max(0.0, (bob_recv_abs_ns - cb_create_completed_abs_ns) / 1e9)
    swap_setup_abs_ns = time.perf_counter_ns()
    age_to_swap_ac = max(0.0, (swap_setup_abs_ns - ac_create_completed_abs_ns) / 1e9)
    age_to_swap_cb = max(0.0, (swap_setup_abs_ns - cb_create_completed_abs_ns) / 1e9)
    effective_wait_ac = link_wait_ac + age_to_swap_ac
    effective_wait_cb = link_wait_cb + age_to_swap_cb

    if not noisy:
        rho_ac = bell_state_density_qobj("phi_plus")
        rho_cb = bell_state_density_qobj("phi_plus")
        timeline["ideal_state_recorded"] = mark_time_ns(start_ns)
    elif noise_model == "lindblad":
        rho_ac = lindblad_direct_epr_state(
            effective_wait_ac,
            gamma_relax=gamma_relax,
            gamma_dephase=gamma_dephase,
        )
        rho_cb = lindblad_direct_epr_state(
            effective_wait_cb,
            gamma_relax=gamma_relax,
            gamma_dephase=gamma_dephase,
        )
        timeline["lindblad_evolution_applied"] = mark_time_ns(start_ns)
    else:
        rho_ac = qt.Qobj(matrix_to_array(werner_state("phi_plus", werner_parameter_from_time(effective_wait_ac, t1, True))), dims=[[2, 2], [2, 2]])
        rho_cb = qt.Qobj(matrix_to_array(werner_state("phi_plus", werner_parameter_from_time(effective_wait_cb, t1, True))), dims=[[2, 2], [2, 2]])
        timeline["werner_decay_applied"] = mark_time_ns(start_ns)

    link_werner_ac = werner_fit(rho_ac.full().tolist(), label="phi_plus")
    link_werner_cb = werner_fit(rho_cb.full().tolist(), label="phi_plus")
    predicted_swap_parameter = link_werner_ac["parameter"] * link_werner_cb["parameter"]

    rho_before_swap = qt.tensor(rho_ac, rho_cb)
    ab_before_swap = rho_before_swap.ptrace([0, 3]).full().tolist()
    before_swap_werner = werner_fit(ab_before_swap)

    bell_measurement, raw_ab_qobj = lindblad_swap_measurement(rho_before_swap, network._rng)
    swap_measurement_abs_ns = time.perf_counter_ns()
    timeline["swap_bell_measurement_completed"] = swap_measurement_abs_ns - start_ns

    raw_label = bell_label_from_swap_bits(*bell_measurement)
    raw_ab_after_swap = raw_ab_qobj.full().tolist()
    raw_werner = werner_fit(raw_ab_after_swap, label=raw_label)

    applied_correction = bob_pauli_correction_from_label(raw_label)
    corrected_ab_qobj = apply_bob_pauli_correction(raw_ab_qobj, applied_correction)
    timeline["pauli_correction_applied"] = mark_time_ns(start_ns)

    corrected_ab = corrected_ab_qobj.full().tolist()
    corrected_werner = werner_fit(corrected_ab, label="phi_plus")
    alice_reduced_before, bob_reduced_before = reduced_pair_states(ab_before_swap)
    alice_reduced_after, bob_reduced_after = reduced_pair_states(corrected_ab)

    q_alice.active = False
    q_bob.active = False
    alice.local_halves.pop(q_alice.pair_id, None)
    bob.local_halves.pop(q_bob.pair_id, None)

    return {
        "timeline_ns": timeline,
        "configured_wait_ac": link_wait_ac,
        "configured_wait_cb": link_wait_cb,
        "runtime_create_call_ac_ns": ac_create_completed_abs_ns - ac_create_started_abs_ns,
        "runtime_create_call_cb_ns": cb_create_completed_abs_ns - cb_create_started_abs_ns,
        "runtime_create_to_recv_ac": runtime_create_to_recv_ac,
        "runtime_create_to_recv_cb": runtime_create_to_recv_cb,
        "age_to_swap_ac": age_to_swap_ac,
        "age_to_swap_cb": age_to_swap_cb,
        "effective_wait_ac": effective_wait_ac,
        "effective_wait_cb": effective_wait_cb,
        "noise_model": noise_model,
        "gamma_relax": gamma_relax,
        "gamma_dephase": gamma_dephase,
        "link_werner_ac": link_werner_ac,
        "link_werner_cb": link_werner_cb,
        "predicted_swap_parameter": predicted_swap_parameter,
        "ab_before_swap": ab_before_swap,
        "before_swap_werner": before_swap_werner,
        "bell_measurement": bell_measurement,
        "ideal_raw_ab_after_swap": BELL_MATRICES[raw_label],
        "raw_ab_after_swap": raw_ab_after_swap,
        "raw_werner": raw_werner,
        "applied_correction": applied_correction,
        "corrected_ab": corrected_ab,
        "corrected_werner": corrected_werner,
        "alice_reduced_before": alice_reduced_before,
        "bob_reduced_before": bob_reduced_before,
        "alice_reduced_after": alice_reduced_after,
        "bob_reduced_after": bob_reduced_after,
        "ab_before_swap_is_mixed": matrix_close(ab_before_swap, maximally_mixed_two_qubits()),
        "ab_before_swap_is_product": matrix_close(
            ab_before_swap,
            tensor_product_matrices(alice_reduced_before, bob_reduced_before),
        ),
        "corrected_matches_bell": matrix_close(corrected_ab, BELL_MATRICES["phi_plus"]),
        "alice_local_ok": matrix_close(alice_reduced_after, maximally_mixed_qubit()),
        "bob_local_ok": matrix_close(bob_reduced_after, maximally_mixed_qubit()),
        "swap_can_create_entanglement": link_werner_ac["is_entangled"] and link_werner_cb["is_entangled"],
    }


def print_direct_results(result, noisy, noise_model):
    """Print the direct EPR scenario in the same human-friendly style as the other demos."""
    closest_werner = result["closest_werner"]
    sender_name = result["sender_name"]
    receiver_name = result["receiver_name"]
    print(f"=== Direct EPR Distribution: {sender_name} -> {receiver_name} ===")
    print("Ideal EPR-pair density matrix associated with the custom createEPR()/recvEPR():")
    print(format_matrix(result["full_before_send"]))
    print()
    if noisy and noise_model == "lindblad":
        print("Full 2-qubit density matrix after Lindblad evolution:")
    elif noisy:
        print("Full 2-qubit density matrix after time-based Werner decay:")
    else:
        print("Full 2-qubit density matrix after the ideal direct distribution:")
    print(format_matrix(result["full_after_distribution"]))
    print()
    print(f"{sender_name} reduced density matrix:")
    print(format_matrix(result["sender_reduced"]))
    print()
    print(f"{receiver_name} reduced density matrix:")
    print(format_matrix(result["receiver_reduced"]))
    print()
    print("Timeline (since the direct experiment started):")
    print(format_timeline(result["timeline_ns"]))
    print()
    print("Checks:")
    print(f"  Configured storage wait: {result['configured_wait']:.6f} s")
    print(f"  Measured createEPR() local call time: {ns_with_seconds(result['runtime_create_call_ns'])}")
    print(f"  Measured createEPR()->recvEPR() runtime: {result['runtime_create_to_recv']:.9f} s")
    if noisy and noise_model == "lindblad":
        print(f"  Effective Lindblad evolution time: {result['effective_wait']:.9f} s")
        print(f"  Bell-centered Werner fit after distribution: {closest_werner['label']}")
        print(f"  Werner-fit parameter after distribution: {closest_werner['parameter']:.6f}")
        print(f"  Werner-fit entangled (w > 1/3): {closest_werner['is_entangled']}")
        print(f"  Werner-fit residual after distribution: {closest_werner['residual']:.6e}")
    else:
        print(f"  Effective wait used for Werner decay: {result['effective_wait']:.9f} s")
        print(f"  Closest Bell-centered Werner state after distribution: {closest_werner['label']}")
        print(f"  Werner parameter after distribution: {closest_werner['parameter']:.6f}")
        print(f"  Werner state still entangled (w > 1/3): {closest_werner['is_entangled']}")
        print(f"  Werner fit residual after distribution: {closest_werner['residual']:.6e}")
    print(f"  full distributed state equals |Phi+><Phi+|: {result['matches_bell']}")
    print(f"  {sender_name} reduced density equals I/2: {result['sender_local_ok']}")
    print(f"  {receiver_name} reduced density equals I/2: {result['receiver_local_ok']}")
    if noisy:
        if noise_model == "lindblad":
            print(
                "  Note: with Lindblad noise enabled, the density matrix comes from mesolve() "
                "and the Werner value is only a Bell-centered fit."
            )
        else:
            print("  Note: with noise enabled, the displayed degradation depends only on the measured time and w(t).")
    print()


def print_swap_results(result, noisy, noise_model):
    """Print the swapping scenario in the same human-friendly style as the other demos."""
    before_swap_werner = result["before_swap_werner"]
    link_werner_ac = result["link_werner_ac"]
    link_werner_cb = result["link_werner_cb"]
    raw_werner = result["raw_werner"]
    corrected_werner = result["corrected_werner"]

    print("=== Entanglement Swapping: Alice - Charlie - Bob ===")
    print(f"Configured storage wait for Alice-Charlie: {result['configured_wait_ac']:.6f} s")
    print(f"Configured storage wait for Charlie-Bob: {result['configured_wait_cb']:.6f} s")
    print(f"Measured Alice-Charlie createEPR() local call: {ns_with_seconds(result['runtime_create_call_ac_ns'])}")
    print(f"Measured Charlie-Bob createEPR() local call: {ns_with_seconds(result['runtime_create_call_cb_ns'])}")
    print(f"Measured createEPR()->recvEPR() runtime for Alice-Charlie: {result['runtime_create_to_recv_ac']:.9f} s")
    print(f"Measured createEPR()->recvEPR() runtime for Charlie-Bob: {result['runtime_create_to_recv_cb']:.9f} s")
    print(f"Measured EPR age until swap for Alice-Charlie: {result['age_to_swap_ac']:.9f} s")
    print(f"Measured EPR age until swap for Charlie-Bob: {result['age_to_swap_cb']:.9f} s")
    print(f"Effective wait at swap for Alice-Charlie: {result['effective_wait_ac']:.9f} s")
    print(f"Effective wait at swap for Charlie-Bob: {result['effective_wait_cb']:.9f} s")
    if noisy and noise_model == "lindblad":
        print(f"Alice-Charlie Werner-fit parameter at swap: {link_werner_ac['parameter']:.6f}")
        print(f"Charlie-Bob Werner-fit parameter at swap: {link_werner_cb['parameter']:.6f}")
        print(f"Werner-fit comparison w1*w2: {result['predicted_swap_parameter']:.6f}")
    else:
        print(f"Alice-Charlie Werner parameter at swap: {link_werner_ac['parameter']:.6f}")
        print(f"Charlie-Bob Werner parameter at swap: {link_werner_cb['parameter']:.6f}")
        print(f"Predicted swapped Werner parameter w1*w2: {result['predicted_swap_parameter']:.6f}")
    print(f"Both input links still entangled at swap time: {result['swap_can_create_entanglement']}")
    print()
    print("Timeline (since the swapping experiment started):")
    print(format_timeline(result["timeline_ns"]))
    print()
    print("Alice-Bob reduced density matrix before Charlie's Bell measurement:")
    print(format_matrix(result["ab_before_swap"]))
    print()
    print(
        "Closest Bell-centered Werner state before swap: "
        f"{before_swap_werner['label']} with w={before_swap_werner['parameter']:.6f}"
    )
    print(f"Werner state before swap still entangled (w > 1/3): {before_swap_werner['is_entangled']}")
    print(f"Werner fit residual before swap: {before_swap_werner['residual']:.6e}")
    print()
    print(
        "Charlie Bell-measurement bits: "
        f"m1={result['bell_measurement'][0]} m2={result['bell_measurement'][1]}"
    )
    print()
    print("Alice-Bob density matrix right after the swap and before Pauli correction:")
    print(format_matrix(result["raw_ab_after_swap"]))
    print()
    print(
        "Closest Bell-centered Werner state before correction: "
        f"{raw_werner['label']} with w={raw_werner['parameter']:.6f}"
    )
    print(f"Werner state before correction still entangled (w > 1/3): {raw_werner['is_entangled']}")
    print(f"Werner fit residual before correction: {raw_werner['residual']:.6e}")
    print()
    print(f"Applied Pauli correction on Bob: {result['applied_correction']}")
    print()
    print("Alice-Bob density matrix after Pauli correction on Bob:")
    print(format_matrix(result["corrected_ab"]))
    print()
    print(
        "Closest Bell-centered Werner state after correction: "
        f"{corrected_werner['label']} with w={corrected_werner['parameter']:.6f}"
    )
    print(f"Werner state after correction still entangled (w > 1/3): {corrected_werner['is_entangled']}")
    print(f"Werner fit residual after correction: {corrected_werner['residual']:.6e}")
    print()
    print("Alice reduced density matrix after swapping:")
    print(format_matrix(result["alice_reduced_after"]))
    print()
    print("Bob reduced density matrix after swapping:")
    print(format_matrix(result["bob_reduced_after"]))
    print()
    print("Checks:")
    if noisy and noise_model == "lindblad":
        print(f"  Alice-Bob state before swap equals rho_A ⊗ rho_B: {result['ab_before_swap_is_product']}")
    else:
        print(f"  Alice-Bob state before swap equals I/4: {result['ab_before_swap_is_mixed']}")
    print(f"  corrected Alice-Bob state equals |Phi+><Phi+|: {result['corrected_matches_bell']}")
    print(f"  Alice reduced density equals I/2: {result['alice_local_ok']}")
    print(f"  Bob reduced density equals I/2: {result['bob_local_ok']}")
    if noisy:
        if noise_model == "lindblad":
            print(
                "  Note: with Lindblad noise enabled, the shown matrices come from the full 4-qubit evolution; "
                "w1, w2, and w1*w2 are Werner-fit reference values."
            )
        else:
            print("  Note: in this model the swap quality is set by w_swap = w1*w2 at the instant of swapping.")
    print()


def main():
    """Run the standalone minimal qutip-only network demo."""
    args = parse_args()
    normalized = normalize_network_description(str(Path(args.config).resolve()), args.network_name)
    swap_wait_ac = args.swap_wait_ac if args.swap_wait_ac is not None else args.noise_wait
    swap_wait_cb = args.swap_wait_cb if args.swap_wait_cb is not None else args.noise_wait
    gamma_relax, gamma_dephase = resolved_lindblad_rates(
        args.noise_model,
        args.t1,
        args.gamma_relax,
        args.gamma_dephase,
    )

    required_nodes = {"Alice", "Bob", "Charlie"}
    present_nodes = {node["name"] for node in normalized["nodes"]}
    missing = required_nodes - present_nodes
    if missing:
        raise ValueError(
            "This minimal standalone demo expects nodes Alice, Bob, and Charlie. "
            f"Missing: {', '.join(sorted(missing))}"
        )

    print_versions()
    print(f"Config: {Path(args.config).resolve()}")
    print(f"Network name: {normalized['network_name']}")
    print(f"Scenario: {args.scenario}")
    print(f"Noise enabled: {args.noise}")
    print(f"Noise model: {args.noise_model}")
    print(f"Noise wait: {args.noise_wait}")
    print(f"Swap wait Alice-Charlie: {swap_wait_ac}")
    print(f"Swap wait Charlie-Bob: {swap_wait_cb}")
    print(f"T1: {args.t1}")
    if args.noise_model == "lindblad":
        print(f"Gamma relax: {gamma_relax}")
        print(f"Gamma dephase: {gamma_dephase}")
    print(f"Seed: {args.seed}")
    print()

    network = MinimalQutipNetwork(normalized, seed=args.seed)
    results = {}

    if args.scenario in {"direct", "all"}:
        results["direct"] = run_direct_distribution(
            network,
            noisy=args.noise,
            noise_wait=args.noise_wait,
            t1=args.t1,
            noise_model=args.noise_model,
            gamma_relax=gamma_relax,
            gamma_dephase=gamma_dephase,
        )
    if args.scenario in {"swap", "all"}:
        results["swap"] = run_entanglement_swapping(
            network,
            noisy=args.noise,
            link_wait_ac=swap_wait_ac,
            link_wait_cb=swap_wait_cb,
            t1=args.t1,
            noise_model=args.noise_model,
            gamma_relax=gamma_relax,
            gamma_dephase=gamma_dephase,
        )

    if "direct" in results:
        print_direct_results(results["direct"], noisy=args.noise, noise_model=args.noise_model)
    if "swap" in results:
        print_swap_results(results["swap"], noisy=args.noise, noise_model=args.noise_model)

    print("This demo does not start SimulaQron services and does not call its runtime APIs.")
    print("It uses a custom in-process createEPR()/recvEPR() model with direct density-matrix access.")
    print("Alice and Bob are endpoints, and Charlie is the repeater used for swapping.")
    if args.noise:
        if args.noise_model == "lindblad":
            print("The displayed degradation is driven by local Lindblad evolution selected from the CLI arguments.")
        else:
            print("The displayed degradation is driven only by the measured time and the Bell-centered Werner model.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
