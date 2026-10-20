from __future__ import annotations

import hashlib
import os
import posixpath
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine
from sqlalchemy.orm import sessionmaker

from llm_d_lens.auth.contracts import Principal, RoleBinding, ScopeType
from llm_d_lens.benchmark_rag.contracts import RagItem
from llm_d_lens.benchmark_rag.evaluation import EvaluationCase, EvaluationSuite, run_evaluation
from llm_d_lens.benchmark_rag.prism import PARSER_VERSION as PRISM_PARSER_VERSION
from llm_d_lens.benchmark_rag.prism import PrismSnapshotImporter
from llm_d_lens.benchmark_rag.prism_initial import INITIAL_EXPERIMENTS, INITIAL_FILES, INITIAL_REVISION
from llm_d_lens.benchmark_rag.publication import RagPublicationService
from llm_d_lens.benchmark_rag.publication_contracts import (
    PublicationError,
    ReleaseManifest,
    SourceApproval,
    item_id,
    registry_id,
)
from llm_d_lens.benchmark_rag.query_contracts import (
    QUERY_PERMISSION,
    Citation,
    ExperimentContext,
    QueryEvidence,
    QueryFilters,
    QueryResponse,
)
from llm_d_lens.benchmark_rag.repository_corpus import PARSER_VERSION, RepositoryCorpusImporter, markdown_sections
from llm_d_lens.benchmark_rag.repository_initial import (
    INITIAL_INVENTORY_DIGESTS,
    INITIAL_ITEM_COUNTS,
    INITIAL_REVISIONS,
    initial_figure_reviews,
)
from llm_d_lens.benchmark_rag.service import PublicRagQueryService, _verify_measurement
from llm_d_lens.benchmark_rag.sources import BENCHMARK_ORIGIN, LLM_D_ORIGIN, PRISM_ORIGIN, allowed_paths
from llm_d_lens.core.exceptions import DomainError
from llm_d_lens.db.dao.rag_query import QueryMatch, RagQueryDao
from llm_d_lens.infrastructure.artifacts.store import artifact_uri

_ROOT = Path(__file__).resolve().parents[5]
_SUITE = Path(__file__).resolve().parents[2] / "fixtures/benchmark_rag/public_baseline_v1.json"
_ORIGINS = (PRISM_ORIGIN, LLM_D_ORIGIN, BENCHMARK_ORIGIN)


def _reader(*, authorized: bool = True) -> Principal:
    return Principal(
        user_id="public-baseline-reader",
        username="public-baseline-reader",
        bindings=(RoleBinding(role="baseline", scope_type=ScopeType.GLOBAL),),
        role_permissions={"baseline": frozenset({QUERY_PERMISSION}) if authorized else frozenset()},
    )


def _approval(origin: str, parser: str) -> SourceApproval:
    return SourceApproval(
        verified_public=True,
        approved_by="isolated fixed public baseline",
        approval_reference="local test only; not production approval",
        license_reference="existing reviewed public inputs",
        parser_version=parser,
        allowed_paths=allowed_paths(origin),
    )


@dataclass
class _Corpus:
    dao: RagQueryDao
    service: PublicRagQueryService
    publication: RagPublicationService


@pytest.fixture
def public_baseline_corpus(postgres_engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Corpus:
    snapshot = _ROOT / "private/prism-public-2026-10-13"
    repos = {LLM_D_ORIGIN: _ROOT.parent / "llm-d", BENCHMARK_ORIGIN: _ROOT.parent / "llm-d-benchmark"}
    if not snapshot.exists() or any(not (repo / ".git").exists() for repo in repos.values()):
        pytest.skip("public baseline needs the separately provisioned pinned local three-corpus inputs")
    monkeypatch.setenv("LENS_DATA_DIR", str(tmp_path / "public-baseline-evidence"))
    dao = RagQueryDao(sessionmaker(bind=postgres_engine, expire_on_commit=False))
    service = PublicRagQueryService(dao=dao)
    publication = RagPublicationService(dao=dao, archive=service.archive)
    PrismSnapshotImporter(publication).import_snapshot(
        snapshot,
        INITIAL_REVISION,
        INITIAL_FILES,
        _approval(PRISM_ORIGIN, PRISM_PARSER_VERSION),
        expected_experiments=INITIAL_EXPERIMENTS,
    )
    importer = RepositoryCorpusImporter(publication)
    for origin, repo in repos.items():
        importer.import_repository(
            repo,
            origin,
            INITIAL_REVISIONS[origin],
            _approval(origin, PARSER_VERSION),
            expected_counts=INITIAL_ITEM_COUNTS[origin],
            expected_inventory_digest=INITIAL_INVENTORY_DIGESTS[origin],
            figure_reviews=initial_figure_reviews() if origin == LLM_D_ORIGIN else {},
        )
    return _Corpus(dao=dao, service=service, publication=publication)


class _PublicCitationVerifier:
    """Resolve emitted identities against published DAO rows and retained source bytes."""

    def __init__(self, corpus: _Corpus) -> None:
        self.corpus = corpus
        self.members = {
            origin: {item.item_id: item for item in corpus.dao.list_published(origin)} for origin in _ORIGINS
        }

    def _bytes(self, item: RagItem) -> bytes:
        members = self.members[item.origin_id]
        source = members[item_id(item.origin_id, f"file:{item.source_path}", item.source_revision)]
        assert source.kind == "source" and source.source_role == "file"
        assert (source.origin_id, source.source_revision, source.source_path, source.content_sha256) == (
            item.origin_id,
            item.source_revision,
            item.source_path,
            item.content_sha256,
        )
        assert item.parent_id in members
        data = self.corpus.service.archive.read(source.content_sha256)
        assert hashlib.sha256(data).hexdigest() == source.content_sha256
        return data

    def _passage(self, item: RagItem) -> None:
        text = self._bytes(item).decode("utf-8")
        pointer = item.source_pointer or {}
        start, end = pointer["start_char"], pointer["end_char"]
        assert type(start) is int and type(end) is int and 0 <= start < end <= len(text)
        assert text[start:end] == item.passage_text
        section = next(section for section in markdown_sections(text) if section.start == start)
        assert (section.end, section.start_line, section.end_line, section.heading_path) == (
            end,
            pointer["start_line"],
            pointer["end_line"],
            tuple(item.heading_path or ()),
        )

    def _context(self, context: ExperimentContext, origin: str) -> RagItem:
        item = self.members[origin][context.item_id]
        assert item.kind == "experiment" and item.origin_id == origin
        trusted_context = {
            name: getattr(item, name)
            for name in ("item_id", "origin_id", "model", "hardware", "runtime", "workload", "configuration")
        } | {"source_pointer": item.source_pointer or {}}
        assert context.model_dump() == trusted_context
        data = self._bytes(item)
        pointer = item.source_pointer or {}
        if "line" in pointer:
            lines = data.decode("utf-8").splitlines()
            assert 1 <= pointer["line"] <= len(lines)
            assert "|" in lines[pointer["line"] - 1]
        return item

    def _figure(self, item: RagItem, evidence: QueryEvidence, case: EvaluationCase) -> None:
        assert self._bytes(item).startswith(b"\x89PNG\r\n\x1a\n")
        assert evidence.value is None and evidence.experiment is None
        assert evidence.coverage == item.coverage
        assert evidence.missing_series == tuple(item.missing_series or ())
        pointer = item.source_pointer or {}
        assert pointer["numeric_extraction"] == "none"
        references = pointer["references"]
        assert isinstance(references, list) and references
        for reference in references:
            assert isinstance(reference, dict)
            passage = self.members[item.origin_id][reference["passage_id"]]
            assert passage.kind == "passage"
            assert passage.source_path == reference["report_path"]
            assert passage.content_sha256 == reference["report_sha256"]
            assert passage.heading_path == reference["heading_path"]
            self._passage(passage)
            line = self._bytes(passage).decode("utf-8").splitlines()[reference["line"] - 1]
            assert reference["target"] in line and reference["caption"] in line
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(passage.source_path), reference["target"]))
            assert resolved == item.source_path == reference["resolved_path"]
        links = [
            member
            for member in self.members[item.origin_id].values()
            if member.kind == "figure_link" and member.parent_id == item.item_id
        ]
        expected = set()
        for link in links:
            experiment = self.members[item.origin_id][link.referenced_item_id]
            if (
                case.request.filters.hardware is not None
                and (experiment.hardware or "").casefold() != case.request.filters.hardware.casefold()
            ):
                continue
            expected.add(experiment.item_id)
            self._bytes(link)
            assert (link.source_pointer or {})["report_sha256"] == experiment.content_sha256
            assert (link.source_pointer or {})["report_path"] == experiment.source_path
            assert (link.source_pointer or {})["table_index"] == (experiment.source_pointer or {})["table_index"]
        assert {context.item_id for context in evidence.related_experiments} == expected
        for context in evidence.related_experiments:
            self._context(context, item.origin_id)

    def __call__(self, case: EvaluationCase, evidence: QueryEvidence) -> tuple[str, ...]:
        try:
            assert evidence.origin == "public" and not evidence.corroborating_citations
            citation = evidence.citation
            assert isinstance(citation, Citation)
            item = self.corpus.dao.get(evidence.item_id)
            assert item is not None and item == self.members[citation.origin_id][evidence.item_id]
            head = self.corpus.dao.head(item.origin_id)
            assert head.state.enabled and head.manifest is not None
            assert item.item_id in (head.manifest.manifest_item_ids or ())
            trusted_citation = {
                "origin_id": item.origin_id,
                "source_revision": item.source_revision,
                "source_path": item.source_path,
                "sha256": item.content_sha256,
                "artifact_uri": artifact_uri("benchmark-rag", item.content_sha256, "source"),
                "source_pointer": item.source_pointer or {},
                "heading_path": tuple(item.heading_path or ()),
                "published_manifest_id": head.registry.published_manifest_id,
            }
            assert citation.model_dump() == trusted_citation
            data = self._bytes(item)
            assert evidence.text == ((item.passage_text or item.caption or "")[:2500] or None)
            for name in (
                "metric",
                "statistic",
                "value",
                "unit",
                "normalized_value",
                "normalized_unit",
                "normalization_rule",
                "series",
            ):
                assert getattr(evidence, name) == getattr(item, name)
            if item.kind == "measurement":
                assert evidence.evidence_type == item.evidence_type
                assert evidence.experiment is not None and not evidence.related_experiments
                experiment = self._context(evidence.experiment, item.origin_id)
                assert experiment.item_id == item.experiment_id
                source = self.members[item.origin_id][
                    item_id(item.origin_id, f"file:{item.source_path}", item.source_revision)
                ]
                _verify_measurement(
                    QueryMatch(item, experiment, source, citation.published_manifest_id, head.state.generation),
                    data,
                )
                cited_item = item.model_copy(update={"source_pointer": citation.source_pointer})
                _verify_measurement(
                    QueryMatch(cited_item, experiment, source, citation.published_manifest_id, head.state.generation),
                    data,
                )
            elif item.kind == "passage":
                assert evidence.evidence_type == "prose_summary" and evidence.value is None
                assert evidence.experiment is None and not evidence.related_experiments
                self._passage(item)
            else:
                assert item.kind == "figure" and evidence.evidence_type == "image_trend"
                self._figure(item, evidence, case)
        except (AssertionError, KeyError, IndexError, TypeError, ValueError, OSError, DomainError):
            return ("public_citation_not_verified_against_source",)
        return ()


def test_public_baseline_is_fixed_and_covers_all_public_categories() -> None:
    suite = EvaluationSuite.model_validate_json(_SUITE.read_bytes())
    assert suite.name == "public-baseline-v1"
    assert len(suite.cases) == 20
    assert {case.category for case in suite.cases} == {
        "numeric",
        "conditions",
        "definition",
        "figure_gap",
        "false_join",
        "comparison",
        "access",
        "provenance",
        "overlap",
    }
    assert set(suite.source_pins) == set(_ORIGINS)
    assert suite.source_pins[PRISM_ORIGIN].revision == INITIAL_REVISION
    for origin in (LLM_D_ORIGIN, BENCHMARK_ORIGIN):
        assert suite.source_pins[origin].revision == INITIAL_REVISIONS[origin]
        assert suite.source_pins[origin].inventory_sha256 == INITIAL_INVENTORY_DIGESTS[origin]
    assert all(not case.request.include_internal for case in suite.cases)


def test_real_three_corpus_fixed_public_baseline(public_baseline_corpus: _Corpus) -> None:
    corpus = public_baseline_corpus
    suite = EvaluationSuite.model_validate_json(_SUITE.read_bytes())
    for origin, pin in suite.source_pins.items():
        head = corpus.dao.head(origin)
        assert head.state.enabled and head.manifest is not None
        assert head.registry.published_manifest_id == head.manifest.item_id
        manifest = ReleaseManifest.model_validate(head.manifest.source_details)
        assert manifest.inventory.origin_id == origin
        assert manifest.inventory.revision == head.manifest.source_revision == pin.revision
        if pin.inventory_sha256 is not None:
            assert manifest.inventory.digest == pin.inventory_sha256
        if origin == PRISM_ORIGIN:
            assert {file.path: file.sha256 for file in manifest.inventory.files} == INITIAL_FILES
    verify = _PublicCitationVerifier(corpus)
    emitted: list[tuple[EvaluationCase, QueryEvidence]] = []

    def execute(case: EvaluationCase) -> QueryResponse:
        if case.actor in ("inactive_source", "revoked_source"):
            origin = PRISM_ORIGIN if case.actor == "inactive_source" else LLM_D_ORIGIN
            head = corpus.dao.head(origin)
            corpus.dao.set_enabled(origin, False, expected_generation=head.state.generation)
            try:
                return corpus.service.query(case.request, _reader())
            finally:
                corpus.dao.set_enabled(origin, True, expected_generation=corpus.dao.head(origin).state.generation)
        if case.actor == "reject_unapproved_source":
            origin = "unapproved:fixed-baseline-negative-control"
            counts = {approved: corpus.dao.count_published(approved) for approved in _ORIGINS}
            with pytest.raises(PublicationError, match="allowlist"):
                corpus.publication.register_source(origin, _approval(PRISM_ORIGIN, PRISM_PARSER_VERSION))
            with pytest.raises(ValidationError, match="approved public"):
                QueryFilters(origins=(origin,))
            assert corpus.dao.get(registry_id(origin)) is None
            assert {approved: corpus.dao.count_published(approved) for approved in _ORIGINS} == counts
        principal = None if case.actor == "anonymous" else _reader(authorized=case.actor != "forbidden")
        return corpus.service.query(case.request, principal)

    def verify_evidence(case: EvaluationCase, evidence: QueryEvidence) -> tuple[str, ...]:
        emitted.append((case, evidence))
        return verify(case, evidence)

    report = run_evaluation(suite, execute=execute, verify_evidence=verify_evidence)
    if directory := os.environ.get("LENS_RAG_BASELINE_REPORT_DIR"):
        destination = Path(directory).expanduser().resolve()
        destination.mkdir(parents=True, exist_ok=True)
        pending = destination / "public-baseline.json.pending"
        pending.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
        pending.replace(destination / "public-baseline.json")
    assert report.passed, report.model_dump_json(indent=2)
    assert report.total_cases == report.passed_cases == 20
    assert report.verified_citations == len(emitted) > 0
    assert {evidence.citation.origin_id for _, evidence in emitted} == set(_ORIGINS)
    assert all(not verify(case, evidence) for case, evidence in emitted)

    for kind in ("raw_json", "published_table", "prose_summary", "image_trend"):
        case, evidence = next(pair for pair in emitted if pair[1].evidence_type == kind)
        assert isinstance(evidence.citation, Citation)
        false_citation = evidence.citation.model_copy(update={"sha256": "0" * 64})
        assert verify(case, evidence.model_copy(update={"citation": false_citation}))
        false_pointer = evidence.citation.model_copy(update={"source_pointer": {"fabricated": True}})
        assert verify(case, evidence.model_copy(update={"citation": false_pointer}))
        assert verify(case, evidence.model_copy(update={"text": "fabricated source excerpt"}))
    case, figure = next(pair for pair in emitted if pair[1].related_experiments)
    assert verify(case, figure.model_copy(update={"related_experiments": ()}))

    case, table = next(
        pair for pair in emitted if pair[1].evidence_type == "published_table" and pair[1].value == Decimal("30.3")
    )
    assert isinstance(table.citation, Citation)
    pointer = table.citation.source_pointer
    assert pointer["cell"] == " 30.3 s "
    assert pointer["subvalue"]["start"] == 1 and pointer["subvalue"]["end"] == 5
    assert pointer["cell"][pointer["subvalue"]["start"] : pointer["subvalue"]["end"]] == "30.3"
    trimmed_pointer = {**pointer, "cell": pointer["cell"].strip()}
    assert trimmed_pointer["subvalue"] == pointer["subvalue"]
    trimmed_citation = table.citation.model_copy(update={"source_pointer": trimmed_pointer})
    assert verify(case, table.model_copy(update={"citation": trimmed_citation}))

    assert table.experiment is not None and table.experiment.configuration is not None
    configuration = table.experiment.configuration
    report_text = configuration["published_report"]
    assert isinstance(report_text, str) and report_text != report_text.strip()
    trimmed_context = table.experiment.model_copy(
        update={"configuration": {**configuration, "published_report": report_text.strip()}}
    )
    assert verify(case, table.model_copy(update={"experiment": trimmed_context}))

    case, passage = next(pair for pair in emitted if pair[1].evidence_type == "prose_summary")
    assert passage.text is not None and passage.text != passage.text.strip()
    assert verify(case, passage.model_copy(update={"text": passage.text.strip()}))
