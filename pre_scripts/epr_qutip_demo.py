#!/usr/bin/env python3
import argparse
import sys
import threading
import time
from importlib import metadata
from pathlib import Path

import numpy as np
import qutip as qt
from cqc.pythonLib import CQCConnection
from twisted.internet import error, reactor
from twisted.internet.defer import DeferredList, inlineCallbacks
from twisted.spread import pb

from simulaqron.general.hostConfig import socketsConfig
from simulaqron.network import Network
from simulaqron.settings import simulaqron_settings


EXPECTED_VERSIONS = {
    "simulaqron": "3.0.16",
    "qutip": "4.7.5",
    "scipy": "1.12.0",
}

DEFAULT_NETWORK_NAME = "madrid_demo"


def package_versions():
    """Return the installed versions of the packages we rely on in this demo."""
    versions = {}
    for package in EXPECTED_VERSIONS:
        versions[package] = metadata.version(package)
    return versions


def assemble_matrix(real_part, imag_part):
    """Rebuild a complex matrix from the real and imaginary parts returned by SimulaQron."""
    matrix = []
    for row_index, row in enumerate(real_part):
        matrix_row = []
        for col_index, value in enumerate(row):
            matrix_row.append(value + 1j * imag_part[row_index][col_index])
        matrix.append(matrix_row)
    return matrix


def matrix_to_array(matrix):
    """Convert a Python nested list into a NumPy complex array for linear-algebra utilities."""
    return np.array(matrix, dtype=complex)


def format_complex(value):
    """Format one complex number so the printed matrices are easy to read in the terminal."""
    return f"{value.real: .6f}{value.imag:+.6f}j"


def format_matrix(matrix):
    """Turn a matrix into a multi-line string that is readable for humans."""
    rows = []
    for row in matrix:
        rows.append("[" + ", ".join(format_complex(value) for value in row) + "]")
    return "\n".join(rows)


def mark_time_ns(start_ns):
    """Return the elapsed monotonic time in nanoseconds since one experiment started."""
    return time.perf_counter_ns() - start_ns


def format_timeline(timeline):
    """Render a small event timeline so non-programmers can see when each step happened."""
    lines = []
    for key, value in timeline.items():
        label = key.replace("_", " ")
        lines.append(f"  {label}: t={value} ns")
    return "\n".join(lines)


def record_event(timeline, start_ns, key):
    """Store one timestamp in the timeline and pass through the callback result unchanged."""
    def _recorder(value):
        timeline[key] = mark_time_ns(start_ns)
        return value

    return _recorder


def bell_state_vector(label="phi_plus"):
    """Return the state vector of the Bell state selected by name."""
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


def density_from_statevector(vector):
    """Build a density matrix from a pure-state vector."""
    return np.outer(vector, np.conjugate(vector)).tolist()


BELL_MATRICES = {
    "phi_plus": density_from_statevector(bell_state_vector("phi_plus")),
    "phi_minus": density_from_statevector(bell_state_vector("phi_minus")),
    "psi_plus": density_from_statevector(bell_state_vector("psi_plus")),
    "psi_minus": density_from_statevector(bell_state_vector("psi_minus")),
}


def maximally_mixed_qubit():
    """Return the 1-qubit maximally mixed state I/2."""
    return [
        [0.5 + 0j, 0j],
        [0j, 0.5 + 0j],
    ]


def maximally_mixed_two_qubits():
    """Return the 2-qubit maximally mixed state I/4."""
    return [
        [0.25 + 0j, 0j, 0j, 0j],
        [0j, 0.25 + 0j, 0j, 0j],
        [0j, 0j, 0.25 + 0j, 0j],
        [0j, 0j, 0j, 0.25 + 0j],
    ]


def matrix_close(actual, expected, atol=1e-6):
    """Check whether two matrices are numerically equal within a small tolerance."""
    actual_array = np.array(actual, dtype=complex)
    expected_array = np.array(expected, dtype=complex)
    return bool(np.allclose(actual_array, expected_array, atol=atol))


def partial_trace(matrix, keep, total_qubits):
    """Trace out all qubits except the indices in `keep` and return the reduced density matrix."""
    rho = qt.Qobj(matrix_to_array(matrix), dims=[[2] * total_qubits, [2] * total_qubits])
    return rho.ptrace(keep).full().tolist()


def bell_fidelity(matrix, label="phi_plus"):
    """Measure how much the given density matrix overlaps with one chosen Bell state."""
    rho = matrix_to_array(matrix)
    bell = bell_state_vector(label)
    return float(np.real(np.conjugate(bell) @ rho @ bell))


def identify_bell_state(matrix):
    """Find the Bell state that best matches the given 2-qubit density matrix."""
    scores = []
    for label in BELL_MATRICES:
        scores.append((label, bell_fidelity(matrix, label=label)))
    scores.sort(key=lambda item: item[1], reverse=True)
    return scores[0]


def werner_parameter_from_fidelity(fidelity):
    """Convert Bell-state fidelity into the Werner parameter and clamp it to the physical range."""
    parameter = (4.0 * fidelity - 1.0) / 3.0
    return max(0.0, min(1.0, parameter))


def werner_parameter_from_time(wait_seconds, t1, enabled):
    """Map elapsed storage time to a deterministic Werner parameter using exponential decay."""
    if not enabled:
        return 1.0
    if wait_seconds <= 0:
        return 1.0
    if t1 <= 0:
        raise ValueError("t1 must be positive when the deterministic Werner-time model is enabled")
    return float(np.exp(-wait_seconds / t1))


def werner_is_entangled(parameter, atol=1e-9):
    """Mark whether a Werner state is still entangled using the w > 1/3 threshold."""
    return parameter > (1.0 / 3.0 + atol)


def werner_state(label, parameter):
    """Construct the Bell-centered Werner state rho_W = w|B><B| + (1-w)I/4."""
    bell = matrix_to_array(BELL_MATRICES[label])
    mixed = np.eye(4, dtype=complex) / 4.0
    return (parameter * bell + (1.0 - parameter) * mixed).tolist()


def werner_fit(matrix, label=None):
    """Fit the closest Bell-centered Werner state and report its quality indicators."""
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


def bob_pauli_correction_from_label(raw_label):
    """Return the Pauli correction name that maps one Bell label back to |Phi+>."""
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
    """Map Charlie's Bell-measurement bits to the Bell label shared by the outer endpoints."""
    mapping = {
        (0, 0): "phi_plus",
        (0, 1): "psi_plus",
        (1, 0): "phi_minus",
        (1, 1): "psi_minus",
    }
    return mapping[(measurement_first, measurement_second)]


def recv_epr_worker(node_name, network_name, app_id, result, received_event, release_event):
    """Receive one EPR half on a background node thread and keep it alive until released."""
    try:
        with CQCConnection(node_name, appID=app_id, network_name=network_name) as connection:
            result["connection_opened_abs_ns"] = time.perf_counter_ns()
            qubit = connection.recvEPR()
            result["recvEPR_completed_abs_ns"] = time.perf_counter_ns()
            result["remote_entangled_node"] = qubit.remote_entangled_node
            received_event.set()
            release_event.wait()
    except Exception as exc:
        result["error"] = exc
        received_event.set()


@inlineCallbacks
def connect_root(name, config_file, network_name):
    """Open a PB connection to one vnode and return its remote root object."""
    vnode_net = socketsConfig(config_file, network_name=network_name, config_type="vnode")
    vnode = vnode_net.hostDict[name]
    factory = pb.PBClientFactory()
    reactor.connectTCP(vnode.hostname, vnode.port, factory)
    root = yield factory.getRootObject()
    return root


@inlineCallbacks
def cleanup_qubits(qubits):
    """Measure leftover qubits so each experiment frees its temporary quantum state cleanly."""
    for qubit in qubits:
        try:
            yield qubit.callRemote("measure")
        except Exception:
            pass


@inlineCallbacks
def get_register_indices(qubit_map):
    """Read the register index of each live qubit so we can trace out the correct subsystems."""
    indices = {}
    for name, qubit in qubit_map.items():
        indices[name] = yield qubit.callRemote("get_number")
    return indices


def reduced_pair_states(matrix):
    """Return the local reduced states of a 2-qubit density matrix."""
    return (
        partial_trace(matrix, [0], total_qubits=2),
        partial_trace(matrix, [1], total_qubits=2),
    )


def run_direct_distribution(sender_name, receiver_name, network_name, noisy, noise_wait, t1):
    """Create one EPR pair using SimulaQron's `createEPR/recvEPR` API and inspect it analytically."""
    start_ns = time.perf_counter_ns()
    timeline = {"experiment_started": 0}
    receiver_result = {}
    received_event = threading.Event()
    release_event = threading.Event()

    receiver_thread = threading.Thread(
        target=recv_epr_worker,
        args=(receiver_name, network_name, 0, receiver_result, received_event, release_event),
        daemon=True,
    )
    receiver_thread.start()

    try:
        with CQCConnection(sender_name, appID=0, network_name=network_name) as sender_connection:
            sender_connection.createEPR(receiver_name, remote_appID=0)
            create_completed_abs_ns = time.perf_counter_ns()
            timeline["createEPR_completed"] = create_completed_abs_ns - start_ns

            if not received_event.wait(timeout=10.0):
                raise TimeoutError(f"{receiver_name} did not finish recvEPR() in time")
            if "error" in receiver_result:
                raise receiver_result["error"]

            recv_completed_abs_ns = receiver_result["recvEPR_completed_abs_ns"]
            timeline["recvEPR_completed"] = recv_completed_abs_ns - start_ns

            runtime_create_to_recv = max(0.0, (recv_completed_abs_ns - create_completed_abs_ns) / 1e9)
            effective_wait = noise_wait + runtime_create_to_recv
            link_parameter = werner_parameter_from_time(effective_wait, t1, noisy)
            full_after_distribution = werner_state("phi_plus", link_parameter)
            sender_reduced, receiver_reduced = reduced_pair_states(full_after_distribution)
            closest_werner = werner_fit(full_after_distribution, label="phi_plus")
            timeline["werner_decay_applied"] = mark_time_ns(start_ns)

            return {
                "sender_name": sender_name,
                "receiver_name": receiver_name,
                "configured_wait": noise_wait,
                "runtime_create_to_recv": runtime_create_to_recv,
                "effective_wait": effective_wait,
                "timeline_ns": timeline,
                "full_before_send": BELL_MATRICES["phi_plus"],
                "ideal_after_distribution": BELL_MATRICES["phi_plus"],
                "full_after_distribution": full_after_distribution,
                "sender_reduced": sender_reduced,
                "receiver_reduced": receiver_reduced,
                "closest_werner": closest_werner,
                "matches_bell": matrix_close(full_after_distribution, BELL_MATRICES["phi_plus"]),
                "sender_local_ok": matrix_close(sender_reduced, maximally_mixed_qubit()),
                "receiver_local_ok": matrix_close(receiver_reduced, maximally_mixed_qubit()),
            }
    finally:
        release_event.set()
        receiver_thread.join(timeout=2.0)


def run_entanglement_swapping(network_name, noisy, link_wait_ac, link_wait_cb, t1):
    """Create two EPR links with `createEPR/recvEPR`, then swap them at Charlie."""
    start_ns = time.perf_counter_ns()
    timeline = {"experiment_started": 0}

    alice_result = {}
    bob_result = {}
    alice_received_event = threading.Event()
    bob_received_event = threading.Event()
    release_endpoints = threading.Event()

    alice_thread = threading.Thread(
        target=recv_epr_worker,
        args=("Alice", network_name, 0, alice_result, alice_received_event, release_endpoints),
        daemon=True,
    )
    bob_thread = threading.Thread(
        target=recv_epr_worker,
        args=("Bob", network_name, 0, bob_result, bob_received_event, release_endpoints),
        daemon=True,
    )
    alice_thread.start()
    bob_thread.start()

    try:
        with CQCConnection("Charlie", appID=0, network_name=network_name) as charlie_connection:
            q_charlie_left = charlie_connection.createEPR("Alice", remote_appID=0)
            ac_create_completed_abs_ns = time.perf_counter_ns()
            timeline["alice_charlie_createEPR_completed"] = ac_create_completed_abs_ns - start_ns

            if not alice_received_event.wait(timeout=10.0):
                raise TimeoutError("Alice did not finish recvEPR() in time")
            if "error" in alice_result:
                raise alice_result["error"]
            alice_recv_abs_ns = alice_result["recvEPR_completed_abs_ns"]
            timeline["alice_recvEPR_completed"] = alice_recv_abs_ns - start_ns

            q_charlie_right = charlie_connection.createEPR("Bob", remote_appID=0)
            cb_create_completed_abs_ns = time.perf_counter_ns()
            timeline["charlie_bob_createEPR_completed"] = cb_create_completed_abs_ns - start_ns

            if not bob_received_event.wait(timeout=10.0):
                raise TimeoutError("Bob did not finish recvEPR() in time")
            if "error" in bob_result:
                raise bob_result["error"]
            bob_recv_abs_ns = bob_result["recvEPR_completed_abs_ns"]
            timeline["bob_recvEPR_completed"] = bob_recv_abs_ns - start_ns

            ab_before_swap = maximally_mixed_two_qubits()
            before_swap_werner = werner_fit(ab_before_swap, label="phi_plus")

            q_charlie_left.cnot(q_charlie_right)
            q_charlie_left.H()
            bell_measurement_first = q_charlie_left.measure()
            bell_measurement_second = q_charlie_right.measure()
            swap_measurement_abs_ns = time.perf_counter_ns()
            timeline["swap_bell_measurement_completed"] = swap_measurement_abs_ns - start_ns

            raw_label = bell_label_from_swap_bits(bell_measurement_first, bell_measurement_second)
            age_ac_to_swap = max(0.0, (swap_measurement_abs_ns - ac_create_completed_abs_ns) / 1e9)
            age_cb_to_swap = max(0.0, (swap_measurement_abs_ns - cb_create_completed_abs_ns) / 1e9)
            effective_wait_ac = link_wait_ac + age_ac_to_swap
            effective_wait_cb = link_wait_cb + age_cb_to_swap
            w_ac_at_swap = werner_parameter_from_time(effective_wait_ac, t1, noisy)
            w_cb_at_swap = werner_parameter_from_time(effective_wait_cb, t1, noisy)
            w_swap_at_swap = w_ac_at_swap * w_cb_at_swap

            raw_ab_after_swap = werner_state(raw_label, w_swap_at_swap)
            raw_werner = werner_fit(raw_ab_after_swap, label=raw_label)
            timeline["werner_decay_applied"] = mark_time_ns(start_ns)

            applied_correction = bob_pauli_correction_from_label(raw_label)
            timeline["pauli_correction_applied"] = mark_time_ns(start_ns)

            corrected_ab = werner_state("phi_plus", w_swap_at_swap)
            corrected_werner = werner_fit(corrected_ab, label="phi_plus")
            alice_reduced_after, bob_reduced_after = reduced_pair_states(corrected_ab)

            return {
                "timeline_ns": timeline,
                "configured_wait_ac": link_wait_ac,
                "configured_wait_cb": link_wait_cb,
                "runtime_create_to_recv_ac": max(0.0, (alice_recv_abs_ns - ac_create_completed_abs_ns) / 1e9),
                "runtime_create_to_recv_cb": max(0.0, (bob_recv_abs_ns - cb_create_completed_abs_ns) / 1e9),
                "age_to_swap_ac": age_ac_to_swap,
                "age_to_swap_cb": age_cb_to_swap,
                "effective_wait_ac": effective_wait_ac,
                "effective_wait_cb": effective_wait_cb,
                "link_werner_ac": werner_fit(werner_state("phi_plus", w_ac_at_swap), label="phi_plus"),
                "link_werner_cb": werner_fit(werner_state("phi_plus", w_cb_at_swap), label="phi_plus"),
                "predicted_swap_parameter": w_swap_at_swap,
                "ab_before_swap": ab_before_swap,
                "before_swap_werner": before_swap_werner,
                "bell_measurement": (bell_measurement_first, bell_measurement_second),
                "ideal_raw_ab_after_swap": BELL_MATRICES[raw_label],
                "raw_ab_after_swap": raw_ab_after_swap,
                "raw_werner": raw_werner,
                "applied_correction": applied_correction,
                "corrected_ab": corrected_ab,
                "corrected_werner": corrected_werner,
                "alice_reduced_after": alice_reduced_after,
                "bob_reduced_after": bob_reduced_after,
                "ab_before_swap_is_mixed": matrix_close(ab_before_swap, maximally_mixed_two_qubits()),
                "corrected_matches_bell": matrix_close(corrected_ab, BELL_MATRICES["phi_plus"]),
                "alice_local_ok": matrix_close(alice_reduced_after, maximally_mixed_qubit()),
                "bob_local_ok": matrix_close(bob_reduced_after, maximally_mixed_qubit()),
                "swap_can_create_entanglement": werner_is_entangled(w_ac_at_swap) and werner_is_entangled(w_cb_at_swap),
            }
    finally:
        release_endpoints.set()
        alice_thread.join(timeout=2.0)
        bob_thread.join(timeout=2.0)


def orchestrate_demo(network_name, scenario, noisy, noise_wait, t1, swap_wait_ac, swap_wait_cb):
    """Run the selected CQC-based experiments on the already started 3-node network."""
    results = {}
    if scenario in {"direct", "all"}:
        results["direct"] = run_direct_distribution(
            sender_name="Alice",
            receiver_name="Charlie",
            network_name=network_name,
            noisy=noisy,
            noise_wait=noise_wait,
            t1=t1,
        )
    if scenario in {"swap", "all"}:
        results["swap"] = run_entanglement_swapping(
            network_name=network_name,
            noisy=noisy,
            link_wait_ac=swap_wait_ac,
            link_wait_cb=swap_wait_cb,
            t1=t1,
        )
    return results


def print_versions():
    """Print the package versions so the user can confirm the tested stack."""
    versions = package_versions()
    print("Detected versions:")
    for package, version in versions.items():
        expected = EXPECTED_VERSIONS[package]
        suffix = " (expected)" if version == expected else f" (expected {expected})"
        print(f"  {package}=={version}{suffix}")


def parse_args():
    """Read command-line arguments for the demo entry point."""
    repo_root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(
        description=(
            "Minimal 3-node SimulaQron/qutip demo that shows direct EPR distribution on the Alice-Charlie edge "
            "and entanglement swapping across Alice-Charlie-Bob while exposing density matrices."
        )
    )
    parser.add_argument(
        "--config",
        default=str(repo_root / "config" / "madrid_3nodes.json"),
        help="Path to a SimulaQron network JSON file.",
    )
    parser.add_argument(
        "--network-name",
        default=DEFAULT_NETWORK_NAME,
        help="Network name inside the SimulaQron JSON file.",
    )
    parser.add_argument(
        "--scenario",
        choices=["direct", "swap", "all"],
        default="all",
        help="Which demo to run.",
    )
    parser.add_argument(
        "--noise",
        action="store_true",
        help=(
            "Enable the deterministic Werner-time decay model. When enabled, the displayed two-qubit states are "
            "degraded according to storage time instead of using SimulaQron's random Pauli-noise trigger."
        ),
    )
    parser.add_argument(
        "--noise-wait",
        type=float,
        default=0.0,
        help="Storage time in seconds used for the direct-link Werner decay model.",
    )
    parser.add_argument(
        "--t1",
        type=float,
        default=1.0,
        help="Time constant of the deterministic Werner decay model when --noise is enabled.",
    )
    parser.add_argument(
        "--swap-wait-ac",
        type=float,
        default=None,
        help="Storage time for the Alice-Charlie link at the moment of swapping. Defaults to --noise-wait.",
    )
    parser.add_argument(
        "--swap-wait-cb",
        type=float,
        default=None,
        help="Storage time for the Charlie-Bob link at the moment of swapping. Defaults to --noise-wait.",
    )
    return parser.parse_args()


def print_direct_results(result, noisy):
    """Print the direct-link experiment in a way that is readable without inspecting the code."""
    closest_werner = result["closest_werner"]
    sender_name = result["sender_name"]
    receiver_name = result["receiver_name"]
    print(f"=== Direct EPR Distribution: {sender_name} -> {receiver_name} ===")
    print("Ideal EPR-pair density matrix associated with createEPR()/recvEPR():")
    print(format_matrix(result["full_before_send"]))
    print()
    print("Full 2-qubit density matrix after time-based Werner decay:")
    print(format_matrix(result["full_after_distribution"]))
    print()
    print(f"{sender_name} reduced density matrix:")
    print(format_matrix(result["sender_reduced"]))
    print()
    print(f"{receiver_name} reduced density matrix:")
    print(format_matrix(result["receiver_reduced"]))
    print()
    print("Timeline (ns since the direct experiment started):")
    print(format_timeline(result["timeline_ns"]))
    print()
    print("Checks:")
    print(f"  Configured storage wait: {result['configured_wait']:.6f} s")
    print(f"  Measured createEPR()->recvEPR() runtime: {result['runtime_create_to_recv']:.6f} s")
    print(f"  Effective wait used for Werner decay: {result['effective_wait']:.6f} s")
    print(
        "  Closest Bell-centered Werner state after distribution: "
        f"{closest_werner['label']}"
    )
    print(f"  Werner parameter after distribution: {closest_werner['parameter']:.6f}")
    print(f"  Werner state still entangled (w > 1/3): {closest_werner['is_entangled']}")
    print(f"  Werner fit residual after distribution: {closest_werner['residual']:.6e}")
    print(f"  full distributed state equals |Phi+><Phi+|: {result['matches_bell']}")
    print(f"  {sender_name} reduced density equals I/2: {result['sender_local_ok']}")
    print(f"  {receiver_name} reduced density equals I/2: {result['receiver_local_ok']}")
    if noisy:
        print("  Note: with noise enabled, the displayed degradation is deterministic and depends only on time through w(t).")
    print()


def print_swap_results(result, noisy):
    """Print the swapping experiment, including the Bell/Werner interpretation before and after correction."""
    before_swap_werner = result["before_swap_werner"]
    link_werner_ac = result["link_werner_ac"]
    link_werner_cb = result["link_werner_cb"]
    raw_werner = result["raw_werner"]
    corrected_werner = result["corrected_werner"]
    print("=== Entanglement Swapping: Alice - Charlie - Bob ===")
    print(f"Configured storage wait for Alice-Charlie: {result['configured_wait_ac']:.6f} s")
    print(f"Configured storage wait for Charlie-Bob: {result['configured_wait_cb']:.6f} s")
    print(f"Measured createEPR()->recvEPR() runtime for Alice-Charlie: {result['runtime_create_to_recv_ac']:.6f} s")
    print(f"Measured createEPR()->recvEPR() runtime for Charlie-Bob: {result['runtime_create_to_recv_cb']:.6f} s")
    print(f"Measured EPR age until swap for Alice-Charlie: {result['age_to_swap_ac']:.6f} s")
    print(f"Measured EPR age until swap for Charlie-Bob: {result['age_to_swap_cb']:.6f} s")
    print(f"Effective wait at swap for Alice-Charlie: {result['effective_wait_ac']:.6f} s")
    print(f"Effective wait at swap for Charlie-Bob: {result['effective_wait_cb']:.6f} s")
    print(f"Alice-Charlie Werner parameter at swap: {link_werner_ac['parameter']:.6f}")
    print(f"Charlie-Bob Werner parameter at swap: {link_werner_cb['parameter']:.6f}")
    print(f"Predicted swapped Werner parameter w1*w2: {result['predicted_swap_parameter']:.6f}")
    print(f"Both input links still entangled at swap time: {result['swap_can_create_entanglement']}")
    print()
    print("Timeline (ns since the swapping experiment started):")
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
    print(f"  Alice-Bob state before swap equals I/4: {result['ab_before_swap_is_mixed']}")
    print(f"  corrected Alice-Bob state equals |Phi+><Phi+|: {result['corrected_matches_bell']}")
    print(f"  Alice reduced density equals I/2: {result['alice_local_ok']}")
    print(f"  Bob reduced density equals I/2: {result['bob_local_ok']}")
    if noisy:
        print("  Note: in this model the swap quality is set by w_swap = w1*w2 evaluated at the instant of swapping.")
    print()


def main():
    """Configure SimulaQron, run the requested scenario, and print the final human-readable report."""
    args = parse_args()
    config_path = str(Path(args.config).resolve())
    swap_wait_ac = args.swap_wait_ac if args.swap_wait_ac is not None else args.noise_wait
    swap_wait_cb = args.swap_wait_cb if args.swap_wait_cb is not None else args.noise_wait

    print_versions()
    print(f"Network config: {config_path}")
    print(f"Network name: {args.network_name}")
    print(f"Scenario: {args.scenario}")
    print(f"Noise enabled: {args.noise}")
    print(f"Noise wait: {args.noise_wait}")
    print(f"Swap wait Alice-Charlie: {swap_wait_ac}")
    print(f"Swap wait Charlie-Bob: {swap_wait_cb}")
    print(f"T1: {args.t1}")
    print()

    network = None

    try:
        simulaqron_settings.network_config_file = config_path
        simulaqron_settings.backend = "qutip"
        simulaqron_settings.noisy_qubits = False
        simulaqron_settings.t1 = float(args.t1)

        network = Network(name=args.network_name, network_config_file=config_path, new=False)
        network.start()

        result = orchestrate_demo(
            network_name=args.network_name,
            scenario=args.scenario,
            noisy=args.noise,
            noise_wait=args.noise_wait,
            t1=args.t1,
            swap_wait_ac=swap_wait_ac,
            swap_wait_cb=swap_wait_cb,
        )
        if "direct" in result:
            print_direct_results(result["direct"], noisy=args.noise)
        if "swap" in result:
            print_swap_results(result["swap"], noisy=args.noise)

        if args.noise and args.noise_wait <= 0:
            print("Note: the deterministic Werner model was enabled, but noise_wait was 0, so the direct-link state stays ideal.")
            print()

        print("This demo uses SimulaQron's createEPR()/recvEPR() API on top of the qutip backend.")
        print("The density matrices shown here are reconstructed analytically from the EPR protocol flow and the Werner-time model.")
        print("It keeps the network minimal: Alice and Bob are endpoints, Charlie is the repeater used for swapping.")
        if args.noise:
            print("The displayed degradation is driven only by the time-dependent Werner parameter, not by random Pauli triggers.")
        return 0
    finally:
        if network is not None:
            network.stop()


if __name__ == "__main__":
    sys.exit(main())
