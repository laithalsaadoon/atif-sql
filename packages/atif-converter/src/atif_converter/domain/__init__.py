# SPDX-License-Identifier: Apache-2.0

"""Pure domain layer: fidelity policy types, the error taxonomy, the two conversions.

The innermost layer of the converter hexagon. It holds the ATIF data classes
(the output contract, vendored from harbor under :mod:`atif_converter.domain.atif`)
and does no file I/O beyond :mod:`atif_converter.domain.pricing` reading the
vendored price table beside it. Nothing here imports harbor or litellm.
"""
