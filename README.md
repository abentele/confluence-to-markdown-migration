# Confluence XML to Markdown

This project was created with help of an AI coding agent (GitHub Copilot) for a
one-time migration of my private Atlassian Confluence spaces.

**I don't maintain this project.**

## Overview

This tool converts Confluence XML space exports into linked Markdown files that
can be used with Obsidian or Visual Studio Code and browsed on GitHub or GitLab.
It preserves page hierarchy, links between spaces, and attachments while
reporting migration issues for review.

## Features

- Converts Confluence space-export ZIP files into one Markdown note per page.
- Preserves the page hierarchy: ancestor pages become folders beneath a
  top-level folder named after the Confluence space.
- Rewrites internal links across all supplied spaces.
- Copies attachments into `_attachments/` folders next to their pages.
  `._.DS_Store` files are ignored, and duplicate attachment names are
  disambiguated with page and attachment IDs.
- Uses the Confluence page title as the filename and H1. Body headings are
  shifted down one level; no YAML frontmatter is generated.
- Adds a nested table of contents to empty parent pages that have child pages.
- Converts the Confluence `toc` macro into a nested table of contents linking
  to sections on the current page.
- Converts `children` macros into nested lists of descendant-page links,
  respecting the configured depth.
- Converts `attachments` macros into lists of links to all available
  attachments on the current page; macro filters are not applied.
- Converts `view-file`, `viewpdf`, and `viewppt` macros into links to copied
  attachments. Consecutive macro links are separated by a blank line.
- Converts `widget` macros into links to their external URL or attachment.
- Converts Confluence tasks into Markdown checklists, preserving their
  completion status and task text.
- Omits `livesearch`, `recently-updated`, `favpages`, and `contributors`
  macros from notes while reporting them as unsupported macros. Sections
  made empty by omitting these macros are removed.
- Flattens layout macros, preserves code and common callout macros including
  their titles, and marks other dynamic or unsupported macros in the note and
  warning report.

## Requirements

- Python packages listed in `requirements.txt`
- Node.js and npm, used to run `markdownlint-cli2` through `npx`

## Quick start

From the migration folder:

```bash
python3 -m pip install -r requirements.txt
python3 converter.py
python3 -m unittest discover -s tests -v
```

After generating the vault, the converter runs `markdownlint-cli2` to validate
MD025. Its configuration is in `.markdownlint-cli2.jsonc`.

## Input and output

By default, the converter reads ZIP files from `confluence-space-exports` and
writes the vault to `generated-markdown`. Each ZIP must contain `entities.xml`;
attachments are read directly from the ZIP.

**Existing contents of the output folder are cleared before each run.**

Override the paths with:

```bash
python3 converter.py \
  --input-dir confluence-space-exports \
  --output-dir generated-markdown
```

## Page and draft handling

- Current pages, drafts that differ from their published counterpart, and
  deleted or trashed pages are included. Historical page versions are
  excluded.
- Drafts are stored under each space's `_drafts` folder; deleted and trashed
  pages are stored under `_deleted`.
- Empty drafts without a Confluence title are skipped rather than exported
  with a generated `Untitled <id>` heading.
- A draft's `originalVersion` reference is used when it identifies an
  exported current or deleted page. A draft based on a deleted page is stored
  under `_deleted` and omitted if it matches that page.
- If no usable reference exists, drafts are matched by page ID or same-space
  title. Ancestor titles disambiguate duplicate titles.
- Draft comparison ignores insignificant HTML whitespace outside code content
  and volatile macro IDs. It includes attachment basenames and content, not
  export or vault paths.
- A published attachment missing from the draft does not by itself keep a
  duplicate draft. Attachments present on the draft must also exist unchanged
  on the published page.
- Unmatched or ambiguous drafts are retained. Links to an omitted draft ID
  resolve to its matched page.
- The CLI reports how many unchanged drafts were omitted.

## Reports

- `generated-markdown/migration-warnings.csv` lists migration warnings.
- `generated-markdown/inter-space-links.csv` lists links between spaces,
  including source and target pages, display text, resolution status, and
  occurrence counts. Absolute external URLs are excluded.
- `generated-markdown/inter-space-summary.csv` groups inter-space links by
  source/target space pair.

Absolute external Confluence-style URLs remain unchanged and are not reported
as migration warnings.

## Limitations

Markdown cannot reproduce Confluence's pixel-perfect layout or live macro
behavior.
