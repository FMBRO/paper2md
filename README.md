# paper2md

Academic-paper PDF → Markdown batch pipeline. Built per [docs/spec.md](docs/spec.md).

## Install

```
uv sync --no-dev
# Tesseract is needed by ocrmypdf at runtime; install via your OS package manager.
```

uv creates and manages the project virtual environment in `.venv`. Run project
commands through `uv run`; activating the environment manually is optional.

The desktop interface uses Tkinter, which is included with the standard
Windows and macOS Python installers. On Debian/Ubuntu, install it separately
with `sudo apt install python3-tk` if needed.

## Run

Place PDFs under `input/` and run:

```
uv run python -m src.batch_convert --input_dir input --output_dir output --engine marker --enable_ocr
```

Output for each PDF appears at `output/{paper_name}/paper.md`. The batch summary lives at `output/batch_summary.json`.

### Engine modes

The `--engine` option accepts the following modes:

| Mode | Intended use | Current status |
|---|---|---|
| `marker` | Default converter for text-layer PDFs and PDFs processed by OCRmyPDF. It is the primary choice for general academic papers, including equations, figures, and tables. | Available and used by default. |
| `mineru` | Planned alternative for layout-aware conversion when Marker is not suitable. | CLI choice reserved, but not yet wired into the conversion pipeline. |
| `docling` | Planned general-purpose structured-document converter and fallback engine. | CLI choice reserved, but not yet wired into the conversion pipeline. |
| `nougat` | Planned fallback for papers where mathematical expressions are the main priority. | CLI choice reserved, but not yet wired into the conversion pipeline. |

The CLI already accepts all four values to preserve the planned interface.
Currently, selecting an engine other than `marker` records a failure for each
affected PDF in `batch_summary.json`; the remaining PDFs in the batch continue
to be processed.

## Desktop GUI

Start the local desktop interface from the project root:

```
uv run python -m src.gui
```

Choose input and output folders, configure OCR, and click **Start conversion**.
The GUI shows per-PDF stages and live Marker/OCR output. Full process output is
also saved to `output/{paper_name}/logs/pipeline.log`. The first GUI version
uses Marker with one worker; the CLI remains available for all scripted use.

The GUI provides the following checkboxes:

| Checkbox | When enabled | Notes |
|---|---|---|
| **Auto OCR** | Runs OCRmyPDF only when the PDF does not have a usable text layer. | A text-layer PDF is sent directly to Marker. |
| **Force OCR** | Runs OCRmyPDF even when the PDF already has a text layer. | Takes precedence over **Auto OCR** and text-layer detection. |
| **Deskew** | Passes `--deskew` to OCRmyPDF to straighten tilted pages. | Has an effect only when OCR runs. |
| **Clean** | Requests OCRmyPDF's `--clean` preprocessing to remove page noise. | Has an effect only when OCR runs. It requires `unpaper`; when `unpaper` is unavailable, paper2md logs a warning and continues without cleaning. |
| **Skip existing** | Skips a PDF when its `output/{paper_name}/paper.md` already exists. | Useful when resuming a batch without converting completed PDFs again. |

See `docs/spec.md` §7 for the full CLI surface and §8 for `configs/config.yaml`.

## Develop

```
uv sync
uv run pytest
```
