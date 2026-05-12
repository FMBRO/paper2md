# paper2md 実装計画

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 英語論文PDFをバッチでMarkdownに変換するパイプラインを `docs/spec.md` の仕様通りに実装する。

**Architecture:** 単一PDFを「検査 → (必要なら) OCR → 変換エンジン → 図抽出 → Markdown正規化 → 品質評価」の直線パイプラインで処理し、`batch_convert.py` がバッチループ・失敗継続・サマリ出力を担う。各モジュールは subprocess での外部CLI呼び出しを担う薄いラッパーで、テストでは subprocess を mock してエンジン本体は走らせない。Markdown正規化と品質評価は純粋なテキスト処理で完結する。

**Tech Stack:** Python 3.10+ / pytest / PyMuPDF (`fitz`) / Pillow / PyYAML / Marker (`marker_single`) / OCRmyPDF (`ocrmypdf`)。後続フェーズで MinerU (`magic-pdf`) / Docling (`docling`) / pix2tex を追加。

**Scope:** 本計画は §16 の Step 1〜4 を完全実装する（Marker単独で動く一括変換 + 正規化 + 品質レポート + バッチサマリ）。Step 5〜6（他エンジン追加・pix2texによる数式補正・LLM整形）は末尾の「将来作業」に概要のみ記載する。

---

## ファイル構成

`docs/spec.md` §6 のディレクトリ構成に合わせる。

```
paper2md/
├── input/                    # .gitkeep のみ
├── output/                   # .gitkeep のみ
├── src/
│   ├── __init__.py
│   ├── batch_convert.py      # CLI エントリポイント / バッチループ
│   ├── convert_one.py        # 単一PDFオーケストレーション (§13.1)
│   ├── inspect_pdf.py        # has_text_layer / page_count / render_pages
│   ├── run_ocr.py            # OCRmyPDF サブプロセスラッパー
│   ├── run_marker.py         # Marker サブプロセスラッパー + 出力 .md の発見
│   ├── extract_figures.py    # markerの images/ を figures/ に整理しMarkdownのパスを書換
│   ├── normalize_markdown.py # 見出し/数式/キャプション/References の正規化
│   ├── evaluate_quality.py   # quality_report.md 生成
│   └── config.py             # configs/config.yaml + CLI 上書きを Settings に統合
├── configs/
│   └── config.yaml           # §8 の構造そのまま
├── tests/
│   ├── conftest.py           # PDF/Markdown フィクスチャ生成
│   ├── test_inspect_pdf.py
│   ├── test_run_ocr.py
│   ├── test_run_marker.py
│   ├── test_extract_figures.py
│   ├── test_normalize_markdown.py
│   ├── test_evaluate_quality.py
│   ├── test_convert_one.py
│   └── test_batch_convert.py
├── requirements.txt
├── requirements-dev.txt
├── .gitignore
└── README.md
```

**設計上の判断:**
- spec §6 のモジュール表に `main.py` も載っているが、`batch_convert.py` が CLI エントリを兼ねるので不要。`run_mineru.py` / `run_docling.py` は将来作業セクションで追加する。
- `convert_one.py` は spec §13.1 の `convert_one_pdf` を独立モジュール化したもの。バッチループと単一処理は責務が違うので分ける。
- `render_pages` は PyMuPDF を使う点で `has_text_layer` と同居しても自然なので `inspect_pdf.py` に同梱する。
- 出力 `paper.json` (§2.2) は中間構造データだが spec はその構造を一切定義していないので、初期実装では Marker の `metadata.json` をそのまま `paper.json` にコピーする。

---

## Task 1: リポジトリスキャフォールド

**Files:**
- Create: `requirements.txt`, `requirements-dev.txt`, `.gitignore`, `src/__init__.py`, `tests/__init__.py`, `tests/conftest.py`, `input/.gitkeep`, `output/.gitkeep`, `configs/config.yaml`, `pytest.ini`

- [ ] **Step 1: `requirements.txt` を作成**

```text
pymupdf>=1.24
pillow>=10.0
pyyaml>=6.0
marker-pdf>=1.0
ocrmypdf>=16.0
```

- [ ] **Step 2: `requirements-dev.txt` を作成**

```text
-r requirements.txt
pytest>=8.0
pytest-mock>=3.12
reportlab>=4.0
```

- [ ] **Step 3: `.gitignore` を作成**

```text
__pycache__/
*.pyc
.pytest_cache/
.venv/
venv/
input/*.pdf
output/*/
!output/.gitkeep
*.egg-info/
```

- [ ] **Step 4: `pytest.ini` を作成**

```ini
[pytest]
testpaths = tests
python_files = test_*.py
addopts = -ra --strict-markers
```

- [ ] **Step 5: 空の `src/__init__.py` と `tests/__init__.py` を作成**

- [ ] **Step 6: 空の `input/.gitkeep` と `output/.gitkeep` を作成**

- [ ] **Step 7: `configs/config.yaml` を spec §8 のままコピー**

```yaml
input_dir: input
output_dir: output

language: eng

ocr:
  enabled: auto
  force: false
  tool: ocrmypdf
  options:
    deskew: true
    clean: true

converter:
  engine: marker
  fallback_engines:
    - mineru
    - docling

figures:
  extract: true
  output_dir: figures
  naming_rule: figure_{index:03d}.png

markdown:
  normalize_headings: true
  normalize_equations: true
  normalize_captions: true
  normalize_references: true

batch:
  workers: 1
  skip_existing: false
  overwrite: false
  continue_on_error: true

quality:
  enabled: true
  check_equations: true
  check_figures: true
  check_captions: true
  check_reading_order: true
```

- [ ] **Step 8: `tests/conftest.py` を作成（PDFフィクスチャ生成）**

```python
"""Shared pytest fixtures: generate small synthetic PDFs for the test suite."""
from pathlib import Path

import fitz
import pytest
from PIL import Image, ImageDraw


@pytest.fixture
def text_pdf(tmp_path: Path) -> Path:
    """Tiny PDF that has a real text layer."""
    pdf_path = tmp_path / "text_paper.pdf"
    doc = fitz.open()
    page = doc.new_page()
    body = ("This is a synthetic test paper used by the paper2md test suite. "
            "It contains enough characters to satisfy the text-layer heuristic. ") * 4
    page.insert_text((72, 72), body, fontsize=10)
    doc.save(pdf_path)
    doc.close()
    return pdf_path


@pytest.fixture
def image_pdf(tmp_path: Path) -> Path:
    """Tiny PDF with NO text layer — a single rasterized page."""
    pdf_path = tmp_path / "image_paper.pdf"
    img = Image.new("RGB", (612, 792), "white")
    draw = ImageDraw.Draw(img)
    draw.text((60, 60), "rasterized page (no text layer)", fill="black")
    img.save(pdf_path, "PDF", resolution=72)
    return pdf_path
```

- [ ] **Step 9: コミット**

```bash
git init
git add .
git commit -m "chore: scaffold paper2md repository layout"
```

---

## Task 2: `inspect_pdf` — テキストレイヤー判定とページ画像化

**Files:**
- Create: `src/inspect_pdf.py`, `tests/test_inspect_pdf.py`

Spec §5.1, §5.4 を実装する。

- [ ] **Step 1: 失敗テストを書く — `has_text_layer` がテキストPDFに True を返す**

`tests/test_inspect_pdf.py`:

```python
import json
from pathlib import Path

from src.inspect_pdf import (
    has_text_layer,
    inspect_pdf,
    render_pages,
    write_text_layer_report,
)


def test_has_text_layer_true_for_text_pdf(text_pdf: Path) -> None:
    assert has_text_layer(text_pdf) is True


def test_has_text_layer_false_for_image_pdf(image_pdf: Path) -> None:
    assert has_text_layer(image_pdf) is False


def test_has_text_layer_threshold(text_pdf: Path) -> None:
    # If we demand 100000 chars, even the text fixture fails the threshold.
    assert has_text_layer(text_pdf, min_chars=100_000) is False
```

- [ ] **Step 2: 実行して失敗を確認**

Run: `pytest tests/test_inspect_pdf.py -v`
Expected: ImportError — `src.inspect_pdf` が存在しない。

- [ ] **Step 3: `has_text_layer` を実装**

`src/inspect_pdf.py`:

```python
"""PDF inspection: text-layer detection, page count, page rasterization."""
from __future__ import annotations

import json
from pathlib import Path

import fitz


def has_text_layer(pdf_path: Path | str, min_chars: int = 100, max_pages: int = 3) -> bool:
    """Return True when the first ``max_pages`` pages contain at least ``min_chars`` of text."""
    doc = fitz.open(str(pdf_path))
    try:
        text = ""
        for i, page in enumerate(doc):
            if i >= max_pages:
                break
            text += page.get_text()
        return len(text.strip()) >= min_chars
    finally:
        doc.close()
```

- [ ] **Step 4: テストを再実行してパスを確認**

Run: `pytest tests/test_inspect_pdf.py::test_has_text_layer_true_for_text_pdf tests/test_inspect_pdf.py::test_has_text_layer_false_for_image_pdf tests/test_inspect_pdf.py::test_has_text_layer_threshold -v`
Expected: 3 PASS。

- [ ] **Step 5: 失敗テスト追加 — `inspect_pdf` がレポート dict を返す**

`tests/test_inspect_pdf.py` に追記:

```python
def test_inspect_pdf_returns_report(text_pdf: Path) -> None:
    report = inspect_pdf(text_pdf)
    assert report["input_pdf"] == str(text_pdf)
    assert report["has_text_layer"] is True
    assert report["page_count"] == 1
    assert report["checked_pages"] == 1
    assert report["extracted_char_count"] > 100
```

- [ ] **Step 6: テスト実行で失敗を確認**

Run: `pytest tests/test_inspect_pdf.py::test_inspect_pdf_returns_report -v`
Expected: FAIL — `inspect_pdf` 未定義。

- [ ] **Step 7: `inspect_pdf` を実装**

`src/inspect_pdf.py` に追加:

```python
def inspect_pdf(pdf_path: Path | str, min_chars: int = 100, max_pages: int = 3) -> dict:
    """Build the §5.1.3 inspection record for a PDF."""
    pdf_path = Path(pdf_path)
    doc = fitz.open(str(pdf_path))
    try:
        text = ""
        checked = 0
        for i, page in enumerate(doc):
            if i >= max_pages:
                break
            text += page.get_text()
            checked = i + 1
        char_count = len(text.strip())
        return {
            "input_pdf": str(pdf_path),
            "has_text_layer": char_count >= min_chars,
            "page_count": doc.page_count,
            "checked_pages": checked,
            "extracted_char_count": char_count,
        }
    finally:
        doc.close()
```

- [ ] **Step 8: テスト再実行してパス確認**

Run: `pytest tests/test_inspect_pdf.py::test_inspect_pdf_returns_report -v`
Expected: PASS。

- [ ] **Step 9: 失敗テスト追加 — `write_text_layer_report` がログJSONを書く**

```python
def test_write_text_layer_report(text_pdf: Path, tmp_path: Path) -> None:
    logs_dir = tmp_path / "logs"
    out = write_text_layer_report(text_pdf, logs_dir)
    assert out == logs_dir / "text_layer_check.json"
    payload = json.loads(out.read_text())
    assert payload["has_text_layer"] is True
```

- [ ] **Step 10: 実装**

```python
def write_text_layer_report(pdf_path: Path | str, logs_dir: Path | str) -> Path:
    """Write the inspection record to ``{logs_dir}/text_layer_check.json``."""
    logs_dir = Path(logs_dir)
    logs_dir.mkdir(parents=True, exist_ok=True)
    out_path = logs_dir / "text_layer_check.json"
    out_path.write_text(json.dumps(inspect_pdf(pdf_path), indent=2))
    return out_path
```

- [ ] **Step 11: 失敗テスト追加 — `render_pages` が PNG を生成**

```python
def test_render_pages_writes_one_png_per_page(text_pdf: Path, tmp_path: Path) -> None:
    pages_dir = tmp_path / "pages"
    paths = render_pages(text_pdf, pages_dir)
    assert len(paths) == 1
    assert paths[0] == pages_dir / "page_001.png"
    assert paths[0].exists()
    assert paths[0].stat().st_size > 0
```

- [ ] **Step 12: `render_pages` を実装**

```python
def render_pages(pdf_path: Path | str, output_dir: Path | str, zoom: float = 2.0) -> list[Path]:
    """Rasterize every page to ``output_dir/page_{NNN}.png`` and return the paths."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(str(pdf_path))
    matrix = fitz.Matrix(zoom, zoom)
    paths: list[Path] = []
    try:
        for i, page in enumerate(doc):
            out = output_dir / f"page_{i + 1:03d}.png"
            page.get_pixmap(matrix=matrix).save(out)
            paths.append(out)
        return paths
    finally:
        doc.close()
```

- [ ] **Step 13: フルテスト実行**

Run: `pytest tests/test_inspect_pdf.py -v`
Expected: 全テスト PASS。

- [ ] **Step 14: コミット**

```bash
git add src/inspect_pdf.py tests/test_inspect_pdf.py
git commit -m "feat(inspect_pdf): text-layer detection, inspection report, page rasterization"
```

---

## Task 3: `run_ocr` — OCRmyPDF ラッパー

**Files:**
- Create: `src/run_ocr.py`, `tests/test_run_ocr.py`

Spec §5.3.2 をラップ。subprocess は mock する。

- [ ] **Step 1: 失敗テスト — 正しいコマンドが組み立てられる**

`tests/test_run_ocr.py`:

```python
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.run_ocr import OCRError, run_ocrmypdf


def test_run_ocrmypdf_builds_expected_command(mocker, tmp_path: Path) -> None:
    src_pdf = tmp_path / "in.pdf"
    src_pdf.write_bytes(b"%PDF-1.4")
    dst_pdf = tmp_path / "out.pdf"
    fake_run = mocker.patch("src.run_ocr.subprocess.run",
                            return_value=MagicMock(returncode=0))

    run_ocrmypdf(src_pdf, dst_pdf, lang="eng", deskew=True, clean=True)

    args = fake_run.call_args.args[0]
    assert args[0] == "ocrmypdf"
    assert "-l" in args and args[args.index("-l") + 1] == "eng"
    assert "--deskew" in args
    assert "--clean" in args
    assert args[-2:] == [str(src_pdf), str(dst_pdf)]


def test_run_ocrmypdf_omits_flags_when_disabled(mocker, tmp_path: Path) -> None:
    src_pdf = tmp_path / "in.pdf"
    src_pdf.write_bytes(b"%PDF-1.4")
    dst_pdf = tmp_path / "out.pdf"
    fake_run = mocker.patch("src.run_ocr.subprocess.run",
                            return_value=MagicMock(returncode=0))

    run_ocrmypdf(src_pdf, dst_pdf, lang="eng", deskew=False, clean=False)

    args = fake_run.call_args.args[0]
    assert "--deskew" not in args
    assert "--clean" not in args


def test_run_ocrmypdf_raises_on_failure(mocker, tmp_path: Path) -> None:
    src_pdf = tmp_path / "in.pdf"
    src_pdf.write_bytes(b"%PDF-1.4")
    mocker.patch("src.run_ocr.subprocess.run",
                 return_value=MagicMock(returncode=2, stderr=b"boom"))

    with pytest.raises(OCRError):
        run_ocrmypdf(src_pdf, tmp_path / "out.pdf")
```

- [ ] **Step 2: 失敗確認**

Run: `pytest tests/test_run_ocr.py -v`
Expected: ImportError。

- [ ] **Step 3: 実装**

`src/run_ocr.py`:

```python
"""Wrapper around the `ocrmypdf` CLI (spec §5.3.2)."""
from __future__ import annotations

import subprocess
from pathlib import Path


class OCRError(RuntimeError):
    """Raised when ocrmypdf exits non-zero."""


def run_ocrmypdf(
    input_pdf: Path | str,
    output_pdf: Path | str,
    *,
    lang: str = "eng",
    deskew: bool = True,
    clean: bool = True,
) -> Path:
    """Run ``ocrmypdf`` and return the output path on success."""
    cmd: list[str] = ["ocrmypdf", "-l", lang]
    if deskew:
        cmd.append("--deskew")
    if clean:
        cmd.append("--clean")
    cmd.extend([str(input_pdf), str(output_pdf)])

    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        raise OCRError(
            f"ocrmypdf failed (exit {result.returncode}): {result.stderr.decode(errors='replace')}"
        )
    return Path(output_pdf)
```

- [ ] **Step 4: テスト実行**

Run: `pytest tests/test_run_ocr.py -v`
Expected: 3 PASS。

- [ ] **Step 5: コミット**

```bash
git add src/run_ocr.py tests/test_run_ocr.py
git commit -m "feat(run_ocr): ocrmypdf subprocess wrapper"
```

---

## Task 4: `run_marker` — Marker ラッパー + 出力 .md の発見

**Files:**
- Create: `src/run_marker.py`, `tests/test_run_marker.py`

Spec §5.2.2。Marker は出力先ディレクトリ配下に `{stem}/` を作って `.md` を置く実装が多いため、戻り値として `.md` パスを再発見するロジックも入れる。

- [ ] **Step 1: 失敗テスト**

`tests/test_run_marker.py`:

```python
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.run_marker import MarkerError, run_marker


def test_run_marker_invokes_cli_with_output_dir(mocker, tmp_path: Path) -> None:
    src_pdf = tmp_path / "paper.pdf"
    src_pdf.write_bytes(b"%PDF-1.4")
    out_dir = tmp_path / "marker"

    def fake_run(_args, **_kw):
        # Marker writes paper/paper.md under the output dir
        produced = out_dir / "paper"
        produced.mkdir(parents=True)
        (produced / "paper.md").write_text("# Paper\n")
        return MagicMock(returncode=0)

    mocker.patch("src.run_marker.subprocess.run", side_effect=fake_run)
    md_path = run_marker(src_pdf, out_dir)
    assert md_path == out_dir / "paper" / "paper.md"
    assert md_path.exists()


def test_run_marker_raises_when_no_markdown_produced(mocker, tmp_path: Path) -> None:
    src_pdf = tmp_path / "paper.pdf"
    src_pdf.write_bytes(b"%PDF-1.4")
    mocker.patch("src.run_marker.subprocess.run",
                 return_value=MagicMock(returncode=0))
    with pytest.raises(MarkerError):
        run_marker(src_pdf, tmp_path / "marker")


def test_run_marker_raises_on_nonzero_exit(mocker, tmp_path: Path) -> None:
    src_pdf = tmp_path / "paper.pdf"
    src_pdf.write_bytes(b"%PDF-1.4")
    mocker.patch("src.run_marker.subprocess.run",
                 return_value=MagicMock(returncode=1, stderr=b"boom"))
    with pytest.raises(MarkerError):
        run_marker(src_pdf, tmp_path / "marker")
```

- [ ] **Step 2: 失敗確認**

Run: `pytest tests/test_run_marker.py -v`
Expected: ImportError。

- [ ] **Step 3: 実装**

`src/run_marker.py`:

```python
"""Wrapper around the `marker_single` CLI (spec §5.2.2)."""
from __future__ import annotations

import subprocess
from pathlib import Path


class MarkerError(RuntimeError):
    """Raised when Marker fails to produce a markdown file."""


def run_marker(input_pdf: Path | str, output_dir: Path | str) -> Path:
    """Run ``marker_single`` and return the produced ``.md`` path.

    Marker writes ``{output_dir}/{stem}/{stem}.md``; we search for any ``*.md``
    under ``output_dir`` to be resilient to layout changes.
    """
    input_pdf = Path(input_pdf)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    result = subprocess.run(
        ["marker_single", str(input_pdf), "--output_dir", str(output_dir)],
        capture_output=True,
    )
    if result.returncode != 0:
        raise MarkerError(
            f"marker_single failed (exit {result.returncode}): {result.stderr.decode(errors='replace')}"
        )

    candidates = sorted(output_dir.rglob("*.md"))
    if not candidates:
        raise MarkerError(f"marker_single produced no .md under {output_dir}")
    return candidates[0]
```

- [ ] **Step 4: テスト実行**

Run: `pytest tests/test_run_marker.py -v`
Expected: 3 PASS。

- [ ] **Step 5: コミット**

```bash
git add src/run_marker.py tests/test_run_marker.py
git commit -m "feat(run_marker): marker_single subprocess wrapper"
```

---

## Task 5: `extract_figures` — Marker出力の画像を `figures/` に再配置

**Files:**
- Create: `src/extract_figures.py`, `tests/test_extract_figures.py`

Spec §5.5。Marker が出力した画像群を `figures/figure_{NNN}.png` にリネームコピーし、Markdown 内の画像リンクを相対パスに書き換える。

- [ ] **Step 1: 失敗テスト**

`tests/test_extract_figures.py`:

```python
from pathlib import Path

from src.extract_figures import collect_marker_figures


def test_collect_marker_figures_renames_and_rewrites(tmp_path: Path) -> None:
    marker_dir = tmp_path / "marker"
    marker_dir.mkdir()
    (marker_dir / "fig_a.png").write_bytes(b"PNGA")
    (marker_dir / "fig_b.jpg").write_bytes(b"JPGB")
    md = marker_dir / "paper.md"
    md.write_text("See ![alt1](fig_a.png) and ![alt2](fig_b.jpg).\n")

    figures_dir = tmp_path / "figures"
    new_md = collect_marker_figures(md, figures_dir)

    assert (figures_dir / "figure_001.png").exists()
    assert (figures_dir / "figure_002.jpg").exists()
    rewritten = new_md.read_text()
    assert "figures/figure_001.png" in rewritten
    assert "figures/figure_002.jpg" in rewritten
    assert "fig_a.png" not in rewritten
    assert "fig_b.jpg" not in rewritten


def test_collect_marker_figures_handles_no_images(tmp_path: Path) -> None:
    marker_dir = tmp_path / "marker"
    marker_dir.mkdir()
    md = marker_dir / "paper.md"
    md.write_text("No figures here.\n")
    figures_dir = tmp_path / "figures"

    new_md = collect_marker_figures(md, figures_dir)
    assert new_md.read_text() == "No figures here.\n"
    assert not figures_dir.exists() or not any(figures_dir.iterdir())
```

- [ ] **Step 2: 失敗確認**

Run: `pytest tests/test_extract_figures.py -v`
Expected: ImportError。

- [ ] **Step 3: 実装**

`src/extract_figures.py`:

```python
"""Move Marker's per-paper images into ``figures/`` and rewrite Markdown links."""
from __future__ import annotations

import re
import shutil
from pathlib import Path

_IMG_LINK = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
_IMG_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


def collect_marker_figures(markdown_path: Path | str, figures_dir: Path | str) -> Path:
    """Rename images referenced from ``markdown_path`` into ``figures_dir`` and rewrite links in place.

    Returns ``markdown_path`` (rewritten on disk).
    """
    markdown_path = Path(markdown_path)
    figures_dir = Path(figures_dir)
    text = markdown_path.read_text()

    matches = list(_IMG_LINK.finditer(text))
    relevant = [m for m in matches
                if Path(m.group(2)).suffix.lower() in _IMG_EXTS]
    if not relevant:
        return markdown_path

    figures_dir.mkdir(parents=True, exist_ok=True)
    mapping: dict[str, str] = {}
    for index, match in enumerate(relevant, start=1):
        original_ref = match.group(2)
        src = (markdown_path.parent / original_ref).resolve()
        ext = src.suffix.lower() if src.suffix else ".png"
        new_name = f"figure_{index:03d}{ext}"
        if src.exists():
            shutil.copy(src, figures_dir / new_name)
        mapping[original_ref] = f"figures/{new_name}"

    def _rewrite(m: re.Match[str]) -> str:
        alt, ref = m.group(1), m.group(2)
        return f"![{alt}]({mapping.get(ref, ref)})"

    markdown_path.write_text(_IMG_LINK.sub(_rewrite, text))
    return markdown_path
```

- [ ] **Step 4: テスト実行**

Run: `pytest tests/test_extract_figures.py -v`
Expected: 2 PASS。

- [ ] **Step 5: コミット**

```bash
git add src/extract_figures.py tests/test_extract_figures.py
git commit -m "feat(extract_figures): collect Marker images into figures/ and rewrite md links"
```

---

## Task 6: `normalize_markdown` — 見出し・数式・キャプション・References 正規化

**Files:**
- Create: `src/normalize_markdown.py`, `tests/test_normalize_markdown.py`

Spec §5.9。複数の正規化ルールがあるので、ルール単位でTDDサイクルを回す。

- [ ] **Step 1: 失敗テスト — Abstract 見出し正規化**

`tests/test_normalize_markdown.py`:

```python
from pathlib import Path

from src.normalize_markdown import (
    normalize_abstract_heading,
    normalize_captions,
    normalize_equations,
    normalize_markdown,
    normalize_references_heading,
)


def test_normalize_abstract_heading_promotes_variants() -> None:
    assert normalize_abstract_heading("Abstract\n\nbody") == "## Abstract\n\nbody"
    assert normalize_abstract_heading("# Abstract\n\nbody") == "## Abstract\n\nbody"
    assert normalize_abstract_heading("### ABSTRACT\n\nbody") == "## Abstract\n\nbody"
    assert normalize_abstract_heading("## Abstract\n\nbody") == "## Abstract\n\nbody"
```

- [ ] **Step 2: 失敗確認**

Run: `pytest tests/test_normalize_markdown.py -v`
Expected: ImportError。

- [ ] **Step 3: モジュール骨組みと `normalize_abstract_heading` を実装**

`src/normalize_markdown.py`:

```python
"""Normalize Marker/MinerU/Docling Markdown output (spec §5.9)."""
from __future__ import annotations

import re
from pathlib import Path

_ABSTRACT_RE = re.compile(r"^(#{1,6}\s*)?abstract\s*$", re.IGNORECASE | re.MULTILINE)


def normalize_abstract_heading(markdown: str) -> str:
    return _ABSTRACT_RE.sub("## Abstract", markdown)
```

- [ ] **Step 4: テスト実行**

Run: `pytest tests/test_normalize_markdown.py::test_normalize_abstract_heading_promotes_variants -v`
Expected: PASS。

- [ ] **Step 5: 失敗テスト — References 見出し正規化**

```python
def test_normalize_references_heading_promotes_variants() -> None:
    assert normalize_references_heading("References\n[1] ...") == "## References\n[1] ..."
    assert normalize_references_heading("# References\n[1] ...") == "## References\n[1] ..."
    assert normalize_references_heading("**References**\n[1] ...") == "## References\n[1] ..."
```

- [ ] **Step 6: 実装**

```python
_REFS_RE = re.compile(
    r"^(?:#{1,6}\s*|\*\*)?references\*?\*?\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def normalize_references_heading(markdown: str) -> str:
    return _REFS_RE.sub("## References", markdown)
```

- [ ] **Step 7: テスト実行で PASS 確認**

Run: `pytest tests/test_normalize_markdown.py::test_normalize_references_heading_promotes_variants -v`
Expected: PASS。

- [ ] **Step 8: 失敗テスト — 数式ブロック正規化**

```python
def test_normalize_equations_converts_brackets_to_dollars() -> None:
    src = "Intro.\n\n\\[\ny = Ax\n\\]\n\nMore."
    assert normalize_equations(src) == "Intro.\n\n$$\ny = Ax\n$$\n\nMore."


def test_normalize_equations_leaves_existing_dollar_blocks() -> None:
    src = "$$\ny = Ax\n$$"
    assert normalize_equations(src) == src
```

- [ ] **Step 9: 実装**

```python
_EQ_BRACKET = re.compile(r"\\\[\s*\n?(.*?)\n?\s*\\\]", re.DOTALL)


def normalize_equations(markdown: str) -> str:
    """Convert ``\\[ ... \\]`` blocks to ``$$ ... $$``. Leaves existing ``$$`` blocks alone."""
    return _EQ_BRACKET.sub(lambda m: f"$$\n{m.group(1).strip()}\n$$", markdown)
```

- [ ] **Step 10: テスト実行**

Run: `pytest tests/test_normalize_markdown.py::test_normalize_equations_converts_brackets_to_dollars tests/test_normalize_markdown.py::test_normalize_equations_leaves_existing_dollar_blocks -v`
Expected: 2 PASS。

- [ ] **Step 11: 失敗テスト — 図表キャプション正規化**

```python
def test_normalize_captions_figure_variants() -> None:
    assert normalize_captions("Figure 1: Overview.") == "**Figure 1.** Overview."
    assert normalize_captions("Fig. 2. Comparison.") == "**Figure 2.** Comparison."
    assert normalize_captions("**Figure 3.** Already normalized.") == "**Figure 3.** Already normalized."


def test_normalize_captions_table_variants() -> None:
    assert normalize_captions("Table 1: Results.") == "**Table 1.** Results."
    assert normalize_captions("Table 2. Numbers.") == "**Table 2.** Numbers."
```

- [ ] **Step 12: 実装**

```python
_FIG_CAP = re.compile(
    r"^(?:\*\*)?(?:Figure|Fig\.)\s+(\d+)(?:\*\*)?[:\.]\s+(.+)$",
    re.MULTILINE,
)
_TABLE_CAP = re.compile(
    r"^(?:\*\*)?Table\s+(\d+)(?:\*\*)?[:\.]\s+(.+)$",
    re.MULTILINE,
)


def normalize_captions(markdown: str) -> str:
    markdown = _FIG_CAP.sub(lambda m: f"**Figure {m.group(1)}.** {m.group(2)}", markdown)
    markdown = _TABLE_CAP.sub(lambda m: f"**Table {m.group(1)}.** {m.group(2)}", markdown)
    return markdown
```

- [ ] **Step 13: テスト実行**

Run: `pytest tests/test_normalize_markdown.py::test_normalize_captions_figure_variants tests/test_normalize_markdown.py::test_normalize_captions_table_variants -v`
Expected: 2 PASS。

- [ ] **Step 14: 失敗テスト — 統合 `normalize_markdown` がファイルを読んで書き出す**

```python
def test_normalize_markdown_writes_normalized_file(tmp_path: Path) -> None:
    src = tmp_path / "raw.md"
    src.write_text(
        "# Title\n\nAbstract\n\nbody\n\n\\[\n y=x\n\\]\n\n"
        "Figure 1: Overview.\n\nReferences\n[1] foo"
    )
    out = tmp_path / "paper.md"
    normalize_markdown(src, out)
    text = out.read_text()
    assert "## Abstract" in text
    assert "$$\ny=x\n$$" in text
    assert "**Figure 1.** Overview." in text
    assert "## References" in text
```

- [ ] **Step 15: 実装**

```python
def normalize_markdown(input_path: Path | str, output_path: Path | str) -> Path:
    """Apply every §5.9 rule and write the result to ``output_path``."""
    text = Path(input_path).read_text()
    text = normalize_abstract_heading(text)
    text = normalize_references_heading(text)
    text = normalize_equations(text)
    text = normalize_captions(text)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(text)
    return output_path
```

- [ ] **Step 16: 全テスト実行**

Run: `pytest tests/test_normalize_markdown.py -v`
Expected: 全 PASS。

- [ ] **Step 17: コミット**

```bash
git add src/normalize_markdown.py tests/test_normalize_markdown.py
git commit -m "feat(normalize_markdown): heading/equation/caption/references normalization"
```

---

## Task 7: `evaluate_quality` — 品質レポート生成

**Files:**
- Create: `src/evaluate_quality.py`, `tests/test_evaluate_quality.py`

Spec §10。出力は `output/{paper_name}/logs/quality_report.md`。

- [ ] **Step 1: 失敗テスト**

`tests/test_evaluate_quality.py`:

```python
from pathlib import Path

from src.evaluate_quality import evaluate_markdown, write_quality_report


SAMPLE_MD = """# Title

## Abstract

Body of abstract.

## 1. Introduction

Some text with $x$ inline math.

$$
y = Ax
$$

![overview](figures/figure_001.png)

**Figure 1.** Overview of the proposed method.

## 2. Method

| a | b |
|---|---|
| 1 | 2 |

**Table 1.** A table.

## References

[1] First reference.
[2] Second reference.
"""


def test_evaluate_markdown_counts(tmp_path: Path) -> None:
    md = tmp_path / "paper.md"
    md.write_text(SAMPLE_MD)
    result = evaluate_markdown(md)
    assert result["section_count"] >= 3   # Abstract + Introduction + Method + References
    assert result["block_equation_count"] == 1
    assert result["figure_count"] == 1
    assert result["figure_caption_count"] == 1
    assert result["table_caption_count"] == 1
    assert result["reference_count"] == 2
    assert result["has_references_section"] is True


def test_write_quality_report_creates_markdown(tmp_path: Path) -> None:
    md = tmp_path / "paper.md"
    md.write_text(SAMPLE_MD)
    logs_dir = tmp_path / "logs"
    report = write_quality_report(
        markdown_path=md,
        source_pdf=tmp_path / "paper.pdf",
        logs_dir=logs_dir,
        engine="marker",
        has_text_layer=True,
        ocr_used=False,
        page_count=12,
    )
    assert report == logs_dir / "quality_report.md"
    body = report.read_text()
    assert body.startswith("# Conversion Quality Report")
    assert "Engine: marker" in body
    assert "Pages: 12" in body
```

- [ ] **Step 2: 失敗確認**

Run: `pytest tests/test_evaluate_quality.py -v`
Expected: ImportError。

- [ ] **Step 3: 実装**

`src/evaluate_quality.py`:

```python
"""Quality evaluation — counts structural elements and writes a Markdown report (spec §10)."""
from __future__ import annotations

import re
from pathlib import Path


_SECTION = re.compile(r"^##\s+\S.*$", re.MULTILINE)
_BLOCK_EQ = re.compile(r"^\$\$\s*$", re.MULTILINE)
_FIG_LINK = re.compile(r"!\[[^\]]*\]\([^)]+\)")
_FIG_CAP = re.compile(r"^\*\*Figure\s+\d+\.\*\*", re.MULTILINE)
_TABLE_CAP = re.compile(r"^\*\*Table\s+\d+\.\*\*", re.MULTILINE)
_REFS_HEADING = re.compile(r"^##\s+References\s*$", re.MULTILINE)
_REF_ENTRY = re.compile(r"^\[\d+\]\s+", re.MULTILINE)


def evaluate_markdown(markdown_path: Path | str) -> dict:
    text = Path(markdown_path).read_text()
    refs_match = _REFS_HEADING.search(text)
    refs_body = text[refs_match.end():] if refs_match else ""
    return {
        "section_count": len(_SECTION.findall(text)),
        "block_equation_count": len(_BLOCK_EQ.findall(text)) // 2,
        "figure_count": len(_FIG_LINK.findall(text)),
        "figure_caption_count": len(_FIG_CAP.findall(text)),
        "table_caption_count": len(_TABLE_CAP.findall(text)),
        "has_references_section": refs_match is not None,
        "reference_count": len(_REF_ENTRY.findall(refs_body)),
    }


def _verdict(condition: bool, ok_note: str, warn_note: str) -> tuple[str, str]:
    return ("OK", ok_note) if condition else ("Warning", warn_note)


def write_quality_report(
    *,
    markdown_path: Path | str,
    source_pdf: Path | str,
    logs_dir: Path | str,
    engine: str,
    has_text_layer: bool,
    ocr_used: bool,
    page_count: int,
) -> Path:
    metrics = evaluate_markdown(markdown_path)
    logs_dir = Path(logs_dir)
    logs_dir.mkdir(parents=True, exist_ok=True)

    headings_v, headings_note = _verdict(
        metrics["section_count"] >= 1,
        f"{metrics['section_count']} sections detected",
        "No `##` sections detected",
    )
    eq_v, eq_note = _verdict(
        metrics["block_equation_count"] >= 0,
        f"{metrics['block_equation_count']} block equations",
        "Equation parse issue",
    )
    fig_v, fig_note = _verdict(
        metrics["figure_count"] == metrics["figure_caption_count"]
        or metrics["figure_count"] == 0,
        f"{metrics['figure_count']} figures / {metrics['figure_caption_count']} captions",
        f"figure count {metrics['figure_count']} != caption count {metrics['figure_caption_count']}",
    )
    refs_v, refs_note = _verdict(
        metrics["has_references_section"],
        f"{metrics['reference_count']} references detected",
        "No `## References` section",
    )

    body = (
        "# Conversion Quality Report\n\n"
        "## Summary\n\n"
        f"- Input PDF: {source_pdf}\n"
        f"- Pages: {page_count}\n"
        f"- Text layer: {str(has_text_layer).lower()}\n"
        f"- OCR used: {str(ocr_used).lower()}\n"
        f"- Engine: {engine}\n\n"
        "## Checks\n\n"
        "| Check | Result | Notes |\n"
        "|---|---|---|\n"
        f"| Headings | {headings_v} | {headings_note} |\n"
        f"| Equations | {eq_v} | {eq_note} |\n"
        f"| Figures | {fig_v} | {fig_note} |\n"
        f"| Tables | OK | {metrics['table_caption_count']} table captions |\n"
        f"| References | {refs_v} | {refs_note} |\n"
    )

    out = logs_dir / "quality_report.md"
    out.write_text(body)
    return out
```

- [ ] **Step 4: テスト実行**

Run: `pytest tests/test_evaluate_quality.py -v`
Expected: 2 PASS。

- [ ] **Step 5: コミット**

```bash
git add src/evaluate_quality.py tests/test_evaluate_quality.py
git commit -m "feat(evaluate_quality): structural metrics and quality_report.md generation"
```

---

## Task 8: `config` — YAML + CLI 上書きを束ねる Settings

**Files:**
- Create: `src/config.py`, `tests/test_config.py`

Spec §8 の YAML 構造をデータクラスに読み込み、CLI からの上書きをマージする。

- [ ] **Step 1: 失敗テスト**

`tests/test_config.py`:

```python
from pathlib import Path

from src.config import Settings, load_settings


def test_load_settings_from_yaml(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "input_dir: in\n"
        "output_dir: out\n"
        "language: eng\n"
        "ocr:\n"
        "  enabled: auto\n"
        "  force: false\n"
        "converter:\n"
        "  engine: marker\n"
        "batch:\n"
        "  workers: 1\n"
        "  skip_existing: false\n"
        "  overwrite: false\n"
        "  continue_on_error: true\n"
    )
    settings = load_settings(cfg)
    assert isinstance(settings, Settings)
    assert settings.input_dir == Path("in")
    assert settings.output_dir == Path("out")
    assert settings.engine == "marker"
    assert settings.workers == 1


def test_load_settings_applies_cli_overrides(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "input_dir: in\n"
        "output_dir: out\n"
        "ocr: {enabled: auto, force: false}\n"
        "converter: {engine: marker}\n"
        "batch: {workers: 1, skip_existing: false, overwrite: false, continue_on_error: true}\n"
    )
    settings = load_settings(cfg, overrides={"engine": "docling", "workers": 4, "force_ocr": True})
    assert settings.engine == "docling"
    assert settings.workers == 4
    assert settings.force_ocr is True
```

- [ ] **Step 2: 失敗確認**

Run: `pytest tests/test_config.py -v`
Expected: ImportError。

- [ ] **Step 3: 実装**

`src/config.py`:

```python
"""Load settings from ``configs/config.yaml`` and merge CLI overrides."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class Settings:
    input_dir: Path
    output_dir: Path
    engine: str = "marker"
    language: str = "eng"
    enable_ocr: bool = True
    force_ocr: bool = False
    workers: int = 1
    skip_existing: bool = False
    overwrite: bool = False
    continue_on_error: bool = True
    save_logs: bool = True


def load_settings(config_path: Path | str, overrides: dict[str, Any] | None = None) -> Settings:
    raw = yaml.safe_load(Path(config_path).read_text()) or {}
    overrides = overrides or {}

    ocr = raw.get("ocr") or {}
    converter = raw.get("converter") or {}
    batch = raw.get("batch") or {}

    s = Settings(
        input_dir=Path(raw.get("input_dir", "input")),
        output_dir=Path(raw.get("output_dir", "output")),
        engine=converter.get("engine", "marker"),
        language=raw.get("language", "eng"),
        enable_ocr=ocr.get("enabled", True) is not False,
        force_ocr=bool(ocr.get("force", False)),
        workers=int(batch.get("workers", 1)),
        skip_existing=bool(batch.get("skip_existing", False)),
        overwrite=bool(batch.get("overwrite", False)),
        continue_on_error=bool(batch.get("continue_on_error", True)),
    )

    for key, value in overrides.items():
        if value is None:
            continue
        if not hasattr(s, key):
            raise KeyError(f"Unknown setting override: {key}")
        setattr(s, key, value)

    if isinstance(s.input_dir, str):
        s.input_dir = Path(s.input_dir)
    if isinstance(s.output_dir, str):
        s.output_dir = Path(s.output_dir)
    return s
```

- [ ] **Step 4: テスト実行**

Run: `pytest tests/test_config.py -v`
Expected: 2 PASS。

- [ ] **Step 5: コミット**

```bash
git add src/config.py tests/test_config.py
git commit -m "feat(config): Settings dataclass with YAML + CLI override merge"
```

---

## Task 9: `convert_one` — 単一PDFオーケストレーション

**Files:**
- Create: `src/convert_one.py`, `tests/test_convert_one.py`

Spec §13.1。各モジュールを順序立てて呼び、`output/{paper_name}/` 配下を完成させる。

- [ ] **Step 1: 失敗テスト**

`tests/test_convert_one.py`:

```python
from pathlib import Path

from src.config import Settings
from src.convert_one import convert_one


def _settings(input_dir: Path, output_dir: Path, **overrides) -> Settings:
    return Settings(input_dir=input_dir, output_dir=output_dir, **overrides)


def test_convert_one_text_pdf_skips_ocr(mocker, text_pdf: Path, tmp_path: Path) -> None:
    output_dir = tmp_path / "output"
    settings = _settings(text_pdf.parent, output_dir)

    def fake_marker(_pdf, out_dir: Path):
        out_dir.mkdir(parents=True, exist_ok=True)
        md = out_dir / "paper.md"
        md.write_text("# Title\n\nAbstract\n\nbody")
        return md

    ocr_spy = mocker.patch("src.convert_one.run_ocrmypdf")
    mocker.patch("src.convert_one.run_marker", side_effect=fake_marker)

    report = convert_one(text_pdf, settings)

    ocr_spy.assert_not_called()
    assert report["status"] == "success"
    assert report["ocr_executed"] is False
    paper_dir = output_dir / text_pdf.stem
    assert (paper_dir / "paper.md").exists()
    assert (paper_dir / "logs" / "text_layer_check.json").exists()
    assert (paper_dir / "logs" / "conversion_report.json").exists()
    assert (paper_dir / "logs" / "quality_report.md").exists()
    # Normalization ran
    assert "## Abstract" in (paper_dir / "paper.md").read_text()


def test_convert_one_image_pdf_runs_ocr(mocker, image_pdf: Path, tmp_path: Path) -> None:
    output_dir = tmp_path / "output"
    settings = _settings(image_pdf.parent, output_dir, enable_ocr=True)

    def fake_ocr(src, dst, **_):
        Path(dst).write_bytes(b"%PDF-1.4-FAKE-OCR")
        return Path(dst)

    def fake_marker(_pdf, out_dir: Path):
        out_dir.mkdir(parents=True, exist_ok=True)
        md = out_dir / "paper.md"
        md.write_text("# Title\n\n## Abstract\n\nbody")
        return md

    ocr_spy = mocker.patch("src.convert_one.run_ocrmypdf", side_effect=fake_ocr)
    mocker.patch("src.convert_one.run_marker", side_effect=fake_marker)

    report = convert_one(image_pdf, settings)
    ocr_spy.assert_called_once()
    assert report["ocr_executed"] is True
    paper_dir = output_dir / image_pdf.stem
    assert (paper_dir / f"{image_pdf.stem}_ocr.pdf").exists()
```

- [ ] **Step 2: 失敗確認**

Run: `pytest tests/test_convert_one.py -v`
Expected: ImportError。

- [ ] **Step 3: 実装**

`src/convert_one.py`:

```python
"""Single-PDF orchestration (spec §4.2 / §13.1)."""
from __future__ import annotations

import json
from pathlib import Path

from src.config import Settings
from src.evaluate_quality import write_quality_report
from src.extract_figures import collect_marker_figures
from src.inspect_pdf import inspect_pdf, write_text_layer_report
from src.normalize_markdown import normalize_markdown
from src.run_marker import run_marker
from src.run_ocr import run_ocrmypdf


def convert_one(input_pdf: Path | str, settings: Settings) -> dict:
    input_pdf = Path(input_pdf)
    paper_name = input_pdf.stem
    paper_dir = settings.output_dir / paper_name
    logs_dir = paper_dir / "logs"
    paper_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)

    # [1] inspection
    inspection = inspect_pdf(input_pdf)
    write_text_layer_report(input_pdf, logs_dir)

    # [2] OCR branch
    ocr_executed = False
    target_pdf = input_pdf
    needs_ocr = settings.force_ocr or (settings.enable_ocr and not inspection["has_text_layer"])
    if needs_ocr:
        ocr_pdf = paper_dir / f"{paper_name}_ocr.pdf"
        run_ocrmypdf(input_pdf, ocr_pdf, lang=settings.language)
        target_pdf = ocr_pdf
        ocr_executed = True

    # [3] conversion
    if settings.engine != "marker":
        raise NotImplementedError(f"Engine '{settings.engine}' not yet wired (see plan §future-work)")
    raw_md = run_marker(target_pdf, paper_dir / "marker")

    # [4] figure relocation
    collect_marker_figures(raw_md, paper_dir / "figures")

    # [5] markdown normalization
    final_md = paper_dir / "paper.md"
    normalize_markdown(raw_md, final_md)

    # [6] quality evaluation
    write_quality_report(
        markdown_path=final_md,
        source_pdf=input_pdf,
        logs_dir=logs_dir,
        engine=settings.engine,
        has_text_layer=inspection["has_text_layer"],
        ocr_used=ocr_executed,
        page_count=inspection["page_count"],
    )

    # [7] conversion report
    report = {
        "input_file": input_pdf.name,
        "has_text_layer": inspection["has_text_layer"],
        "ocr_executed": ocr_executed,
        "engine": settings.engine,
        "status": "success",
        "output_markdown": str(final_md),
    }
    (logs_dir / "conversion_report.json").write_text(json.dumps(report, indent=2))
    return report
```

- [ ] **Step 4: テスト実行**

Run: `pytest tests/test_convert_one.py -v`
Expected: 2 PASS。

- [ ] **Step 5: コミット**

```bash
git add src/convert_one.py tests/test_convert_one.py
git commit -m "feat(convert_one): single-PDF pipeline orchestration"
```

---

## Task 10: `batch_convert` — バッチループ + CLI

**Files:**
- Create: `src/batch_convert.py`, `tests/test_batch_convert.py`

Spec §4.1, §7, §9.2。

- [ ] **Step 1: 失敗テスト**

`tests/test_batch_convert.py`:

```python
import json
from pathlib import Path

import pytest

from src.batch_convert import build_arg_parser, run_batch
from src.config import Settings


def _seed_pdfs(input_dir: Path, names: list[str]) -> None:
    input_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        (input_dir / name).write_bytes(b"%PDF-1.4")


def test_run_batch_writes_summary_after_all_pdfs(mocker, tmp_path: Path) -> None:
    input_dir = tmp_path / "in"
    output_dir = tmp_path / "out"
    _seed_pdfs(input_dir, ["a.pdf", "b.pdf"])
    settings = Settings(input_dir=input_dir, output_dir=output_dir)

    mocker.patch(
        "src.batch_convert.convert_one",
        side_effect=lambda pdf, _s: {"input_file": pdf.name, "status": "success"},
    )

    summary = run_batch(settings)
    assert summary == {"total": 2, "success": 2, "failed": 0, "failed_files": []}
    assert (output_dir / "batch_summary.json").exists()
    payload = json.loads((output_dir / "batch_summary.json").read_text())
    assert payload["total"] == 2


def test_run_batch_continues_on_failure(mocker, tmp_path: Path) -> None:
    input_dir = tmp_path / "in"
    output_dir = tmp_path / "out"
    _seed_pdfs(input_dir, ["good.pdf", "bad.pdf", "good2.pdf"])
    settings = Settings(input_dir=input_dir, output_dir=output_dir, continue_on_error=True)

    def maybe_fail(pdf, _s):
        if pdf.name == "bad.pdf":
            raise RuntimeError("boom")
        return {"input_file": pdf.name, "status": "success"}

    mocker.patch("src.batch_convert.convert_one", side_effect=maybe_fail)

    summary = run_batch(settings)
    assert summary["success"] == 2
    assert summary["failed"] == 1
    assert summary["failed_files"] == ["bad.pdf"]


def test_run_batch_skip_existing(mocker, tmp_path: Path) -> None:
    input_dir = tmp_path / "in"
    output_dir = tmp_path / "out"
    _seed_pdfs(input_dir, ["a.pdf"])
    existing = output_dir / "a" / "paper.md"
    existing.parent.mkdir(parents=True)
    existing.write_text("# already done")

    settings = Settings(input_dir=input_dir, output_dir=output_dir, skip_existing=True)
    spy = mocker.patch("src.batch_convert.convert_one")

    summary = run_batch(settings)
    spy.assert_not_called()
    assert summary == {"total": 1, "success": 1, "failed": 0, "failed_files": []}


def test_arg_parser_minimal() -> None:
    parser = build_arg_parser()
    args = parser.parse_args(["--input_dir", "in", "--output_dir", "out"])
    assert args.input_dir == "in"
    assert args.output_dir == "out"
    assert args.engine == "marker"


def test_arg_parser_supports_all_flags() -> None:
    parser = build_arg_parser()
    args = parser.parse_args([
        "--input_dir", "in", "--output_dir", "out",
        "--engine", "docling", "--enable_ocr", "--force_ocr",
        "--ocr_lang", "eng", "--workers", "2",
        "--skip_existing", "--overwrite", "--save_logs",
    ])
    assert args.engine == "docling"
    assert args.enable_ocr is True
    assert args.force_ocr is True
    assert args.workers == 2
    assert args.skip_existing is True
    assert args.overwrite is True
```

- [ ] **Step 2: 失敗確認**

Run: `pytest tests/test_batch_convert.py -v`
Expected: ImportError。

- [ ] **Step 3: 実装**

`src/batch_convert.py`:

```python
"""Batch entry point (spec §4.1, §7)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from src.config import Settings, load_settings
from src.convert_one import convert_one


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="paper2md — batch convert academic PDFs to Markdown")
    p.add_argument("--input_dir", required=True)
    p.add_argument("--output_dir", required=True)
    p.add_argument("--config", default="configs/config.yaml")
    p.add_argument("--engine", choices=["marker", "mineru", "docling", "nougat"], default="marker")
    p.add_argument("--enable_ocr", action="store_true")
    p.add_argument("--force_ocr", action="store_true")
    p.add_argument("--ocr_lang", default="eng")
    p.add_argument("--workers", type=int, default=1)
    p.add_argument("--skip_existing", action="store_true")
    p.add_argument("--overwrite", action="store_true")
    p.add_argument("--save_logs", action="store_true")
    return p


def _settings_from_args(args: argparse.Namespace) -> Settings:
    overrides: dict[str, Any] = {
        "input_dir": Path(args.input_dir),
        "output_dir": Path(args.output_dir),
        "engine": args.engine,
        "language": args.ocr_lang,
        "enable_ocr": args.enable_ocr or None,  # only override if set
        "force_ocr": args.force_ocr or None,
        "workers": args.workers,
        "skip_existing": args.skip_existing or None,
        "overwrite": args.overwrite or None,
    }
    cleaned = {k: v for k, v in overrides.items() if v is not None}
    config_path = Path(args.config)
    if config_path.exists():
        return load_settings(config_path, overrides=cleaned)
    # No YAML — build Settings directly from CLI defaults.
    return Settings(**{k: cleaned[k] for k in cleaned if hasattr(Settings, k)})


def run_batch(settings: Settings) -> dict:
    pdfs = sorted(Path(settings.input_dir).glob("*.pdf"))
    if not pdfs:
        raise FileNotFoundError(f"No PDF files found in {settings.input_dir}")

    settings.output_dir.mkdir(parents=True, exist_ok=True)
    summary = {"total": len(pdfs), "success": 0, "failed": 0, "failed_files": []}

    for pdf in pdfs:
        paper_md = settings.output_dir / pdf.stem / "paper.md"
        if settings.skip_existing and paper_md.exists():
            summary["success"] += 1
            continue
        try:
            convert_one(pdf, settings)
            summary["success"] += 1
        except Exception as err:
            summary["failed"] += 1
            summary["failed_files"].append(pdf.name)
            if not settings.continue_on_error:
                _write_summary(settings.output_dir, summary)
                raise
            print(f"[paper2md] {pdf.name} failed: {err}", file=sys.stderr)

    _write_summary(settings.output_dir, summary)
    return summary


def _write_summary(output_dir: Path, summary: dict) -> None:
    (output_dir / "batch_summary.json").write_text(json.dumps(summary, indent=2))


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    settings = _settings_from_args(args)
    summary = run_batch(settings)
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: テスト実行**

Run: `pytest tests/test_batch_convert.py -v`
Expected: 全 PASS。

- [ ] **Step 5: 全テスト走らせて緑を確認**

Run: `pytest -v`
Expected: 全 PASS。

- [ ] **Step 6: コミット**

```bash
git add src/batch_convert.py tests/test_batch_convert.py
git commit -m "feat(batch_convert): batch loop, failure continuation, batch_summary.json, CLI"
```

---

## Task 11: README — 利用方法

**Files:**
- Create: `README.md`

- [ ] **Step 1: README を書く**

```markdown
# paper2md

English academic-paper PDF → Markdown batch pipeline. Built per [docs/spec.md](docs/spec.md).

## Install

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
# Tesseract is needed by ocrmypdf at runtime; install via your OS package manager.
```

## Run

Place PDFs under `input/` and run:

```bash
python -m src.batch_convert --input_dir input --output_dir output --engine marker --enable_ocr
```

Output for each PDF appears at `output/{paper_name}/paper.md`. The batch summary lives at `output/batch_summary.json`.

See `docs/spec.md` §7 for the full CLI surface and §8 for `configs/config.yaml`.

## Develop

```bash
pip install -r requirements-dev.txt
pytest
```
```

- [ ] **Step 2: コミット**

```bash
git add README.md
git commit -m "docs: README with install/run/develop"
```

---

## 将来作業（本計画の範囲外）

Spec §16 の Step 5〜6 と §11 の完全フォールバック。別計画として書き下ろす想定。タスク見出しのみ示す。

- **追加エンジン**: `src/run_mineru.py`（`magic-pdf -p ... -o ...`、出力 `paper_content_list.json` を `paper.json` に転用）、`src/run_docling.py`（`docling ... --to md --output ...`）。`convert_one` の engine 分岐を辞書ディスパッチに置換する。
- **フォールバックチェーン (§11.1)**: Marker → MinerU → Docling → OCRmyPDF+Docling。`convert_one` をリトライ層でラップし、各失敗を `conversion_report.json` に記録する。
- **数式OCR補強 (§11.2)**: 品質評価で「数式が崩れている」とマークされた領域をページ画像から切り出し、pix2tex を呼んで LaTeX に置換する `src/repair_equations.py` を追加。
- **複雑な表の画像フォールバック (§5.8.3, §11.4)**: テーブル領域を画像保存し、Markdown table の代わりに `**Table N.**` + 画像参照を挿入する。
- **並列バッチ (§7.5)**: `concurrent.futures.ProcessPoolExecutor` で `--workers` を実装。初期は1で運用するため後回し。
- **LLM整形補助 (§16 Step 6)**: 正規化後の Markdown を任意プロバイダの LLM に渡して見出し・参考文献の整形を行う段階を追加。

---

## Self-review

- §5.1 PDF検査 → Task 2
- §5.2 Marker起動 → Task 4
- §5.3 OCRmyPDF → Task 3, Task 9 (orchestration)
- §5.4 ページ画像化 → Task 2 (`render_pages`)
- §5.5 図抽出 → Task 5
- §5.6 キャプション → Task 6 (`normalize_captions`)
- §5.7 数式 → Task 6 (`normalize_equations`)
- §5.8 表 → 単純表は変換ツール側、複雑表は将来作業
- §5.9 Markdown正規化 → Task 6
- §6 ディレクトリ構成 → Task 1, Task 9
- §7 CLI仕様 → Task 10
- §8 設定ファイル → Task 1 (YAML), Task 8 (loader)
- §9 ログとサマリ → Task 9 (`conversion_report.json`, `text_layer_check.json`), Task 10 (`batch_summary.json`)
- §10 品質評価 → Task 7
- §11 フォールバック → 将来作業
- §13 疑似コード → Task 9 が直接対応
- §19 成功条件: 1〜7, 9, 10 を Task 1〜10 で網羅。条件 8 (品質レポート) は Task 7。条件 10 (失敗継続) は Task 10。

未カバー: 5.8.3 の複雑表→画像化、§11 のエンジンフォールバック、§17 並列処理 → すべて将来作業セクションに記載。
