# SPDX-License-Identifier: Apache-2.0

"""Pytest conftest — re-exports shared fixtures from cli_fixtures (unique name avoids cross-package conftest collisions)."""

from cli_fixtures import *  # noqa: F403
