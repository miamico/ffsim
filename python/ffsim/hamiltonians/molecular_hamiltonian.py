# (C) Copyright IBM 2023.
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

from __future__ import annotations

from typing import Dict, Tuple

import dataclasses
import itertools

import numpy as np
from opt_einsum import contract
from pyscf.fci.direct_nosym import absorb_h1e, contract_2e, make_hdiag
from scipy.sparse.linalg import LinearOperator

from ffsim.cistring import gen_linkstr_index
from ffsim.operators import FermionOperator, cre_a, cre_b, des_a, des_b
from ffsim.states import dim


@dataclasses.dataclass(frozen=True)
class MolecularHamiltonian:
    r"""A molecular Hamiltonian.

    A Hamiltonian of the form

    .. math::

        H = \sum_{\sigma, pq} h_{pq} a^\dagger_{\sigma, p} a_{\sigma, q}
            + \frac12 \sum_{\sigma \tau, pqrs} h_{pqrs}
            a^\dagger_{\sigma, p} a^\dagger_{\tau, r} a_{\tau, s} a_{\sigma, q}
            + \text{constant}.

    Here :math:`h_{pq}` is called the one-body tensor and :math:`h_{pqrs}` is called
    the two-body tensor.

    Attributes:
        one_body_tensor (np.ndarray): The one-body tensor.
        two_body_tensor (np.ndarray): The two-body tensor.
        constant (float): The constant.
    """

    one_body_tensor: np.ndarray
    two_body_tensor: np.ndarray
    constant: float = 0.0

    @property
    def norb(self) -> int:
        """The number of spatial orbitals."""
        return self.one_body_tensor.shape[0]

    def rotated(self, orbital_rotation: np.ndarray) -> MolecularHamiltonian:
        r"""Return the Hamiltonian in a rotated orbital basis.

        Given an orbital rotation :math:`\mathcal{U}`, returns the operator

        .. math::

            \mathcal{U} H \mathcal{U}^\dagger

        where :math:`H` is the original Hamiltonian.

        Args:
            orbital_rotation: The orbital rotation.

        Returns:
            The rotated Hamiltonian.
        """
        one_body_tensor_rotated = contract(
            "ab,Aa,Bb->AB",
            self.one_body_tensor,
            orbital_rotation,
            orbital_rotation.conj(),
            optimize="greedy",
        )
        two_body_tensor_rotated = contract(
            "abcd,Aa,Bb,Cc,Dd->ABCD",
            self.two_body_tensor,
            orbital_rotation,
            orbital_rotation.conj(),
            orbital_rotation,
            orbital_rotation.conj(),
            optimize="greedy",
        )
        return MolecularHamiltonian(
            one_body_tensor=one_body_tensor_rotated,
            two_body_tensor=two_body_tensor_rotated,
            constant=self.constant,
        )

    def _linear_operator_(self, norb: int, nelec: tuple[int, int]) -> LinearOperator:
        """Return a SciPy LinearOperator representing the object."""
        n_alpha, n_beta = nelec
        linkstr_index_a = gen_linkstr_index(range(norb), n_alpha)
        linkstr_index_b = gen_linkstr_index(range(norb), n_beta)
        link_index = (linkstr_index_a, linkstr_index_b)
        two_body = absorb_h1e(
            self.one_body_tensor, self.two_body_tensor, norb, nelec, 0.5
        )

        def matvec(vec: np.ndarray):
            result = contract_2e(
                two_body,
                vec.astype(complex, copy=False),
                norb,
                nelec,
                link_index=link_index,
            )
            if self.constant:
                result += self.constant * vec
            return result

        dim_ = dim(norb, nelec)
        return LinearOperator(
            shape=(dim_, dim_), matvec=matvec, rmatvec=matvec, dtype=complex
        )

    def _diag_(self, norb: int, nelec: tuple[int, int]) -> np.ndarray:
        """Return the diagonal entries of the Hamiltonian."""
        if np.iscomplexobj(self.two_body_tensor) or np.iscomplexobj(
            self.one_body_tensor
        ):
            raise NotImplementedError(
                "Computing diagonal of complex molecular Hamiltonian is not yet "
                "supported."
            )
        return (
            make_hdiag(self.one_body_tensor, self.two_body_tensor, norb, nelec)
            + self.constant
        )

    def _fermion_operator_(self) -> FermionOperator:
        """Return a FermionOperator representing the object."""
        op = FermionOperator({(): self.constant})
        for p, q in itertools.product(range(self.norb), repeat=2):
            coeff = self.one_body_tensor[p, q]
            op += FermionOperator(
                {
                    (cre_a(p), des_a(q)): coeff,
                    (cre_b(p), des_b(q)): coeff,
                }
            )
        for p, q, r, s in itertools.product(range(self.norb), repeat=4):
            coeff = 0.5 * self.two_body_tensor[p, q, r, s]
            op += FermionOperator(
                {
                    (cre_a(p), cre_a(r), des_a(s), des_a(q)): coeff,
                    (cre_a(p), cre_b(r), des_b(s), des_a(q)): coeff,
                    (cre_b(p), cre_a(r), des_a(s), des_b(q)): coeff,
                    (cre_b(p), cre_b(r), des_b(s), des_b(q)): coeff,
                }
            )
        return op
    

    @staticmethod
    def from_fermion_operator(op: FermionOperator) -> MolecularHamiltonian:
        """Convert a FermionOperator to a MolecularHamiltonian."""
        # extract number of spatial orbitals
        norb = 1 + max(orb for term in op for _, _, orb in term)

        # initialize constant, one‑ and two‑body tensors
        constant: float = 0.0
        one_body_tensor = np.zeros((norb, norb), dtype=complex)
        two_body_tensor = np.zeros((norb, norb, norb, norb), dtype=complex)

        # track which (p,q,r,s) we've already processed
        seen_2b = set()

        for term, coeff in op.items():
            # constant term: empty tuple
            if len(term) == 0:
                if coeff.imag:
                    raise ValueError(f"Constant term must be real. Instead, got {coeff}.")
                constant = coeff.real


            # one‑body term: a†_σ,p a_σ,q  (σ = α or β)
            elif len(term) == 2:
                (_, _, p), (_, _, q) = term
                valid_1b = [(cre_a(p), des_a(q)), (cre_b(p), des_b(q))]
                if term in valid_1b:
                    one_body_tensor[p, q] += 0.5 * coeff
                else:
                    raise ValueError(
                        "FermionOperator cannot be converted to "
                        f"MolecularHamiltonian. The one-body term {term} is not "
                        "of the form a^\\dagger_{\\sigma, p} a_{\\sigma, q}."
                    )

            # two‑body term: a†_σ,p a†_τ,r a_τ,s a_σ,q
            elif len(term) == 4:
                (_, _, p), (_, _, r), (_, _, s), (_, _, q) = term

                valid_2b = [
                    (cre_a(p), cre_a(r), des_a(s), des_a(q)),
                    (cre_a(p), cre_b(r), des_b(s), des_a(q)),
                    (cre_b(p), cre_a(r), des_a(s), des_b(q)),
                    (cre_b(p), cre_b(r), des_b(s), des_b(q)),
                ]
                if term not in valid_2b:
                    raise ValueError(
                        "FermionOperator cannot be converted to "
                        f"MolecularHamiltonian. The two-body term {term} is not "
                        "of the form a^{\\dagger}_{\\sigma, p} a^{\\dagger}_{\\sigma, r} a_{\\sigma, s} a_{\\sigma, q}, or "
                        "a^{\\dagger}_{\\sigma, p} a^{\\dagger}_{\\tau, r} a_{\\tau, s} a_{\\sigma, q}."
                    )

                key = (p, q, r, s)
                if key not in seen_2b:

                    h_pqrs = 2.0 * coeff
                    # fill (p,q,r,s) and its 7 symmetric equivalents
                    for a, b, c, d in [
                        (p, q, r, s), (q, p, r, s),
                        (p, q, s, r), (q, p, s, r),
                        (r, s, p, q), (s, r, p, q),
                        (r, s, q, p), (s, r, q, p),
                    ]:
                        two_body_tensor[a, b, c, d] = h_pqrs

            # more terms
            else:
                raise ValueError(
                    "FermionOperator cannot be converted to MolecularHamiltonian."
                    f" The term {term} is neither a constant, one-body, or two-body "
                    "term."
                )


        return MolecularHamiltonian(
            one_body_tensor=one_body_tensor,
            two_body_tensor=two_body_tensor,
            constant=constant,
        )

    def _approx_eq_(self, other, rtol: float, atol: float) -> bool:
        if isinstance(other, MolecularHamiltonian):
            if not np.allclose(self.constant, other.constant, rtol=rtol, atol=atol):
                return False
            if not np.allclose(
                self.one_body_tensor, other.one_body_tensor, rtol=rtol, atol=atol
            ):
                return False
            if not np.allclose(
                self.two_body_tensor, other.two_body_tensor, rtol=rtol, atol=atol
            ):
                return False
            return True
        return NotImplemented
