from src.document_normalizer import normalize_marker_document, normalize_markdown_document


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
        "id": "section-001", "title": "Introduction", "level": 2,
        "page": 1, "source_position": {"page": 1},
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


def test_markdown_fallback_keeps_form_feed_page_positions() -> None:
    document = normalize_markdown_document(
        "# First\nFirst body.\f# Second\nSecond body.", page_count=2,
    )

    assert [(section["title"], section["page"]) for section in document["sections"]] == [
        ("First", 1), ("Second", 2),
    ]
    assert document["paragraphs"][1]["source_position"]["page"] == 2
