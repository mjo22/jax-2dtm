"""Functions common to multiple scattering theories."""

import jax.numpy as jnp
from jaxtyping import Array, Float, Inexact


def compute_phase_shifts_from_integrated_potential(
    integrated_potential: Inexact[Array, "y_dim x_dim"],
    wavelength_in_angstroms: Float[Array, ""] | float,
) -> Inexact[Array, "y_dim x_dim"]:
    """Given an integrated potential, compute a phase shift distribution.

    !!! info
        In the projection approximation in cryo-EM, the phase shifts in the
        exit plane are given by

        $$\\eta(x, y) = \\sigma \\int dz \\ V(x, y, z),$$

        where $\\sigma$ is typically referred to as the interaction
        constant. However, in `cryojax`, the potential is rescaled
        to units of inverse length squared as

        $$U(x, y, z) = \\frac{2 m e}{\\hbar^2} V(x, y, z).$$

        With this rescaling of the potential, the phase shifts are equal to

        $$\\eta(x, y) = \\frac{\\lambda}{4 \\pi} \\int dz \\ U(x, y, z).$$

        **References**:

        - For the definition of the rescaled potential, see
        Chapter 69, Page 2003, Equation 69.6 from *Hawkes, Peter W., and Erwin Kasper.
        Principles of Electron Optics, Volume 4: Advanced Wave Optics. Academic Press,
        2022.*
        - For the definition of the phase shifts in terms of the rescaled potential, see
        Chapter 69, Page 2012, Equation 69.34b from *Hawkes, Peter W., and Erwin Kasper.
        Principles of Electron Optics, Volume 4: Advanced Wave Optics. Academic Press,
        2022.*

    See the documentation on atom-based scattering potentials for more information.
    """  # noqa: E501
    return integrated_potential * jnp.asarray(wavelength_in_angstroms) / (4 * jnp.pi)


# Not public API
def compute_fourier_phase_shifts_from_scattering_potential(
    structural_ensemble, potential_integrator, instrument_config
):
    # Get potential in the lab frame
    potential = structural_ensemble.get_potential_in_lab_frame()
    # Compute the phase shifts in the exit plane
    fourier_integrated_potential = (
        potential_integrator.compute_fourier_integrated_potential(
            potential, instrument_config
        )
    )
    # Compute in-plane translation through fourier phase shifts
    translational_phase_shifts = structural_ensemble.pose.compute_shifts(
        instrument_config.padded_frequency_grid_in_angstroms
    )
    # Compute the phase shifts in exit plane and multiply by the translation.
    phase_shifts_in_exit_plane = compute_phase_shifts_from_integrated_potential(
        fourier_integrated_potential, instrument_config.wavelength_in_angstroms
    )
    return phase_shifts_in_exit_plane * translational_phase_shifts
