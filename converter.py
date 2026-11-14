from __future__ import annotations

import argparse
import csv
import hashlib
import html as html_stdlib
import os
import re
import shutil
import subprocess
import unicodedata
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Iterable
from urllib.parse import parse_qs, quote, unquote, urlsplit

from lxml import etree, html
from markdownify import MarkdownConverter


CDATA_PATTERN = re.compile(r"<!\[CDATA\[(.*?)\]\]\s*>", re.DOTALL)
INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WHITESPACE = re.compile(r"\s+")
BLOCK_TAGS = frozenset(
    {
        "blockquote",
        "div",
        "li",
        "ol",
        "p",
        "pre",
        "table",
        "tbody",
        "td",
        "tfoot",
        "th",
        "thead",
        "tr",
        "ul",
        "ac:rich-text-body",
    }
)
PRESERVE_WHITESPACE_TAGS = frozenset(
    {"ac:plain-text-body", "code", "pre", "script", "style"}
)


@dataclass
class Asset:
    page_id: str
    attachment_id: str
    filename: str
    archive_member: str
    output_path: PurePosixPath


@dataclass
class Page:
    space_key: str
    page_id: str
    title: str
    status: str
    original_version: str
    body: str
    output_path: PurePosixPath
    ancestor_ids: list[str] = field(default_factory=list)
    ancestor_titles: list[str] = field(default_factory=list)
    position: str = ""
    assets: dict[str, list[Asset]] = field(default_factory=dict)
    archive_path: Path | None = None


@dataclass
class MigrationResult:
    page_count: int
    attachment_count: int
    omitted_draft_count: int
    warning_count: int
    inter_space_link_count: int
    output_dir: Path


class WarningLog:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []
        self.page_ids_by_row: dict[tuple[str, str, str], set[str]] = {}

    def add(self, page: Page | None, code: str, detail: str) -> None:
        source = page.output_path.as_posix() if page else ""
        row = (source, code, detail)
        if row not in self.rows:
            self.rows.append(row)
        if page is not None:
            self.page_ids_by_row.setdefault(row, set()).add(page.page_id)

    def remove_page(self, page: Page) -> None:
        source = page.output_path.as_posix()
        for row in self.rows[:]:
            if row[0] != source:
                continue
            page_ids = self.page_ids_by_row.get(row)
            if page_ids is None:
                continue
            page_ids.discard(page.page_id)
            if not page_ids:
                self.rows.remove(row)
                del self.page_ids_by_row[row]

    def has_page_warnings(self, page: Page) -> bool:
        return any(
            page.page_id in page_ids
            for page_ids in self.page_ids_by_row.values()
        )

    def write(self, output_dir: Path) -> None:
        report_path = output_dir / "migration-warnings.csv"
        with report_path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(("source_page", "warning", "detail"))
            writer.writerows(sorted(self.rows))


class InterSpaceLinkLog:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, ...], int] = {}

    @property
    def occurrence_count(self) -> int:
        return sum(self.rows.values())

    def add(
        self,
        source: Page,
        target_space: str,
        target_title: str,
        target_page_id: str,
        link_text: str,
        target: Page | None,
    ) -> None:
        effective_space = target.space_key if target is not None else target_space
        if effective_space.casefold() == source.space_key.casefold():
            return

        row = (
            source.space_key,
            source.output_path.as_posix(),
            effective_space,
            target.title if target is not None else target_title,
            target.page_id if target is not None else target_page_id,
            target.output_path.as_posix() if target is not None else "",
            link_text,
            "resolved" if target is not None else "unresolved",
        )
        self.rows[row] = self.rows.get(row, 0) + 1

    def write(self, output_dir: Path) -> None:
        report_path = output_dir / "inter-space-links.csv"
        with report_path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(
                (
                    "source_space",
                    "target_space",
                    "source_page",
                    "target_page",
                    "target_page_id",
                    "target_path",
                    "link_text",
                    "resolution",
                    "occurrences",
                )
            )
            writer.writerows(
                (row[0], row[2], row[1], *row[3:], occurrences)
                for row, occurrences in sorted(self.rows.items())
            )

        summaries: dict[tuple[str, str], list[int]] = {}
        for row, occurrences in self.rows.items():
            summary = summaries.setdefault((row[0], row[2]), [0, 0, 0, 0])
            summary[0] += 1
            summary[1] += occurrences
            summary[2 if row[7] == "resolved" else 3] += occurrences

        summary_path = output_dir / "inter-space-summary.csv"
        with summary_path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(
                (
                    "source_space",
                    "target_space",
                    "distinct_links",
                    "occurrences",
                    "resolved_occurrences",
                    "unresolved_occurrences",
                )
            )
            for (source_space, target_space), counts in sorted(summaries.items()):
                writer.writerow((source_space, target_space, *counts))


def _property_value(element: etree._Element, name: str) -> str:
    prop = element.find(f"./property[@name='{name}']")
    if prop is None:
        return ""
    if prop.text and prop.text.strip():
        return prop.text.strip()
    id_element = prop.find("./id")
    return (id_element.text or "").strip() if id_element is not None else ""


def _safe_segment(value: str, fallback: str) -> str:
    normalized = unicodedata.normalize("NFC", value).strip()
    normalized = INVALID_FILENAME_CHARS.sub("-", normalized)
    normalized = normalized.rstrip(". ")
    if normalized in {"", ".", ".."}:
        return fallback
    return normalized


def _page_path(
    space_name: str,
    title: str,
    ancestor_titles: list[str],
    status_folder: str | None = None,
) -> PurePosixPath:
    parts = [_safe_segment(space_name, "unknown-space")]
    if status_folder:
        parts.append(status_folder)
    parts.extend(
        _safe_segment(ancestor, "unknown-ancestor") for ancestor in ancestor_titles
    )
    parts.append(f"{_safe_segment(title, 'untitled')}.md")
    return PurePosixPath(*parts)


def _deduplicate_page_paths(pages: Iterable[Page]) -> None:
    collisions: dict[str, list[Page]] = {}
    for page in pages:
        collisions.setdefault(
            page.output_path.as_posix().casefold(), []
        ).append(page)

    for matches in collisions.values():
        if len(matches) < 2:
            continue
        for page in matches:
            filename = page.output_path.name
            stem = filename.removesuffix(".md")
            page.output_path = page.output_path.with_name(
                f"{stem}--{_safe_segment(page.page_id, 'unknown-id')}.md"
            )


def _deduplicate_asset_paths(pages: Iterable[Page]) -> None:
    assets = {
        id(asset): (page, asset)
        for page in pages
        for page_assets in page.assets.values()
        for asset in page_assets
    }
    used_paths: set[str] = set()
    ordered = sorted(
        assets.values(),
        key=lambda item: (
            item[1].output_path.as_posix().casefold(),
            item[0].page_id,
            item[1].attachment_id,
        ),
    )
    for page, asset in ordered:
        candidate = asset.output_path
        key = candidate.as_posix().casefold()
        if key in used_paths:
            filename = candidate.name
            suffix = PurePosixPath(filename).suffix
            stem = filename[: -len(suffix)] if suffix else filename
            identity = (
                f"{_safe_segment(page.page_id, 'unknown-id')}-"
                f"{_safe_segment(asset.attachment_id, 'unknown-id')}"
            )
            filename = f"{stem}--{identity}{suffix}"
            candidate = candidate.with_name(filename)
            counter = 2
            while candidate.as_posix().casefold() in used_paths:
                candidate = candidate.with_name(
                    f"{stem}--{identity}-{counter}{suffix}"
                )
                counter += 1
            asset.output_path = candidate
            key = candidate.as_posix().casefold()
        used_paths.add(key)


def _clear_output_dir(output_dir: Path, input_dir: Path) -> None:
    cwd = Path.cwd().resolve()
    home = Path.home().resolve()
    if (
        output_dir == input_dir
        or output_dir in input_dir.parents
        or input_dir in output_dir.parents
        or output_dir == cwd
        or output_dir in cwd.parents
        or output_dir == home
        or output_dir in home.parents
        or output_dir == Path(output_dir.anchor)
    ):
        raise ValueError(f"Refusing to clear unsafe output directory {output_dir}")
    if output_dir.is_symlink():
        raise ValueError(f"Refusing to clear symlink output directory {output_dir}")
    if not output_dir.exists():
        return
    for child in output_dir.iterdir():
        if child.is_symlink() or not child.is_dir():
            child.unlink()
        else:
            shutil.rmtree(child)


def _attachment_member(
    members: set[str], page_id: str, attachment_id: str, version: str
) -> str | None:
    prefix = f"attachments/{page_id}/{attachment_id}/"
    exact = f"{prefix}{version}"
    if exact in members:
        return exact

    candidates = [
        name
        for name in members
        if name.startswith(prefix) and not name.endswith("/")
    ]
    if not candidates:
        return None

    def version_number(member: str) -> tuple[int, str]:
        value = member[len(prefix) :]
        try:
            return int(value), value
        except ValueError:
            return -1, value

    return max(candidates, key=version_number)


def _space_key(root: etree._Element, archive_path: Path) -> str:
    spaces = root.findall("./object[@class='Space']")
    current = [
        space
        for space in spaces
        if _property_value(space, "spaceStatus") in {"", "CURRENT"}
    ]
    if not current:
        current = spaces
    keys = sorted(
        {
            _property_value(space, "key")
            for space in current
            if _property_value(space, "key")
        }
    )
    if len(keys) != 1:
        raise ValueError(
            f"{archive_path.name}: expected one Confluence space key, found {keys}"
        )
    return keys[0]


def _space_name(
    root: etree._Element, archive_path: Path, fallback: str
) -> str:
    spaces = root.findall("./object[@class='Space']")
    current = [
        space
        for space in spaces
        if _property_value(space, "spaceStatus") in {"", "CURRENT"}
    ]
    if not current:
        current = spaces
    names = sorted(
        {
            _property_value(space, "name")
            for space in current
            if _property_value(space, "name")
        }
    )
    if len(names) > 1:
        raise ValueError(
            f"{archive_path.name}: expected one Confluence space name, found {names}"
        )
    return names[0] if names else fallback


def _read_archive(
    archive_path: Path, warnings: WarningLog
) -> list[Page]:
    with zipfile.ZipFile(archive_path) as archive:
        names = set(archive.namelist())
        if "entities.xml" not in names:
            raise ValueError(f"{archive_path.name}: missing entities.xml")
        parser = etree.XMLParser(
            resolve_entities=False,
            no_network=True,
            recover=False,
            huge_tree=True,
        )
        root = etree.fromstring(archive.read("entities.xml"), parser)

        space_key = _space_key(root, archive_path)
        space_name = _space_name(root, archive_path, space_key)
        body_by_id = {
            body.findtext("./id", default=""): _property_value(body, "body")
            for body in root.findall("./object[@class='BodyContent']")
        }
        attachment_by_id = {
            item.findtext("./id", default=""): item
            for item in root.findall("./object[@class='Attachment']")
        }
        page_objects = root.findall("./object[@class='Page']")
        page_status_by_id = {
            page_obj.findtext("./id", default="").strip(): _property_value(
                page_obj, "contentStatus"
            ).casefold()
            for page_obj in page_objects
        }
        page_title_by_id = {
            page_obj.findtext("./id", default="").strip(): (
                _property_value(page_obj, "title") or ""
            )
            for page_obj in page_objects
        }
        historical_ids = {
            element.findtext("./id", default="")
            for page_obj in page_objects
            for collection in page_obj.findall("./collection[@name='historicalVersions']")
            for element in collection.findall("./element")
        }

        pages: list[Page] = []
        for page_obj in page_objects:
            page_id = page_obj.findtext("./id", default="").strip()
            status = _property_value(page_obj, "contentStatus").casefold()
            original_version = _property_value(page_obj, "originalVersion")
            if not page_id or page_id in historical_ids:
                continue

            source_title = _property_value(page_obj, "title")
            title = source_title or f"Untitled {page_id}"
            body_ids = [
                element.findtext("./id", default="")
                for collection in page_obj.findall("./collection[@name='bodyContents']")
                for element in collection.findall("./element")
            ]
            body = "\n".join(body_by_id.get(body_id, "") for body_id in body_ids)
            if status == "draft" and not source_title and not body.strip():
                continue

            ancestor_ids = [
                element.findtext("./id", default="")
                for collection in page_obj.findall("./collection[@name='ancestors']")
                for element in collection.findall("./element")
            ]
            ancestor_titles = [
                page_title_by_id[ancestor_id]
                for ancestor_id in ancestor_ids
                if page_title_by_id.get(ancestor_id)
            ]
            if status == "draft":
                status_folder = (
                    "_deleted"
                    if page_status_by_id.get(original_version)
                    in {"deleted", "trashed"}
                    else "_drafts"
                )
            elif status in {"deleted", "trashed"}:
                status_folder = "_deleted"
            else:
                status_folder = None
            page = Page(
                space_key=space_key,
                page_id=page_id,
                title=title,
                status=status or "unknown",
                original_version=original_version,
                body=body,
                output_path=_page_path(
                    space_name, title, ancestor_titles, status_folder
                ),
                ancestor_ids=ancestor_ids,
                ancestor_titles=ancestor_titles,
                position=_property_value(page_obj, "position"),
                archive_path=archive_path,
            )

            attachment_ids = [
                element.findtext("./id", default="")
                for collection in page_obj.findall("./collection[@name='attachments']")
                for element in collection.findall("./element")
            ]
            assets_by_name: dict[str, list[Asset]] = {}
            missing_attachment_files: list[str] = []
            for attachment_id in attachment_ids:
                metadata = attachment_by_id.get(attachment_id)
                if metadata is None:
                    warnings.add(page, "missing-attachment-metadata", attachment_id)
                    continue
                filename = _property_value(metadata, "title")
                if PurePosixPath(filename).name.casefold() == "._.ds_store":
                    continue
                if not filename:
                    warnings.add(page, "attachment-without-filename", attachment_id)
                    continue
                member = _attachment_member(
                    names,
                    page_id,
                    attachment_id,
                    _property_value(metadata, "version") or "1",
                )
                if member is None:
                    missing_attachment_files.append(filename)
                    continue

                safe_name = _safe_segment(Path(filename).name, attachment_id)
                asset = Asset(
                    page_id=page_id,
                    attachment_id=attachment_id,
                    filename=filename,
                    archive_member=member,
                    output_path=page.output_path.parent / "_attachments" / safe_name,
                )
                assets_by_name.setdefault(filename, []).append(asset)
            available_names = {
                _attachment_name_key(filename) for filename in assets_by_name
            }
            for filename in missing_attachment_files:
                if _attachment_name_key(filename) not in available_names:
                    warnings.add(page, "missing-attachment-file", filename)
            page.assets = assets_by_name
            pages.append(page)

        return pages


def _normalized_title(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def _attachment_name_key(filename: str) -> str:
    basename = PurePosixPath(filename.replace("\\", "/")).name
    return unicodedata.normalize("NFC", basename).casefold()


def _page_body_signature(body: str) -> tuple[object, ...]:
    root = html.fragment_fromstring(_safe_cdata(body), create_parent="div")

    def normalized_text(
        value: str | None, preserve: bool, indentation: bool
    ) -> str:
        if value is None:
            return ""
        if preserve:
            return value
        if indentation and not value.strip():
            return ""
        return WHITESPACE.sub(" ", value)

    def node_signature(
        node: etree._Element, preserve: bool = False
    ) -> tuple[object, ...]:
        if not isinstance(node.tag, str):
            return ("comment", node.text or "")

        preserve = preserve or node.tag.casefold() in PRESERVE_WHITESPACE_TAGS
        has_block_children = any(
            isinstance(child.tag, str) and child.tag.casefold() in BLOCK_TAGS
            for child in node
        )
        text = normalized_text(node.text, preserve, has_block_children)
        attributes = tuple(
            sorted(
                (
                    name,
                    _attachment_name_key(value)
                    if name.casefold() == "ri:filename"
                    else value,
                )
                for name, value in node.attrib.items()
                if name.casefold() != "ac:macro-id"
            )
        )
        children = []
        for child in node:
            child_tag = child.tag.casefold() if isinstance(child.tag, str) else ""
            if (
                node.tag.casefold() == "ri:attachment"
                and child_tag == "ri:content-entity"
            ):
                continue
            child_signature = node_signature(child, preserve)
            tail = normalized_text(
                child.tail,
                preserve,
                has_block_children and child_tag in BLOCK_TAGS,
            )
            if child_tag == "br" and not preserve:
                tail = tail.lstrip()
            children.append((child_signature, tail))
        return node.tag.casefold(), attributes, text, tuple(children)

    body_html = html.tostring(root, encoding="unicode", method="html")
    markdown = _ConfluenceMarkdownConverter(
        heading_style="ATX",
        bullets="*",
    ).convert(body_html)
    normalized_markdown = []
    fence_character = ""
    fence_length = 0
    for line in markdown.splitlines():
        fence = re.match(r"^\s*(`{3,}|~{3,})", line)
        if fence_character:
            normalized_markdown.append(("code", line))
            if (
                fence
                and fence.group(1)[0] == fence_character
                and len(fence.group(1)) >= fence_length
            ):
                fence_character = ""
                fence_length = 0
            continue
        if fence:
            marker = fence.group(1)
            fence_character = marker[0]
            fence_length = len(marker)
            normalized_markdown.append(("fence", line))
            continue
        if not line.strip():
            continue

        leading_whitespace = line[: len(line) - len(line.lstrip())]
        content = line[len(leading_whitespace) :]
        indentation = (
            leading_whitespace
            if re.match(r"(?:[*+-]|\d+[.)])\s+|>", content)
            else ""
        )
        segments = []
        cursor = 0
        for match in re.finditer(r"(`+)(.*?)\1", content):
            if match.start() > cursor:
                segments.append(
                    ("text", WHITESPACE.sub(" ", content[cursor : match.start()]))
                )
            segments.append(("inline-code", match.group(0)))
            cursor = match.end()
        if cursor < len(content):
            segments.append(("text", WHITESPACE.sub(" ", content[cursor:]).rstrip()))
        if not segments:
            segments.append(("text", WHITESPACE.sub(" ", content).strip()))
        normalized_markdown.append((indentation, tuple(segments)))

    semantic_tags = {
        "ac:link",
        "ac:structured-macro",
        "ri:attachment",
        "ri:page",
        "ri:url",
    }
    semantic_nodes = tuple(
        node_signature(node)
        for node in root.iter()
        if isinstance(node.tag, str) and node.tag.casefold() in semantic_tags
    )

    def preserved_signatures(node: etree._Element) -> tuple[tuple[object, ...], ...]:
        if (
            isinstance(node.tag, str)
            and node.tag.casefold() in PRESERVE_WHITESPACE_TAGS
        ):
            return (node_signature(node),)
        return tuple(
            signature
            for child in node
            for signature in preserved_signatures(child)
        )

    return (
        tuple(normalized_markdown),
        semantic_nodes,
        preserved_signatures(root),
    )


def _page_attachment_signature(page: Page) -> tuple[tuple[str, str], ...]:
    assets = [
        asset for page_assets in page.assets.values() for asset in page_assets
    ]
    if not assets:
        return ()
    if page.archive_path is None:
        return tuple(
            sorted((_attachment_name_key(asset.filename), "") for asset in assets)
        )

    signatures = []
    with zipfile.ZipFile(page.archive_path) as archive:
        for asset in assets:
            digest = hashlib.sha256()
            with archive.open(asset.archive_member) as source:
                while chunk := source.read(1024 * 1024):
                    digest.update(chunk)
            signatures.append((_attachment_name_key(asset.filename), digest.hexdigest()))
    return tuple(sorted(signatures))


def _same_page_attachments(draft: Page, current: Page) -> bool:
    draft_signature = _page_attachment_signature(draft)
    current_signature = _page_attachment_signature(current)
    available = {}
    for signature in current_signature:
        available[signature] = available.get(signature, 0) + 1
    for signature in draft_signature:
        count = available.get(signature, 0)
        if not count:
            return False
        available[signature] = count - 1
    return True


def _omit_unchanged_drafts(
    pages: list[Page], warnings: WarningLog
) -> tuple[list[Page], int, dict[str, Page]]:
    pages_by_id: dict[tuple[str, str], list[Page]] = {}
    current_by_id: dict[tuple[str, str], list[Page]] = {}
    current_by_title: dict[tuple[str, str], list[Page]] = {}
    for page in pages:
        pages_by_id.setdefault(
            (page.space_key.casefold(), page.page_id), []
        ).append(page)
        if page.status != "current":
            continue
        current_by_id.setdefault(
            (page.space_key.casefold(), page.page_id), []
        ).append(page)
        current_by_title.setdefault(
            (page.space_key.casefold(), _normalized_title(page.title)), []
        ).append(page)

    signature_cache: dict[int, tuple[object, ...]] = {}

    def signature(page: Page) -> tuple[object, ...]:
        cached = signature_cache.get(id(page))
        if cached is None:
            cached = (
                unicodedata.normalize("NFC", page.title),
                _page_body_signature(page.body),
            )
            signature_cache[id(page)] = cached
        return cached

    omitted: set[int] = set()
    aliases: dict[str, Page] = {}
    for draft in pages:
        if draft.status != "draft":
            continue

        id_key = (draft.space_key.casefold(), draft.page_id)
        original_key = (draft.space_key.casefold(), draft.original_version)
        original_candidates = pages_by_id.get(original_key, [])
        candidates = (
            original_candidates
            if len(original_candidates) == 1
            and original_candidates[0].status in {"current", "deleted", "trashed"}
            else current_by_id.get(id_key, [])
        )
        if not candidates:
            candidates = current_by_title.get(
                (draft.space_key.casefold(), _normalized_title(draft.title)), []
            )
        if len(candidates) > 1:
            ancestor_titles = tuple(
                _normalized_title(title) for title in draft.ancestor_titles
            )
            hierarchy_matches = [
                page
                for page in candidates
                if tuple(_normalized_title(title) for title in page.ancestor_titles)
                == ancestor_titles
            ]
            if len(hierarchy_matches) == 1:
                candidates = hierarchy_matches
        if (
            len(candidates) != 1
            or warnings.has_page_warnings(draft)
            or warnings.has_page_warnings(candidates[0])
            or signature(draft) != signature(candidates[0])
            or not _same_page_attachments(draft, candidates[0])
        ):
            continue

        matched_page = candidates[0]
        omitted.add(id(draft))
        if not current_by_id.get(id_key):
            aliases[draft.page_id] = matched_page
        warnings.remove_page(draft)

    return [page for page in pages if id(page) not in omitted], len(omitted), aliases


class _PageIndex:
    def __init__(
        self, pages: Iterable[Page], aliases: dict[str, Page] | None = None
    ) -> None:
        self.aliases = aliases or {}
        self.by_id: dict[str, list[Page]] = {}
        self.by_space_title: dict[tuple[str, str], list[Page]] = {}
        self.by_parent: dict[tuple[str, str], list[Page]] = {}
        for page in pages:
            self.by_id.setdefault(page.page_id, []).append(page)
            self.by_space_title.setdefault(
                (page.space_key.casefold(), page.title), []
            ).append(page)
            if page.status == "current" and page.ancestor_ids:
                parent_id = page.ancestor_ids[-1]
                self.by_parent.setdefault(
                    (page.space_key.casefold(), parent_id), []
                ).append(page)

    def find(
        self, title: str = "", space_key: str = "", page_id: str = ""
    ) -> tuple[Page | None, str | None]:
        if page_id:
            matches = self.by_id.get(page_id, [])
            if not matches and page_id in self.aliases:
                return self.aliases[page_id], None
            selected, problem = self._select(matches, "ambiguous-page-id")
            if selected is not None or problem:
                return selected, problem

        target_space = space_key.casefold()
        if not title:
            return None, None
        matches = self.by_space_title.get((target_space, title), [])
        selected, problem = self._select(matches, "ambiguous-page-title")
        if selected is not None or problem:
            return selected, problem

        folded = title.casefold()
        matches = [
            page
            for (space, candidate), candidates in self.by_space_title.items()
            if space == target_space and candidate.casefold() == folded
            for page in candidates
        ]
        return self._select(matches, "ambiguous-page-title")

    @staticmethod
    def _select(
        matches: list[Page], ambiguity: str
    ) -> tuple[Page | None, str | None]:
        current = [page for page in matches if page.status == "current"]
        if len(current) == 1:
            return current[0], None
        if len(current) > 1:
            return None, ambiguity
        if len(matches) == 1:
            return matches[0], None
        if matches:
            return None, ambiguity
        return None, None

    def children_toc(
        self,
        source: Page,
        parent: Page,
        output_dir: Path,
        max_depth: int | None,
        link_report: InterSpaceLinkLog,
    ) -> html.HtmlElement | None:
        visited = {(parent.space_key.casefold(), parent.page_id)}

        def build_list(current: Page, depth: int) -> html.HtmlElement | None:
            children = self.by_parent.get(
                (current.space_key.casefold(), current.page_id), []
            )
            if not children:
                return None

            listing = html.Element("ul")
            for child in sorted(children, key=_page_order):
                key = (child.space_key.casefold(), child.page_id)
                if key in visited:
                    continue
                visited.add(key)
                item = etree.SubElement(listing, "li")
                link = etree.SubElement(item, "a")
                link.set("href", _markdown_target(source, child.output_path, output_dir))
                link.text = child.title
                link_report.add(
                    source,
                    child.space_key,
                    child.title,
                    child.page_id,
                    child.title,
                    child,
                )
                if max_depth is None or depth < max_depth:
                    nested = build_list(child, depth + 1)
                    if nested is not None:
                        item.append(nested)
            return listing if len(listing) else None

        return build_list(parent, 1)


def _page_order(page: Page) -> tuple[int, int | str, str]:
    try:
        return (0, int(page.position), page.title.casefold())
    except ValueError:
        return (1, page.title.casefold(), page.page_id)


def _run_markdownlint(output_dir: Path) -> None:
    config_path = Path(__file__).resolve().parent / ".markdownlint-cli2.jsonc"
    markdown_glob = output_dir.resolve().as_posix() + "/**/*.md"
    subprocess.run(
        [
            "npx",
            "--yes",
            "--package",
            "markdownlint-cli2@0.24.1",
            "markdownlint-cli2",
            "--config",
            str(config_path),
            markdown_glob,
        ],
        check=True,
    )


class _AssetIndex:
    def __init__(self, pages: Iterable[Page]) -> None:
        self.by_space_name: dict[tuple[str, str], list[Asset]] = {}
        for page in pages:
            for name, assets in page.assets.items():
                key = (page.space_key.casefold(), name.casefold())
                self.by_space_name.setdefault(key, []).extend(assets)

    def find(self, page: Page, filename: str) -> tuple[Asset | None, str | None]:
        local, problem = _find_asset(page, filename)
        if local is not None or problem:
            return local, problem
        matches = self.by_space_name.get(
            (page.space_key.casefold(), filename.casefold()), []
        )
        unique = {
            (asset.page_id, asset.attachment_id): asset for asset in matches
        }
        if len(unique) == 1:
            return next(iter(unique.values())), None
        if len(unique) > 1:
            return None, "ambiguous-attachment"
        return None, None


def _markdown_target(
    source: Page, target: PurePosixPath, output_dir: Path
) -> str:
    source_file = output_dir.joinpath(*source.output_path.parts)
    target_file = output_dir.joinpath(*target.parts)
    relative = Path(os.path.relpath(target_file, source_file.parent)).as_posix()
    return quote(relative, safe="/-._~")


def _find_asset(page: Page, filename: str) -> tuple[Asset | None, str | None]:
    exact = page.assets.get(filename, [])
    if len(exact) == 1:
        return exact[0], None
    if len(exact) > 1:
        return None, "ambiguous-attachment"
    folded = filename.casefold()
    matches = [
        assets[0]
        for name, assets in page.assets.items()
        if name.casefold() == folded and len(assets) == 1
    ]
    if len(matches) == 1:
        return matches[0], None
    if len(matches) > 1:
        return None, "ambiguous-attachment"
    return None, None


def _copy_children(source: html.HtmlElement, target: html.HtmlElement) -> None:
    target.text = source.text
    for child in list(source):
        source.remove(child)
        target.append(child)


def _replace_node(node: html.HtmlElement, replacement: html.HtmlElement) -> None:
    parent = node.getparent()
    if parent is None:
        return
    replacement.tail = node.tail
    parent.replace(node, replacement)


def _link_label(link: html.HtmlElement, default: str) -> str:
    for tag in ("ac:plain-text-link-body", "ac:link-body"):
        body = next((child for child in link if child.tag == tag), None)
        if body is not None:
            label = WHITESPACE.sub(" ", body.text_content()).strip()
            if label:
                return label
    return default


def _relative_asset(page: Page, asset: Asset, output_dir: Path) -> str:
    return _markdown_target(page, asset.output_path, output_dir)


def _is_external_url(href: str) -> bool:
    parsed = urlsplit(href)
    return bool(parsed.scheme or parsed.netloc)


def _resolve_page_link(
    page: Page,
    title: str,
    space_key: str,
    page_id: str,
    index: _PageIndex,
    output_dir: Path,
    warnings: WarningLog,
    link_report: InterSpaceLinkLog,
    link_text: str,
) -> str | None:
    target_space = space_key or page.space_key
    target, problem = index.find(title, target_space, page_id)
    link_report.add(
        page, target_space, title, page_id, link_text, target
    )
    if problem:
        warnings.add(page, problem, f"{space_key}:{title or page_id}")
    if target is None:
        warnings.add(page, "unresolved-page-link", f"{space_key}:{title or page_id}")
        return None
    return _markdown_target(page, target.output_path, output_dir)


def _handle_confluence_link(
    element: html.HtmlElement,
    page: Page,
    index: _PageIndex,
    asset_index: _AssetIndex,
    output_dir: Path,
    warnings: WarningLog,
    link_report: InterSpaceLinkLog,
) -> None:
    # lxml's HTML parser keeps Confluence's namespace prefixes in tag names.
    page_node = next(
        (child for child in element.iter() if child.tag == "ri:page"), None
    )
    url_node = next(
        (child for child in element.iter() if child.tag == "ri:url"), None
    )
    attachment_node = next(
        (child for child in element.iter() if child.tag == "ri:attachment"), None
    )
    if page_node is not None:
        space_key = page_node.get("ri:space-key", "")
        title = page_node.get("ri:content-title", "")
        page_id = page_node.get("ri:content-id", "")
        label = _link_label(element, title or page_id)
        href = _resolve_page_link(
            page,
            title,
            space_key,
            page_id,
            index,
            output_dir,
            warnings,
            link_report,
            label,
        )
    elif url_node is not None:
        href = url_node.get("ri:value")
        label = _link_label(element, href or "")
        if href:
            local = _resolve_confluence_url(
                href, page, index, output_dir, warnings, link_report, label
            )
            if local:
                href = local
            elif (
                not _is_external_url(href)
                and ("pageId=" in href or "/display/" in href)
            ):
                warnings.add(page, "unresolved-confluence-url", href)
    elif attachment_node is not None:
        filename = attachment_node.get("ri:filename", "")
        asset, problem = asset_index.find(page, filename)
        if problem:
            warnings.add(page, problem, filename)
        href = _relative_asset(page, asset, output_dir) if asset else None
        if asset is None:
            warnings.add(page, "unresolved-attachment-link", filename)
        label = _link_label(element, filename)
    else:
        return

    anchor = html.Element("a")
    if href:
        anchor.set("href", href)
    label_node = next(
        (
            child
            for child in element
            if child.tag in {"ac:plain-text-link-body", "ac:link-body"}
        ),
        None,
    )
    if label_node is not None:
        _copy_children(label_node, anchor)
        if not anchor.text:
            anchor.text = label
    else:
        image = next((child for child in element.iter() if child.tag == "img"), None)
        if image is not None:
            image_parent = image.getparent()
            if image_parent is not None:
                image_parent.remove(image)
            anchor.append(image)
        else:
            anchor.text = label
    _replace_node(element, anchor)


def _resolve_confluence_url(
    href: str,
    page: Page,
    index: _PageIndex,
    output_dir: Path,
    warnings: WarningLog,
    link_report: InterSpaceLinkLog,
    link_text: str,
) -> str | None:
    parsed = urlsplit(href)
    if parsed.scheme or parsed.netloc:
        return None

    query = parse_qs(parsed.query)
    page_id = (query.get("pageId") or [""])[0]
    target: Page | None = None
    problem: str | None = None
    requested_space = ""
    requested_title = ""

    if page_id:
        target, problem = index.find(page_id=page_id)
    if target is None:
        parts = parsed.path.split("/")
        if "display" in parts:
            position = parts.index("display")
            if len(parts) > position + 2:
                requested_space = unquote(parts[position + 1])
                requested_title = unquote(parts[position + 2]).replace("+", " ")
                target, problem = index.find(
                    title=requested_title, space_key=requested_space
                )

    if problem:
        warnings.add(page, problem, href)
    if target is None:
        if requested_space:
            link_report.add(
                page,
                requested_space,
                requested_title,
                page_id,
                link_text,
                None,
            )
        return None
    link_report.add(
        page,
        target.space_key,
        target.title,
        target.page_id,
        link_text,
        target,
    )
    fragment = f"#{parsed.fragment}" if parsed.fragment else ""
    return _markdown_target(page, target.output_path, output_dir) + fragment


def _rewrite_anchor(
    element: html.HtmlElement,
    page: Page,
    index: _PageIndex,
    output_dir: Path,
    warnings: WarningLog,
    link_report: InterSpaceLinkLog,
) -> None:
    href = element.get("href", "")
    if not href:
        return
    link_text = WHITESPACE.sub(" ", element.text_content()).strip() or href
    local = _resolve_confluence_url(
        href, page, index, output_dir, warnings, link_report, link_text
    )
    if local:
        element.set("href", local)
        return
    if not _is_external_url(href) and (
        "pageId=" in href or "/display/" in href
    ):
        warnings.add(page, "unresolved-confluence-url", href)


def _safe_cdata(body: str) -> str:
    return CDATA_PATTERN.sub(
        lambda match: html_stdlib.escape(match.group(1), quote=False),
        body,
    )


def _macro_parameters(macro: html.HtmlElement) -> list[str]:
    values = []
    for parameter in macro:
        if parameter.tag != "ac:parameter":
            continue
        name = parameter.get("ac:name", "")
        value = WHITESPACE.sub(" ", parameter.text_content()).strip()
        if value:
            values.append(f"{name}: {value}" if name else value)
    return values


def _replace_children_macro(
    macro: html.HtmlElement,
    page: Page,
    index: _PageIndex,
    output_dir: Path,
    warnings: WarningLog,
    link_report: InterSpaceLinkLog,
) -> None:
    page_parameter = next(
        (
            parameter
            for parameter in macro
            if parameter.tag == "ac:parameter"
            and parameter.get("ac:name") == "page"
        ),
        None,
    )
    parent = page
    if page_parameter is not None:
        page_link = next(
            (element for element in page_parameter.iter() if element.tag == "ri:page"),
            None,
        )
        if page_link is not None:
            title = page_link.get("ri:content-title", "")
            space_key = page_link.get("ri:space-key") or page.space_key
            page_id = page_link.get("ri:content-id", "")
            parent, problem = index.find(title, space_key, page_id)
            if problem:
                warnings.add(page, problem, f"{space_key}:{title or page_id}")
            if parent is None:
                warnings.add(
                    page, "unresolved-children-root", f"{space_key}:{title or page_id}"
                )
                parent = page
        else:
            title = WHITESPACE.sub(" ", page_parameter.text_content()).strip()
            if title:
                parent, problem = index.find(title, page.space_key)
                if problem:
                    warnings.add(page, problem, f"{page.space_key}:{title}")
                if parent is None:
                    warnings.add(page, "unresolved-children-root", title)
                    parent = page

    depth_parameter = next(
        (
            parameter
            for parameter in macro
            if parameter.tag == "ac:parameter"
            and parameter.get("ac:name") == "depth"
        ),
        None,
    )
    max_depth: int | None = None
    if depth_parameter is not None:
        depth_value = WHITESPACE.sub(" ", depth_parameter.text_content()).strip()
        if depth_value.casefold() != "all":
            try:
                max_depth = max(0, int(depth_value))
            except ValueError:
                warnings.add(page, "invalid-children-depth", depth_value)

    replacement = index.children_toc(
        page, parent, output_dir, max_depth, link_report
    )
    if replacement is None:
        replacement = html.Element("div")
    replacement.tail = macro.tail
    _replace_node(macro, replacement)


def _toc_heading_text(element: html.HtmlElement) -> str:
    parts = [element.text or ""]
    for child in element:
        is_anchor_macro = (
            child.tag == "ac:structured-macro"
            and child.get("ac:name", "").casefold() == "anchor"
        )
        if not is_anchor_macro:
            parts.append(_toc_heading_text(child))
        parts.append(child.tail or "")
    return WHITESPACE.sub(" ", "".join(parts)).strip()


def _build_toc_list(
    entries: list[tuple[int, str, str]],
    start: int = 0,
    parent_level: int = 0,
) -> tuple[html.HtmlElement, int]:
    listing = html.Element("ul")
    while start < len(entries):
        level, title, anchor = entries[start]
        if level <= parent_level:
            break
        item = etree.SubElement(listing, "li")
        link = etree.SubElement(item, "a")
        link.set("href", f"#{anchor}")
        link.text = title
        start += 1
        if start < len(entries) and entries[start][0] > level:
            nested, start = _build_toc_list(entries, start, level)
            item.append(nested)
    return listing, start


def _replace_toc_macro(
    macro: html.HtmlElement, root: html.HtmlElement
) -> None:
    entries = []
    for element in root.iter():
        if not isinstance(element.tag, str) or not re.fullmatch(
            r"h[1-6]", element.tag
        ):
            continue
        title = _toc_heading_text(element)
        if not title:
            continue
        anchor = f"confluence-toc-section-{len(entries) + 1}"
        element.set("data-toc-anchor", anchor)
        entries.append((int(element.tag[1]), title, anchor))

    replacement, _ = _build_toc_list(entries)
    if not entries:
        replacement = html.Element("div")
    replacement.tail = macro.tail
    _replace_node(macro, replacement)


def _is_macro_link_block(element: html.HtmlElement) -> bool:
    if element.get("data-macro-link-output") == "true":
        return True
    return (
        element.tag in {"p", "div"}
        and not (element.text or "").strip()
        and len(element) > 0
        and all(
            not (child.tail or "").strip() and _is_macro_link_block(child)
            for child in element
        )
    )


def _separate_consecutive_macro_links(root: html.HtmlElement) -> None:
    for parent in root.iter():
        if not isinstance(parent.tag, str):
            continue
        children = list(parent)
        for left, right in zip(children, children[1:]):
            if (
                (left.tail or "").strip()
                or not _is_macro_link_block(left)
                or not _is_macro_link_block(right)
            ):
                continue
            insertion_index = parent.index(right)
            for _ in range(2):
                separator = html.Element("br")
                separator.set("data-view-macro-separator", "true")
                parent.insert(insertion_index, separator)
                insertion_index += 1


def _replace_attachment_macro(
    macro: html.HtmlElement,
    page: Page,
    asset_index: _AssetIndex,
    output_dir: Path,
    warnings: WarningLog,
    macro_name: str,
) -> None:
    attachment = next(
        (element for element in macro.iter() if element.tag == "ri:attachment"),
        None,
    )
    filename = attachment.get("ri:filename", "") if attachment is not None else ""
    replacement = html.Element("span")
    if not filename:
        warnings.add(page, "unresolved-attachment-link", macro_name)
        replacement.text = f"[Anhang fehlt: {macro_name}]"
    else:
        asset, problem = asset_index.find(page, filename)
        if problem:
            warnings.add(page, problem, filename)
        if asset is None:
            warnings.add(page, "unresolved-attachment-link", filename)
            replacement.text = f"[Anhang fehlt: {filename}]"
        else:
            replacement.tag = "a"
            replacement.set("href", _relative_asset(page, asset, output_dir))
            replacement.text = filename
    replacement.set("data-macro-link-output", "true")
    replacement.tail = macro.tail
    _replace_node(macro, replacement)


def _replace_macro(
    macro: html.HtmlElement,
    page: Page,
    asset_index: _AssetIndex,
    output_dir: Path,
    warnings: WarningLog,
) -> None:
    name = macro.get("ac:name", "unknown").casefold()
    tail = macro.tail

    if name in {"view-file", "viewpdf", "viewppt"}:
        _replace_attachment_macro(
            macro, page, asset_index, output_dir, warnings, name
        )
        return

    if name == "widget":
        attachment = next(
            (element for element in macro.iter() if element.tag == "ri:attachment"),
            None,
        )
        if attachment is not None:
            _replace_attachment_macro(
                macro, page, asset_index, output_dir, warnings, name
            )
            return

        url_node = next(
            (element for element in macro.iter() if element.tag == "ri:url"),
            None,
        )
        url = url_node.get("ri:value", "") if url_node is not None else ""
        if not url:
            url_parameter = next(
                (
                    element
                    for element in macro
                    if element.tag == "ac:parameter"
                    and element.get("ac:name") == "url"
                ),
                None,
            )
            if url_parameter is not None:
                url = WHITESPACE.sub(" ", url_parameter.text_content()).strip()
        if url:
            replacement = html.Element("a")
            replacement.set("href", url)
            replacement.set("data-macro-link-output", "true")
            replacement.text = url
            replacement.tail = tail
            _replace_node(macro, replacement)
            return

        warnings.add(page, "unresolved-widget-link", "missing URL or attachment")
        replacement = html.Element("span")
        replacement.text = "[Widget-Link fehlt]"
        replacement.tail = tail
        _replace_node(macro, replacement)
        return

    if name == "anchor":
        anchor_name = next(
            (
                parameter.text_content().strip()
                for parameter in macro
                if parameter.tag == "ac:parameter"
                if parameter.text_content().strip()
            ),
            "",
        )
        anchor = html.Element("a")
        if anchor_name:
            anchor.set("id", anchor_name)
        anchor.tail = tail
        _replace_node(macro, anchor)
        return

    if name in {"code", "noformat"}:
        plain_body = next(
            (child for child in macro if child.tag == "ac:plain-text-body"), None
        )
        code_text = plain_body.text_content() if plain_body is not None else ""
        language = next(
            (
                parameter.text_content().strip()
                for parameter in macro
                if parameter.tag == "ac:parameter"
                if parameter.get("ac:name") == "language"
            ),
            "",
        )
        pre = html.Element("pre")
        code = etree.SubElement(pre, "code")
        if language:
            code.set("class", f"language-{_safe_segment(language, 'text')}")
        code.text = code_text.rstrip("\n")
        pre.tail = tail
        _replace_node(macro, pre)
        return

    if name in {"section", "column", "layout", "layout-section", "layout-cell"}:
        for child in macro:
            if child.tag == "ac:rich-text-body":
                child.tag = "div"
                child.attrib.clear()
        macro.tag = "div"
        macro.attrib.clear()
        return

    if name in {"info", "warning", "tip", "note", "panel"}:
        blockquote = html.Element("blockquote")
        heading = etree.SubElement(blockquote, "p")
        strong = etree.SubElement(heading, "strong")
        strong.text = name.capitalize()
        rich_body = next(
            (child for child in macro if child.tag == "ac:rich-text-body"), None
        )
        if rich_body is not None:
            _copy_children(rich_body, blockquote)
        blockquote.tail = tail
        _replace_node(macro, blockquote)
        return

    blockquote = html.Element("blockquote")
    heading = etree.SubElement(blockquote, "p")
    strong = etree.SubElement(heading, "strong")
    strong.text = f"Confluence-Makro: {name}"
    parameters = _macro_parameters(macro)
    for value in parameters:
        paragraph = etree.SubElement(blockquote, "p")
        paragraph.text = value
    rich_body = next(
        (child for child in macro if child.tag == "ac:rich-text-body"), None
    )
    if rich_body is not None:
        _copy_children(rich_body, blockquote)
    blockquote.tail = tail
    _replace_node(macro, blockquote)
    warnings.add(page, "unsupported-macro", name)


def _rewrite_image(
    element: html.HtmlElement,
    page: Page,
    asset_index: _AssetIndex,
    output_dir: Path,
    warnings: WarningLog,
) -> None:
    attachment = next(
        (child for child in element.iter() if child.tag == "ri:attachment"), None
    )
    if attachment is None:
        url = next((child for child in element.iter() if child.tag == "ri:url"), None)
        if url is not None and url.get("ri:value"):
            image = html.Element("img")
            image.set("src", url.get("ri:value"))
            if element.get("ac:alt"):
                image.set("alt", element.get("ac:alt", ""))
            _replace_node(element, image)
        else:
            warnings.add(page, "unsupported-image", "Confluence image without attachment")
        return

    filename = attachment.get("ri:filename", "")
    asset, problem = asset_index.find(page, filename)
    if problem:
        warnings.add(page, problem, filename)
    if asset is None:
        warnings.add(page, "unresolved-image-attachment", filename)
        replacement = html.Element("span")
        replacement.text = f"[Bild-Anhang fehlt: {filename}]"
        _replace_node(element, replacement)
        return

    image = html.Element("img")
    image.set("src", _relative_asset(page, asset, output_dir))
    image.set("alt", element.get("ac:alt", filename))
    for dimension in ("width", "height"):
        value = element.get(f"ac:{dimension}")
        if value:
            image.set(dimension, value)
    _replace_node(element, image)


def _preprocess_body(
    page: Page,
    index: _PageIndex,
    asset_index: _AssetIndex,
    output_dir: Path,
    warnings: WarningLog,
    link_report: InterSpaceLinkLog,
) -> html.HtmlElement:
    root = html.fragment_fromstring(_safe_cdata(page.body), create_parent="div")
    for element in root.iter():
        if isinstance(element.tag, str) and re.fullmatch(r"h[1-5]", element.tag):
            element.tag = f"h{int(element.tag[1]) + 1}"

    for element in list(root.iter()):
        if (
            element.tag != "ac:structured-macro"
            or not isinstance(element.tag, str)
        ):
            continue
        macro_name = element.get("ac:name", "").casefold()
        if macro_name == "children":
            _replace_children_macro(
                element, page, index, output_dir, warnings, link_report
            )
        elif macro_name == "toc":
            _replace_toc_macro(element, root)
    elements = list(root.iter())

    for element in reversed(elements):
        if not isinstance(element.tag, str):
            continue
        if element.tag == "ac:link":
            _handle_confluence_link(
                element,
                page,
                index,
                asset_index,
                output_dir,
                warnings,
                link_report,
            )
        elif element.tag == "ac:image":
            _rewrite_image(element, page, asset_index, output_dir, warnings)
        elif element.tag == "ac:structured-macro":
            _replace_macro(element, page, asset_index, output_dir, warnings)
        elif element.tag == "a":
            _rewrite_anchor(
                element, page, index, output_dir, warnings, link_report
            )
        elif element.tag in {
            "ac:layout",
            "ac:layout-section",
            "ac:layout-cell",
        }:
            element.tag = "div"
            element.attrib.clear()
        elif element.tag == "ac:rich-text-body":
            parent = element.getparent()
            if parent is None or parent.tag != "ac:structured-macro":
                element.tag = "div"
                element.attrib.clear()
    _separate_consecutive_macro_links(root)
    return root


class _ConfluenceMarkdownConverter(MarkdownConverter):
    def convert_hN(
        self, n: int, el: object, text: str, parent_tags: object
    ) -> str:
        heading = super().convert_hN(n, el, text, parent_tags)
        anchor = el.get("data-toc-anchor")
        if anchor:
            return (
                f'<a id="{html_stdlib.escape(anchor, quote=True)}"></a>'
                f"\n{heading}"
            )
        return heading

    def convert_pre(
        self, element: object, text: str, **kwargs: object
    ) -> str:
        code = element.find("code", recursive=False)
        source = code.get_text() if code is not None else element.get_text()
        classes = code.get("class", []) if code is not None else []
        if isinstance(classes, str):
            classes = classes.split()
        language = next(
            (
                item.removeprefix("language-")
                for item in classes
                if item.startswith("language-")
            ),
            "",
        )
        fence_size = max(3, max((len(run) for run in re.findall(r"`+", source)), default=0) + 1)
        fence = "`" * fence_size
        return f"\n\n{fence}{language}\n{source.rstrip()}\n{fence}\n\n"

    def convert_br(self, el: object, text: str, parent_tags: object) -> str:
        if el.get("data-view-macro-separator") == "true":
            return "<br>"
        return super().convert_br(el, text, parent_tags)


def _escape_heading(title: str) -> str:
    return re.sub(r"([\\`*_{}\[\]#+.!|>])", r"\\\1", title)


def _escape_plain_h1_lines(markdown: str) -> str:
    lines = markdown.splitlines()
    fence_character = ""
    fence_length = 0
    fence_pattern = re.compile(r"^ {0,3}(`{3,}|~{3,})")
    heading_pattern = re.compile(r"^(\s*)#(?=\s|$)")

    for index, line in enumerate(lines):
        fence = fence_pattern.match(line)
        if fence_character:
            if (
                fence
                and fence.group(1)[0] == fence_character
                and len(fence.group(1)) >= fence_length
            ):
                fence_character = ""
            continue
        if fence:
            marker = fence.group(1)
            fence_character = marker[0]
            fence_length = len(marker)
            continue
        lines[index] = heading_pattern.sub(r"\1\\#", line)

    return "\n".join(lines)


def _convert_page(
    page: Page,
    index: _PageIndex,
    asset_index: _AssetIndex,
    output_dir: Path,
    warnings: WarningLog,
    link_report: InterSpaceLinkLog,
) -> str:
    root = _preprocess_body(
        page, index, asset_index, output_dir, warnings, link_report
    )
    body_html = html.tostring(root, encoding="unicode", method="html")
    markdown_converter = _ConfluenceMarkdownConverter(
        heading_style="ATX",
        bullets="*",
    )
    body_markdown = markdown_converter.convert(body_html).strip()
    if not body_markdown:
        toc = index.children_toc(page, page, output_dir, None, link_report)
        if toc is not None:
            toc_html = html.tostring(toc, encoding="unicode", method="html")
            body_markdown = markdown_converter.convert(toc_html).strip()
    body_markdown = _escape_plain_h1_lines(body_markdown)
    heading = f"# {_escape_heading(page.title)}"
    content = f"{heading}\n"
    if body_markdown:
        content += f"\n{body_markdown}\n"
    return content


def _copy_page_assets(
    page: Page, output_dir: Path, warnings: WarningLog
) -> int:
    if page.archive_path is None:
        return 0
    copied = 0
    with zipfile.ZipFile(page.archive_path) as archive:
        for assets in page.assets.values():
            for asset in assets:
                target = output_dir.joinpath(*asset.output_path.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    if (
                        target.stat().st_size
                        == archive.getinfo(asset.archive_member).file_size
                    ):
                        copied += 1
                        continue
                    warnings.add(page, "attachment-output-conflict", target.as_posix())
                with archive.open(asset.archive_member) as source, target.open("wb") as dest:
                    while chunk := source.read(1024 * 1024):
                        dest.write(chunk)
                copied += 1
    return copied


def convert_exports(input_dir: Path, output_dir: Path) -> MigrationResult:
    input_dir = input_dir.expanduser().resolve()
    raw_output_dir = output_dir.expanduser()
    if raw_output_dir.is_symlink():
        raise ValueError(f"Refusing to clear symlink output directory {raw_output_dir}")
    output_dir = raw_output_dir.resolve()
    archives = sorted(input_dir.glob("*.zip"))
    if not archives:
        raise ValueError(f"No Confluence export ZIP files found in {input_dir}")

    warnings = WarningLog()
    link_report = InterSpaceLinkLog()
    pages: list[Page] = []
    for archive_path in archives:
        pages.extend(_read_archive(archive_path, warnings))

    pages, omitted_draft_count, draft_aliases = _omit_unchanged_drafts(
        pages, warnings
    )
    _deduplicate_page_paths(pages)
    _deduplicate_asset_paths(pages)
    index = _PageIndex(pages, draft_aliases)
    asset_index = _AssetIndex(pages)
    _clear_output_dir(output_dir, input_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    attachment_count = 0
    for page in pages:
        target = output_dir.joinpath(*page.output_path.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            _convert_page(
                page, index, asset_index, output_dir, warnings, link_report
            ),
            encoding="utf-8",
            newline="\n",
        )
        attachment_count += _copy_page_assets(page, output_dir, warnings)

    warnings.write(output_dir)
    link_report.write(output_dir)
    return MigrationResult(
        page_count=len(pages),
        attachment_count=attachment_count,
        omitted_draft_count=omitted_draft_count,
        warning_count=len(warnings.rows),
        inter_space_link_count=link_report.occurrence_count,
        output_dir=output_dir,
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Convert Confluence XML space-export ZIP files into Markdown."
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=Path("confluence-space-exports"),
        help="Directory containing one or more Confluence export ZIP files.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("generated-markdown"),
        help="Directory where the Markdown vault and attachments are written.",
    )
    args = parser.parse_args(argv)
    try:
        result = convert_exports(args.input_dir, args.output_dir)
        _run_markdownlint(result.output_dir)
    except (
        OSError,
        ValueError,
        zipfile.BadZipFile,
        etree.XMLSyntaxError,
        subprocess.CalledProcessError,
    ) as error:
        parser.error(str(error))
    print(
        f"Generated {result.page_count} pages and "
        f"{result.attachment_count} attachments in {result.output_dir} "
        f"({result.omitted_draft_count} unchanged drafts omitted, "
        f"{result.warning_count} warnings, "
        f"{result.inter_space_link_count} inter-space links)."
    )


if __name__ == "__main__":
    main()
