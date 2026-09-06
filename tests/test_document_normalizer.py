from pathlib import Path

import src.document_normalizer as document_normalizer
from src.document_normalizer import normalize_marker_document, normalize_markdown_document


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "marker-1.10.2"


def test_normalize_marker_document_preserves_typed_blocks_and_positions() -> None:
    marker_document = {
        "pages": [
            {
                "page": 2,
                "blocks": [
                    {"type": "Text", "text": "Methods text", "bbox": [10, 20, 30, 40]},
                    {"type": "Table", "markdown": "| a | b |\n|---|---|\n| 1 | 2 |"},
                    {"type": "Equation", "text": "E = mc^2"},
                ],
            },
            {
                "page": 1,
                "blocks": [
                    {"type": "SectionHeader", "text": "Introduction", "level": 2},
                    {"type": "Figure", "path": "images/fig-1.png", "alt_text": "Flow"},
                    {"type": "Caption", "text": "Figure 1. Flow diagram"},
                ],
            },
        ]
    }

    document = normalize_marker_document(marker_document)

    assert document["schema_version"] == 1
    assert document["source"] == "marker"
    assert [page["number"] for page in document["pages"]] == [1, 2]
    assert document["sections"] == [{
        "id": "section-001", "title": "Introduction", "level": 1,
        "page": 1, "ordinal": 0,
        "source_position": {"page": 1},
    }]
    assert document["paragraphs"][0]["source_position"] == {
        "page": 2, "bbox": [10, 20, 30, 40],
    }
    assert document["tables"][0]["markdown"].startswith("| a |")
    assert document["equations"][0]["text"] == "E = mc^2"
    assert document["figures"][0]["caption"] == "Figure 1. Flow diagram"


def test_markdown_fallback_assigns_page_markers_and_typed_content() -> None:
    markdown = "# Title\n\n## Introduction\n\nFirst page text.\n\n<!-- page: 2 -->\n\n| x | y |\n|---|---|\n| 1 | 2 |\n\n$$\nx = y\n$$\n\n![plot](figures/plot.png)\n\nFigure 1. A plot.\n"

    document = normalize_markdown_document(markdown, page_count=2)

    assert [page["number"] for page in document["pages"]] == [1, 2]
    assert document["source"] == "markdown_fallback"
    assert document["tables"][0]["page"] == 2
    assert document["equations"][0]["text"] == "x = y"
    assert document["figures"][0]["caption"] == "Figure 1. A plot."


def test_marker_normalization_defaults_an_invalid_heading_level() -> None:
    document = normalize_marker_document({"pages": [{"blocks": [
        {"type": "SectionHeader", "text": "Title", "level": "unknown"},
    ]}]})

    assert document["sections"][0]["level"] == 1


def test_marker_normalization_uses_numbered_section_depth() -> None:
    document = normalize_marker_document({"pages": [{"page": 1, "blocks": [
        {"type": "SectionHeader", "text": "Paper title", "level": 1},
        {"type": "SectionHeader", "text": "1 Introduction", "level": 4},
        {"type": "SectionHeader", "text": "2 Method", "level": 1},
        {"type": "SectionHeader", "text": "2.1 Model", "level": 4},
    ]}]})

    assert [section["level"] for section in document["sections"]] == [1, 2, 2, 3]


def test_marker_normalization_clamps_observed_heading_level_jumps() -> None:
    """Marker's Title→Abstract H4 jump must not block an otherwise intact PDF."""
    document = normalize_marker_document({"pages": [{"page": 1, "blocks": [
        {"type": "SectionHeader", "text": "Paper title", "level": 1},
        {"type": "SectionHeader", "text": "Abstract", "level": 4},
        {"type": "SectionHeader", "text": "Introduction", "level": 2},
        {"type": "SectionHeader", "text": "Experiment", "level": 4},
    ]}]})

    assert [section["title"] for section in document["sections"]] == [
        "Paper title", "Abstract", "Introduction", "Experiment",
    ]
    assert [section["level"] for section in document["sections"]] == [1, 2, 2, 3]


def test_markdown_fallback_keeps_form_feed_page_positions() -> None:
    document = normalize_markdown_document(
        "# First\nFirst body.\f# Second\nSecond body.", page_count=2,
    )

    assert [(section["title"], section["page"]) for section in document["sections"]] == [
        ("First", 1), ("Second", 2),
    ]
    assert document["paragraphs"][1]["source_position"]["page"] == 2


def test_real_marker_1_10_2_json_contract_is_recursive_multi_page_and_ordered() -> None:
    marker_document = document_normalizer.load_marker_document(FIXTURE_DIR)

    assert marker_document is not None
    assert marker_document["metadata"]["page_stats"][1]["page_id"] == 1

    document = normalize_marker_document(marker_document)

    assert [page["number"] for page in document["pages"]] == [1, 2]
    assert [section["title"] for section in document["sections"]] == [
        "Introduction", "Results",
    ]
    assert [paragraph["text"] for paragraph in document["paragraphs"]] == [
        "Left column first.", "Right column second.",
    ]
    assert [paragraph["page"] for paragraph in document["paragraphs"]] == [1, 1]
    assert [paragraph["ordinal"] for paragraph in document["paragraphs"]] == [1, 2]
    assert document["tables"][0]["page"] == 2
    assert "<table>" in document["tables"][0]["markdown"]
    assert document["equations"][0]["text"] == "E = mc^2"
    assert document["figures"][0]["path"] == "images/figure-1.png"
    assert document["figures"][0]["caption"] == "Figure 1. Architecture overview."

    markdown = document_normalizer.materialize_marker_markdown(marker_document)
    assert markdown.count("<!-- page:") == 2
    assert markdown.index("Left column first.") < markdown.index("Right column second.")
    assert "## Results" in markdown
    assert "$$\nE = mc^2\n$$" in markdown
