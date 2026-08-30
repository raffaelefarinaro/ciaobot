---
name: convert-documents-to-markdown
description: Convert Word, PowerPoint, Excel, OpenDocument, RTF, EPUB, CSV, and PDF files to Markdown with firecrawl-anydoc.
---

# Convert Documents to Markdown

Use the local Python binding, not a shell converter:

```python
import anydoc
markdown = anydoc.to_markdown("report.docx")
markdown = anydoc.to_markdown_bytes(data)
```

For CSV, whose bytes have no reliable signature, use
`anydoc.to_markdown_bytes(data, "csv")`. The result may be `str` or UTF-8
bytes. Keep the original when it already exists and write a `.md` companion.

Supported extensions are `.doc`, `.docx`, `.docm`, `.ppt`, `.pps`, `.pot`,
`.pptx`, `.pptm`, `.ppsx`, `.ppsm`, `.xls`, `.xlsx`, `.xlsm`, `.xlsb`, `.odt`,
`.ods`, `.odp`, `.rtf`, `.epub`, `.csv`, and `.pdf`. Output preserves headings,
formatting, lists, tables, links, notes, LaTeX equations, and useful asset alt
text.

Respect Ciaobot's 50 MB per-file cap. Large documents must use a bounded
temp-file/worker flow and must not be read unboundedly; report anydoc's
`ResourceLimit` clearly rather than retaining a temporary source. Do not split
documents blindly because page, table, and heading structure can be lost.

Text PDFs convert locally. Scanned/image-only PDFs raise `NeedsOcr`; never claim
that output is complete. With explicit user authorization and configured
credentials, hosted OCR is available:

```python
markdown = anydoc.to_markdown("scan.pdf", ocr="hosted")
```

This sends the whole document to Firecrawl Parse. Otherwise preserve the source
and explain that OCR is required. `Unsupported`, `Malformed`, `Encrypted`,
`MissingPart`, I/O, and other conversion errors are non-fatal in batch work:
identify the filename and continue.
