"""Gemini embeddings provider — issue #223 follow-up.

Implements Google's Embeddings docs correctly:

* gemini-embedding-001: task_type via API field (RETRIEVAL_DOCUMENT/QUERY etc.),
  2048 token limit, 128-3072 dims via outputDimensionality (MRL).
* gemini-embedding-2: DO NOT send task_type — instead prefix prompt with
  "task: {task} | query: {content}" (or document), 8192 token limit,
  same MRL dims. Embedding spaces 001 vs 2 are INCOMPATIBLE.

Env:
  GEMINI_API_KEY or GOOGLE_API_KEY
  GEMINI_EMBEDDING_MODEL (default: models/gemini-embedding-2)
  GEMINI_EMBEDDING_DIM (optional outputDimensionality, e.g. 768, 3072)

API: https://generativelanguage.googleapis.com/v1beta/models/{model}:batchEmbedContents
"""

from __future__ import annotations

import os
import time
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Per https://ai.google.dev/gemini-api/docs/embeddings — April 2026
# gemini-embedding-2 is GA (multimodal, 8192 tokens), 001 is previous stable (2048 tokens).
# Spaces are incompatible — don't mix.
DEFAULT_GEMINI_MODEL = "models/gemini-embedding-2"  # Gemini Embeddings 2 — you asked for 2
FALLBACK_MODEL = "models/gemini-embedding-001"
LEGACY_MODEL = "models/text-embedding-004"
DEFAULT_TASK_TYPE = "RETRIEVAL_DOCUMENT"
BATCH_SIZE = 100
MAX_RETRIES = 3
TIMEOUT_S = 30.0


def _api_key() -> str | None:
    return os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or os.getenv("GOOGLE_GENAI_API_KEY")


def _model_name(raw: str | None) -> str:
    m = (raw or os.getenv("GEMINI_EMBEDDING_MODEL") or DEFAULT_GEMINI_MODEL).strip()
    if not m.startswith("models/"):
        m = f"models/{m}"
    return m


def _output_dim() -> int | None:
    val = os.getenv("GEMINI_EMBEDDING_DIM") or os.getenv("GEMINI_OUTPUT_DIMENSIONALITY")
    if val and val.strip().isdigit():
        return int(val.strip())
    return None


def _is_embedding_2(model_name: str) -> bool:
    base = model_name.split("/")[-1]
    return base.startswith("gemini-embedding-2") or base == "gemini-embedding-2-preview"


def _prefix_for_2(text: str, task_type: str) -> str:
    """For gemini-embedding-2, task is NOT a field — prefix prompt.

    Docs: f'task: classification | query: {content}' etc. For retrieval we use
    'task: retrieval | document: ...' / 'task: retrieval | query: ...'
    """
    tt = task_type.strip().lower()
    if tt == "retrieval_document":
        return f"task: retrieval | document: {text}"
    if tt == "retrieval_query":
        return f"task: retrieval | query: {text}"
    pretty = tt.replace("_", " ")
    return f"task: {pretty} | query: {text}"


def _chunked(seq: list[Any], size: int):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def _build_requests(batch_texts: list[str], model_name: str, task_type: str, dim: int | None, is_v2: bool) -> list[dict[str, Any]]:
    reqs: list[dict[str, Any]] = []
    for t in batch_texts:
        effective = _prefix_for_2(t, task_type) if is_v2 else t
        req: dict[str, Any] = {
            "model": model_name,
            "content": {"parts": [{"text": effective}]},
        }
        if not is_v2:
            req["taskType"] = task_type
        if dim is not None:
            req["outputDimensionality"] = dim
        reqs.append(req)
    return reqs


def gemini_embed_texts(
    texts: list[str],
    *,
    model: str | None = None,
    api_key: str | None = None,
    task_type: str = DEFAULT_TASK_TYPE,
    output_dimensionality: int | None = None,
    batch_size: int = BATCH_SIZE,
    timeout_s: float = TIMEOUT_S,
) -> list[list[float]]:
    """Embed texts via Gemini batchEmbedContents. Returns list of vectors aligned to texts."""
    if not texts:
        return []
    key = api_key or _api_key()
    if not key:
        raise RuntimeError("GEMINI_API_KEY (or GOOGLE_API_KEY) not set — cannot embed with Gemini")
    model_name = _model_name(model)
    dim = output_dimensionality if output_dimensionality is not None else _output_dim()
    is_v2 = _is_embedding_2(model_name)

    max_chars = 32000 if is_v2 else 8000
    truncated = [t[:max_chars] if len(t) > max_chars else t for t in texts]

    vectors: list[list[float]] = []
    base_url = f"https://generativelanguage.googleapis.com/v1beta/{model_name}:batchEmbedContents"

    for batch in _chunked(truncated, batch_size):
        requests = _build_requests(batch, model_name, task_type, dim, is_v2)
        payload = {"requests": requests}
        last_err: Exception | None = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                resp = httpx.post(
                    base_url,
                    params={"key": key},
                    json=payload,
                    timeout=timeout_s,
                    headers={"Content-Type": "application/json"},
                )
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise httpx.HTTPStatusError(f"retryable {resp.status_code}", request=resp.request, response=resp)  # type: ignore
                resp.raise_for_status()
                data = resp.json()
                embs = data.get("embeddings") or data.get("embedding") or []
                if isinstance(embs, dict) and "values" in embs:
                    embs = [embs]
                if len(embs) != len(batch):
                    raise RuntimeError(f"Gemini batch size mismatch: sent {len(batch)} got {len(embs)} — {data}")
                for e in embs:
                    vals = e.get("values") or e.get("embedding", {}).get("values") or []
                    if not vals:
                        raise RuntimeError(f"Missing embedding values: {e}")
                    vectors.append([float(x) for x in vals])
                last_err = None
                break
            except Exception as exc:
                last_err = exc
                msg = str(exc).lower()
                retryable = any(k in msg for k in ("429", "500", "502", "503", "504", "timeout", "temporar", "unavailable", "retryable"))
                if isinstance(exc, httpx.HTTPStatusError):
                    retryable = exc.response.status_code in (429, 500, 502, 503, 504)
                if retryable and attempt < MAX_RETRIES:
                    backoff = min(0.5 * (2**attempt), 4.0)
                    logger.warning("gemini_embed_retry", extra={"attempt": attempt + 1, "error": str(exc)[:300], "backoff": backoff})
                    time.sleep(backoff)
                    continue
                break
        if last_err is not None:
            raise RuntimeError(f"Gemini embed failed after {MAX_RETRIES+1} attempts: {last_err}") from last_err

    return vectors


def gemini_embed_query(
    text: str,
    *,
    model: str | None = None,
    api_key: str | None = None,
    output_dimensionality: int | None = None,
) -> list[float]:
    """Embed single query with RETRIEVAL_QUERY task type (better for search)."""
    vals = gemini_embed_texts(
        [text],
        model=model,
        api_key=api_key,
        task_type="RETRIEVAL_QUERY",
        output_dimensionality=output_dimensionality,
        batch_size=1,
    )
    return vals[0] if vals else []


def make_gemini_provider(
    *,
    model: str | None = None,
    api_key: str | None = None,
    task_type: str = DEFAULT_TASK_TYPE,
    output_dimensionality: int | None = None,
):
    """Factory returning provider(texts: list[str]) -> list[list[float]] for build_embeddings."""
    def provider(texts: list[str]) -> list[list[float]]:
        return gemini_embed_texts(
            texts,
            model=model,
            api_key=api_key,
            task_type=task_type,
            output_dimensionality=output_dimensionality,
        )
    provider.model_name = _model_name(model)  # type: ignore
    provider.task_type = task_type  # type: ignore
    return provider
