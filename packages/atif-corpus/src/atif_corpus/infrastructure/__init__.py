# SPDX-License-Identifier: Apache-2.0

"""Infrastructure layer: filesystem scanning, atomic writes, settings, fakes.

Everything that touches the world (env, clock-adjacent file mtimes, disk)
lives here so the domain stays a pure function of its inputs.
"""
