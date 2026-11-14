from __future__ import annotations

import csv
import html
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from converter import convert_exports, main


def make_export(
    archive_path: Path,
    space_key: str,
    pages: list[tuple[str, str, str, str]],
    attachment: tuple[str, str, str, bytes] | None = None,
    historical_refs: dict[str, list[str]] | None = None,
    ancestors: dict[str, list[str]] | None = None,
    additional_attachments: list[tuple[str, str, str, bytes]] | None = None,
    missing_attachment_ids: set[str] | None = None,
    original_versions: dict[str, str] | None = None,
    space_name: str | None = None,
) -> None:
    page_objects = []
    body_objects = []
    attachment_objects = []
    attachment_payloads = []
    attachments = ([attachment] if attachment is not None else []) + (
        additional_attachments or []
    )
    missing_attachment_ids = missing_attachment_ids or set()

    for page_id, title, status, body in pages:
        body_id = f"body-{page_id}"
        original_version = (original_versions or {}).get(page_id, "")
        original_version_property = (
            f'<property name="originalVersion">{original_version}</property>'
            if original_version
            else ""
        )
        history = "".join(
            f'<element class="Page"><id>{old_id}</id></element>'
            for old_id in (historical_refs or {}).get(page_id, [])
        )
        history_collection = (
            f'<collection name="historicalVersions">{history}</collection>'
            if history
            else ""
        )
        ancestor_elements = "".join(
            f'<element class="Page"><id>{ancestor_id}</id></element>'
            for ancestor_id in (ancestors or {}).get(page_id, [])
        )
        ancestor_collection = (
            f'<collection name="ancestors">{ancestor_elements}</collection>'
            if ancestor_elements
            else ""
        )
        page_attachments = [
            item for item in attachments if item[0] == page_id
        ]
        attachment_collection = ""
        if page_attachments:
            attachment_elements = "".join(
                f'<element class="Attachment"><id>{item[1]}</id></element>'
                for item in page_attachments
            )
            attachment_collection = (
                f'<collection name="attachments">{attachment_elements}</collection>'
            )
        page_objects.append(
            f"""
            <object class="Page">
              <id>{page_id}</id>
              <property name="title">{title}</property>
              <property name="contentStatus">{status}</property>
              {original_version_property}
              <collection name="bodyContents">
                <element class="BodyContent"><id>{body_id}</id></element>
              </collection>
              {history_collection}
              {ancestor_collection}
              {attachment_collection}
            </object>
            """
        )
        body_objects.append(
            f"""
            <object class="BodyContent">
              <id>{body_id}</id>
              <property name="body">{html.escape(body, quote=False)}</property>
            </object>
            """
        )

    for page_id, attachment_id, filename, payload in attachments:
        attachment_objects.append(f"""
        <object class="Attachment">
          <id>{attachment_id}</id>
          <property name="title">{filename}</property>
          <property name="version">1</property>
          <property name="contentStatus">current</property>
          <property name="containerContent"><id name="id">{page_id}</id></property>
        </object>
        """)
        if attachment_id not in missing_attachment_ids:
            attachment_payloads.append((
                f"attachments/{page_id}/{attachment_id}/1",
                payload,
            ))

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
    <hibernate-generic>
      <object class="Space">
        <property name="key">{space_key}</property>
        <property name="name">{space_name or space_key}</property>
        <property name="spaceStatus">CURRENT</property>
      </object>
      {''.join(page_objects)}
      {''.join(body_objects)}
      {''.join(attachment_objects)}
    </hibernate-generic>
    """
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("entities.xml", xml.encode("utf-8"))
        for attachment_payload in attachment_payloads:
            archive.writestr(*attachment_payload)


class ConverterTests(unittest.TestCase):
    def test_attachments_macro_lists_current_page_assets(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_dir = root / "input"
            input_dir.mkdir()
            make_export(
                input_dir / "space.zip",
                "DEV",
                [
                    ("1", "Other", "current", ""),
                    (
                        "2",
                        "Attachments",
                        "current",
                        "<p>Before</p>"
                        '<ac:structured-macro ac:name="attachments">'
                        '<ac:parameter ac:name="patterns">*.pdf</ac:parameter>'
                        "</ac:structured-macro><p>After</p>",
                    ),
                    (
                        "3",
                        "Empty",
                        "current",
                        '<ac:structured-macro ac:name="attachments"/>',
                    ),
                ],
                additional_attachments=[
                    ("1", "10", "shared.pdf", b"other"),
                    ("1", "11", "other.txt", b"other"),
                    ("2", "20", "shared.pdf", b"first"),
                    ("2", "21", "shared.pdf", b"second"),
                    ("2", "22", "my file.txt", b"text"),
                    ("2", "23", "missing.txt", b"missing"),
                ],
                missing_attachment_ids={"23"},
            )

            result = convert_exports(input_dir, root / "output")

            output = root / "output/DEV"
            note = (output / "Attachments.md").read_text(encoding="utf-8")
            self.assertIn("* [shared.pdf](_attachments/shared--2-20.pdf)", note)
            self.assertIn("* [shared.pdf](_attachments/shared--2-21.pdf)", note)
            self.assertIn("* [my file.txt](_attachments/my%20file.txt)", note)
            self.assertNotIn("other.txt", note)
            self.assertNotIn("missing.txt", note)
            self.assertIn("Before", note)
            self.assertIn("After", note)
            self.assertEqual((output / "_attachments/shared--2-20.pdf").read_bytes(), b"first")
            self.assertEqual((output / "_attachments/shared--2-21.pdf").read_bytes(), b"second")
            self.assertEqual((output / "Empty.md").read_text(encoding="utf-8"), "# Empty\n")
            self.assertEqual(result.attachment_count, 5)
            with (root / "output/migration-warnings.csv").open(encoding="utf-8") as file:
                warnings = list(csv.DictReader(file))
            self.assertEqual(len(warnings), 1)
            self.assertIn("missing-attachment-file", str(warnings[0]))

    def test_widget_macros_render_external_and_attachment_links(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_dir = root / "input"
            input_dir.mkdir()
            make_export(
                input_dir / "space.zip",
                "DEV",
                [
                    (
                        "1",
                        "Widget links",
                        "current",
                        '<ac:structured-macro ac:name="widget">'
                        '<ac:parameter ac:name="url"><ri:url '
                        'ri:value="https://widgets.example/preview"/></ac:parameter>'
                        "</ac:structured-macro>"
                        '<ac:structured-macro ac:name="widget">'
                        '<ac:parameter ac:name="url"><ri:attachment '
                        'ri:filename="preview.pdf"/></ac:parameter>'
                        "</ac:structured-macro>",
                    )
                ],
                ("1", "10", "preview.pdf", b"pdf"),
            )

            result = convert_exports(input_dir, root / "generated-markdown")

            note = (
                root / "generated-markdown/DEV/Widget links.md"
            ).read_text(encoding="utf-8")
            self.assertIn("<https://widgets.example/preview>", note)
            self.assertIn("[preview.pdf](_attachments/preview.pdf)", note)
            self.assertEqual(result.attachment_count, 1)
            self.assertEqual(result.warning_count, 0)

    def test_attachment_view_macros_render_links_to_attachments(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_dir = root / "input"
            input_dir.mkdir()
            make_export(
                input_dir / "space.zip",
                "DEV",
                [
                    (
                        "1",
                        "Attachments",
                        "current",
                        "<table><tbody><tr><td>"
                        "<p>"
                        '<ac:structured-macro ac:name="view-file">'
                        '<ac:parameter ac:name="name"><ri:attachment '
                        'ri:filename="diagram.dot"/></ac:parameter>'
                        "</ac:structured-macro>"
                        '<ac:structured-macro ac:name="viewpdf">'
                        '<ac:parameter ac:name="name"><ri:attachment '
                        'ri:filename="document.pdf"/></ac:parameter>'
                        "</ac:structured-macro></p>"
                        '<p><ac:structured-macro ac:name="viewppt">'
                        '<ac:parameter ac:name="name"><ri:attachment '
                        'ri:filename="slides.pptx"/></ac:parameter>'
                        "</ac:structured-macro></p>"
                        '<p><ac:structured-macro ac:name="viewpdf">'
                        '<ac:parameter ac:name="name"><ri:attachment '
                        'ri:filename="missing.pdf"/></ac:parameter>'
                        "</ac:structured-macro></p>"
                        "</td></tr></tbody></table>",
                    )
                ],
                ("1", "10", "diagram.dot", b"diagram"),
                additional_attachments=[
                    ("1", "11", "document.pdf", b"pdf"),
                    ("1", "12", "slides.pptx", b"ppt"),
                ],
            )

            result = convert_exports(input_dir, root / "generated-markdown")

            note = (
                root / "generated-markdown/DEV/Attachments.md"
            ).read_text(encoding="utf-8")
            self.assertIn("[diagram.dot](_attachments/diagram.dot)", note)
            self.assertIn("[document.pdf](_attachments/document.pdf)", note)
            self.assertIn("[slides.pptx](_attachments/slides.pptx)", note)
            self.assertIn("[Anhang fehlt: missing.pdf]", note)
            self.assertEqual(note.count("<br><br>"), 3)
            self.assertEqual(result.attachment_count, 3)
            self.assertEqual(result.warning_count, 1)

    def test_external_confluence_urls_are_not_reported_as_unresolved(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_dir = root / "input"
            input_dir.mkdir()
            make_export(
                input_dir / "space.zip",
                "DEV",
                [
                    (
                        "1",
                        "Links",
                        "current",
                        '<p><ac:link><ri:url ri:value="https://docs.example/'
                        'display/DOC/External+Article"/>'
                        "<ac:plain-text-link-body>External</ac:plain-text-link-body>"
                        "</ac:link></p>"
                        '<p><a href="/display/UNKNOWN/Missing">Relative</a></p>',
                    )
                ],
            )

            result = convert_exports(input_dir, root / "generated-markdown")

            self.assertEqual(result.warning_count, 1)
            note = (
                root / "generated-markdown/DEV/Links.md"
            ).read_text(encoding="utf-8")
            self.assertIn("https://docs.example/display/DOC/External+Article", note)
            with (
                root / "generated-markdown/migration-warnings.csv"
            ).open(encoding="utf-8-sig", newline="") as stream:
                warnings = list(csv.DictReader(stream))
            self.assertEqual(
                [(row["warning"], row["detail"]) for row in warnings],
                [("unresolved-confluence-url", "/display/UNKNOWN/Missing")],
            )
            with (
                root / "generated-markdown/inter-space-links.csv"
            ).open(encoding="utf-8-sig", newline="") as stream:
                inter_space_links = list(csv.DictReader(stream))
            self.assertEqual(result.inter_space_link_count, 1)
            self.assertEqual(
                [
                    (row["target_space"], row["target_page"])
                    for row in inter_space_links
                ],
                [("UNKNOWN", "Missing")],
            )

    def test_links_pages_across_spaces_and_copies_attachments(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_dir = root / "confluence-space-exports"
            output_dir = root / "generated-markdown"
            input_dir.mkdir()
            output_dir.mkdir()
            (output_dir / "stale.md").write_text("stale", encoding="utf-8")
            make_export(
                input_dir / "space-priv.zip",
                "PRIV",
                [
                    (
                        "1",
                        "Start",
                        "current",
                        '<p><ac:link><ri:page ri:space-key="DEV" '
                        'ri:content-title="Target"/><ac:plain-text-link-body>'
                        "Zum Ziel</ac:plain-text-link-body></ac:link></p>"
                        '<p><ac:link><ri:page ri:space-key="OTHER" '
                        'ri:content-title="Missing"/></ac:link></p>'
                        '<p><ac:link><ri:url ri:value="https://wiki.example/'
                        'display/DEV/Target"/><ac:plain-text-link-body>'
                        "Per URL</ac:plain-text-link-body></ac:link></p>"
                        '<p><ac:image><ri:attachment ri:filename="logo.png"/>'
                        "</ac:image></p>",
                    ),
                    ("10", "Old version", "current", "<p>Alt</p>"),
                    ("3", "Deleted", "deleted", "<p>Geloescht</p>"),
                    ("11", "Trashed", "trashed", "<p>Im Papierkorb</p>"),
                    ("6", "Asset owner", "current", "<p>Anhang</p>"),
                ],
                ("6", "20", "logo.png", b"png-data"),
                historical_refs={"1": ["10"]},
                additional_attachments=[
                    ("6", "21", "._.DS_Store", b"ignored")
                ],
                space_name="Privat Bereich",
            )
            make_export(
                input_dir / "space-dev.zip",
                "DEV",
                [
                    ("2", "Target", "current", "<p>Inhalt</p>"),
                    ("4", "Target", "draft", "<p>Entwurf</p>"),
                ],
                space_name="Development Space",
            )

            result = convert_exports(input_dir, output_dir)

            self.assertEqual(result.page_count, 6)
            self.assertEqual(result.attachment_count, 1)
            self.assertEqual(result.inter_space_link_count, 2)
            self.assertFalse((output_dir / "stale.md").exists())
            source = (output_dir / "Privat Bereich" / "Start.md").read_text(
                encoding="utf-8"
            )
            self.assertIn(
                "[Zum Ziel](../Development%20Space/Target.md)", source
            )
            self.assertIn(
                "[Per URL](https://wiki.example/display/DEV/Target)", source
            )
            self.assertIn(
                "![logo.png](_attachments/logo.png)", source
            )
            self.assertEqual(
                (output_dir / "Privat Bereich/_attachments/logo.png").read_bytes(),
                b"png-data",
            )
            self.assertFalse(
                (output_dir / "Privat Bereich/_attachments/._.DS_Store").exists()
            )
            self.assertTrue(
                (output_dir / "Development Space/_drafts/Target.md").exists()
            )
            self.assertFalse(
                (output_dir / "Privat Bereich/Old version.md").exists()
            )
            self.assertTrue(
                (output_dir / "Privat Bereich/_deleted/Deleted.md").exists()
            )
            self.assertTrue(
                (output_dir / "Privat Bereich/_deleted/Trashed.md").exists()
            )
            with (output_dir / "inter-space-links.csv").open(
                encoding="utf-8-sig", newline=""
            ) as stream:
                inter_space_links = list(csv.DictReader(stream))
            self.assertEqual(len(inter_space_links), 2)
            self.assertEqual(
                list(inter_space_links[0])[:2],
                ["source_space", "target_space"],
            )
            resolved_link = next(
                row for row in inter_space_links if row["target_space"] == "DEV"
            )
            self.assertEqual(resolved_link["source_space"], "PRIV")
            self.assertEqual(resolved_link["target_page"], "Target")
            self.assertEqual(
                resolved_link["target_path"],
                "Development Space/Target.md",
            )
            self.assertEqual(resolved_link["resolution"], "resolved")
            unresolved_link = next(
                row for row in inter_space_links if row["target_space"] == "OTHER"
            )
            self.assertEqual(unresolved_link["target_page"], "Missing")
            self.assertEqual(unresolved_link["resolution"], "unresolved")
            with (output_dir / "inter-space-summary.csv").open(
                encoding="utf-8-sig", newline=""
            ) as stream:
                space_pairs = list(csv.DictReader(stream))
            self.assertEqual(
                {(row["source_space"], row["target_space"]) for row in space_pairs},
                {("PRIV", "DEV"), ("PRIV", "OTHER")},
            )

    def test_only_unchanged_drafts_with_matching_published_pages_are_omitted(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_dir = root / "input"
            input_dir.mkdir()
            make_export(
                input_dir / "space.zip",
                "DEV",
                [
                    (
                        "1",
                        "Same",
                        "current",
                        '<p>Same content</p><ac:structured-macro '
                        'ac:name="info" ac:macro-id="published-id">'
                        "<ac:rich-text-body><p>Details</p></ac:rich-text-body>"
                        "</ac:structured-macro>",
                    ),
                    (
                        "2",
                        "Same",
                        "draft",
                        '<p>Same   content</p>\n<ac:structured-macro '
                        'ac:macro-id="draft-id" ac:name="info">\n'
                        "<ac:rich-text-body>\n  <p>Details</p>\n"
                        "</ac:rich-text-body>\n</ac:structured-macro>",
                    ),
                    ("3", "Changed", "current", "<p>Published</p>"),
                    ("4", "Changed", "draft", "<p>Draft update</p>"),
                    ("5", "Unpublished", "draft", "<p>Draft only</p>"),
                    (
                        "6",
                        "Reference",
                        "current",
                        '<ac:link><ri:page ri:content-id="2" '
                        'ri:content-title="Same"/></ac:link>',
                    ),
                    ("7", "With asset", "current", "<p>Same body</p>"),
                    ("8", "With asset", "draft", "<p>Same body</p>"),
                    (
                        "9",
                        "Code",
                        "current",
                        "<ac:structured-macro ac:name=\"code\">"
                        "<ac:plain-text-body><![CDATA[x = 1\n]]>"
                        "</ac:plain-text-body></ac:structured-macro>",
                    ),
                    (
                        "10",
                        "Code",
                        "draft",
                        "<ac:structured-macro ac:name=\"code\">"
                        "<ac:plain-text-body><![CDATA[x  = 1\n]]>"
                        "</ac:plain-text-body></ac:structured-macro>",
                    ),
                    (
                        "11",
                        "Shared attachment",
                        "current",
                        '<ac:image><ri:attachment ri:filename="shared.png"/>'
                        "</ac:image>",
                    ),
                    (
                        "12",
                        "Shared attachment",
                        "draft",
                        '<ac:image><ri:attachment ri:filename="shared.png"/>'
                        "</ac:image>",
                    ),
                ],
                ("7", "70", "asset.bin", b"published asset"),
                additional_attachments=[
                    ("8", "80", "asset.bin", b"draft asset"),
                    ("11", "110", "shared.png", b"shared attachment"),
                    ("11", "111", "unused.svg", b"unreferenced attachment"),
                ],
            )

            result = convert_exports(input_dir, root / "generated-markdown")

            output_dir = root / "generated-markdown/DEV"
            self.assertEqual(result.omitted_draft_count, 2)
            self.assertEqual(result.page_count, 10)
            self.assertEqual(result.attachment_count, 4)
            self.assertTrue((output_dir / "Same.md").exists())
            self.assertFalse((output_dir / "_drafts/Same.md").exists())
            self.assertTrue((output_dir / "_drafts/Changed.md").exists())
            self.assertTrue((output_dir / "_drafts/Unpublished.md").exists())
            self.assertTrue((output_dir / "_drafts/With asset.md").exists())
            self.assertTrue((output_dir / "_drafts/Code.md").exists())
            self.assertFalse(
                (output_dir / "_drafts/Shared attachment.md").exists()
            )
            self.assertIn(
                "[Same](Same.md)",
                (output_dir / "Reference.md").read_text(encoding="utf-8"),
            )

    def test_attachment_metadata_ids_and_duplicate_missing_records_are_ignored(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_dir = root / "input"
            input_dir.mkdir()
            make_export(
                input_dir / "space.zip",
                "DEV",
                [
                    (
                        "1",
                        "Same attachment page",
                        "current",
                        "<p>Line one<br /> (line two)</p>"
                        '<ac:image><ri:attachment ri:filename="word.reg"/>'
                        "</ac:image>",
                    ),
                    (
                        "2",
                        "Same attachment page",
                        "draft",
                        "<p>Line one<br />(line two)</p>"
                        '<ac:image><ri:attachment ri:filename="word.reg">'
                        '<ri:content-entity ri:content-id="2"/>'
                        "</ri:attachment></ac:image>",
                    ),
                ],
                ("1", "10", "word.reg", b"same file"),
                additional_attachments=[("1", "11", "word.reg", b"duplicate")],
                missing_attachment_ids={"11"},
            )

            result = convert_exports(input_dir, root / "generated-markdown")

            output_dir = root / "generated-markdown/DEV"
            self.assertEqual(result.omitted_draft_count, 1)
            self.assertEqual(result.page_count, 1)
            self.assertEqual(result.attachment_count, 1)
            self.assertTrue((output_dir / "Same attachment page.md").exists())
            self.assertFalse(
                (output_dir / "_drafts/Same attachment page.md").exists()
            )
            self.assertEqual(result.warning_count, 0)

    def test_drafts_linked_to_deleted_originals_are_stored_as_deleted(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_dir = root / "input"
            input_dir.mkdir()
            make_export(
                input_dir / "space.zip",
                "DEV",
                [
                    ("1", "Deleted base", "deleted", "<p>Same</p>"),
                    ("2", "Deleted base", "draft", "<p>Same</p>"),
                    ("3", "Changed original", "deleted", "<p>Original</p>"),
                    ("4", "Changed original", "draft", "<p>Modified</p>"),
                    (
                        "5",
                        "Reference",
                        "current",
                        '<ac:link><ri:page ri:content-id="2" '
                        'ri:content-title="Deleted base"/></ac:link>',
                    ),
                ],
                original_versions={"2": "1", "4": "3"},
            )

            result = convert_exports(input_dir, root / "generated-markdown")

            output_dir = root / "generated-markdown/DEV"
            self.assertEqual(result.omitted_draft_count, 1)
            self.assertEqual(result.page_count, 4)
            self.assertTrue((output_dir / "_deleted/Deleted base.md").exists())
            self.assertFalse((output_dir / "_drafts/Deleted base.md").exists())
            self.assertTrue(
                (output_dir / "_deleted/Changed original--3.md").exists()
            )
            self.assertTrue(
                (output_dir / "_deleted/Changed original--4.md").exists()
            )
            reference = (output_dir / "Reference.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("_deleted/Deleted%20base.md", reference)

    def test_final_excludes_deleted_pages_and_csv_reports(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_dir = root / "input"
            input_dir.mkdir()
            make_export(
                input_dir / "space.zip",
                "DEV",
                [
                    ("1", "Deleted", "deleted", "<p>Same</p>"),
                    ("2", "Deleted", "draft", "<p>Same</p>"),
                    ("3", "Trashed", "trashed", "<p>Trash</p>"),
                    ("4", "Changed", "deleted", "<p>Original</p>"),
                    ("5", "Changed", "draft", "<p>Changed</p>"),
                    ("6", "Draft", "draft", "<p>Draft content</p>"),
                    (
                        "7",
                        "Current",
                        "current",
                        '<ac:link><ri:page ri:content-id="2" '
                        'ri:content-title="Deleted"/></ac:link>',
                    ),
                ],
                ("1", "10", "deleted.pdf", b"deleted"),
                original_versions={"2": "1", "5": "4"},
            )
            output = root / "output"
            convert_exports(input_dir, output)
            self.assertTrue((output / "DEV/_deleted").is_dir())
            self.assertTrue((output / "migration-warnings.csv").exists())

            with patch("converter._run_markdownlint") as lint:
                main(["--input-dir", str(input_dir), "--output-dir", str(output), "--final"])

            lint.assert_called_once_with(output.resolve())
            self.assertFalse((output / "DEV/_deleted").exists())
            self.assertFalse(list(output.rglob("*.csv")))
            self.assertTrue((output / "DEV/_drafts/Draft.md").exists())
            note = (output / "DEV/Current.md").read_text(encoding="utf-8")
            self.assertIn("Deleted", note)
            self.assertNotIn("_deleted", note)
            self.assertFalse(list(output.rglob("deleted.pdf")))

    def test_column_widths_are_not_rendered_but_content_is_preserved(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_dir = root / "input"
            input_dir.mkdir()
            make_export(
                input_dir / "space.zip",
                "DEV",
                [(
                    "1",
                    "Columns",
                    "current",
                    '<ac:structured-macro ac:name="section"><ac:rich-text-body>'
                    '<ac:structured-macro ac:name="column">'
                    '<ac:parameter ac:name="width">60%</ac:parameter>'
                    '<ac:rich-text-body><p>First column: 60% complete</p>'
                    "</ac:rich-text-body></ac:structured-macro>"
                    '<ac:structured-macro ac:name="column">'
                    '<ac:parameter ac:name="width">40%</ac:parameter>'
                    "<ac:rich-text-body><p>Second column</p>"
                    "</ac:rich-text-body></ac:structured-macro>"
                    "</ac:rich-text-body></ac:structured-macro>",
                )],
            )

            result = convert_exports(input_dir, root / "output")

            note = (root / "output/DEV/Columns.md").read_text(encoding="utf-8")
            self.assertIn("First column: 60% complete", note)
            self.assertIn("Second column", note)
            self.assertNotIn("\n60%\n", note)
            self.assertNotIn("40%", note)
            self.assertEqual(result.warning_count, 0)

    def test_table_whitespace_and_column_layout_do_not_keep_a_draft(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_dir = root / "input"
            input_dir.mkdir()
            make_export(
                input_dir / "space.zip",
                "DEV",
                [
                    (
                        "1",
                        "Sample plan",
                        "current",
                        "<h3>Sample section</h3><table>"
                        "<colgroup><col/><col/></colgroup><tbody><tr>"
                        "<th><p>Column A</p></th><td><p><strong>"
                        "Option A/Region One</strong>"
                        '<br class="atl-forced-newline" /> Option B, Region Two'
                        "</p></td></tr></tbody></table>",
                    ),
                    (
                        "2",
                        "Sample plan",
                        "draft",
                        "<h3>Sample section</h3><table><tbody><tr>"
                        "<th>Column A</th><td><p><strong>"
                        "Option A/Region One</strong> Option B, Region Two"
                        "</p></td></tr></tbody></table>",
                    ),
                ],
                original_versions={"2": "1"},
            )

            result = convert_exports(input_dir, root / "generated-markdown")

            output_dir = root / "generated-markdown/DEV"
            self.assertEqual(result.omitted_draft_count, 1)
            self.assertEqual(result.page_count, 1)
            self.assertTrue((output_dir / "Sample plan.md").exists())
            self.assertFalse(
                (output_dir / "_drafts/Sample plan.md").exists()
            )

    def test_empty_untitled_drafts_are_not_exported(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_dir = root / "input"
            input_dir.mkdir()
            make_export(
                input_dir / "space.zip",
                "DEV",
                [
                    ("900001", "", "draft", ""),
                    ("900002", "", "draft", "<p>Draft content</p>"),
                ],
            )

            result = convert_exports(input_dir, root / "generated-markdown")

            self.assertEqual(result.page_count, 1)
            self.assertFalse(
                (root / "generated-markdown/DEV/_drafts/Untitled 900001.md")
                .exists()
            )
            self.assertTrue(
                (root / "generated-markdown/DEV/_drafts/Untitled 900002.md")
                .exists()
            )

    def test_confluence_tasks_render_as_checklists(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_dir = root / "input"
            input_dir.mkdir()
            make_export(
                input_dir / "space.zip",
                "DEV",
                [(
                    "1",
                    "Tasks",
                    "current",
                    "<ac:task-list>"
                    "<ac:task><ac:task-id>5</ac:task-id>"
                    "<ac:task-status>incomplete</ac:task-status>"
                    "<ac:task-body>\n  <strong>Open</strong> task"
                    "</ac:task-body></ac:task>"
                    "<ac:task><ac:task-id>6</ac:task-id>"
                    "<ac:task-status>complete</ac:task-status>"
                    "<ac:task-body><p>Done <em>task</em></p></ac:task-body>"
                    "</ac:task>"
                    "<ac:task><ac:task-status>unknown</ac:task-status>"
                    "<ac:task-body>Review</ac:task-body></ac:task>"
                    "</ac:task-list>",
                )],
            )

            result = convert_exports(input_dir, root / "output")

            note = (root / "output/DEV/Tasks.md").read_text(encoding="utf-8")
            self.assertIn("* [ ] **Open** task", note)
            self.assertIn("* [x] Done *task*", note)
            self.assertIn("* [?] Review", note)
            self.assertNotIn("incomplete", note)
            self.assertNotIn("\n5\n", note)
            with (root / "output/migration-warnings.csv").open(
                encoding="utf-8-sig", newline=""
            ) as stream:
                warnings = list(csv.DictReader(stream))
            self.assertEqual(
                [(row["warning"], row["detail"]) for row in warnings],
                [("invalid-task-status", "unknown")],
            )
            self.assertEqual(result.warning_count, 1)

    def test_selected_dynamic_macros_are_omitted_but_reported(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_dir = root / "input"
            input_dir.mkdir()
            names = ("livesearch", "recently-updated", "favpages", "contributors")
            macros = "".join(
                f'<ac:structured-macro ac:name="{name}">'
                '<ac:parameter ac:name="example">hidden parameter</ac:parameter>'
                "<ac:rich-text-body><p>hidden body</p></ac:rich-text-body>"
                "</ac:structured-macro>"
                for name in names
            )
            make_export(
                input_dir / "space.zip",
                "DEV",
                [(
                    "1",
                    "Dynamic",
                    "current",
                    f"<p>Before {macros} after</p>"
                    '<ac:structured-macro ac:name="other"/>',
                )],
            )

            result = convert_exports(input_dir, root / "output")

            note = (root / "output/DEV/Dynamic.md").read_text(encoding="utf-8")
            self.assertIn("Before", note)
            self.assertIn("after", note)
            self.assertNotIn("hidden", note)
            for name in names:
                self.assertNotIn(name, note)
            self.assertIn("Confluence-Makro: other", note)
            with (root / "output/migration-warnings.csv").open(
                encoding="utf-8-sig", newline=""
            ) as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(
                {(row["source_page"], row["warning"], row["detail"]) for row in rows},
                {
                    ("DEV/Dynamic.md", "unsupported-macro", name)
                    for name in (*names, "other")
                },
            )
            self.assertEqual(result.warning_count, 5)

    def test_omitted_macros_remove_only_newly_empty_sections(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_dir = root / "input"
            input_dir.mkdir()
            make_export(
                input_dir / "space.zip",
                "DEV",
                [(
                    "1",
                    "Sections",
                    "current",
                    '<ac:structured-macro ac:name="toc"/>'
                    "<h1>Remove parent</h1><h2>Remove child</h2>"
                    '<div><p><ac:structured-macro ac:name="livesearch"/></p></div>'
                    "<h1>Keep parent</h1><h2>Keep child</h2>"
                    '<ac:structured-macro ac:name="favpages"/><p>Real content</p>'
                    "<h1>Keep image</h1>"
                    '<ac:structured-macro ac:name="contributors"/>'
                    '<ac:image><ri:attachment ri:filename="image.png"/></ac:image>'
                    "<h1>Already empty</h1>"
                    "<h1>Remove inline</h1><p>"
                    '<ac:structured-macro ac:name="recently-updated"/></p>'
                    "<h1><ac:structured-macro ac:name=\"favpages\"/></h1>"
                    "<h1>Last section</h1><p>Visible</p>",
                )],
                ("1", "10", "image.png", b"image"),
            )

            result = convert_exports(input_dir, root / "output")

            note = (root / "output/DEV/Sections.md").read_text(encoding="utf-8")
            for title in ("Remove parent", "Remove child", "Remove inline"):
                self.assertNotIn(title, note)
            for title in (
                "Keep parent", "Keep child", "Keep image", "Already empty",
                "Last section",
            ):
                self.assertIn(title, note)
            self.assertIn("[Keep child](#confluence-toc-section-", note)
            self.assertIn("![image.png](_attachments/image.png)", note)
            self.assertIn("Real content", note)
            self.assertEqual(result.warning_count, 4)

    def test_code_macro_is_preserved_as_a_fenced_block(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_dir = root / "input"
            input_dir.mkdir()
            make_export(
                input_dir / "space.zip",
                "DEV",
                [
                    (
                        "5",
                        "Code",
                        "current",
                        "<ac:structured-macro ac:name=\"code\">"
                        "<ac:parameter ac:name=\"language\">python</ac:parameter>"
                        "<ac:plain-text-body><![CDATA[print('<ok>')]]>"
                        "</ac:plain-text-body></ac:structured-macro>",
                    )
                ],
            )

            convert_exports(input_dir, root / "generated-markdown")
            note = (root / "generated-markdown/DEV/Code.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("```python\nprint('<ok>')\n```", note)

    def test_info_macro_text_is_preserved(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_dir = root / "input"
            input_dir.mkdir()
            make_export(
                input_dir / "space.zip",
                "DEV",
                [
                    (
                        "9",
                        "Information",
                        "current",
                        '<ac:structured-macro ac:name="info">'
                        "<ac:rich-text-body><p>Important "
                        "<strong>information</strong> here.</p>"
                        "</ac:rich-text-body></ac:structured-macro>",
                    )
                ],
            )

            convert_exports(input_dir, root / "generated-markdown")

            note = (root / "generated-markdown/DEV/Information.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("> **Info**", note)
            self.assertIn("> Important **information** here.", note)

    def test_callout_macro_uses_its_title(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_dir = root / "input"
            input_dir.mkdir()
            make_export(
                input_dir / "space.zip",
                "DEV",
                [(
                    "1",
                    "Callouts",
                    "current",
                    '<ac:structured-macro ac:name="tip">'
                    '<ac:parameter ac:name="title">Welcome to your new space!'
                    "</ac:parameter>"
                    "<ac:rich-text-body><p>Tip text</p></ac:rich-text-body>"
                    "</ac:structured-macro>"
                    '<ac:structured-macro ac:name="warning">'
                    '<ac:parameter ac:name="title">   </ac:parameter>'
                    "<ac:rich-text-body><p>Warning text</p></ac:rich-text-body>"
                    "</ac:structured-macro>",
                )],
            )

            convert_exports(input_dir, root / "output")

            note = (root / "output/DEV/Callouts.md").read_text(encoding="utf-8")
            self.assertIn("> **Welcome to your new space!**", note)
            self.assertIn("> Tip text", note)
            self.assertIn("> **Warning**", note)
            self.assertIn("> Warning text", note)

    def test_same_folder_attachment_names_do_not_overwrite_each_other(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_dir = root / "input"
            input_dir.mkdir()
            make_export(
                input_dir / "space.zip",
                "DEV",
                [
                    (
                        "1",
                        "First",
                        "current",
                        '<ac:image><ri:attachment ri:filename="shared.png"/>'
                        "</ac:image>",
                    ),
                    (
                        "2",
                        "Second",
                        "current",
                        '<ac:image><ri:attachment ri:filename="shared.png"/>'
                        "</ac:image>",
                    ),
                ],
                ("1", "10", "shared.png", b"first"),
                additional_attachments=[
                    ("2", "20", "shared.png", b"second")
                ],
            )

            result = convert_exports(input_dir, root / "generated-markdown")

            output_dir = root / "generated-markdown/DEV"
            self.assertEqual(result.attachment_count, 2)
            self.assertEqual(
                (output_dir / "_attachments/shared.png").read_bytes(), b"first"
            )
            self.assertEqual(
                (output_dir / "_attachments/shared--2-20.png").read_bytes(),
                b"second",
            )
            second_note = (output_dir / "Second.md").read_text(
                encoding="utf-8"
            )
            self.assertIn(
                "![shared.png](_attachments/shared--2-20.png)", second_note
            )

    def test_children_macro_renders_nested_toc_with_configured_depth(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_dir = root / "input"
            input_dir.mkdir()
            make_export(
                input_dir / "space.zip",
                "DEV",
                [
                    (
                        "1",
                        "Root",
                        "current",
                        '<ac:structured-macro ac:name="children">'
                        '<ac:parameter ac:name="depth">all</ac:parameter>'
                        "</ac:structured-macro>",
                    ),
                    ("2", "Child", "current", ""),
                    ("3", "Grandchild", "current", ""),
                    ("4", "Sibling", "current", ""),
                    ("8", "Duplicate", "current", ""),
                    ("9", "Duplicate", "current", ""),
                    (
                        "5",
                        "Limited",
                        "current",
                        '<ac:structured-macro ac:name="children">'
                        '<ac:parameter ac:name="depth">1</ac:parameter>'
                        "</ac:structured-macro>",
                    ),
                    ("6", "Direct", "current", ""),
                    ("7", "Nested", "current", ""),
                ],
                ancestors={
                    "2": ["1"],
                    "3": ["1", "2"],
                    "4": ["1"],
                    "8": ["1"],
                    "9": ["1"],
                    "6": ["5"],
                    "7": ["5", "6"],
                },
            )

            convert_exports(input_dir, root / "generated-markdown")

            generated = root / "generated-markdown/DEV"
            root_note = (generated / "Root.md").read_text(encoding="utf-8")
            limited_note = (generated / "Limited.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("[Child](Root/Child.md)", root_note)
            self.assertIn("[Grandchild](Root/Child/Grandchild.md)", root_note)
            self.assertIn("[Sibling](Root/Sibling.md)", root_note)
            self.assertIn("[Direct](Limited/Direct.md)", limited_note)
            self.assertNotIn("[Nested](Limited/Direct/Nested.md)", limited_note)
            self.assertTrue((generated / "Root/Duplicate--8.md").exists())
            self.assertTrue((generated / "Root/Duplicate--9.md").exists())

    def test_toc_macro_links_to_all_sections_on_the_current_page(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_dir = root / "input"
            input_dir.mkdir()
            make_export(
                input_dir / "space.zip",
                "DEV",
                [
                    (
                        "1",
                        "Sample page",
                        "current",
                        "<ac:structured-macro ac:name=\"toc\"/>"
                        "<h1><ac:structured-macro ac:name=\"anchor\">"
                        "<ac:parameter ac:name=\"\">intro_anchor</ac:parameter>"
                        "</ac:structured-macro>Section one</h1>"
                        "<p>Body</p><h2>Section two</h2><h1>Section three</h1>",
                    )
                ],
            )

            result = convert_exports(input_dir, root / "generated-markdown")

            note = (
                root / "generated-markdown/DEV/Sample page.md"
            ).read_text(encoding="utf-8")
            self.assertIn(
                "[Section one](#confluence-toc-section-1)", note
            )
            self.assertIn("[Section two](#confluence-toc-section-2)", note)
            self.assertIn("[Section three](#confluence-toc-section-3)", note)
            self.assertIn('<a id="confluence-toc-section-1"></a>', note)
            self.assertNotIn("Confluence-Makro: toc", note)
            self.assertEqual(result.warning_count, 0)

    def test_empty_parent_page_gets_a_nested_children_toc(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_dir = root / "input"
            input_dir.mkdir()
            make_export(
                input_dir / "space.zip",
                "DEV",
                [
                    ("1", "Parent page", "current", ""),
                    ("2", "Child page", "current", ""),
                    ("3", "Nested page", "current", ""),
                    ("4", "Other page", "current", "<p>Existing content</p>"),
                    ("5", "Other child", "current", ""),
                ],
                ancestors={
                    "2": ["1"],
                    "3": ["1", "2"],
                    "5": ["4"],
                },
            )

            convert_exports(input_dir, root / "generated-markdown")

            generated = root / "generated-markdown/DEV"
            parent_page = (generated / "Parent page.md").read_text(
                encoding="utf-8"
            )
            other_page = (generated / "Other page.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("[Child page](Parent%20page/Child%20page.md)", parent_page)
            self.assertIn(
                "[Nested page](Parent%20page/Child%20page/Nested%20page.md)",
                parent_page,
            )
            self.assertNotIn("[Other child]", other_page)

    def test_body_headings_are_nested_under_the_page_title(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_dir = root / "input"
            input_dir.mkdir()
            make_export(
                input_dir / "space.zip",
                "DEV",
                [
                    (
                        "8",
                        "Example (sample) page",
                        "current",
                        "<h1>Section</h1><h2>Subsection</h2>"
                        "<p># command line</p><pre><code># code line</code></pre>",
                    )
                ],
            )

            convert_exports(input_dir, root / "generated-markdown")

            note_path = (
                root
                / "generated-markdown/DEV/"
                / "Example (sample) page.md"
            )
            note = note_path.read_text(encoding="utf-8")
            self.assertFalse(note.startswith("---\n"))
            code_fence = False
            h1_count = 0
            for line in note.splitlines():
                if line.startswith("```"):
                    code_fence = not code_fence
                elif not code_fence and line.startswith("# "):
                    h1_count += 1
            self.assertEqual(h1_count, 1)
            self.assertIn(
                "# Example (sample) page", note
            )
            self.assertNotIn(r"\(", note)
            self.assertNotIn(r"\)", note)
            self.assertIn("## Section", note)
            self.assertIn("### Subsection", note)
            self.assertIn(r"\# command line", note)
            self.assertIn("# code line", note)


if __name__ == "__main__":
    unittest.main()
