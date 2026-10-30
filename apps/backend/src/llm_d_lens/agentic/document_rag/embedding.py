"""Pinned, offline CPU encoding and rebuildable content-addressed vector cache."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import TYPE_CHECKING

from llm_d_lens.agentic.document_rag.search_contracts import (
    EmbeddingContract,
    EvidenceSearchError,
    PublicPassageDocument,
)
from llm_d_lens.infrastructure.storage.paths import storage_path

if TYPE_CHECKING:
    from transformers import PreTrainedTokenizerFast

CONTRACT = EmbeddingContract()
_LOAD_LOCK = threading.Lock()
PINNED_FILES = {
    "model.safetensors": "1a55775f53449dac10a2bcbc312469fac40b96d53198c407081a831f81c98477",
    "tokenizer.json": "0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39",
    "config.json": "69137736cab8b8903a07fe8afaafdda25aac55415a12a55d1bffa9f581abf959",
    "tokenizer_config.json": "a1d6bc8734a6f635dc158508bef000f8e2e5a759c7d92f984b2c86e5ff53425b",
    "special_tokens_map.json": "d05497f1da52c5e09554c0cd874037a083e1dc1b9cfd48034d1c717f1afc07a7",
}


def verify_model_files(path: Path) -> None:
    for name, expected in PINNED_FILES.items():
        with (path / name).open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
                raise EvidenceSearchError("Local model files differ from the pinned revision")


def validate_vector(vector: Sequence[float], dimension: int) -> tuple[float, ...]:
    values = tuple(vector)
    if (
        len(values) != dimension
        or any(
            isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
            for value in values
        )
        or not math.isclose(sum(value * value for value in values), 1.0, abs_tol=0.002)
    ):
        raise EvidenceSearchError("Embedding must have the declared dimension, finite values and unit norm")
    return values


def passage_input(document: PublicPassageDocument) -> str:
    return "passage: " + document.embedding_context + document.text


def located_chunk(
    parent: PublicPassageDocument, start: int, end: int, ordinal: int, context: str, contract: EmbeddingContract
) -> PublicPassageDocument:
    text = parent.text[start:end]
    identity = f"{parent.evidence_id}:{contract.chunking_version}:{start}:{end}:{ordinal}"
    digest = hashlib.sha256(("passage: " + context + text).encode()).hexdigest()
    return PublicPassageDocument(
        **{
            **parent.model_dump(),
            "chunk_id": hashlib.sha256(identity.encode()).hexdigest(),
            "text": text,
            "chunk_start": start,
            "chunk_end": end,
            "chunk_index": ordinal,
            "chunking_version": contract.chunking_version,
            "embedding_input_sha256": digest,
            "embedding_context": context,
        }
    )


def structural_context(text: str, start: int) -> str:
    """Repeat table headers and an enclosing fence in embedding input, not citations."""
    before = text[:start]
    fences = re.findall(r"(?m)^(`{3,}|~{3,})([^\n]*)", before)
    if len(fences) % 2:
        marker, language = fences[-1]
        return marker + language + "\n"
    if text[start:].lstrip().startswith("|"):
        headers = re.findall(r"(?m)^(\|[^\n]*\n\|[ :|\t-]+\|[ \t]*\n)", before)
        if headers:
            return headers[-1]
    return ""


def split_passage(
    parent: PublicPassageDocument, tokenizer: PreTrainedTokenizerFast, contract: EmbeddingContract = CONTRACT
) -> tuple[PublicPassageDocument, ...]:
    chunks = []
    start = 0
    while start < len(parent.text):
        context = structural_context(parent.text, start)
        prefix = "passage: " + context
        remaining = parent.text[start:]
        encoded = tokenizer(
            prefix + remaining, add_special_tokens=True, return_offsets_mapping=True, truncation=False, verbose=False
        )
        if len(encoded["input_ids"]) <= contract.max_tokens:
            end = len(parent.text)
        else:
            offsets = encoded["offset_mapping"]
            # Reserve the closing special token; offsets address the original Unicode string.
            boundary = offsets[contract.max_tokens - 2][1] - len(prefix)
            if boundary <= 0:
                raise EvidenceSearchError("Repeated structural context exceeds the embedding token budget")
            end = start + boundary
            newline = parent.text.rfind("\n", start, end)
            if newline > start + boundary // 2:
                end = newline + 1
        while (
            end > start
            and len(tokenizer(prefix + parent.text[start:end], add_special_tokens=True, truncation=False)["input_ids"])
            > contract.max_tokens
        ):
            end -= 1
        if end <= start:
            raise EvidenceSearchError("Cannot split passage within the embedding token budget")
        chunks.append(located_chunk(parent, start, end, len(chunks), context, contract))
        start = end
    return tuple(chunks)


class LocalE5Encoder:
    def __init__(self, model_path: Path, cache_path: Path, *, batch_size: int = 8, threads: int = 2) -> None:
        if not 1 <= batch_size <= 32 or not 1 <= threads <= 4:
            raise ValueError("Embedding batch size must be 1-32 and CPU threads 1-4")
        self.contract = CONTRACT
        self.model_path = model_path
        self.cache_path = cache_path
        self.batch_size = batch_size
        self.encoded_inputs = 0
        self.cache_hits = 0
        self._lock = threading.Lock()
        try:
            verify_model_files(model_path)
            import torch
            from transformers import AutoModel, AutoTokenizer

            torch.set_num_threads(threads)
            self.tokenizer = AutoTokenizer.from_pretrained(model_path, local_files_only=True, trust_remote_code=False)
            if not self.tokenizer.is_fast:
                raise ValueError("Offset-preserving fast tokenizer is required")
            self.model = (
                AutoModel.from_pretrained(
                    model_path, local_files_only=True, trust_remote_code=False, use_safetensors=True
                )
                .to("cpu")
                .eval()
            )
            if self.model.config.hidden_size != self.contract.dimension:
                raise ValueError("Model dimension differs from the pinned contract")
        except (ImportError, OSError, ValueError, RuntimeError) as error:
            raise EvidenceSearchError(
                "Pinned local embedding model or optional rag-embedding dependencies unavailable"
            ) from error

    def chunks(self, document: PublicPassageDocument) -> tuple[PublicPassageDocument, ...]:
        return split_passage(document, self.tokenizer, self.contract)

    def _key(self, text: str) -> str:
        return hashlib.sha256((self.contract.model_dump_json() + "\n" + text).encode()).hexdigest()

    def _infer(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        import torch

        inputs = self.tokenizer(list(texts), padding=True, truncation=False, return_tensors="pt", verbose=False)
        if inputs["input_ids"].shape[1] > self.contract.max_tokens:
            raise EvidenceSearchError("Embedding input exceeds the pinned token budget; truncation is disabled")
        with torch.inference_mode():
            output = self.model(**inputs).last_hidden_state
            mask = inputs["attention_mask"].unsqueeze(-1)
            pooled = (output * mask).sum(dim=1) / mask.sum(dim=1)
            vectors = torch.nn.functional.normalize(pooled, p=2, dim=1).tolist()
        if len(vectors) != len(texts):
            raise EvidenceSearchError("Embedding model returned an incomplete batch")
        return tuple(validate_vector(vector, self.contract.dimension) for vector in vectors)

    def encode(self, texts: Sequence[str], *, rebuild_cache: bool = False) -> tuple[tuple[float, ...], ...]:
        if not texts or any(
            not any(text.startswith(prefix) and text[len(prefix) :].strip() for prefix in ("query: ", "passage: "))
            for text in texts
        ):
            raise EvidenceSearchError("Embedding inputs must be nonempty and carry a query or passage prefix")
        with self._lock:
            try:
                return self._encode(texts, rebuild_cache=rebuild_cache)
            except (OSError, ValueError, TypeError, KeyError, RuntimeError) as error:
                raise EvidenceSearchError(
                    "Embedding inference or cache failed; explicitly rebuild a corrupt cache"
                ) from error

    def _encode(self, texts: Sequence[str], *, rebuild_cache: bool) -> tuple[tuple[float, ...], ...]:
        self.cache_path.mkdir(parents=True, exist_ok=True)
        vectors: dict[str, tuple[float, ...]] = {}
        missing = []
        for text in dict.fromkeys(texts):
            key = self._key(text)
            path = self.cache_path / (key + ".json")
            if path.exists() and not rebuild_cache:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if payload["key"] != key:
                    raise EvidenceSearchError("Embedding cache identity is corrupt; explicit rebuild required")
                vectors[text] = validate_vector(payload["vector"], self.contract.dimension)
                self.cache_hits += 1
            else:
                missing.append(text)
        for offset in range(0, len(missing), self.batch_size):
            batch = missing[offset : offset + self.batch_size]
            encoded = self._infer(batch)
            for text, vector in zip(batch, encoded, strict=True):
                key = self._key(text)
                temporary = None
                try:
                    with NamedTemporaryFile(mode="w", dir=self.cache_path, suffix=".tmp", delete=False) as stream:
                        temporary = Path(stream.name)
                        json.dump({"key": key, "vector": vector}, stream, allow_nan=False)
                    temporary.replace(self.cache_path / (key + ".json"))
                finally:
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)
                vectors[text] = vector
            self.encoded_inputs += len(batch)
        return tuple(vectors[text] for text in texts)

    def query(self, text: str) -> tuple[float, ...]:
        if not text.strip():
            raise EvidenceSearchError("Embedding query must not be blank")
        return self.encode(("query: " + text,))[0]


@lru_cache(maxsize=1)
def _load_encoder(path: str) -> LocalE5Encoder:
    return LocalE5Encoder(Path(path), storage_path("cache", "benchmark-rag", "embeddings"))


def configured_encoder() -> LocalE5Encoder:
    path = os.environ.get("LENS_PUBLIC_RAG_MODEL_PATH") or str(
        storage_path("cache", "models", "multilingual-e5-small", CONTRACT.model_revision)
    )
    with _LOAD_LOCK:
        return _load_encoder(path)
