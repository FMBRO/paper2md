# 英語論文PDF OCR・Markdown変換パイプライン仕様書

## 1. 目的

本仕様書は、英語論文PDFをMarkdownファイルへ変換するためのパイプラインを定義する。

本パイプラインは、本文、見出し、数式、図、表、図表キャプション、参考文献を可能な限り保持したMarkdown出力を目的とする。

対象PDFは、次の2種類である。

1. テキストレイヤーを含むPDF
2. テキストレイヤーを含まない画像PDFまたはスキャンPDF

最終出力は、LLM処理、RAG、論文要約、翻訳、再編集に利用できるMarkdownファイルとする。

本パイプラインは、最初から `input/` 配下の複数PDFを一括処理する設計とする。単一PDFは、PDFが1件のバッチとして扱う。

---

## 2. 想定入力と出力

### 2.1 入力

```text
input/
├── paper_a.pdf
├── paper_b.pdf
└── paper_c.pdf
```

入力PDFは、英語論文を想定する。

PDFには、次の要素が含まれる可能性がある。

- タイトル
- 著者情報
- Abstract
- Section / Subsection
- 本文
- 数式
- 図
- 表
- 図表キャプション
- 参考文献
- 脚注
- 2段組レイアウト

### 2.2 出力

PDFごとに独立した出力ディレクトリを作る。これにより、図ファイルやログが混ざらない。

```text
output/
├── paper_a/
│   ├── paper.md
│   ├── paper.json
│   ├── paper_ocr.pdf
│   ├── figures/
│   │   ├── figure_001.png
│   │   └── ...
│   ├── tables/
│   │   ├── table_001.md
│   │   └── ...
│   ├── pages/
│   │   ├── page_001.png
│   │   └── ...
│   └── logs/
│       ├── pipeline.log
│       ├── text_layer_check.json
│       ├── conversion_report.json
│       └── quality_report.md
├── paper_b/
│   └── ...
├── paper_c/
│   └── ...
└── batch_summary.json
```

主要出力は各PDFディレクトリ配下の `paper.md` である。

`paper.json` は、後処理や品質評価のための中間構造データとして保存する。

`batch_summary.json` は、バッチ全体の成否を集約する。

---

## 3. 基本方針

本パイプラインは、PDFごとに次の流れで処理する。

```text
PDF
↓
テキストレイヤー有無を判定
├── あり: PDF-to-Markdownツールで変換
└── なし: OCR前処理後にPDF-to-Markdownツールで変換
```

変換ツールは、次の候補から選択する。

| 優先度 | ツール | 主な用途 |
|---:|---|---|
| 1 | Marker | 論文PDFからMarkdownへの変換 |
| 2 | MinerU | 科学文書の構造解析とMarkdown/JSON変換 |
| 3 | Docling | PDFの読み順、表、図、OCRを含む変換 |
| 4 | Nougat | 学術論文向けOCRと数式付きMarkdown変換 |
| 5 | OCRmyPDF | スキャンPDFへのテキストレイヤー追加 |
| 6 | PaddleOCR | OCR・レイアウト解析・表認識の部品 |

本仕様では、初期実装の第一候補を次の構成とする。

```text
テキストPDF: Marker
画像PDF: OCRmyPDF → Marker
```

精度比較のため、MinerUとDoclingも切り替え可能にする。

---

## 4. パイプライン全体構成

### 4.1 バッチ全体

```text
input/*.pdf を列挙
↓
PDFごとに出力ディレクトリを作成
↓
PDFごとに [4.2] の単一PDF処理を実行
↓
失敗しても次のPDFへ進む
↓
batch_summary.json を出力
```

### 4.2 単一PDF処理

```text
input.pdf
↓
[1] PDF検査
    - テキストレイヤー有無
    - ページ数
    - メタデータ
    - 画像PDF判定
↓
[2-A] テキストPDF処理
    - Marker / MinerU / Docling によるMarkdown変換
↓
[2-B] 画像PDF処理
    - ページ画像化
    - OCRmyPDFによるOCR付きPDF生成
    - Marker / MinerU / Docling によるMarkdown変換
↓
[3] 図・表・数式の後処理
    - 画像パス整理
    - 図番号とキャプション対応
    - 数式ブロック整形
    - 表のMarkdown整形
↓
[4] Markdown正規化
    - 見出し整理
    - 改行整理
    - 参照リンク整理
    - References整理
↓
[5] 品質評価
    - 変換成功率
    - 数式保持率
    - 図キャプション対応率
    - 読み順確認
↓
output/{paper_name}/paper.md
```

---

## 5. 詳細処理仕様

### 5.1 PDF検査

#### 5.1.1 目的

PDFにテキストレイヤーがあるかを判定する。

この判定により、OCR前処理の必要性を決める。

#### 5.1.2 判定方法

PyMuPDFを用いて、先頭数ページからテキストを抽出する。

```python
import fitz


def has_text_layer(pdf_path: str, min_chars: int = 100, max_pages: int = 3) -> bool:
    doc = fitz.open(pdf_path)
    text = ""

    for i, page in enumerate(doc):
        if i >= max_pages:
            break
        text += page.get_text()

    return len(text.strip()) >= min_chars
```

#### 5.1.3 出力

```json
{
  "input_pdf": "input/paper_a.pdf",
  "has_text_layer": true,
  "page_count": 12,
  "checked_pages": 3,
  "extracted_char_count": 8420
}
```

保存先:

```text
output/{paper_name}/logs/text_layer_check.json
```

---

### 5.2 テキストPDF処理

#### 5.2.1 目的

テキスト情報を含むPDFからMarkdownを生成する。

#### 5.2.2 第一候補: Marker

実行例:

```bash
marker_single input/paper_a.pdf --output_dir output/paper_a/marker
```

想定出力:

```text
output/paper_a/marker/
├── paper.md
├── images/
└── metadata.json
```

#### 5.2.3 第二候補: MinerU

実行例:

```bash
magic-pdf -p input/paper_a.pdf -o output/paper_a/mineru
```

想定出力:

```text
output/paper_a/mineru/
├── paper.md
├── paper_content_list.json
├── paper_middle.json
└── images/
```

#### 5.2.4 第三候補: Docling

実行例:

```bash
docling input/paper_a.pdf --to md --output output/paper_a/docling
```

想定出力:

```text
output/paper_a/docling/
└── paper.md
```

---

### 5.3 画像PDF・スキャンPDF処理

#### 5.3.1 目的

テキストレイヤーを持たないPDFをOCR処理し、Markdown変換可能な状態にする。

#### 5.3.2 OCR前処理

OCRmyPDFを用いて、スキャンPDFに透明テキストレイヤーを追加する。

```bash
ocrmypdf -l eng --deskew --clean input/paper_a.pdf output/paper_a/paper_ocr.pdf
```

オプション:

| オプション | 内容 |
|---|---|
| `-l eng` | 英語OCRを指定する |
| `--deskew` | ページの傾きを補正する |
| `--clean` | ノイズを除去する |
| `output/.../paper_ocr.pdf` | OCR済みPDFの保存先 |

#### 5.3.3 OCR後の処理

OCR済みPDFを、通常のPDF-to-Markdown処理に渡す。

```bash
marker_single output/paper_a/paper_ocr.pdf --output_dir output/paper_a/marker
```

または:

```bash
docling output/paper_a/paper_ocr.pdf --to md --output output/paper_a/docling
```

---

### 5.4 ページ画像化

#### 5.4.1 目的

ページ全体の画像を保存し、後続の品質確認や図表抽出に利用する。

#### 5.4.2 実装例

PyMuPDFを用いてページ画像を生成する。

```python
import fitz
from pathlib import Path


def render_pages(pdf_path: str, output_dir: str, zoom: float = 2.0) -> None:
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    doc = fitz.open(pdf_path)
    matrix = fitz.Matrix(zoom, zoom)

    for i, page in enumerate(doc):
        pix = page.get_pixmap(matrix=matrix)
        pix.save(f"{output_dir}/page_{i + 1:03d}.png")
```

出力:

```text
output/{paper_name}/pages/page_001.png
output/{paper_name}/pages/page_002.png
...
```

---

### 5.5 図抽出

#### 5.5.1 目的

PDF内の図を画像ファイルとして抽出し、Markdown内から参照できるようにする。

#### 5.5.2 抽出方針

図抽出は、次のいずれかで行う。

1. Marker / MinerU / Docling の抽出結果を利用する
2. PyMuPDFでPDF内画像を抽出する
3. ページ画像からレイアウト解析で図領域を切り出す

初期実装では、変換ツールの画像抽出結果を優先する。

#### 5.5.3 保存形式

```text
output/{paper_name}/figures/figure_001.png
output/{paper_name}/figures/figure_002.png
...
```

#### 5.5.4 Markdown記述形式

```markdown
![Overview of the proposed method.](figures/figure_001.png)

**Figure 1.** Overview of the proposed method.
```

---

### 5.6 図表キャプション抽出

#### 5.6.1 目的

図と表に対応するキャプションを抽出し、Markdown中に明示する。

#### 5.6.2 検出対象

次のパターンを検出対象とする。

```text
Figure 1.
Fig. 1.
Table 1.
Algorithm 1.
```

#### 5.6.3 正規表現例

```python
import re

caption_patterns = [
    r"^(Figure|Fig\.)\s+\d+[:\.]\s+.+",
    r"^Table\s+\d+[:\.]\s+.+",
    r"^Algorithm\s+\d+[:\.]\s+.+",
]
```

#### 5.6.4 出力形式

```markdown
![Qualitative comparison.](figures/figure_002.png)

**Figure 2.** Qualitative comparison of the proposed method and baseline methods.
```

表の場合:

```markdown
**Table 1.** Quantitative comparison of spectral reconstruction accuracy.

| Method | RMSE | SAM |
|---|---:|---:|
| Baseline | 0.052 | 0.184 |
| Proposed | 0.031 | 0.102 |
```

---

### 5.7 数式処理

#### 5.7.1 目的

数式をMarkdown内でLaTeX形式として保持する。

#### 5.7.2 基本形式

インライン数式:

```markdown
The observed RGB value $y_p$ is generated from spectral reflectance $r_p$.
```

ブロック数式:

```markdown
$$
y_p = S^\top \operatorname{diag}(e) r_p
$$
```

複数行数式:

```markdown
$$
\begin{aligned}
\mu_p &= A r_p, \\
A &= S^\top \operatorname{diag}(e).
\end{aligned}
$$
```

#### 5.7.3 数式OCRの扱い

初期実装では、Marker / MinerU / Nougat の数式認識結果を利用する。

数式認識精度が不足する場合は、次の追加手段を検討する。

| ツール | 用途 |
|---|---|
| pix2tex | ローカル数式OCR |
| LaTeX-OCR | 画像数式からLaTeXへの変換 |
| Mathpix | 高精度な数式OCR。OSSではないため任意利用 |

OSS構成を優先する場合は、pix2texを第一候補とする。

---

### 5.8 表処理

#### 5.8.1 目的

表をMarkdown tableまたは画像として保持する。

#### 5.8.2 方針

単純な表はMarkdown tableに変換する。

```markdown
| Method | RMSE | SAM |
|---|---:|---:|
| Baseline | 0.052 | 0.184 |
| Proposed | 0.031 | 0.102 |
```

複雑な表は画像として保持し、キャプションを付与する。

```markdown
![Quantitative comparison table.](figures/table_001.png)

**Table 1.** Quantitative comparison of each method.
```

#### 5.8.3 判定基準

次の条件を満たす場合、画像として保持する。

- セル結合がある
- 複数行ヘッダーがある
- 数式を多く含む
- Markdown tableへ変換すると構造が崩れる
- 表の読み順が不明確である

---

### 5.9 Markdown正規化

#### 5.9.1 目的

変換結果を、後続処理しやすいMarkdown形式に整える。

#### 5.9.2 正規化項目

- タイトルを `#` に統一する
- Abstractを `## Abstract` に統一する
- セクション見出しを `##` に統一する
- サブセクション見出しを `###` に統一する
- 数式を `$$ ... $$` に統一する
- 図キャプションを `**Figure N.**` に統一する
- 表キャプションを `**Table N.**` に統一する
- 画像パスを相対パスに統一する
- Referencesを `## References` に統一する

#### 5.9.3 推奨Markdown構造

```markdown
# Paper Title

## Abstract

...

## 1. Introduction

...

## 2. Related Work

...

## 3. Method

...

$$
y = Ax
$$

![Overview of the proposed method.](figures/figure_001.png)

**Figure 1.** Overview of the proposed method.

## 4. Experiments

...

## 5. Conclusion

...

## References

...
```

---

## 6. ディレクトリ構成

推奨構成は次の通りである。

```text
paper-md-converter/
├── input/
│   ├── paper_a.pdf
│   ├── paper_b.pdf
│   └── paper_c.pdf
├── output/
│   ├── paper_a/
│   │   ├── paper.md
│   │   ├── paper.json
│   │   ├── paper_ocr.pdf
│   │   ├── figures/
│   │   ├── tables/
│   │   ├── pages/
│   │   └── logs/
│   ├── paper_b/
│   ├── paper_c/
│   └── batch_summary.json
├── src/
│   ├── main.py
│   ├── batch_convert.py
│   ├── inspect_pdf.py
│   ├── run_ocr.py
│   ├── run_marker.py
│   ├── run_mineru.py
│   ├── run_docling.py
│   ├── extract_figures.py
│   ├── normalize_markdown.py
│   └── evaluate_quality.py
├── configs/
│   └── config.yaml
├── requirements.txt
└── README.md
```

---

## 7. CLI仕様

### 7.1 バッチ実行（基本）

```bash
python src/batch_convert.py \
  --input_dir input \
  --output_dir output \
  --engine marker
```

### 7.2 OCRを有効化する場合

```bash
python src/batch_convert.py \
  --input_dir input \
  --output_dir output \
  --engine marker \
  --enable_ocr
```

### 7.3 OCRを強制する場合

```bash
python src/batch_convert.py \
  --input_dir input \
  --output_dir output \
  --engine marker \
  --force_ocr
```

### 7.4 変換エンジンを切り替える場合

```bash
python src/batch_convert.py --input_dir input --output_dir output --engine mineru
python src/batch_convert.py --input_dir input --output_dir output --engine docling
```

### 7.5 並列処理

```bash
python src/batch_convert.py \
  --input_dir input \
  --output_dir output \
  --engine marker \
  --enable_ocr \
  --workers 4
```

OCRやMarkerはメモリ・GPUを使うため、初期は `--workers 1` を推奨する。

### 7.6 再実行スキップ

すでに `paper.md` が存在する場合はスキップする。

```bash
python src/batch_convert.py --input_dir input --output_dir output --skip_existing
```

これにより、失敗したPDFだけ再実行できる。

### 7.7 単一PDFの品質評価のみ実行する場合

```bash
python src/evaluate_quality.py \
  --markdown output/paper_a/paper.md \
  --source input/paper_a.pdf
```

### 7.8 CLIオプション一覧

```text
--input_dir        入力PDFフォルダ
--output_dir       出力フォルダ
--engine           marker / mineru / docling / nougat
--enable_ocr       テキストレイヤーがないPDFにOCRを実行
--force_ocr        テキストレイヤーがあってもOCRを実行
--ocr_lang         OCR言語。英語なら eng
--workers          並列数
--skip_existing    既存出力をスキップ
--overwrite        既存出力を上書き
--save_logs        ログ保存
```

---

## 8. 設定ファイル仕様

`configs/config.yaml` の例を次に示す。

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

---

## 9. ログとサマリ仕様

### 9.1 PDFごとのログ

各PDFについて、変換状況をJSONで残す。

保存先:

```text
output/{paper_name}/logs/conversion_report.json
```

形式:

```json
{
  "input_file": "paper_a.pdf",
  "has_text_layer": true,
  "ocr_executed": false,
  "engine": "marker",
  "status": "success",
  "output_markdown": "output/paper_a/paper.md"
}
```

### 9.2 バッチサマリ

1本のPDFで失敗しても、全体の処理は止めない。

```text
paper_a.pdf → success
paper_b.pdf → failed
paper_c.pdf → success
```

最後に `output/batch_summary.json` を出力する。

```json
{
  "total": 3,
  "success": 2,
  "failed": 1,
  "failed_files": [
    "paper_b.pdf"
  ]
}
```

---

## 10. 品質評価仕様

### 10.1 評価目的

変換結果が論文Markdownとして利用可能かを評価する。

### 10.2 評価項目

| 項目 | 評価内容 |
|---|---|
| 見出し | Section / Subsectionが正しく抽出されたか |
| 読み順 | 2段組の本文順序が崩れていないか |
| 数式 | 数式がLaTeXとして残っているか |
| 図 | 図画像が抽出されたか |
| キャプション | Figure / Table番号と説明が残っているか |
| 表 | Markdown tableまたは画像として保持されたか |
| 参考文献 | Referencesがまとまっているか |
| ノイズ | ヘッダー、フッター、ページ番号が混入していないか |

### 10.3 品質レポート形式

```markdown
# Conversion Quality Report

## Summary

- Input PDF: input/paper_a.pdf
- Pages: 12
- Text layer: true
- OCR used: false
- Engine: marker

## Checks

| Check | Result | Notes |
|---|---|---|
| Headings | OK | 6 sections detected |
| Equations | Warning | 2 equations may be broken |
| Figures | OK | 5 figures extracted |
| Captions | OK | 5 figure captions detected |
| Tables | Warning | 1 complex table saved as image |
| References | OK | 32 references detected |

## Issues

1. Equation near Section 3.2 may require manual review.
2. Table 2 was kept as an image due to complex layout.
```

保存先:

```text
output/{paper_name}/logs/quality_report.md
```

---

## 11. 失敗時のフォールバック方針

### 11.1 Markerで失敗した場合

```text
Marker
↓ 失敗
MinerU
↓ 失敗
Docling
↓ 失敗
OCRmyPDF + Docling
```

### 11.2 数式が崩れる場合

```text
変換ツールの数式出力
↓ 不十分
数式領域を画像として切り出し
↓
pix2texでLaTeX化
↓
Markdownへ差し替え
```

### 11.3 図キャプションが崩れる場合

```text
Markdown本文から Figure / Fig. / Table を正規表現で抽出
↓
図画像の出現順と対応付け
↓
対応できないものは quality_report.md に記録
```

### 11.4 表が崩れる場合

```text
Markdown table化を試行
↓ 不十分
表領域を画像として保存
↓
キャプション付き画像としてMarkdownに挿入
```

### 11.5 バッチ全体での失敗継続

PDF単位で失敗しても次のPDFへ進む。失敗したファイル名は `batch_summary.json` の `failed_files` に記録する。

---

## 12. 実装に必要な主な依存関係

### 12.1 Pythonパッケージ

```text
pymupdf
pillow
pyyaml
marker-pdf
docling
```

必要に応じて追加する。

```text
pix2tex
paddleocr
```

### 12.2 外部コマンド

```text
ocrmypdf
tesseract
```

### 12.3 インストール例

```bash
pip install pymupdf pillow pyyaml marker-pdf docling
```

OCRmyPDFを使う場合:

```bash
pip install ocrmypdf
```

Tesseractが必要な場合は、OSごとに別途インストールする。

---

## 13. 最小実装の疑似コード

### 13.1 単一PDF処理

```python
from pathlib import Path

from inspect_pdf import has_text_layer
from run_ocr import run_ocrmypdf
from run_marker import run_marker
from normalize_markdown import normalize_markdown
from evaluate_quality import evaluate_quality


def convert_one_pdf(
    input_pdf: Path,
    output_dir: Path,
    engine: str = "marker",
    enable_ocr: bool = True,
    force_ocr: bool = False,
) -> None:
    paper_name = input_pdf.stem
    paper_output = output_dir / paper_name
    paper_output.mkdir(parents=True, exist_ok=True)

    has_text = has_text_layer(str(input_pdf))

    if force_ocr or (enable_ocr and not has_text):
        ocr_pdf = paper_output / f"{paper_name}_ocr.pdf"
        run_ocrmypdf(str(input_pdf), str(ocr_pdf), lang="eng")
        target_pdf = str(ocr_pdf)
    else:
        target_pdf = str(input_pdf)

    if engine == "marker":
        raw_md = run_marker(target_pdf, output_dir=str(paper_output / "marker"))
    else:
        raise ValueError(f"Unsupported engine: {engine}")

    final_md = paper_output / "paper.md"
    normalize_markdown(raw_md, output_path=str(final_md))

    evaluate_quality(
        markdown_path=str(final_md),
        source_pdf=str(input_pdf),
        output_dir=str(paper_output / "logs"),
    )
```

### 13.2 バッチ処理

```python
import json
import subprocess
from pathlib import Path

import fitz


def has_text_layer(pdf_path: Path, min_chars: int = 100) -> bool:
    doc = fitz.open(pdf_path)
    text = ""

    for page in doc[:3]:
        text += page.get_text()

    return len(text.strip()) >= min_chars


def run_command(command: list[str]) -> None:
    subprocess.run(command, check=True)


def convert_one_pdf(
    pdf_path: Path,
    output_dir: Path,
    engine: str = "marker",
    enable_ocr: bool = True,
) -> None:
    paper_name = pdf_path.stem
    paper_output_dir = output_dir / paper_name
    paper_output_dir.mkdir(parents=True, exist_ok=True)

    working_pdf = pdf_path

    if enable_ocr and not has_text_layer(pdf_path):
        ocr_pdf = paper_output_dir / f"{paper_name}_ocr.pdf"

        run_command([
            "ocrmypdf",
            "-l", "eng",
            "--deskew",
            "--clean",
            str(pdf_path),
            str(ocr_pdf),
        ])

        working_pdf = ocr_pdf

    if engine == "marker":
        run_command([
            "marker_single",
            str(working_pdf),
            "--output_dir",
            str(paper_output_dir),
        ])

    elif engine == "docling":
        run_command([
            "docling",
            str(working_pdf),
            "--to",
            "md",
            "--output",
            str(paper_output_dir),
        ])

    else:
        raise ValueError(f"Unsupported engine: {engine}")


def batch_convert(
    input_dir: Path,
    output_dir: Path,
    engine: str = "marker",
    enable_ocr: bool = True,
) -> None:
    pdf_files = sorted(input_dir.glob("*.pdf"))

    if not pdf_files:
        raise FileNotFoundError(f"No PDF files found in {input_dir}")

    summary = {"total": len(pdf_files), "success": 0, "failed": 0, "failed_files": []}

    for pdf_path in pdf_files:
        print(f"Processing: {pdf_path.name}")

        try:
            convert_one_pdf(
                pdf_path=pdf_path,
                output_dir=output_dir,
                engine=engine,
                enable_ocr=enable_ocr,
            )
            summary["success"] += 1
        except Exception as error:
            print(f"Failed: {pdf_path.name}")
            print(error)
            summary["failed"] += 1
            summary["failed_files"].append(pdf_path.name)

    (output_dir / "batch_summary.json").write_text(json.dumps(summary, indent=2))


if __name__ == "__main__":
    batch_convert(
        input_dir=Path("input"),
        output_dir=Path("output"),
        engine="marker",
        enable_ocr=True,
    )
```

---

## 14. Markdown出力例

```markdown
# Example Paper Title

## Abstract

This paper proposes a method for reconstructing spectral reflectance from RGB observations.

## 1. Introduction

The spectral image formation model maps a high-dimensional reflectance vector to a low-dimensional RGB vector.

$$
y_p = S^\top \operatorname{diag}(e) r_p
$$

where $y_p$ is the observed RGB value, $S$ is the camera spectral sensitivity, $e$ is the illumination spectrum, and $r_p$ is the spectral reflectance.

![Overview of the proposed method.](figures/figure_001.png)

**Figure 1.** Overview of the proposed method.

## 2. Method

...

## References

[1] ...
```

---

## 15. 初期検証手順

同じ論文PDFに対して、次の3つを比較する。

```bash
python src/batch_convert.py --input_dir input --output_dir output_marker --engine marker
python src/batch_convert.py --input_dir input --output_dir output_mineru --engine mineru
python src/batch_convert.py --input_dir input --output_dir output_docling --engine docling
```

比較観点:

| 観点 | 確認内容 |
|---|---|
| 見出し | セクション構造が維持されているか |
| 本文 | 2段組の読み順が正しいか |
| 数式 | LaTeX形式で再現されているか |
| 図 | 図画像が保存されているか |
| キャプション | 図番号と説明が対応しているか |
| 表 | Markdown tableとして使えるか |
| 参考文献 | 参考文献が崩れていないか |

---

## 16. 推奨開発ステップ

### Step 1: 最小構成

```text
PDF判定
→ OCRmyPDF
→ Marker
→ paper.md出力
```

### Step 2: バッチ化

```text
input/*.pdf 列挙
→ PDFごとに独立した出力ディレクトリ
→ 失敗継続
→ batch_summary.json 出力
```

### Step 3: 後処理追加

```text
Markdown正規化
→ 図パス整理
→ キャプション整理
→ References整理
```

### Step 4: 品質評価追加

```text
数式数の検出
図数の検出
キャプション数の検出
品質レポート生成
```

### Step 5: エンジン比較

```text
Marker
MinerU
Docling
Nougat
```

### Step 6: 精度改善

```text
数式OCR補正
表画像フォールバック
LLMによるMarkdown整形補助
```

---

## 17. 注意点

### 17.1 完全自動化は難しい

PDF論文は、レイアウト情報と意味構造が分離していない場合が多い。

そのため、数式、表、図キャプション、2段組の読み順は崩れる可能性がある。

最終的な用途が論文再利用や翻訳である場合、人手確認を前提にする必要がある。

### 17.2 スキャンPDFは精度が画質に依存する

画像PDFでは、次の条件でOCR精度が低下する。

- 解像度が低い
- 文字が小さい
- ページが傾いている
- ノイズが多い
- 数式が複雑
- 図と本文が近い
- 2段組の間隔が狭い

### 17.3 数式は専用OCRが必要になる場合がある

通常のOCRは、数式の上付き、下付き、ギリシャ文字、行列、積分記号を誤認識しやすい。

数式品質が重要な場合は、pix2texやNougatの利用を検討する。

### 17.4 並列処理時のリソース

OCRやMarkerはメモリ・GPUを使う。`--workers` を増やす場合は、メモリ使用量とGPU占有を確認する。初期は `--workers 1` から開始する。

---

## 18. 推奨構成まとめ

初期実装では、次の構成を採用する。

```text
input/*.pdf
↓
PDFごとに処理
↓
PyMuPDFでテキストレイヤー判定
↓
テキストあり:
    MarkerでMarkdown変換
テキストなし:
    OCRmyPDFでOCR付きPDF生成
    MarkerでMarkdown変換
↓
Markdown正規化
↓
図・表・数式・キャプション確認
↓
品質レポート生成
↓
output/{paper_name}/paper.md
↓
全PDF完了後に batch_summary.json 出力
```

将来的な精度改善では、次を追加する。

```text
MinerU / Docling / Nougatとの比較
pix2texによる数式補正
PaddleOCRによる表・レイアウト解析
LLMによるMarkdown整形補助
```

---

## 19. 成功条件

本パイプラインの成功条件は次の通りである。

1. PDFごとに `output/{paper_name}/paper.md` が生成される
2. セクション構造がMarkdown見出しとして保持される
3. 数式がLaTeX形式で保持される
4. 図が画像ファイルとして保存される
5. 図キャプションがMarkdown中に明示される
6. 表がMarkdown tableまたは画像として保持される
7. 参考文献がReferencesセクションにまとまる
8. PDFごとの変換品質レポートが生成される
9. バッチ全体のサマリ `batch_summary.json` が生成される
10. 1本のPDFで失敗しても残りのPDFの処理が継続する

---

## 20. 非目標

初期実装では、次を非目標とする。

- すべての数式の完全なLaTeX復元
- すべての表の完全なMarkdown table化
- 図とキャプションの100%自動対応
- 参考文献のBibTeX化
- 論文内容の意味的校正
- 日本語論文への完全対応

---

## 21. 今後の拡張案

今後、次の機能を追加できる。

1. arXiv IDからのPDF取得
2. DOIからのメタデータ取得
3. BibTeX出力
4. 数式番号の復元
5. 図表番号の自動対応
6. LLMによるセクション要約
7. MarkdownからHTML / Word / LaTeXへの再変換
8. RAG用チャンク分割
9. 品質スコアによる変換エンジン自動選択
10. 失敗したPDFの自動再試行（別エンジンへのフォールバック）
