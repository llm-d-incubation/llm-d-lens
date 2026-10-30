"""Offline tests for bounded encoding, cache identity and exact parent offsets."""

import hashlib
import json
import math
import threading
from unittest.mock import Mock

import pytest

from llm_d_lens.agentic.document_rag import embedding
from llm_d_lens.agentic.document_rag.embedding import (
    CONTRACT,
    LocalE5Encoder,
    passage_input,
    split_passage,
    validate_vector,
    verify_model_files,
)
from llm_d_lens.agentic.document_rag.search_contracts import EvidenceSearchError, PublicPassageDocument
from llm_d_lens.agentic.document_rag.sources import BENCHMARK_ORIGIN


def parent(text):
    return PublicPassageDocument(
        chunk_id="parent",
        evidence_id="parent",
        origin_id=BENCHMARK_ORIGIN,
        manifest_id="manifest",
        publication_generation=1,
        source_revision="a" * 40,
        content_sha256=hashlib.sha256(text.encode()).hexdigest(),
        source_path="docs/example.md",
        title="Example",
        heading_path=("Example",),
        text=text,
    )


def character_tokenizer(text, **kwargs):
    return {
        "input_ids": [0, *range(len(text)), 2],
        "offset_mapping": [(0, 0), *((index, index + 1) for index in range(len(text))), (0, 0)],
    }


@pytest.mark.parametrize("text", ["# Example\nshort", ("中文 text\n" * 50), "x" * 1000])
def test_chunks_cover_every_character_without_truncation_and_keep_stable_offsets(text):
    tokenizer = Mock(side_effect=character_tokenizer)
    contract = CONTRACT.model_copy(update={"max_tokens": 64})
    chunks = split_passage(parent(text), tokenizer, contract)
    assert "".join(chunk.text for chunk in chunks) == text
    assert chunks == split_passage(parent(text), tokenizer, contract)
    assert len({chunk.chunk_id for chunk in chunks}) == len(chunks)
    for index, chunk in enumerate(chunks):
        assert chunk.chunk_index == index
        assert chunk.text == text[chunk.chunk_start : chunk.chunk_end]
        assert len(character_tokenizer(passage_input(chunk))["input_ids"]) <= 64
        assert chunk.embedding_input_sha256 == hashlib.sha256(passage_input(chunk).encode()).hexdigest()


def test_table_headers_and_code_fences_are_repeated_only_as_embedding_context():
    text = "| col | value |\n| --- | --- |\n" + "| a | bbbbb |\n" * 30
    chunks = split_passage(
        parent(text), Mock(side_effect=character_tokenizer), CONTRACT.model_copy(update={"max_tokens": 100})
    )
    assert any("| col | value |" in chunk.embedding_context for chunk in chunks[1:])
    assert "".join(chunk.text for chunk in chunks) == text
    fenced = "```python\n" + "print(1)\n" * 50 + "```\n"
    chunks = split_passage(
        parent(fenced), Mock(side_effect=character_tokenizer), CONTRACT.model_copy(update={"max_tokens": 100})
    )
    assert any(chunk.embedding_context == "```python\n" for chunk in chunks[1:])
    assert "".join(chunk.text for chunk in chunks) == fenced


@pytest.mark.parametrize("vector", [(1.0,), (math.nan, 0), (math.inf, 0), (0.0, 0.0), (True, 0)])
def test_invalid_vectors_fail_explicitly(vector):
    with pytest.raises(EvidenceSearchError):
        validate_vector(vector, 2)


def cached_encoder(tmp_path):
    encoder = LocalE5Encoder.__new__(LocalE5Encoder)
    encoder.contract = CONTRACT.model_copy(update={"dimension": 2})
    encoder.cache_path = tmp_path
    encoder.batch_size = 2
    encoder.encoded_inputs = 0
    encoder.cache_hits = 0
    encoder._lock = threading.Lock()
    encoder._infer = Mock(side_effect=lambda texts: tuple((1.0, 0.0) for _ in texts))
    return encoder


def test_bounded_batches_deduplication_replay_change_and_contract_invalidation(tmp_path):
    encoder = cached_encoder(tmp_path)
    texts = ("passage: first", "passage: second", "passage: first", "passage: third")
    assert len(encoder.encode(texts)) == 4
    assert encoder.encoded_inputs == 3
    assert [len(call.args[0]) for call in encoder._infer.call_args_list] == [2, 1]
    encoder.encode(texts)
    assert encoder.encoded_inputs == 3 and encoder.cache_hits == 3
    encoder.encode(("passage: changed",))
    assert encoder.encoded_inputs == 4
    encoder.contract = encoder.contract.model_copy(update={"encoding_version": "new-version"})
    encoder.encode(("passage: first",))
    assert encoder.encoded_inputs == 5
    encoder.contract = encoder.contract.model_copy(update={"model_revision": "b" * 40})
    encoder.encode(("passage: first",))
    assert encoder.encoded_inputs == 6


@pytest.mark.parametrize("corruption", ["json", "key", "dimension", "nan"])
def test_corrupt_cache_is_an_error_until_explicitly_rebuilt(tmp_path, corruption):
    encoder = cached_encoder(tmp_path)
    text = "query: example"
    encoder.encode((text,))
    path = tmp_path / (encoder._key(text) + ".json")
    payload = json.loads(path.read_text())
    if corruption == "json":
        path.write_text("{")
    else:
        if corruption == "key":
            payload["key"] = "wrong"
        else:
            payload["vector"] = [1] if corruption == "dimension" else [math.nan, 0]
        path.write_text(json.dumps(payload))
    with pytest.raises(EvidenceSearchError):
        encoder.encode((text,))
    assert encoder.encode((text,), rebuild_cache=True) == ((1.0, 0.0),)
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("texts", [(), ("",), ("missing prefix",), ("query: ",), ("passage: \n",)])
def test_invalid_inputs_do_not_become_successful_empty_vectors(tmp_path, texts):
    with pytest.raises(EvidenceSearchError):
        cached_encoder(tmp_path).encode(texts)


def test_model_files_are_hash_pinned_not_trusted_by_directory_name(tmp_path, monkeypatch):
    data = b"pinned model fixture"
    monkeypatch.setattr(embedding, "PINNED_FILES", {"model.safetensors": hashlib.sha256(data).hexdigest()})
    path = tmp_path / "model.safetensors"
    path.write_bytes(data)
    verify_model_files(tmp_path)
    path.write_bytes(b"wrong revision")
    with pytest.raises(EvidenceSearchError, match="pinned revision"):
        verify_model_files(tmp_path)
