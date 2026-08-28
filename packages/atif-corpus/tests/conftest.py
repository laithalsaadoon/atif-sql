# SPDX-License-Identifier: Apache-2.0

"""Pytest conftest — re-exports shared fixtures from corpus_fixtures (unique name avoids cross-package conftest collisions)."""

from corpus_fixtures import *  # noqa: F403
