# paper2md

English academic-paper PDF → Markdown batch pipeline. Built per [docs/spec.md](docs/spec.md).

## Install

```
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
# Tesseract is needed by ocrmypdf at runtime; install via your OS package manager.
```

## Run

Place PDFs under `input/` and run:

```
python -m src.batch_convert --input_dir input --output_dir output --engine marker --enable_ocr
```

Output for each PDF appears at `output/{paper_name}/paper.md`. The batch summary lives at `output/batch_summary.json`.

See `docs/spec.md` §7 for the full CLI surface and §8 for `configs/config.yaml`.

## Develop

```
pip install -r requirements-dev.txt
pytest
```
