"""
Base class for an integration method.
"""

from abc import abstractmethod
from typing import Any

import jax
import jax.numpy as jnp
from equinox import Module, AbstractVar

from .._specimen import AbstractSpecimen
from .._potential import AbstractScatteringPotential, AbstractVoxelPotential
from .._config import ImageConfig

from ...image import rfftn, irfftn
from ...typing import ComplexImage


class AbstractPotentialIntegrator(Module, strict=True):
    """Base class for a method of integrating a potential onto
    the exit plane."""

    config: AbstractVar[ImageConfig]

    @abstractmethod
    def integrate_potential(
        self, potential: AbstractScatteringPotential
    ) -> ComplexImage:
        """Compute the scattering potential in the exit plane.

        **Arguments:**

        `potential`: The scattering potential representation.
        """
        raise NotImplementedError

    def __call__(self, specimen: AbstractSpecimen, **kwargs: Any) -> ComplexImage:
        # Get potential in the lab frame
        potential = specimen.potential_in_lab_frame
        # Compute the fourier projection in the exit plane
        potential_at_exit_plane = self.integrate_potential(potential, **kwargs)
        # Rescale the pixel size if different from the voxel size
        if isinstance(potential, AbstractVoxelPotential):
            rescale_fn = lambda fourier_potential: rfftn(
                self.config.rescale_to_pixel_size(
                    irfftn(fourier_potential, s=self.config.padded_shape),
                    potential.voxel_size,
                )
            )
            null_fn = lambda fourier_potential: fourier_potential
            potential_at_exit_plane = jax.lax.cond(
                jnp.isclose(potential.voxel_size, self.config.pixel_size),
                null_fn,
                rescale_fn,
                potential_at_exit_plane,
            )
        # Apply translation through phase shifts
        potential_at_exit_plane *= specimen.pose.compute_shifts(
            self.config.padded_frequency_grid_in_angstroms.get()
        )

        return potential_at_exit_plane
