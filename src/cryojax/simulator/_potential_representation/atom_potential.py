"""
Atomistic representation of the scattering potential.
"""

from abc import abstractmethod
from typing_extensions import override, Self

import equinox as eqx
import jax
import jax.numpy as jnp
import numpy as np
from jaxtyping import Array, Float

from .._pose import AbstractPose
from .base_potential import AbstractPotentialRepresentation


class AbstractAtomicPotential(AbstractPotentialRepresentation, strict=True):
    atom_positions: eqx.AbstractVar[Float[Array, "n_atoms 3"]]

    def rotate_to_pose(self, pose: AbstractPose) -> Self:
        return eqx.tree_at(
            lambda d: d.atom_positions,
            self,
            pose.rotate_coordinates(self.atom_positions),
        )

    @abstractmethod
    def as_real_voxel_grid(
        self, coordinate_grid_in_angstroms: Float[Array, "z_dim y_dim x_dim 3"]
    ) -> Float[Array, "z_dim y_dim x_dim"]:
        """Build a voxel grid in real space of an `AbstractAtomicPotential`."""
        raise NotImplementedError


class GaussianMixtureAtomicPotential(AbstractAtomicPotential, strict=True):
    """An atomistic representation of scattering potential as a mixture of
    gaussians.

    The naming and numerical convention of parameters `atom_a_factors` and
    `atom_b_factors` follows "Robust Parameterization of Elastic and Absorptive
    Electron Atomic Scattering Factors" by Peng et al. (1996).

    !!! info
        In order to load a `GaussianMixtureAtomicPotential` from tabulated scattering
        factors, use the `cryojax.constants` submodule.

        ```python
        from cryojax.constants import (
            peng1996_form_factor_param_table,
            get_form_factor_params_from_table,
        )

        atom_positions = ...   # Load positions of atoms
        atom_identities = ...  # Load one-hot encoded atom names
        atom_a_factors, atom_b_factors = get_form_factor_params_from_table(
            atom_identities, peng1996_form_factor_param_table
        )
        potential = GaussianAtomicPotential(
            atom_positions, atom_a_factors, atom_b_factors
        )
        ```
    """

    atom_positions: Float[Array, "n_atoms 3"]
    atom_a_factors: Float[Array, "n_atoms n_gaussians_per_atom"]
    atom_b_factors: Float[Array, "n_atoms n_gaussians_per_atom"]

    def __init__(
        self,
        atom_positions: Float[Array, "n_atoms 3"] | Float[np.ndarray, "n_atoms 3"],
        atom_a_factors: (
            Float[Array, "n_atoms n_gaussians_per_atom"]
            | Float[np.ndarray, "n_atoms n_gaussians_per_atom"]
        ),
        atom_b_factors: (
            Float[Array, "n_atoms n_gaussians_per_atom"]
            | Float[np.ndarray, "n_atoms n_gaussians_per_atom"]
        ),
    ):
        """**Arguments:**

        - `atom_positions`: The coordinates of the atoms.
        - `atom_a_factors`: The strength for each atom and for each gaussian per atom.
                            This has units of angstroms.
        - `atom_b_factors`: The variance (up to numerical constants) for each atom and
                            for each gaussian per atom. This has units of angstroms
                            squared.
        """
        self.atom_positions = jnp.asarray(atom_positions)
        self.atom_a_factors = jnp.asarray(atom_a_factors)
        self.atom_b_factors = jnp.asarray(atom_b_factors)

    @override
    def as_real_voxel_grid(
        self, coordinate_grid_in_angstroms: Float[Array, "z_dim y_dim x_dim 3"]
    ) -> Float[Array, "z_dim y_dim x_dim"]:
        return _build_real_space_voxels_from_atoms(
            self.atom_positions,
            self.atom_a_factors,
            self.atom_b_factors,
            coordinate_grid_in_angstroms,
        )


def _evaluate_3d_real_space_gaussian(
    coordinate_grid_in_angstroms: Float[Array, "z_dim y_dim x_dim 3"],
    atom_position: Float[Array, "3"],
    a: Float[Array, ""],
    b: Float[Array, ""],
) -> Float[Array, "z_dim y_dim x_dim"]:
    """Evaluate a gaussian on a 3D grid.

    **Arguments:**

    - `coordinate_grid`: The coordinate system of the grid.
    - `pos`: The center of the gaussian.
    - `a`: A scale factor.
    - `b`: The scale of the gaussian.

    **Returns:**

    The potential of the gaussian on the grid.
    """
    b_inverse = 4.0 * jnp.pi / b
    sq_distances = jnp.sum(
        b_inverse * (coordinate_grid_in_angstroms - atom_position) ** 2, axis=-1
    )
    return jnp.exp(-jnp.pi * sq_distances) * a * b_inverse ** (3.0 / 2.0)


def _evaluate_3d_atom_potential(
    coordinate_grid_in_angstroms: Float[Array, "z_dim y_dim x_dim 3"],
    atom_position: Float[Array, "3"],
    atomic_as: Float[Array, " n_form_factors"],
    atomic_bs: Float[Array, " n_form_factors"],
) -> Float[Array, "z_dim y_dim x_dim"]:
    """Evaluates the electron potential of a single atom on a 3D grid.

    **Arguments:**

    - `coordinate_grid_in_angstroms`: The coordinate system of the grid.
    - `atom_position`: The location of the atom.
    - `atomic_as`: The intensity values for each gaussian in the atom.
    - `atomic_bs`: The inverse scale factors for each gaussian in the atom.

    **Returns:**

    The potential of the atom evaluated on the grid.
    """
    eval_fxn = jax.vmap(_evaluate_3d_real_space_gaussian, in_axes=(None, None, 0, 0))
    return jnp.sum(
        eval_fxn(coordinate_grid_in_angstroms, atom_position, atomic_as, atomic_bs),
        axis=0,
    )


def _build_real_space_voxels_from_atoms(
    atom_positions: Float[Array, "n_atoms 3"],
    ff_a: Float[Array, "n_atoms n_gaussians_per_atom"],
    ff_b: Float[Array, "n_atoms n_gaussians_per_atom"],
    coordinate_grid_in_angstroms: Float[Array, "z_dim y_dim x_dim 3"],
) -> Float[Array, "z_dim y_dim x_dim"]:
    """
    Build a voxel representation of an atomic model.

    **Arguments**

    - `atom_coords`: The coordinates of the atoms.
    - `ff_a`: Intensity values for each Gaussian in the atom
    - `ff_b` : The inverse scale factors for each Gaussian in the atom
    - `coordinate_grid` : The coordinates of each voxel in the grid.

    **Returns:**

    The voxel representation of the atomic model.
    """
    voxel_grid_buffer = jnp.zeros(coordinate_grid_in_angstroms.shape[:-1])

    # TODO: Look into forcing JAX to do in-place updates with equinox.internal.while_loop
    def add_gaussian_to_potential(i, potential):
        potential += _evaluate_3d_atom_potential(
            coordinate_grid_in_angstroms, atom_positions[i], ff_a[i], ff_b[i]
        )
        return potential

    voxel_grid = jax.lax.fori_loop(
        0, atom_positions.shape[0], add_gaussian_to_potential, voxel_grid_buffer
    )

    return voxel_grid