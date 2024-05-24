"""
Atomistic representation of the scattering potential.
"""

from abc import abstractmethod
from typing import Optional
from typing_extensions import override, Self

import equinox as eqx
import jax
import jax.numpy as jnp
import jax.scipy as jsp
import numpy as np
from jaxtyping import Array, Float, Int

from ..._errors import error_if_negative, error_if_not_positive
from ...constants import (
    get_tabulated_scattering_factor_parameters,
    peng_element_scattering_factor_parameter_table,
)
from ...coordinates import make_1d_coordinate_grid
from .._pose import AbstractPose
from .base_potential import AbstractPotentialRepresentation


class AbstractAtomicPotential(AbstractPotentialRepresentation, strict=True):
    """Abstract interface for an atom-based scattering potential representation.

    !!! info
        In, `cryojax`, potentials should be built in units of *inverse length squared*,
        $[L]^{-2}$. This rescaled potential is defined to be

        $$U(\\mathbf{r}) = \\frac{2 m e}{\\hbar^2} V(\\mathbf{r}),$$

        where $V$ is the electrostatic potential energy, $\\mathbf{r}$ is a positional
        coordinate, $m$ is the electron mass, and $e$ is the electron charge.

        For a single atom, this rescaled potential has the advantage that under usual
        scattering approximations (i.e. the first-born approximation), the
        fourier transform of this quantity is closely related to tabulated electron scattering
        factors. In particular, for a single atom with scattering factor $f^{(e)}(\\mathbf{q})$
        and scattering vector $\\mathbf{q}$, its rescaled potential is equal to

        $$U(\\mathbf{r}) = 4 \\pi \\mathcal{F}^{-1}[f^{(e)}(\\boldsymbol{\\xi} / 2)](\\mathbf{r}),$$

        where $\\boldsymbol{\\xi} = 2 \\mathbf{q}$ is the wave vector coordinate and
        $\\mathcal{F}^{-1}$ is the inverse fourier transform operator in the convention

        $$\\mathcal{F}[f](\\boldsymbol{\\xi}) = \\int d^3\\mathbf{r} \\ \\exp(2\\pi i \\boldsymbol{\\xi}\\cdot\\mathbf{r}) f(\\mathbf{r}).$$

        The rescaled potential $U$ gives the following time-independent schrodinger equation
        for the scattering problem,

        $$(\\nabla^2 + k^2) \\psi(\\mathbf{r}) = - U(\\mathbf{r}) \\psi(\\mathbf{r}),$$

        where $k$ is the incident wavenumber of the electron beam.

        **References**:

        - For the definition of the rescaled potential, see
        Chapter 69, Page 2003, Equation 69.6 from *Hawkes, Peter W., and Erwin Kasper.
        Principles of Electron Optics, Volume 4: Advanced Wave Optics. Academic Press,
        2022.*
        - To work out the correspondence between the rescaled potential and the electron
        scattering factors, see the supplementary information from *Vulović, Miloš, et al.
        "Image formation modeling in cryo-electron microscopy." Journal of structural
        biology 183.1 (2013): 19-32.*
    """  # noqa: E501

    atom_positions: eqx.AbstractVar[Float[Array, "n_atoms 3"]]

    def rotate_to_pose(self, pose: AbstractPose) -> Self:
        """Return a new potential with rotated `atom_positions`."""
        return eqx.tree_at(
            lambda d: d.atom_positions,
            self,
            pose.rotate_coordinates(self.atom_positions),
        )

    @abstractmethod
    def as_real_voxel_grid(
        self,
        shape: tuple[int, int, int],
        voxel_size: Float[Array, ""] | float,
        *,
        batch_size: int = 1,
    ) -> Float[Array, "{shape[0]} {shape[1]} {shape[2]}"]:
        raise NotImplementedError


class GaussianMixtureAtomicPotential(AbstractAtomicPotential, strict=True):
    """An atomistic representation of scattering potential as a mixture of
    gaussians.

    The naming and numerical convention of parameters `gaussian_amplitudes` and
    `gaussian_widths` follows "Robust Parameterization of Elastic and Absorptive
    Electron Atomic Scattering Factors" by Peng et al. (1996), where $a_i$ are
    the `gaussian_amplitudes` and $b_i$ are the `gaussian_widths`.

    !!! info
        In order to load a `GaussianMixtureAtomicPotential` from tabulated
        scattering factors, use the `cryojax.constants` submodule.

        ```python
        from cryojax.constants import (
            peng_element_scattering_factor_parameter_table,
            get_tabulated_scattering_factor_parameters,
        )
        from cryojax.io import read_atoms_from_pdb
        from cryojax.simulator import GaussianMixtureAtomicPotential

        # Load positions of atoms and one-hot encoded atom names
        atom_positions, atom_identities = read_atoms_from_pdb(...)
        scattering_factor_a, scattering_factor_b = get_tabulated_scattering_factor_parameters(
            atom_identities, peng_element_scattering_factor_parameter_table
        )
        potential = GaussianMixtureAtomicPotential(
            atom_positions, scattering_factor_a, scattering_factor_b
        )
        ```
    """  # noqa: E501

    atom_positions: Float[Array, "n_atoms 3"]
    gaussian_amplitudes: Float[Array, "n_atoms n_gaussians_per_atom"]
    gaussian_widths: Float[Array, "n_atoms n_gaussians_per_atom"]

    def __init__(
        self,
        atom_positions: Float[Array, "n_atoms 3"] | Float[np.ndarray, "n_atoms 3"],
        gaussian_amplitudes: (
            Float[Array, "n_atoms n_gaussians_per_atom"]
            | Float[np.ndarray, "n_atoms n_gaussians_per_atom"]
        ),
        gaussian_widths: (
            Float[Array, "n_atoms n_gaussians_per_atom"]
            | Float[np.ndarray, "n_atoms n_gaussians_per_atom"]
        ),
    ):
        """**Arguments:**

        - `atom_positions`: The coordinates of the atoms in units of angstroms.
        - `gaussian_amplitudes`:
            The strength for each atom and for each gaussian per atom.
            This has units of angstroms.
        - `gaussian_widths`:
            The variance (up to numerical constants) for each atom and
            for each gaussian per atom. This has units of angstroms
            squared.
        """
        self.atom_positions = jnp.asarray(atom_positions)
        self.gaussian_amplitudes = jnp.asarray(gaussian_amplitudes)
        self.gaussian_widths = error_if_not_positive(jnp.asarray(gaussian_widths))

    @override
    def as_real_voxel_grid(
        self,
        shape: tuple[int, int, int],
        voxel_size: Float[Array, ""] | float,
        *,
        batch_size: int = 1,
    ) -> Float[Array, "{shape[0]} {shape[1]} {shape[2]}"]:
        """Return a voxel grid in real space of the potential.

        See [`PengAtomicPotential.as_real_voxel_grid`](scattering_potential.md#cryojax.simulator.PengAtomicPotential.as_real_voxel_grid)
        for the numerical conventions used when computing the sum of gaussians.

        **Arguments:**

        - `shape`: The shape of the resulting voxel grid.
        - `voxel_size`: The voxel size of the resulting voxel grid.
        - `batch_size`:
            The number of z-planes to evaluate in parallel with
            `jax.vmap`. By default, `1`.

        **Returns:**

        The rescaled potential $U(\\mathbf{r})$ as a voxel grid of shape `shape`
        and voxel size `voxel_size`.
        """  # noqa: E501
        return _build_real_space_voxel_potential_from_atoms(
            shape,
            jnp.asarray(voxel_size),
            self.atom_positions,
            self.gaussian_amplitudes,
            self.gaussian_widths,
            batch_size=batch_size,
        )


class AbstractTabulatedAtomicPotential(AbstractAtomicPotential, strict=True):
    b_factors: eqx.AbstractVar[Optional[Float[Array, " n_atoms"]]]


class PengAtomicPotential(AbstractTabulatedAtomicPotential, strict=True):
    """The scattering potential parameterized as a mixture of five gaussians
    per atom, through work by Lian-Mao Peng.

    To load this object, the following pattern can be used:

    ```python
    from cryojax.io import read_atoms_from_pdb
    from cryojax.simulator import PengAtomicPotential

    # Load positions of atoms and one-hot encoded atom names
    filename = "example.pdb"
    atom_positions, atom_identities = read_atoms_from_pdb(filename)
    potential = PengAtomicPotential(atom_positions, atom_identities)
    ```

    Alternatively, use the following to load with B-factors:

    ```python
    from cryojax.io import read_atoms_from_pdb
    from cryojax.simulator import PengAtomicPotential

    # Load positions of atoms, encoded atom names, and B-factors
    filename = "example.pdb"
    atom_positions, atom_identities, b_factors = read_atoms_from_pdb(
        filename, get_b_factors=True
    )
    potential = PengAtomicPotential(atom_positions, atom_identities, b_factors)
    ```

    **References:**

    - Peng, L-M. "Electron atomic scattering factors and scattering potentials of crystals."
      Micron 30.6 (1999): 625-648.
    - Peng, L-M., et al. "Robust parameterization of elastic and absorptive electron atomic
      scattering factors." Acta Crystallographica Section A: Foundations of Crystallography
      52.2 (1996): 257-276.
    """  # noqa: E501

    atom_positions: Float[Array, "n_atoms 3"]
    scattering_factor_a: Float[Array, "n_atoms 5"]
    scattering_factor_b: Float[Array, "n_atoms 5"]
    b_factors: Optional[Float[Array, " n_atoms"]]

    def __init__(
        self,
        atom_positions: Float[Array, "n_atoms 3"] | Float[np.ndarray, "n_atoms 3"],
        atom_identities: Int[Array, " n_atoms"] | Int[np.ndarray, " n_atoms"],
        b_factors: Optional[
            Float[Array, " n_atoms"] | Float[np.ndarray, " n_atoms"]
        ] = None,
    ):
        """**Arguments:**

        - `atom_positions`: The coordinates of the atoms in units of angstroms.
        - `atom_identities`: Array containing the index of the one-hot encoded atom names.
                             Hydrogen is "1", Carbon is "6", Nitrogen is "7", etc.
        - `b_factors`: The B-factors applied to each atom.
        """
        self.atom_positions = jnp.asarray(atom_positions)
        scattering_factor_a, scattering_factor_b = (
            get_tabulated_scattering_factor_parameters(
                atom_identities, peng_element_scattering_factor_parameter_table
            )
        )
        self.scattering_factor_a = jnp.asarray(scattering_factor_a)
        self.scattering_factor_b = jnp.asarray(scattering_factor_b)
        if b_factors is None:
            self.b_factors = None
        else:
            self.b_factors = error_if_negative(jnp.asarray(b_factors))

    @override
    def as_real_voxel_grid(
        self,
        shape: tuple[int, int, int],
        voxel_size: Float[Array, ""] | float,
        *,
        batch_size: int = 1,
    ) -> Float[Array, "{shape[0]} {shape[1]} {shape[2]}"]:
        """Return a voxel grid in real space of the potential.

        Through the work of Peng et al. (1996), tabulated elastic electron scattering factors
        are defined as

        $$f^{(e)}(\\mathbf{q}) = \\sum\\limits_{i = 1}^5 a_i \\exp(- b_i |\\mathbf{q}|^2),$$

        where $a_i$ is stored as `PengAtomicPotential.scattering_factor_a` and $b_i$ is
        stored as `PengAtomicPotential.scattering_factor_b` for the scattering vector $\\mathbf{q}$.
        Under usual scattering approximations (i.e. the first-born approximation),
        the rescaled electrostatic potential energy $U(\\mathbf{r})$ is then given by
        $4 \\pi \\mathcal{F}^{-1}[f^{(e)}(\\boldsymbol{\\xi} / 2)](\\mathbf{r})$, which is computed
        analytically as

        $$U(\\mathbf{r}) = 4 \\pi \\sum\\limits_{i = 1}^5 \\frac{a_i}{(2\\pi (b_i / 8 \\pi^2))^{3/2}} \\exp(- \\frac{|\\mathbf{r} - \\mathbf{r}_0|^2}{2 (b_i / 8 \\pi^2)}),$$

        where $\\mathbf{r}_0$ is the position of the atom. Including an additional B-factor (denoted by
        $B$ and stored as `PengAtomicPotential.b_factors`) gives the expression for the potential
        $U(\\mathbf{r})$ of a single atom type and its fourier transform pair $\\tilde{U}(\\boldsymbol{\\xi}) \\equiv \\mathcal{F}[U](\\boldsymbol{\\xi})$,

        $$U(\\mathbf{r}) = 4 \\pi \\sum\\limits_{i = 1}^5 \\frac{a_i}{(2\\pi ((b_i + B) / 8 \\pi^2))^{3/2}} \\exp(- \\frac{|\\mathbf{r} - \\mathbf{r}_0|^2}{2 ((b_i + B) / 8 \\pi^2)}),$$

        $$\\tilde{U}(\\boldsymbol{\\xi}) = 4 \\pi \\sum\\limits_{i = 1}^5 a_i \\exp(- (b_i + B) |\\boldsymbol{\\xi}|^2 / 4) \\exp(2 \\pi i \\boldsymbol{\\xi}\\cdot\\mathbf{r}_0),$$

        where $\\mathbf{q} = \\boldsymbol{\\xi} / 2$ gives the relationship between the wave vector and the
        scattering vector.

        In practice, for a discretization on a grid with voxel size $\\Delta r$ and grid point $\\mathbf{r}_{\\ell}$,
        the potential is evaluated as the average value inside the voxel

        $$U_{\\ell} = 4 \\pi \\frac{1}{\\Delta r^3} \\sum\\limits_{i = 1}^5 a_i \\prod\\limits_{j = 1}^3 \\int_{r^{\\ell}_j-\\Delta r/2}^{r^{\\ell}_j+\\Delta r/2} dr_j \\ \\frac{1}{{\\sqrt{2\\pi ((b_i + B) / 8 \\pi^2)}}} \\exp(- \\frac{(r_j - r^0_j)^2}{2 ((b_i + B) / 8 \\pi^2)}),$$

        where $j$ indexes the components of the spatial coordinate vector $\\mathbf{r}$. The above expression is evaluated using the error function as

        $$U_{\\ell} = 4 \\pi \\frac{1}{(2 \\Delta r)^3} \\sum\\limits_{i = 1}^5 a_i \\prod\\limits_{j = 1}^3 \\textrm{erf}(\\frac{r_j^{\\ell} - r_j^0 + \\Delta r / 2}{\\sqrt{2 ((b_i + B) / 8\\pi^2)}}) - \\textrm{erf}(\\frac{r_j^{\\ell} - r^0_j - \\Delta r / 2}{\\sqrt{2 ((b_i + B) / 8\\pi^2)}}).$$

        **Arguments:**

        - `shape`: The shape of the resulting voxel grid.
        - `voxel_size`: The voxel size of the resulting voxel grid.
        - `batch_size`:
            The number of z-planes to evaluate in parallel with
            `jax.vmap`. By default, `1`.

        **Returns:**

        The rescaled potential $U(\\mathbf{r})$ as a voxel grid of shape `shape`
        and voxel size `voxel_size`.
        """  # noqa: E501
        gaussian_amplitudes = self.scattering_factor_a
        if self.b_factors is None:
            gaussian_widths = self.scattering_factor_b
        else:
            gaussian_widths = self.scattering_factor_b + self.b_factors[:, None]
        return _build_real_space_voxel_potential_from_atoms(
            shape,
            jnp.asarray(voxel_size),
            self.atom_positions,
            gaussian_amplitudes,
            gaussian_widths,
            batch_size=batch_size,
        )


@eqx.filter_jit
def _build_real_space_voxel_potential_from_atoms(
    shape: tuple[int, int, int],
    voxel_size: Float[Array, ""],
    atom_positions: Float[Array, "n_atoms 3"],
    a: Float[Array, "n_atoms n_gaussians_per_atom"],
    b: Float[Array, "n_atoms n_gaussians_per_atom"],
    batch_size: int,
) -> Float[Array, "{shape[0]} {shape[1]} {shape[2]}"]:
    # Make coordinate systems for each of x, y, and z dimensions
    z_dim, y_dim, x_dim = shape
    grid_x, grid_y, grid_z = [
        make_1d_coordinate_grid(dim, voxel_size) for dim in [x_dim, y_dim, z_dim]
    ]
    # Evaluate 1D gaussian integrals for each of x, y, and z dimensions
    (
        gaussian_integrals_times_prefactor_per_atom_per_interval_x,
        gaussian_integrals_per_atom_per_interval_y,
        gaussian_integrals_per_atom_per_interval_z,
    ) = _evaluate_gaussian_integrals_for_all_atoms_and_intervals(
        grid_x, grid_y, grid_z, atom_positions, a, b, voxel_size
    )
    # Get function to compute voxel grid at a single z-plane
    compute_potential_at_z_plane = jax.jit(
        lambda gaussian_integrals_per_atom_z: _evaluate_gaussian_potential_at_z_plane(
            gaussian_integrals_times_prefactor_per_atom_per_interval_x,
            gaussian_integrals_per_atom_per_interval_y,
            gaussian_integrals_per_atom_z,
        )
    )
    # Map over z-planes
    if batch_size > z_dim:
        raise ValueError(
            "The `batch_size` when building a voxel grid must be an "
            "integer less than or equal to the z-dimension of the grid, "
            "or `shape[0]`."
        )
    elif batch_size == 1:
        # ... compute the volume iteratively
        potential_as_voxel_grid = jax.lax.map(
            compute_potential_at_z_plane, gaussian_integrals_per_atom_per_interval_z
        )
    elif batch_size > 1:
        # ... compute the volume by tuning how many z-planes to batch over
        compute_potential_at_z_planes = jax.vmap(compute_potential_at_z_plane, in_axes=0)
        # ... reshape the number of grid points into an iterative dimension and a batching
        # dimension
        gaussian_integrals_per_atom_per_batch_z = (
            gaussian_integrals_per_atom_per_interval_z[
                : z_dim - z_dim % batch_size, ...
            ].reshape(
                (
                    z_dim // batch_size,
                    batch_size,
                    *gaussian_integrals_per_atom_per_interval_z.shape[1:],
                )
            )
        )
        # .. compute the volume, batching over z-plane
        potential_as_voxel_grid = jax.lax.map(
            compute_potential_at_z_planes, gaussian_integrals_per_atom_per_batch_z
        ).reshape(((z_dim // batch_size) * batch_size, y_dim, x_dim))
        # ... if the batch size is divisible by the z-dimension, need to take care
        # of the remainder
        if z_dim % batch_size != 0:
            potential_as_voxel_grid = jnp.concatenate(
                [
                    potential_as_voxel_grid,
                    compute_potential_at_z_planes(
                        gaussian_integrals_per_atom_per_interval_z[
                            z_dim - z_dim % batch_size :, ...
                        ]
                    ),
                ],
                axis=0,
            )
    else:
        raise ValueError(
            "The `batch_size` when building a voxel grid must be an "
            "integer greater than 1."
        )

    return potential_as_voxel_grid


@eqx.filter_jit
def _evaluate_gaussian_integrals_for_all_atoms_and_intervals(
    grid_x: Float[Array, " dim_x"],
    grid_y: Float[Array, " dim_y"],
    grid_z: Float[Array, " dim_z"],
    atom_positions: Float[Array, "n_atoms 3"],
    a: Float[Array, "n_atoms n_gaussians_per_atom"],
    b: Float[Array, "n_atoms n_gaussians_per_atom"],
    voxel_size: Float[Array, ""],
) -> tuple[
    Float[Array, "dim_x n_atoms n_gaussians_per_atom"],
    Float[Array, "dim_y n_atoms n_gaussians_per_atom"],
    Float[Array, "dim_z n_atoms n_gaussians_per_atom"],
]:
    """Evaluate 1D averaged gaussians in x, y, and z dimensions
    for each atom and each gaussian per atom.
    """
    # Define function to compute integrals for each dimension
    scaling = 2 * jnp.pi / jnp.sqrt(b)
    integration_kernel_1d = lambda delta: (
        jsp.special.erf(scaling[None, :, :] * (delta + voxel_size)[:, :, None])
        - jsp.special.erf(scaling[None, :, :] * delta[:, :, None])
    )
    # Compute outer product of left edge of grid points minus atomic positions
    left_edge_grid_x, left_edge_grid_y, left_edge_grid_z = (
        grid_x - voxel_size / 2,
        grid_y - voxel_size / 2,
        grid_z - voxel_size / 2,
    )
    delta_x, delta_y, delta_z = (
        left_edge_grid_x[:, None] - atom_positions[:, 0],
        left_edge_grid_y[:, None] - atom_positions[:, 1],
        left_edge_grid_z[:, None] - atom_positions[:, 2],
    )
    # Compute gaussian integrals for each grid point, each atom, and
    # each gaussian per atom
    gauss_x, gauss_y, gauss_z = (
        integration_kernel_1d(delta_x),
        integration_kernel_1d(delta_y),
        integration_kernel_1d(delta_z),
    )
    # Compute the prefactors for each atom and each gaussian per atom
    # for the potential
    prefactor = (4 * jnp.pi * a) / (2 * voxel_size) ** 3
    # Multiply the prefactor onto one of the gaussians for efficiency
    return prefactor * gauss_x, gauss_y, gauss_z


def _evaluate_gaussian_potential_at_z_plane(
    gaussian_integrals_per_atom_per_interval_x: Float[
        Array, "dim_x n_atoms n_gaussians_per_atom"
    ],
    gaussian_integrals_per_atom_per_interval_y: Float[
        Array, "dim_y n_atoms n_gaussians_per_atom"
    ],
    gaussian_integrals_per_atom_z: Float[Array, "n_atoms n_gaussians_per_atom"],
) -> Float[Array, "dim_y dim_x"]:
    # Prepare matrices with dimensions of the number of atoms and the number of grid
    # points. There are as many matrices as number of gaussians per atom
    gauss_x = jnp.transpose(gaussian_integrals_per_atom_per_interval_x, (2, 1, 0))
    gauss_yz = jnp.transpose(
        gaussian_integrals_per_atom_per_interval_y
        * gaussian_integrals_per_atom_z[None, :, :],
        (2, 0, 1),
    )
    # Compute matrix multiplication then sum over the number of gaussians per atom
    return jnp.sum(jnp.matmul(gauss_yz, gauss_x), axis=0)
