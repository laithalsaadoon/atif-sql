# SPDX-License-Identifier: Apache-2.0

"""Pure domain layer: fidelity policy types, the error taxonomy, the two conversions.

The innermost layer of the converter hexagon. It imports harbor's public ATIF
data classes (the output contract) and nothing else of harbor, and does no file
I/O beyond :mod:`atif_converter.domain.pricing` reading litellm's bundled price
table so the conversion can price steps without importing litellm.
"""
