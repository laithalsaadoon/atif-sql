# SPDX-License-Identifier: Apache-2.0

"""Pytest conftest — re-exports shared fixtures from embed_fixtures (unique name avoids cross-package conftest collisions)."""

from embed_fixtures import *  # noqa: F403
