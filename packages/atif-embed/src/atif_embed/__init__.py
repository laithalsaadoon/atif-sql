# SPDX-License-Identifier: Apache-2.0

"""atif-embed: embedding provider + LanceDB vector store + embed backfill.

The VSS write path over the ATIF corpus: Cohere Embed v4 on Bedrock
(``infrastructure/cohere_bedrock.py``), a local LanceDB embeddings store
(``infrastructure/lance_store.py``), a contract-layout corpus reader behind
the :class:`~atif_embed.domain.ports.TextRowsPort`
(``infrastructure/corpus_text_rows.py``), and the backfill use case
(``application/embed.py``). Independent of every sibling package — only
atif-cli composes (import-linter independence contract).
"""
