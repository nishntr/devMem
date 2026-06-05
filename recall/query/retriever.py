"""Hybrid retriever — combines FAISS semantic search with FTS5 BM25 via RRF."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from recall.models import Event, EventType
from recall.query.timeparser import parse_time_expression, to_iso

if TYPE_CHECKING:
    from recall.processor.embedder import EmbedderQueue
    from recall.storage.db import DB
    from recall.storage.vectors import VectorStore

logger = logging.getLogger(__name__)

# Reciprocal Rank Fusion constant
_RRF_K = 60


class Retriever:
    """
    Hybrid search: FAISS semantic + FTS5 BM25, fused with Reciprocal Rank Fusion.
    """

    def __init__(
        self,
        db: "DB",
        vectors: "VectorStore",
        embedder: "EmbedderQueue",
        rrf_k: int = _RRF_K,
    ) -> None:
        self._db = db
        self._vectors = vectors
        self._embedder = embedder
        self._rrf_k = rrf_k

    def search(
        self,
        query: str,
        top_k: int = 10,
        date_range: Optional[tuple[datetime, datetime]] = None,
        event_types: Optional[list[EventType]] = None,
        repo_name: Optional[str] = None,
    ) -> list[Event]:
        """
        Run hybrid search and return the top_k most relevant Events.

        Parameters
        ----------
        query:
            Natural language search query.  Time expressions in the query are
            automatically extracted to narrow the date range.
        top_k:
            Number of results to return.
        date_range:
            Explicit (start_dt, end_dt) to restrict results.  If None,
            time expressions in *query* are parsed automatically.
        event_types:
            Optional list of EventType values to filter by.
        repo_name:
            Optional repo name to restrict results.
        """
        # 1. Auto-detect date range from query if not supplied
        if date_range is None:
            parsed_range = parse_time_expression(query)
        else:
            parsed_range = date_range

        iso_range: Optional[tuple[str, str]] = None
        if parsed_range:
            iso_range = (to_iso(parsed_range[0]), to_iso(parsed_range[1]))

        # 2. Build candidate set from SQL filters
        candidates = self._db.get_events_by_filters(
            date_range=iso_range,
            event_types=event_types,
            repo_name=repo_name,
            limit=5000,
        )
        candidate_ids = [e.id for e in candidates if e.id is not None]
        embedding_candidate_ids = [
            e.embedding_id for e in candidates if e.embedding_id is not None
        ]

        # 3. Semantic search (FAISS)
        semantic_results: list[tuple[int, float]] = []
        if self._vectors.size() > 0:
            try:
                query_vec = self._embedder.encode_query(query)
                raw = self._vectors.search(
                    query_vec,
                    top_k=top_k * 3,
                    candidate_ids=embedding_candidate_ids if embedding_candidate_ids else None,
                )
                # raw = [(embedding_id, score)] — map back to event_id via DB
                emb_to_event = {
                    e.embedding_id: e.id
                    for e in candidates
                    if e.embedding_id is not None and e.id is not None
                }
                for emb_id, score in raw:
                    ev_id = emb_to_event.get(emb_id)
                    if ev_id is not None:
                        semantic_results.append((ev_id, score))
            except Exception:
                logger.exception("FAISS search failed")

        # 4. FTS5 BM25 search
        fts_results: list[tuple[int, float]] = []
        try:
            fts_raw = self._db.fts_search(query, limit=top_k * 3)
            # Filter to candidates if we have a date/type constraint
            if candidate_ids:
                cand_set = set(candidate_ids)
                fts_raw = [(eid, rank) for eid, rank in fts_raw if eid in cand_set]
            fts_results = fts_raw
        except Exception:
            logger.exception("FTS search failed")

        # 5. Reciprocal Rank Fusion
        fused_scores: dict[int, float] = {}

        for rank, (event_id, _) in enumerate(semantic_results):
            fused_scores[event_id] = fused_scores.get(event_id, 0.0) + 1.0 / (self._rrf_k + rank + 1)

        for rank, (event_id, _) in enumerate(fts_results):
            fused_scores[event_id] = fused_scores.get(event_id, 0.0) + 1.0 / (self._rrf_k + rank + 1)

        # If neither search returned anything, fall back to chronological candidates
        if not fused_scores and candidates:
            return list(reversed(candidates))[:top_k]

        # 6. Sort by fused score, fetch top_k
        sorted_ids = sorted(fused_scores, key=lambda x: fused_scores[x], reverse=True)[:top_k]

        # Fetch events (prefer from candidate cache for speed)
        id_to_event = {e.id: e for e in candidates if e.id in sorted_ids}
        missing_ids = [eid for eid in sorted_ids if eid not in id_to_event]
        if missing_ids:
            extra = self._db.get_events_by_ids(missing_ids)
            for e in extra:
                id_to_event[e.id] = e

        return [id_to_event[eid] for eid in sorted_ids if eid in id_to_event]
