"""Local Git/PostgreSQL proofs for reviewed repository compatibility refreshes."""

from __future__ import annotations

import hashlib
import json
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import sessionmaker

from llm_d_lens.benchmark_rag.guide_json import parse_custom_json
from llm_d_lens.benchmark_rag.guide_tables import REVIEWED_REVISION, parse_published_tables
from llm_d_lens.benchmark_rag.publication import RagPublicationService
from llm_d_lens.benchmark_rag.publication_contracts import (
    InventoryFile,
    PublicationError,
    ReleaseManifest,
    SourceApproval,
)
from llm_d_lens.benchmark_rag.repository_corpus import (
    PARSER_VERSION,
    RepositoryCorpusImporter,
    parse_repository,
    repository_counts,
)
from llm_d_lens.benchmark_rag.sources import BENCHMARK_ORIGIN, LLM_D_ORIGIN, allowed_paths, collect_git
from llm_d_lens.db.dao.rag_publication import RagPublicationDao

_TABLE = "guides/modelexpress-p2p/benchmark-results/h200-ib-gpt-oss-120b.md"
_JSON = "guides/p2p-kv-cache-sharing/benchmark-results/data/glm-5.2-c64/complete-policy-source-exact-window.json"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def _commit(repo: Path, files: dict[str, bytes | None]) -> str:
    for path, data in files.items():
        target = repo / path
        if data is None:
            target.unlink()
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "Local refresh fixture")
    return _git(repo, "rev-parse", "HEAD")


def _approval(origin: str) -> SourceApproval:
    return SourceApproval(
        verified_public=True,
        approved_by="isolated refresh acceptance",
        approval_reference="fixture only; not production approval",
        license_reference="fixture only; operator approval remains mandatory",
        parser_version=PARSER_VERSION,
        allowed_paths=allowed_paths(origin),
    )


@pytest.fixture
def importer(postgres_engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LENS_DATA_DIR", str(tmp_path / "evidence"))
    return RepositoryCorpusImporter(
        RagPublicationService(dao=RagPublicationDao(sessionmaker(bind=postgres_engine, expire_on_commit=False)))
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "--initial-branch=main")
    _git(root, "config", "user.name", "Fixture")
    _git(root, "config", "user.email", "fixture@example.invalid")
    return root


def _reviewed_counts(importer, repo: Path, origin: str, revision: str):
    inventory = collect_git(origin, repo, revision, _approval(origin), importer.publication.archive)
    parsed = parse_repository(
        inventory, importer.publication.archive, figure_reviews={}, reviewed_metadata_revision=revision
    )
    return repository_counts(parsed)


@pytest.mark.parametrize("origin", [LLM_D_ORIGIN, BENCHMARK_ORIGIN])
def test_refresh_add_delete_rename_edit_and_parse_failure_preserve_archive(importer, repo: Path, origin: str):
    _git(repo, "remote", "add", "origin", origin)
    prefix = "guides" if origin == LLM_D_ORIGIN else "docs"
    before = {
        f"{prefix}/edit.md": b"# Before\nOriginal content.\n",
        f"{prefix}/delete.md": b"# Delete\nRetired evidence.\n",
        f"{prefix}/rename.md": b"# Rename\nUnchanged bytes at a new path.\n",
    }
    revision = _commit(repo, before)
    kwargs = {"expected_counts": {"source": 3, "passage": 3}, "figure_reviews": {}}
    first = importer.import_repository(repo, origin, revision, _approval(origin), **kwargs)
    old_members = importer.publication.dao.list_published(origin)
    old_sources = [item for item in old_members if item.source_role == "file"]
    next_revision = _commit(
        repo,
        {
            f"{prefix}/edit.md": b"# After\nChanged content.\n",
            f"{prefix}/delete.md": None,
            f"{prefix}/rename.md": None,
            f"{prefix}/renamed.md": before[f"{prefix}/rename.md"],
            f"{prefix}/added.md": b"# Added\nNew evidence.\n",
        },
    )
    staged = importer.stage_repository(repo, origin, next_revision, _approval(origin), **kwargs)
    assert importer.publication.dao.list_published(origin) == old_members
    with pytest.raises(PublicationError, match="exact inventory revision"):
        importer.stage_repository(
            repo, origin, next_revision, _approval(origin), reviewed_metadata_revision=revision, **kwargs
        )
    assert importer.publication.dao.list_published(origin) == old_members
    refreshed = importer.publication.activate(origin, staged.item_id)
    assert refreshed.generation == first.generation + 1
    current = importer.publication.dao.list_published(origin)
    assert {item.source_path for item in current} == {
        f"{prefix}/edit.md",
        f"{prefix}/renamed.md",
        f"{prefix}/added.md",
    }
    assert any(item.heading_path == ["After"] and "Changed content." in item.passage_text for item in current)
    assert not {item.item_id for item in current} & {item.item_id for item in old_members}
    manifest = ReleaseManifest.model_validate(importer.publication.dao.head(origin).manifest.source_details)
    assert {file.path for file in manifest.inventory.files} == {item.source_path for item in current}
    for item in old_sources:
        assert importer.publication.dao.get(item.item_id) is not None
        assert importer.publication.archive.read(item.content_sha256) == before[item.source_path]
    repeated = importer.import_repository(repo, origin, next_revision, _approval(origin), **kwargs)
    assert not repeated.changed
    assert (repeated.manifest_id, repeated.generation) == (refreshed.manifest_id, refreshed.generation)
    head = importer.publication.dao.head(origin)
    broken = _commit(repo, {f"{prefix}/edit.md": b"# Broken\n```json\n{}\n"})
    with pytest.raises(PublicationError, match="unterminated"):
        importer.import_repository(repo, origin, broken, _approval(origin), **kwargs)
    assert importer.publication.dao.head(origin) == head
    assert importer.publication.dao.list_published(origin) == current


@pytest.fixture(params=[_TABLE, _JSON], ids=["published-table", "fixed-window-json"])
def published_source(request) -> tuple[str, bytes]:
    checkout = Path(__file__).resolve().parents[6] / "llm-d"
    if not (checkout / ".git").exists():
        pytest.skip("pinned published-number proofs need the separately provisioned local llm-d checkout")
    return request.param, subprocess.check_output(
        ["git", "-C", str(checkout), "show", f"{REVIEWED_REVISION}:{request.param}"]
    )


def test_published_number_refresh_requires_exact_metadata_review(importer, repo: Path, published_source):
    path, original = published_source
    _git(repo, "remote", "add", "origin", LLM_D_ORIGIN)
    revision = _commit(repo, {path: original})
    counts = _reviewed_counts(importer, repo, LLM_D_ORIGIN, revision)
    kwargs = {"expected_counts": counts, "figure_reviews": {}}
    with pytest.raises(PublicationError, match="metadata requires"):
        importer.import_repository(repo, LLM_D_ORIGIN, revision, _approval(LLM_D_ORIGIN), **kwargs)
    first = importer.import_repository(
        repo, LLM_D_ORIGIN, revision, _approval(LLM_D_ORIGIN), reviewed_metadata_revision=revision, **kwargs
    )
    old = importer.publication.dao.list_published(LLM_D_ORIGIN, kind="measurement")
    if path == _TABLE:
        assert b"| 30.3 s |" in original
        changed = original.replace(b"| 30.3 s |", b"| 31.7 s |", 1)
        old_value, new_value = Decimal("30.3"), Decimal("31.7")
    else:
        value = json.loads(original, parse_float=Decimal)
        old_value = Decimal(str(value[0]["input_ktok_s"]))
        new_value = old_value + Decimal("1.125")
        old_literal = str(value[0]["input_ktok_s"]).encode()
        # Replace the exact published numeric token, never round-trip through float.
        key = b'"input_ktok_s": '
        start = original.index(key) + len(key)
        assert original[start : start + len(old_literal)] == old_literal
        changed = original[:start] + str(new_value).encode() + original[start + len(old_literal) :]

    def select_measurement(item):
        if path == _TABLE:
            return item.unit == "s" and item.value in (old_value, new_value)
        return item.source_pointer["json_pointer"] == "/0/input_ktok_s"

    assert [item.value for item in old if select_measurement(item)] == [old_value]
    next_revision = _commit(repo, {path: changed})
    head = importer.publication.dao.head(LLM_D_ORIGIN)
    for review, message in [(None, "metadata requires"), (revision, "exact inventory revision")]:
        with pytest.raises(PublicationError, match=message):
            importer.import_repository(
                repo,
                LLM_D_ORIGIN,
                next_revision,
                _approval(LLM_D_ORIGIN),
                reviewed_metadata_revision=review,
                **kwargs,
            )
        assert importer.publication.dao.head(LLM_D_ORIGIN) == head
    refreshed = importer.import_repository(
        repo,
        LLM_D_ORIGIN,
        next_revision,
        _approval(LLM_D_ORIGIN),
        reviewed_metadata_revision=next_revision,
        **kwargs,
    )
    assert refreshed.generation == first.generation + 1
    measurements = importer.publication.dao.list_published(LLM_D_ORIGIN, kind="measurement")
    assert [item.value for item in measurements if select_measurement(item)] == [new_value]
    assert all(item.source_revision == next_revision for item in measurements)
    assert importer.publication.archive.read(hashlib.sha256(original).hexdigest()) == original
    repeated = importer.import_repository(
        repo,
        LLM_D_ORIGIN,
        next_revision,
        _approval(LLM_D_ORIGIN),
        reviewed_metadata_revision=next_revision,
        **kwargs,
    )
    assert not repeated.changed
    assert (repeated.manifest_id, repeated.generation) == (refreshed.manifest_id, refreshed.generation)
    if path == _TABLE:
        unsupported = changed.replace(b"Weight-load time per rank", b"Unknown new timing layout")
        match = "unreviewed published table layout"
    else:
        unsupported = changed.replace(b'"input_ktok_s"', b'"unknown_new_rate"', 1)
        match = "unsupported custom JSON shape"
    broken = _commit(repo, {path: unsupported})
    head = importer.publication.dao.head(LLM_D_ORIGIN)
    with pytest.raises(PublicationError, match=match):
        importer.import_repository(
            repo,
            LLM_D_ORIGIN,
            broken,
            _approval(LLM_D_ORIGIN),
            reviewed_metadata_revision=broken,
            **kwargs,
        )
    assert importer.publication.dao.head(LLM_D_ORIGIN) == head


@pytest.mark.parametrize(
    "parser,path,data",
    [
        (parse_published_tables, _TABLE, b""),
        (parse_custom_json, _JSON, b"[]"),
    ],
)
def test_numeric_adapter_review_cannot_name_another_revision(parser, path: str, data: bytes):
    file = InventoryFile(
        path=path, format="json" if path.endswith(".json") else "markdown", sha256=hashlib.sha256(data).hexdigest()
    )
    for revision in (REVIEWED_REVISION, "b" * 40):
        with pytest.raises(PublicationError, match="exact source revision"):
            parser(file, revision, data, reviewed_metadata_revision="c" * 40)
    for revision in ("main", ""):
        with pytest.raises(PublicationError, match="pinned full commit"):
            parser(file, revision, data, reviewed_metadata_revision=revision)


def test_metadata_review_does_not_replace_figure_review(importer, repo: Path):
    _git(repo, "remote", "add", "origin", LLM_D_ORIGIN)
    revision = _commit(repo, {"guides/example/benchmark-results/chart.png": b"\x89PNG\r\n\x1a\n"})
    with pytest.raises(PublicationError, match="coverage review"):
        importer.import_repository(
            repo,
            LLM_D_ORIGIN,
            revision,
            _approval(LLM_D_ORIGIN),
            reviewed_metadata_revision=revision,
            expected_counts={"source": 1, "figure": 1},
            figure_reviews={},
        )
