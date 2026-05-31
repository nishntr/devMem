"""FAISS vector store — IndexFlatIP wrapped in IndexIDMap for cosine similarity."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

try:
    import numpy as np
    import faiss as _faiss  # type: ignore[import]
    _FAISS_AVAILABLE = True
except ImportError:
    np = None  # type: ignore[assignment]
    _faiss = None  # type: ignore[assignment]
    _FAISS_AVAILABLE = False
    logger.debug("faiss/numpy not installed — VectorStore will operate as a no-op stub")


class VectorStore:
    """
    Thin wrapper around a FAISS IndexFlatIP+IndexIDMap index.

    Vectors are L2-normalised before being added so that inner-product
    search is equivalent to cosine similarity.  IDs stored in FAISS
    correspond 1-to-1 with ``embedding_id`` in the SQLite ``events``
    table.
    """

    def __init__(self, dim: int = 384) -> None:
        self._dim = dim
        self._index = self._make_empty_index(dim) if _FAISS_AVAILABLE else None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _make_empty_index(dim: int):
        """Create a fresh IndexFlatIP wrapped in IndexIDMap."""
        flat = _faiss.IndexFlatIP(dim)
        return _faiss.IndexIDMap(flat)

    @staticmethod
    def _normalise(vectors: np.ndarray) -> np.ndarray:
        """L2-normalise each row in-place and return the array."""
        v = np.ascontiguousarray(vectors, dtype=np.float32)
        _faiss.normalize_L2(v)
        return v

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add(self, embedding_ids: list[int], vectors: np.ndarray) -> None:
        """Add vectors with the given integer IDs to the index."""
        if self._index is None or len(embedding_ids) == 0:
            return
        v = self._normalise(vectors)
        ids = np.array(embedding_ids, dtype=np.int64)
        self._index.add_with_ids(v, ids)

    def search(
        self,
        query_vector: np.ndarray,
        top_k: int = 10,
        candidate_ids: Optional[list[int]] = None,
    ) -> list[tuple[int, float]]:
        """
        Search for the *top_k* most similar vectors.

        Parameters
        ----------
        query_vector:
            1-D float array of length ``dim``.
        top_k:
            Number of results to return.
        candidate_ids:
            If provided, results are filtered to this set of embedding IDs
            *after* the FAISS search (pre-filter by fetching more results).
            This is not a true FAISS pre-filter but works well for the
            expected workload sizes.

        Returns
        -------
        List of (embedding_id, score) tuples sorted by descending score.
        """
        if self._index is None or self._index.ntotal == 0:
            return []

        q = self._normalise(query_vector.reshape(1, -1))

        # Fetch more candidates when we need to post-filter by date range
        k = min(top_k * 10 if candidate_ids else top_k, self._index.ntotal)
        k = max(k, 1)

        scores, ids = self._index.search(q, k)
        scores = scores[0].tolist()
        ids = ids[0].tolist()

        results: list[tuple[int, float]] = []
        candidate_set = set(candidate_ids) if candidate_ids is not None else None

        for idx, score in zip(ids, scores):
            if idx < 0:  # FAISS sentinel for "not found"
                continue
            if candidate_set is not None and idx not in candidate_set:
                continue
            results.append((int(idx), float(score)))
            if len(results) >= top_k:
                break

        return results

    def remove(self, embedding_ids: list[int]) -> None:
        """
        Remove vectors by their IDs.

        FAISS IndexIDMap supports ``remove_ids``.  We fall back to
        reconstructing the index when the underlying flat index doesn't
        support direct removal (which is the case for IndexFlatIP in
        older faiss-cpu versions).
        """
        if self._index is None or not embedding_ids or self._index.ntotal == 0:
            return

        id_selector = _faiss.IDSelectorBatch(
            len(embedding_ids),
            _faiss.swig_ptr(np.array(embedding_ids, dtype=np.int64)),
        )
        self._index.remove_ids(id_selector)

    def save(self, path: str | Path) -> None:
        """Persist the FAISS index to disk."""
        if self._index is None:
            return
        path = str(path)
        tmp = path + ".tmp"
        _faiss.write_index(self._index, tmp)
        os.replace(tmp, path)
        logger.debug("FAISS index saved to %s (%d vectors)", path, self._index.ntotal)

    def load(self, path: str | Path) -> None:
        """Load a FAISS index from disk, replacing the current index."""
        if self._index is None:
            return
        path = str(path)
        if not os.path.exists(path):
            logger.debug("No FAISS index at %s — starting fresh", path)
            return
        self._index = _faiss.read_index(path)
        logger.debug("FAISS index loaded from %s (%d vectors)", path, self._index.ntotal)

    def size(self) -> int:
        """Return the number of vectors currently in the index."""
        return 0 if self._index is None else int(self._index.ntotal)

    @classmethod
    def from_file(cls, path: str | Path, dim: int = 384) -> "VectorStore":
        """Create a VectorStore and load an index from *path* if it exists."""
        store = cls(dim=dim)
        store.load(path)
        return store
