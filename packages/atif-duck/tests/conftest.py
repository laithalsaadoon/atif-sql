# SPDX-License-Identifier: Apache-2.0

"""Pytest conftest — re-exports shared fixtures from duck_fixtures (unique name avoids cross-package conftest collisions)."""

from duck_fixtures import *  # noqa: F403
