# README Mode and GUI Option Documentation Design

## Goal

Explain every CLI engine choice and every Desktop GUI checkbox without implying
that unfinished conversion engines are currently usable.

## Design

- Add an engine table under `## Run` with each mode's intended use, current
  implementation status, and relevant caveats.
- State explicitly that Marker is the only wired engine. MinerU, Docling, and
  Nougat remain reserved CLI choices and currently produce a per-PDF failure.
- Add a checkbox table under `## Desktop GUI` covering Auto OCR, Force OCR,
  Deskew, Clean, and Skip existing.
- Describe interactions accurately: Force OCR takes precedence over Auto OCR;
  Deskew and Clean only matter when OCR runs; Clean is skipped with a warning
  when `unpaper` is unavailable.
- Keep the existing commands and concise English README style.

## Verification

Compare every documented label and behavior against `src/batch_convert.py`,
`src/convert_one.py`, `src/gui.py`, and `src/run_ocr.py`, then run a Markdown
diff/whitespace check.
