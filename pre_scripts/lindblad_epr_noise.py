#!/usr/bin/env python3
"""
Small Lindblad helpers for 2-qubit EPR states.

This file is intentionally separate from the main demos so you can inspect the
noise model in isolation before deciding where to plug it in.
"""

import numpy as np
import qutip as qt


def bell_state_density(label="phi_plus"):
    """Return the density matrix of one Bell state as a QuTiP Qobj."""
    scale = 1 / np.sqrt(2)

    if label == "phi_plus":
        vector = np.array([scale, 0, 0, scale], dtype=complex)
    elif label == "phi_minus":
        vector = np.array([scale, 0, 0, -scale], dtype=complex)
    elif label == "psi_plus":
        vector = np.array([0, scale, scale, 0], dtype=complex)
    elif label == "psi_minus":
        vector = np.array([0, scale, -scale, 0], dtype=complex)
    else:
        raise ValueError(f"Unknown Bell state label: {label}")

    return qt.Qobj(
        np.outer(vector, np.conjugate(vector)),
        dims=[[2, 2], [2, 2]],
    )


def build_local_lindblad_operators(
    gamma_relax_a=0.0,
    gamma_relax_b=0.0,
    gamma_dephase_a=0.0,
    gamma_dephase_b=0.0,
):
    """Build common single-qubit jump operators acting locally on a 2-qubit EPR pair.

    The returned list can be passed directly to `apply_lindblad_epr_noise`.

    Parameters:
    - gamma_relax_a / gamma_relax_b:
      Amplitude-damping strengths for qubit A and qubit B.
      They model energy relaxation, typically with jump operator sigma_-.
    - gamma_dephase_a / gamma_dephase_b:
      Pure dephasing strengths for qubit A and qubit B.
      They model phase randomization, typically with jump operator sigma_z.
    """
    operators = []

    sm = qt.sigmam()
    sz = qt.sigmaz()
    ident = qt.qeye(2)

    if gamma_relax_a > 0:
        operators.append(np.sqrt(gamma_relax_a) * qt.tensor(sm, ident))
    if gamma_relax_b > 0:
        operators.append(np.sqrt(gamma_relax_b) * qt.tensor(ident, sm))
    if gamma_dephase_a > 0:
        operators.append(np.sqrt(gamma_dephase_a) * qt.tensor(sz, ident))
    if gamma_dephase_b > 0:
        operators.append(np.sqrt(gamma_dephase_b) * qt.tensor(ident, sz))

    return operators


def apply_lindblad_epr_noise(
    rho0,
    duration,
    collapse_operators,
    hamiltonian=None,
    steps=2,
):
    """Evolve one 2-qubit EPR density matrix with a Lindblad master equation.

    Parameters:
    - rho0:
      Initial 2-qubit density matrix. It can be either a QuTiP Qobj or a
      4x4 matrix-like object.
    - duration:
      Physical evolution time.
    - collapse_operators:
      List of Lindblad jump operators `L_i`.
      Example: local amplitude damping or dephasing operators.
    - hamiltonian:
      Optional system Hamiltonian. If omitted, only the dissipative part acts.
    - steps:
      Number of time points used by QuTiP. For "just give me rho(t)" two or
      three points are enough.

    Returns:
    - rho_t:
      Final density matrix as a QuTiP Qobj.
    """
    if duration < 0:
        raise ValueError("duration must be non-negative")

    if not isinstance(rho0, qt.Qobj):
        rho0 = qt.Qobj(np.array(rho0, dtype=complex), dims=[[2, 2], [2, 2]])

    if hamiltonian is None:
        hamiltonian = 0 * qt.tensor(qt.qeye(2), qt.qeye(2))

    if duration == 0:
        return rho0

    if steps < 2:
        raise ValueError("steps must be at least 2")

    times = np.linspace(0.0, float(duration), int(steps))
    result = qt.mesolve(hamiltonian, rho0, times, c_ops=collapse_operators, e_ops=[])
    return result.states[-1]


def example_two_qubit_epr_with_lindblad(
    label="phi_plus",
    duration=0.1,
    gamma_relax_a=0.0,
    gamma_relax_b=0.0,
    gamma_dephase_a=0.0,
    gamma_dephase_b=0.0,
):
    """Convenience wrapper for the common 'apply local Lindblad noise to an EPR' case."""
    rho0 = bell_state_density(label=label)
    collapse_operators = build_local_lindblad_operators(
        gamma_relax_a=gamma_relax_a,
        gamma_relax_b=gamma_relax_b,
        gamma_dephase_a=gamma_dephase_a,
        gamma_dephase_b=gamma_dephase_b,
    )
    return apply_lindblad_epr_noise(
        rho0=rho0,
        duration=duration,
        collapse_operators=collapse_operators,
    )


if __name__ == "__main__":
    rho = example_two_qubit_epr_with_lindblad(
        label="phi_plus",
        duration=0.1,
        gamma_relax_a=0.05,
        gamma_relax_b=0.05,
        gamma_dephase_a=0.02,
        gamma_dephase_b=0.02,
    )
    print(rho.full())
