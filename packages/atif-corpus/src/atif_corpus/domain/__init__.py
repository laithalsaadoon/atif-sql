# SPDX-License-Identifier: Apache-2.0

"""Pure domain layer for corpus materialization.

Value objects and pure functions only: no filesystem, no env, no clock.
Every timestamp is passed IN (epoch nanoseconds from ``os.stat().st_mtime_ns``
or ``time.time_ns()``) so the materialization decision is a deterministic
function of its inputs — the property the plan-determinism tests pin.
"""
