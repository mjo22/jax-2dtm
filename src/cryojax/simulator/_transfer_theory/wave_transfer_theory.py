from typing import Optional
from typing_extensions import override

import jax.numpy as jnp
from equinox import field
from jaxtyping import Array, Complex, Float

from ..._errors import error_if_negative, error_if_not_positive
from ...constants import convert_keV_to_angstroms
from .._instrument_config import InstrumentConfig
from .base_transfer_theory import AbstractTransferFunction, AbstractTransferTheory
from .common_functions import compute_phase_shifts


class WaveTransferFunction(AbstractTransferFunction, strict=True):
    """Compute an astigmatic lens transfer function
    (referred to as the "wave transfer function") with aspherical aberration correction.

    **References:**

    - For the definition of the wave transfer function, see Chapter 65, Page 1697
      from *Hawkes, Peter W., and Erwin Kasper. Principles of Electron
      Optics, Volume 3: Fundamental Wave Optics. Academic Press, 2022.*
    """

    defocus_in_angstroms: Float[Array, ""]
    astigmatism_in_angstroms: Float[Array, ""]
    astigmatism_angle: Float[Array, ""]
    voltage_in_kilovolts: Float[Array, ""] | float = field(static=True)
    spherical_aberration_in_mm: Float[Array, ""]

    def __init__(
        self,
        defocus_in_angstroms: float | Float[Array, ""] = 10000.0,
        astigmatism_in_angstroms: float | Float[Array, ""] = 0.0,
        astigmatism_angle: float | Float[Array, ""] = 0.0,
        voltage_in_kilovolts: float | Float[Array, ""] = 300.0,
        spherical_aberration_in_mm: float | Float[Array, ""] = 2.7,
    ):
        """**Arguments:**

        - `defocus_u_in_angstroms`: The major axis defocus in Angstroms.
        - `defocus_v_in_angstroms`: The minor axis defocus in Angstroms.
        - `astigmatism_angle`: The defocus angle.
        - `voltage_in_kilovolts`:
            The accelerating voltage in kV. This field is treated as *static*, i.e.
            as part of the pytree. This is because the accelerating voltage is treated
            as a traced value in the `InstrumentConfig`, since many modeling components
            are interested in the accelerating voltage.
        - `spherical_aberration_in_mm`: The spherical aberration coefficient in mm.
        """
        self.defocus_in_angstroms = error_if_not_positive(defocus_in_angstroms)
        self.astigmatism_in_angstroms = jnp.asarray(astigmatism_in_angstroms)
        self.astigmatism_angle = jnp.asarray(astigmatism_angle)
        self.voltage_in_kilovolts = voltage_in_kilovolts
        self.spherical_aberration_in_mm = error_if_negative(spherical_aberration_in_mm)

    def __call__(
        self,
        frequency_grid_in_angstroms: Float[Array, "y_dim x_dim 2"],
        *,
        wavelength_in_angstroms: Optional[Float[Array, ""] | float] = None,
        defocus_offset: Float[Array, ""] | float = 0.0,
    ) -> Complex[Array, "y_dim x_dim"]:
        # Convert degrees to radians
        astigmatism_angle = jnp.deg2rad(self.astigmatism_angle)
        # Convert spherical abberation coefficient to angstroms
        spherical_aberration_in_angstroms = self.spherical_aberration_in_mm * 1e7
        # Get the wavelength. It can either be passed from upstream or stored in the
        # CTF
        if wavelength_in_angstroms is None:
            wavelength_in_angstroms = convert_keV_to_angstroms(
                jnp.asarray(self.voltage_in_kilovolts)
            )
        else:
            wavelength_in_angstroms = jnp.asarray(wavelength_in_angstroms)
        defocus_axis_1_in_angstroms = self.defocus_in_angstroms + jnp.asarray(
            defocus_offset
        )
        defocus_axis_2_in_angstroms = (
            self.defocus_in_angstroms
            + self.astigmatism_in_angstroms
            + jnp.asarray(defocus_offset)
        )
        # Compute phase shifts for CTF
        phase_shifts = compute_phase_shifts(
            frequency_grid_in_angstroms,
            defocus_axis_1_in_angstroms,
            defocus_axis_2_in_angstroms,
            astigmatism_angle,
            wavelength_in_angstroms,
            spherical_aberration_in_angstroms,
            phase_shift=jnp.asarray(0.0),
        )
        # Compute the CTF
        return jnp.exp(1.0j * phase_shifts)


class WaveTransferTheory(AbstractTransferTheory, strict=True):
    """An optics model that propagates the exit wave to the detector plane."""

    wtf: WaveTransferFunction

    def __init__(
        self,
        wtf: WaveTransferFunction,
    ):
        """**Arguments:**

        - `wtf`: The wave transfer function model.
        """

        self.wtf = wtf

    @override
    def __call__(
        self,
        fourier_wavefunction_at_exit_plane: Complex[
            Array,
            "{instrument_config.padded_y_dim} {instrument_config.padded_x_dim}",
        ],
        instrument_config: InstrumentConfig,
        defocus_offset: Float[Array, ""] | float = 0.0,
    ) -> Complex[
        Array, "{instrument_config.padded_y_dim} {instrument_config.padded_x_dim}"
    ]:
        """Apply the wave transfer function to the wavefunction in the exit plane."""
        frequency_grid = instrument_config.padded_full_frequency_grid_in_angstroms
        # Compute the wave transfer function
        wtf_array = self.wtf(
            frequency_grid,
            wavelength_in_angstroms=instrument_config.wavelength_in_angstroms,
            defocus_offset=defocus_offset,
        )
        # ... compute the contrast as the CTF multiplied by the exit plane
        # phase shifts
        fourier_wavefunction_at_detector_plane = (
            wtf_array * fourier_wavefunction_at_exit_plane
        )

        return fourier_wavefunction_at_detector_plane
