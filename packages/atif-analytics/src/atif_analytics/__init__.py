# SPDX-License-Identifier: Apache-2.0

"""atif-analytics: LLM analytics over the materialized ATIF corpus.

Deliberately import-lean: pipelines are imported from their modules
(``atif_analytics.application.analyze`` etc.), never re-exported here, so a
bare package import stays off the boto3 subtree (workspace lean-import
convention).
"""
