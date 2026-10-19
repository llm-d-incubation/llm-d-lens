from __future__ import annotations

import hashlib
import subprocess
from collections import Counter
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import Engine, create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateSchema, DropSchema

from llm_d_lens.benchmark_rag.publication import RagPublicationService
from llm_d_lens.benchmark_rag.publication_contracts import (
    InventoryFile,
    PinnedInventory,
    PublicationError,
    ReleaseManifest,
    SourceApproval,
    registry_id,
)
from llm_d_lens.benchmark_rag.repository_corpus import (
    PARSER_VERSION,
    FigureReview,
    RepositoryCorpusImporter,
    markdown_sections,
    parse_markdown,
    parse_repository,
)
from llm_d_lens.benchmark_rag.repository_initial import initial_figure_reviews
from llm_d_lens.benchmark_rag.sources import BENCHMARK_ORIGIN, LLM_D_ORIGIN, SourceArchive, allowed_paths
from llm_d_lens.db.dao.rag_publication import RagPublicationDao
from llm_d_lens.db.dao.rag_transfer import RagTransferDao
from llm_d_lens.db.models.rag_item import RagItemRow


def approval(origin: str) -> SourceApproval:
    return SourceApproval(
        verified_public=True,
        approved_by="isolated guide acceptance",
        approval_reference="test only; not production approval or a public endpoint",
        license_reference="fixture only; operator approval remains mandatory",
        parser_version=PARSER_VERSION,
        allowed_paths=allowed_paths(origin),
    )


def source(text: str, origin: str = LLM_D_ORIGIN) -> tuple[InventoryFile, PinnedInventory, bytes]:
    data = text.encode("utf-8")
    file = InventoryFile(
        path="guides/example.md" if origin == LLM_D_ORIGIN else "docs/example.md",
        sha256=hashlib.sha256(data).hexdigest(),
        format="markdown",
    )
    return file, PinnedInventory(origin_id=origin, revision="a" * 40, complete=True, files=(file,)), data


def test_markdown_offsets_and_repeated_headings():
    text = "# Guide\nA Unicode unit: \u00b5s.\n## Results\nfirst\n## Results\nsecond\n"
    file, inventory, data = source(text)
    items = parse_markdown(file, inventory, data)
    assert len(items) == 3
    assert len({item.item_id for item in items}) == 3
    for item in items:
        assert item.source_pointer is not None
        pointer = item.source_pointer
        assert text[pointer["start_char"] : pointer["end_char"]] == item.passage_text
        assert (
            "".join(text.splitlines(keepends=True)[pointer["start_line"] - 1 : pointer["end_line"]])
            == item.passage_text
        )
    assert items[-1].heading_path == ["Guide", "Results"]


@pytest.mark.parametrize("delimiter", ["```", "~~~~"])
def test_fenced_examples_are_not_headings_or_measurements(delimiter: str):
    file, inventory, data = source(
        f"# Definition\nReport format, not an observation.\n{delimiter}json\n# Not a heading\n"
        '{"throughput": 99.123}\n'
        f"![not evidence](./chart.png)\n{delimiter}\n## Explanation\nAggregate statistics.\n",
        BENCHMARK_ORIGIN,
    )
    items = parse_markdown(file, inventory, data)
    assert len(items) == 2
    assert all(item.kind == "passage" for item in items)
    assert items[0].source_pointer["contains_code_examples"] is True
    assert items[0].source_pointer["image_references"] == []
    assert items[0].source_pointer["evidence_role"] == "documentation"


def test_unterminated_fence_and_changed_bytes_fail():
    file, inventory, data = source("# Guide\n```json\n{}\n")
    with pytest.raises(PublicationError, match="unterminated"):
        parse_markdown(file, inventory, data)
    with pytest.raises(PublicationError, match="hash"):
        parse_markdown(file, inventory, data + b"changed")


def test_image_references_keep_captions_and_do_not_follow_urls():
    text = (
        "# Guide\n![curve](./example/benchmark-results/curve.png)\n"
        '<img src="example/benchmark-results/curve.png" alt="same pinned curve">\n'
        "![reference][plot]\n[plot]: example/benchmark-results/curve.png\n"
        "![external](https://example.invalid/curve.png)\n"
        "![escape](../../../../outside.png)\n"
    )
    file, inventory, data = source(text)
    items = parse_markdown(file, inventory, data)
    refs = items[0].source_pointer["image_references"]
    assert len(refs) == 5
    assert {ref["caption"] for ref in refs} == {"curve", "same pinned curve", "reference", "external", "escape"}
    assert sum(ref["resolved_path"] == "guides/example/benchmark-results/curve.png" for ref in refs) == 3
    assert all(ref["in_inventory"] is False for ref in refs)
    assert refs[-1]["resolved_path"] is None


def test_unresolved_image_reference_fails_explicitly():
    file, inventory, data = source("# Guide\n![plot][missing]\n")
    with pytest.raises(PublicationError, match="unresolved"):
        parse_markdown(file, inventory, data)


def test_initial_figure_review_is_explicit_and_conservative():
    reviews = initial_figure_reviews()
    assert len(reviews) == 37
    assert Counter(review.coverage for review in reviews.values()) == {
        "partial": 15,
        "summary_only": 3,
        "unknown": 19,
    }
    assert all(review.reason for review in reviews.values())
    assert all(
        "glm" not in (review.report_path or "") and "gptoss" not in path
        for path, review in reviews.items()
        if review.table_indices
    )
    with pytest.raises(PublicationError, match="explicit report"):
        FigureReview(coverage="partial", reason="invalid link", table_indices=(1,))


def test_section_parser_does_not_invent_empty_passages():
    assert markdown_sections("") == ()
    sections = markdown_sections("preface\n# Heading\n")
    assert sections[0].heading_path == ()
    assert sections[1].heading_path == ("Heading",)


@pytest.fixture
def importer(postgres_engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RepositoryCorpusImporter:
    monkeypatch.setenv("LENS_DATA_DIR", str(tmp_path / "evidence"))
    return RepositoryCorpusImporter(
        RagPublicationService(dao=RagPublicationDao(sessionmaker(bind=postgres_engine, expire_on_commit=False)))
    )


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def commit(repo: Path, text: str) -> str:
    file = repo / "guides/example.md"
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(text, encoding="utf-8")
    git(repo, "add", "guides/example.md")
    git(repo, "commit", "-m", "Pinned fixture update")
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "--initial-branch=main")
    git(root, "config", "user.name", "Fixture")
    git(root, "config", "user.email", "fixture@example.invalid")
    git(root, "remote", "add", "origin", LLM_D_ORIGIN)
    return root


def test_repository_publication_uses_pinned_git_and_is_idempotent(importer, repo: Path, postgres_engine: Engine):
    revision = commit(repo, "# Guide\nPinned introduction.\n## Configuration\nNot a measured result.\n")
    (repo / "guides/example.md").write_text("dirty worktree is not input", encoding="utf-8")
    kwargs = {"expected_counts": {"source": 1, "passage": 2}, "figure_reviews": {}}
    result = importer.import_repository(repo, LLM_D_ORIGIN, revision, approval(LLM_D_ORIGIN), **kwargs)
    assert result.counts == {"source": 1, "passage": 2}
    with postgres_engine.connect() as connection:
        total = connection.scalar(select(func.count()).select_from(RagItemRow))
    again = importer.import_repository(repo, LLM_D_ORIGIN, revision, approval(LLM_D_ORIGIN), **kwargs)
    assert again.changed is False
    assert again.manifest_id == result.manifest_id
    assert again.generation == result.generation
    with postgres_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(RagItemRow)) == total
    passages = importer.publication.dao.list_published(LLM_D_ORIGIN, kind="passage")
    assert any("Pinned introduction." in item.passage_text for item in passages)


def test_bad_counts_or_inventory_never_publish_or_register(importer, repo: Path):
    revision = commit(repo, "# Guide\nPinned content.\n")
    with pytest.raises(PublicationError, match="per-kind counts"):
        importer.stage_repository(
            repo,
            LLM_D_ORIGIN,
            revision,
            approval(LLM_D_ORIGIN),
            expected_counts={"source": 1, "passage": 999},
            figure_reviews={},
        )
    assert importer.publication.dao.get(registry_id(LLM_D_ORIGIN)) is None
    with pytest.raises(PublicationError, match="reviewed pinned inventory"):
        importer.stage_repository(
            repo,
            LLM_D_ORIGIN,
            revision,
            approval(LLM_D_ORIGIN),
            expected_counts={"source": 1, "passage": 1},
            figure_reviews={},
            expected_inventory_digest="0" * 64,
        )
    assert importer.publication.dao.get(registry_id(LLM_D_ORIGIN)) is None


def test_parser_rejects_unreviewed_figures_and_unsafe_links(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LENS_DATA_DIR", str(tmp_path / "evidence"))
    archive = SourceArchive()
    file = InventoryFile(
        path="guides/example/benchmark-results/chart.png", sha256=archive.put(b"\x89PNG\r\n\x1a\n"), format="png"
    )
    inventory = PinnedInventory(origin_id=LLM_D_ORIGIN, revision="a" * 40, complete=True, files=(file,))
    with pytest.raises(PublicationError, match="coverage review"):
        parse_repository(inventory, archive, figure_reviews={})
    with pytest.raises(PublicationError, match="explicitly reference"):
        parse_repository(
            inventory,
            archive,
            figure_reviews={
                file.path: FigureReview(coverage="unknown", reason="no verified data", report_path="guides/missing.md")
            },
        )
    parsed = parse_repository(
        inventory,
        archive,
        figure_reviews={file.path: FigureReview(coverage="unknown", reason="no verified numeric data")},
    )
    assert len(parsed[0].items) == 1
    assert parsed[0].items[0].kind == "figure"
    assert parsed[0].items[0].coverage == "unknown"


def test_activation_hash_failure_keeps_old_release(importer, repo: Path):
    kwargs = {"expected_counts": {"source": 1, "passage": 1}, "figure_reviews": {}}
    revision = commit(repo, "# Guide\nOriginal.\n")
    original = importer.import_repository(repo, LLM_D_ORIGIN, revision, approval(LLM_D_ORIGIN), **kwargs)
    revision = commit(repo, "# Guide\nNew release.\n")
    staged = importer.stage_repository(repo, LLM_D_ORIGIN, revision, approval(LLM_D_ORIGIN), **kwargs)
    source = next(
        item for item in importer.publication.dao.list_version(LLM_D_ORIGIN, revision) if item.source_role == "file"
    )
    root = importer.publication.archive._root(source.content_sha256)
    (root / "source").write_bytes(b"corrupted test-only archived file")
    with pytest.raises(PublicationError, match="hash"):
        importer.publication.activate(LLM_D_ORIGIN, staged.item_id)
    head = importer.publication.dao.head(LLM_D_ORIGIN)
    assert head.registry.published_manifest_id == original.manifest_id
    assert head.state.generation == original.generation


@pytest.mark.parametrize("origin,checkout", [(LLM_D_ORIGIN, "llm-d"), (BENCHMARK_ORIGIN, "llm-d-benchmark")])
def test_initial_repository_postgres_acceptance(
    origin: str, checkout: str, importer, postgres_engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from llm_d_lens.benchmark_rag.repository_initial import (
        INITIAL_FORMAT_COUNTS,
        INITIAL_INVENTORY_DIGESTS,
        INITIAL_ITEM_COUNTS,
        INITIAL_REVISIONS,
    )
    from llm_d_lens.benchmark_rag.transfer import RagBackupService

    repo = Path(__file__).resolve().parents[5].parent / checkout
    if not (repo / ".git").exists():
        pytest.skip("real pinned repository acceptance needs the separately provisioned local source checkout")
    kwargs = {
        "expected_counts": INITIAL_ITEM_COUNTS[origin],
        "expected_inventory_digest": INITIAL_INVENTORY_DIGESTS[origin],
        "figure_reviews": initial_figure_reviews() if origin == LLM_D_ORIGIN else {},
    }
    result = importer.import_repository(repo, origin, INITIAL_REVISIONS[origin], approval(origin), **kwargs)
    assert result.counts == INITIAL_ITEM_COUNTS[origin]
    head = importer.publication.dao.head(origin)
    manifest = ReleaseManifest.model_validate(head.manifest.source_details)
    assert dict(Counter(file.format for file in manifest.inventory.files)) == INITIAL_FORMAT_COUNTS[origin]
    assert manifest.inventory.digest == INITIAL_INVENTORY_DIGESTS[origin]
    items = importer.publication.dao.list_published(origin)
    assert dict(Counter(item.kind for item in items)) == INITIAL_ITEM_COUNTS[origin]
    by_id = {item.item_id: item for item in items}
    for item in items:
        if item.kind == "passage" and "start_char" in (item.source_pointer or {}):
            pointer = item.source_pointer
            text = importer.publication.archive.read(item.content_sha256).decode("utf-8")
            assert text[pointer["start_char"] : pointer["end_char"]] == item.passage_text
        elif item.kind == "figure_link":
            experiment = by_id[item.referenced_item_id]
            assert experiment.kind == "experiment"
            assert experiment.source_path == item.source_pointer["report_path"]
            assert "GLM" not in (experiment.model or "").upper()
        elif item.kind == "figure":
            assert item.source_pointer["numeric_extraction"] == "none"
    if origin == BENCHMARK_ORIGIN:
        assert {item.kind for item in items} == {"source", "passage"}
        assert any("rather than detailed traces" in (item.passage_text or "") for item in items)
    else:
        weight_rows = [
            item
            for item in items
            if item.kind == "measurement" and item.source_path.endswith("/h200-ib-gpt-oss-120b.md")
        ]
        assert {Decimal("0.93"), Decimal("30.3")}.issubset({item.value for item in weight_rows if item.unit == "s"})
        assert sum(item.kind == "figure" for item in items) == 37
        assert len({item.source_path for item in items if item.kind == "source" and item.format == "json"}) == 5
        assert (
            sum(
                item.kind == "source" and item.format == "markdown" and "/benchmark-results/" in item.source_path
                for item in items
            )
            == 20
        )
    with postgres_engine.connect() as connection:
        total = connection.scalar(select(func.count()).select_from(RagItemRow))
    repeated = importer.import_repository(repo, origin, INITIAL_REVISIONS[origin], approval(origin), **kwargs)
    assert repeated.changed is False
    assert repeated.manifest_id == result.manifest_id
    assert repeated.generation == result.generation
    with postgres_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(RagItemRow)) == total
    bundle = tmp_path / "repository-rag.zip"
    backup = RagBackupService(RagTransferDao(sessionmaker(bind=postgres_engine, expire_on_commit=False))).backup(bundle)
    assert backup.row_count == total
    assert RagBackupService.verify(bundle).backup_id == backup.backup_id
    schema = f"rag_guide_restore_{uuid4().hex}"
    with postgres_engine.begin() as connection:
        connection.execute(CreateSchema(schema))
    target = create_engine(postgres_engine.url.update_query_dict({"options": f"-csearch_path={schema}"}))
    try:
        dao = RagTransferDao(sessionmaker(bind=target, expire_on_commit=False))
        dao.prepare(initialize=True)
        monkeypatch.setenv("LENS_DATA_DIR", str(tmp_path / "restored-evidence"))
        service = RagBackupService(dao)
        restored = service.restore(bundle)
        assert restored.inserted == total
        assert dao.list_published(origin) == items
        repeated_restore = service.restore(bundle)
        assert repeated_restore.inserted == repeated_restore.updated_sources == 0
        for file in manifest.inventory.files:
            assert hashlib.sha256(SourceArchive().read(file.sha256)).hexdigest() == file.sha256
    finally:
        target.dispose()
        with postgres_engine.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))
