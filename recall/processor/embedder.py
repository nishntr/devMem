"""Embedder — sentence-transformers wrapper with background queue."""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import TYPE_CHECKING, Callable, Optional

try:
    import numpy as np
except ImportError:
    np = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from recall.storage.db import DB
    from recall.storage.vectors import VectorStore

logger = logging.getLogger(__name__)

_BATCH_SIZE = 32
_SAVE_EVERY = 100  # save FAISS index every N new vectors


class EmbedderQueue:
    """
    Queue-based background embedder.

    Events are inserted to the DB immediately (by the main thread);
    embedding happens asynchronously so collectors are never blocked.
    """

    def __init__(
        self,
        db: "DB",
        vectors: "VectorStore",
        model_name: str = "all-MiniLM-L6-v2",
        faiss_save_path: Optional[str] = None,
        get_next_embedding_id: Optional[Callable[[], int]] = None,
        set_next_embedding_id: Optional[Callable[[int], None]] = None,
    ) -> None:
        self._db = db
        self._vectors = vectors
        self._model_name = model_name
        self._faiss_save_path = faiss_save_path
        self._get_next_id = get_next_embedding_id
        self._set_next_id = set_next_embedding_id

        self._queue: queue.Queue[list[int]] = queue.Queue(maxsize=5000)
        self._worker_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._model = None  # lazy-loaded
        self._vectors_since_save = 0

        # Load the starting embedding ID from KV store
        self._next_embedding_id: int = 1
        if get_next_embedding_id:
            try:
                self._next_embedding_id = get_next_embedding_id()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def enqueue(self, event_ids: list[int]) -> None:
        """Add event IDs to the embedding queue (non-blocking)."""
        if not event_ids:
            return
        try:
            self._queue.put_nowait(event_ids)
        except queue.Full:
            logger.warning("Embedding queue full — dropping %d event IDs", len(event_ids))

    def start(self) -> None:
        """Start the background worker thread."""
        self._stop_event.clear()
        self._worker_thread = threading.Thread(
            target=self._worker,
            daemon=True,
            name="dev-recall-embedder",
        )
        self._worker_thread.start()
        logger.info("EmbedderQueue worker started (model=%s)", self._model_name)

    def stop(self) -> None:
        """Signal the worker to stop and wait for it."""
        self._stop_event.set()
        # Unblock the queue.get()
        try:
            self._queue.put_nowait([])
        except queue.Full:
            pass
        if self._worker_thread:
            self._worker_thread.join(timeout=10)
        logger.info("EmbedderQueue stopped")

    def embed_pending(self) -> int:
        """
        Synchronously embed all events with no embedding_id (for init / catch-up).
        Returns the number of events embedded.
        """
        total = 0
        while True:
            events = self._db.get_events_unembedded(limit=_BATCH_SIZE)
            if not events:
                break
            self._embed_events(events)
            total += len(events)
        return total

    def encode_query(self, text: str) -> np.ndarray:
        """Encode a query string into a normalised float32 vector."""
        model = self._load_model()
        vec = model.encode(text, normalize_embeddings=True, show_progress_bar=False)
        return np.array(vec, dtype=np.float32)

    # ------------------------------------------------------------------
    # Worker thread
    # ------------------------------------------------------------------

    def _worker(self) -> None:
        while not self._stop_event.is_set():
            try:
                event_ids = self._queue.get(timeout=2.0)
            except queue.Empty:
                continue

            if not event_ids:
                continue

            # Drain additional pending batches (up to _BATCH_SIZE total)
            while len(event_ids) < _BATCH_SIZE:
                try:
                    more = self._queue.get_nowait()
                    if more:
                        event_ids.extend(more)
                except queue.Empty:
                    break

            events = self._db.get_events_by_ids(event_ids[:_BATCH_SIZE])
            if events:
                try:
                    self._embed_events(events)
                except Exception:
                    logger.exception("Error embedding batch of %d events", len(events))

    def _embed_events(self, events) -> None:
        if not events:
            return

        model = self._load_model()
        contents = [e.content for e in events]

        try:
            vectors = model.encode(
                contents,
                normalize_embeddings=True,
                show_progress_bar=False,
                batch_size=_BATCH_SIZE,
            )
        except Exception:
            logger.exception("sentence-transformers encode failed")
            return

        vectors = np.array(vectors, dtype=np.float32)

        embedding_ids: list[int] = []
        for i, event in enumerate(events):
            emb_id = self._allocate_embedding_id()
            embedding_ids.append(emb_id)
            try:
                self._db.update_embedding_id(event.id, emb_id)
            except Exception:
                logger.exception("update_embedding_id failed for event %s", event.id)

        try:
            self._vectors.add(embedding_ids, vectors)
        except Exception:
            logger.exception("FAISS add failed")
            return

        self._vectors_since_save += len(events)
        if self._vectors_since_save >= _SAVE_EVERY and self._faiss_save_path:
            try:
                self._vectors.save(self._faiss_save_path)
                self._vectors_since_save = 0
            except Exception:
                logger.exception("FAISS save failed")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _load_model(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer  # type: ignore[import]

            logger.info("Loading sentence-transformers model: %s", self._model_name)
            self._model = SentenceTransformer(self._model_name)
        return self._model

    def _allocate_embedding_id(self) -> int:
        eid = self._next_embedding_id
        self._next_embedding_id += 1
        if self._set_next_id:
            try:
                self._set_next_id(self._next_embedding_id)
            except Exception:
                pass
        return eid
