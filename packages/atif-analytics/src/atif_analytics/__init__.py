# SPDX-License-Identifier: Apache-2.0

"""atif-analytics: LLM + structural analytics over the materialized ATIF corpus.

Deliberately import-lean: pipelines are imported from their modules
(``atif_analytics.application.analyze`` etc.), never re-exported here, so a
bare package import stays off the umap/boto3 subtrees (workspace lean-import
convention).
"""
