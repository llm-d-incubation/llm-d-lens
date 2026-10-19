"""Offline, explicitly approved guide and benchmark documentation ingestion."""

from __future__ import annotations

import hashlib
import posixpath
import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

from pydantic import JsonValue

from llm_d_lens.benchmark_rag.contracts import Coverage, ItemKind, RagItem
from llm_d_lens.benchmark_rag.publication import RagPublicationService
from llm_d_lens.benchmark_rag.publication_contracts import (
    InventoryFile,
    ParsedFile,
    PinnedInventory,
    PublicationError,
    PublicationResult,
    SourceApproval,
    item_id,
)
from llm_d_lens.benchmark_rag.sources import (
    BENCHMARK_ORIGIN,
    LLM_D_ORIGIN,
    SourceArchive,
    collect_git,
    validate_approval,
)

PARSER_VERSION = "guide-evidence-v1"
_HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_IMAGE = re.compile(r"!\[([^\]\n]*)\]\(\s*(<[^>\n]+>|[^\s)\n]+)(?:\s+[\"'][^\"'\n]*[\"'])?\s*\)")
_REFERENCE_IMAGE = re.compile(r"!\[([^\]\n]*)\]\[([^\]\n]*)\]")
_REFERENCE = re.compile(r"^ {0,3}\[([^\]]+)\]:\s*<?([^\s>]+)>?")


@dataclass(frozen=True)
class MarkdownSection:
    heading_path: tuple[str, ...]
    start: int
    end: int
    start_line: int
    end_line: int


@dataclass(frozen=True)
class FigureReview:
    coverage: Coverage
    reason: str
    report_path: str | None = None
    table_indices: tuple[int, ...] = ()
    missing_series: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.reason.strip() or any(index < 0 for index in self.table_indices):
            raise PublicationError("figure coverage needs a reason and nonnegative table indices")
        if self.table_indices and self.report_path is None:
            raise PublicationError("verified figure links need an explicit report")


def markdown_sections(text: str) -> tuple[MarkdownSection, ...]:
    """Offsets address the decoded pinned text, not another rendering or revision."""
    lines = text.splitlines(keepends=True)
    starts: list[tuple[int, int, tuple[str, ...]]] = [(0, 1, ())]
    headings: list[tuple[int, str]] = []
    fence: str | None = None
    offset = 0
    for index, line in enumerate(lines):
        stripped = line.rstrip("\r\n")
        marker = _FENCE.match(stripped)
        if fence is not None:
            if re.fullmatch(rf" {{0,3}}{re.escape(fence[0])}{{{len(fence)},}}[ \t]*", stripped):
                fence = None
        elif marker:
            fence = marker[1]
        else:
            heading = _HEADING.match(stripped)
            if heading:
                level = len(heading[1])
                while headings and headings[-1][0] >= level:
                    headings.pop()
                headings.append((level, heading[2]))
                entry = (offset, index + 1, tuple(value for _, value in headings))
                if starts[-1][0] == offset:
                    starts[-1] = entry
                else:
                    starts.append(entry)
        offset += len(line)
    if fence is not None:
        raise PublicationError("unterminated Markdown fenced block")
    return tuple(
        MarkdownSection(
            heading_path=heading_path,
            start=start,
            end=starts[index + 1][0] if index + 1 < len(starts) else len(text),
            start_line=line,
            end_line=starts[index + 1][1] - 1 if index + 1 < len(starts) else len(lines),
        )
        for index, (start, line, heading_path) in enumerate(starts)
        if text[start : starts[index + 1][0] if index + 1 < len(starts) else len(text)].strip()
    )


def _base(file: InventoryFile, inventory: PinnedInventory, key: str) -> dict[str, JsonValue]:
    return {
        "item_id": item_id(inventory.origin_id, key, inventory.revision),
        "logical_key": key,
        "origin_id": inventory.origin_id,
        "source_revision": inventory.revision,
        "content_sha256": file.sha256,
        "access_classification": "verified_public",
        "source_path": file.path,
        "format": file.format,
    }


def _item(file: InventoryFile, inventory: PinnedInventory, suffix: str, **fields: object) -> RagItem:
    return RagItem.model_validate({**_base(file, inventory, f"{file.path}#{suffix}"), **fields})


class _Images(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.images: list[tuple[str, str, int]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "img":
            values = dict(attrs)
            source = values.get("src")
            if source:
                self.images.append((source, values.get("alt") or values.get("title") or "", self.getpos()[0]))


def _local_reference(source: str, target: str) -> str | None:
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc:
        return None
    decoded = unquote(parsed.path)
    if not decoded or decoded.startswith("/") or "\\" in decoded or "\x00" in decoded:
        return None
    normalized = posixpath.normpath(posixpath.join(posixpath.dirname(source), decoded))
    return None if normalized.startswith("../") or normalized == ".." else normalized


def _image_references(text: str) -> list[tuple[str, str, int]]:
    visible: list[str] = []
    fence: str | None = None
    for line in text.splitlines(keepends=True):
        marker = _FENCE.match(line.rstrip("\r\n"))
        if fence is None and marker:
            fence = marker[1]
            visible.append("\n")
        elif fence is not None:
            if re.fullmatch(rf" {{0,3}}{re.escape(fence[0])}{{{len(fence)},}}[ \t]*", line.rstrip("\r\n")):
                fence = None
            visible.append("\n")
        else:
            visible.append(line)
    rendered = "".join(visible)
    html = _Images()
    html.feed(rendered)
    results = list(html.images)
    references = {match[1].casefold(): match[2] for line in visible if (match := _REFERENCE.match(line)) is not None}
    for index, line in enumerate(visible, 1):
        results.extend((match[2].strip("<>"), match[1], index) for match in _IMAGE.finditer(line))
        for match in _REFERENCE_IMAGE.finditer(line):
            label = (match[2] or match[1]).casefold()
            if label not in references:
                raise PublicationError("unresolved Markdown image reference")
            results.append((references[label], match[1], index))
    return sorted(set(results), key=lambda entry: (entry[2], entry[0], entry[1]))


def parse_markdown(file: InventoryFile, inventory: PinnedInventory, data: bytes) -> tuple[RagItem, ...]:
    if hashlib.sha256(data).hexdigest() != file.sha256:
        raise PublicationError("Markdown bytes do not match the pinned source hash")
    try:
        text = data.decode("utf-8")
    except UnicodeError as error:
        raise PublicationError("Markdown is not UTF-8") from error
    sections = markdown_sections(text)
    references = _image_references(text)
    eligible = {entry.path for entry in inventory.files}
    result = []
    for section in sections:
        excerpt = text[section.start : section.end]
        images: list[JsonValue] = [
            {
                "target": target,
                "resolved_path": _local_reference(file.path, target),
                "caption": caption,
                "line": line,
                "in_inventory": _local_reference(file.path, target) in eligible,
            }
            for target, caption, line in references
            if section.start_line <= line <= section.end_line
        ]
        result.append(
            _item(
                file,
                inventory,
                f"section:{section.start}",
                kind="passage",
                parent_id=item_id(inventory.origin_id, f"file:{file.path}", inventory.revision),
                passage_text=excerpt,
                heading_path=list(section.heading_path),
                source_pointer={
                    "start_char": section.start,
                    "end_char": section.end,
                    "start_line": section.start_line,
                    "end_line": section.end_line,
                    "offset_encoding": "unicode_codepoints",
                    "evidence_role": "documentation" if inventory.origin_id == BENCHMARK_ORIGIN else "guide_context",
                    "contains_code_examples": bool(re.search(r"(?m)^ {0,3}(?:`{3,}|~{3,})", excerpt)),
                    "image_references": images,
                },
            )
        )
    return tuple(result)


def _figures(
    inventory: PinnedInventory,
    parsed: Mapping[str, tuple[RagItem, ...]],
    reviews: Mapping[str, FigureReview],
) -> dict[str, tuple[RagItem, ...]]:
    images = {file.path: file for file in inventory.files if file.format == "png"}
    if set(images) != set(reviews):
        raise PublicationError("every pinned figure needs an explicit coverage review, with no extra paths")
    references: dict[str, list[JsonValue]] = {path: [] for path in images}
    for path, items in parsed.items():
        for item in items:
            if item.kind != "passage":
                continue
            pointer = item.source_pointer or {}
            image_references = pointer.get("image_references", [])
            if not isinstance(image_references, list):
                raise PublicationError("invalid parsed image reference list")
            for reference in image_references:
                if not isinstance(reference, dict):
                    raise PublicationError("invalid parsed image reference")
                resolved = reference.get("resolved_path")
                if isinstance(resolved, str) and resolved in references:
                    references[resolved].append(
                        {
                            "report_path": path,
                            "report_sha256": item.content_sha256,
                            "passage_id": item.item_id,
                            "heading_path": item.heading_path,
                            **reference,
                        }
                    )
    result = {}
    for path, file in images.items():
        review = reviews[path]
        refs = references[path]
        caption = next(
            (str(reference["caption"]) for reference in refs if isinstance(reference, dict) and reference["caption"]),
            None,
        )
        figure = _item(
            file,
            inventory,
            "figure",
            kind="figure",
            parent_id=item_id(inventory.origin_id, f"file:{path}", inventory.revision),
            caption=caption,
            coverage=review.coverage,
            missing_series=list(review.missing_series),
            source_pointer={
                "coverage_reason": review.reason,
                "references": refs,
                "numeric_extraction": "none",
                "reviewed_report": review.report_path,
                "reviewed_tables": list(review.table_indices),
            },
        )
        items = [figure]
        if review.report_path is not None:
            if review.report_path not in parsed or not any(
                isinstance(reference, dict) and reference["report_path"] == review.report_path for reference in refs
            ):
                raise PublicationError("figure review report must explicitly reference this pinned image")
            experiments = [
                item
                for item in parsed[review.report_path]
                if item.kind == "experiment" and (item.source_pointer or {}).get("table_index") in review.table_indices
            ]
            if review.table_indices and not experiments:
                raise PublicationError("reviewed figure tables have no verified experiments")
            for experiment in experiments:
                items.append(
                    _item(
                        file,
                        inventory,
                        f"figure-link:{experiment.item_id}",
                        kind="figure_link",
                        parent_id=figure.item_id,
                        referenced_item_id=experiment.item_id,
                        source_pointer={
                            "report_path": review.report_path,
                            "report_sha256": experiment.content_sha256,
                            "table_index": (experiment.source_pointer or {}).get("table_index"),
                            "basis": review.reason,
                        },
                    )
                )
        result[path] = tuple(items)
    return result


def parse_repository(
    inventory: PinnedInventory,
    archive: SourceArchive,
    *,
    figure_reviews: Mapping[str, FigureReview],
) -> tuple[ParsedFile, ...]:
    from llm_d_lens.benchmark_rag.guide_json import parse_custom_json
    from llm_d_lens.benchmark_rag.guide_tables import parse_published_tables

    if inventory.origin_id not in (LLM_D_ORIGIN, BENCHMARK_ORIGIN):
        raise PublicationError("repository parser only supports the two reviewed repository origins")
    parsed: dict[str, tuple[RagItem, ...]] = {}
    for file in inventory.files:
        data = archive.read(file.sha256)
        if file.format == "markdown":
            items = parse_markdown(file, inventory, data)
            if inventory.origin_id == LLM_D_ORIGIN:
                items += parse_published_tables(file, inventory.revision, data)
            parsed[file.path] = items
        elif file.format == "json" and inventory.origin_id == LLM_D_ORIGIN:
            parsed[file.path] = parse_custom_json(file, inventory.revision, data)
        elif file.format == "png" and inventory.origin_id == LLM_D_ORIGIN:
            if not data.startswith(b"\x89PNG\r\n\x1a\n"):
                raise PublicationError("pinned figure is not a PNG")
        else:
            raise PublicationError("repository contains a format unsupported by its approved adapter")
    parsed.update(_figures(inventory, parsed, figure_reviews))
    return tuple(ParsedFile(path=file.path, items=parsed[file.path]) for file in inventory.files)


def repository_counts(parsed: tuple[ParsedFile, ...]) -> dict[ItemKind, int]:
    counts: Counter[ItemKind] = Counter(item.kind for file in parsed for item in file.items)
    if parsed:
        counts["source"] = len(parsed)
    return dict(counts)


class RepositoryCorpusImporter:
    def __init__(self, publication: RagPublicationService | None = None) -> None:
        self.publication = publication if publication is not None else RagPublicationService()

    def stage_repository(
        self,
        repo: Path,
        origin_id: str,
        revision: str,
        approval: SourceApproval,
        *,
        expected_counts: dict[ItemKind, int],
        figure_reviews: Mapping[str, FigureReview],
        expected_inventory_digest: str | None = None,
    ) -> RagItem:
        if origin_id not in (LLM_D_ORIGIN, BENCHMARK_ORIGIN) or approval.parser_version != PARSER_VERSION:
            raise PublicationError("repository approval needs the supported origin and parser version")
        validate_approval(origin_id, approval)
        inventory = collect_git(origin_id, repo, revision, approval, self.publication.archive)
        if expected_inventory_digest is not None and inventory.digest != expected_inventory_digest:
            raise PublicationError("repository inventory differs from the reviewed pinned inventory")
        parsed = parse_repository(inventory, self.publication.archive, figure_reviews=figure_reviews)
        if repository_counts(parsed) != {kind: count for kind, count in expected_counts.items() if count}:
            raise PublicationError("repository extraction differs from reviewed per-kind counts")
        self.publication.register_source(origin_id, approval)
        return self.publication.stage(inventory, parsed, expected_counts=expected_counts)

    def import_repository(
        self,
        repo: Path,
        origin_id: str,
        revision: str,
        approval: SourceApproval,
        *,
        expected_counts: dict[ItemKind, int],
        figure_reviews: Mapping[str, FigureReview],
        expected_inventory_digest: str | None = None,
    ) -> PublicationResult:
        manifest = self.stage_repository(
            repo,
            origin_id,
            revision,
            approval,
            expected_counts=expected_counts,
            figure_reviews=figure_reviews,
            expected_inventory_digest=expected_inventory_digest,
        )
        return self.publication.activate(origin_id, manifest.item_id)
