from src.quality_gate import evaluate_document_quality


def test_quality_gate_blocks_llm_on_structural_and_text_failures() -> None:
    document = {
        "pages": [{"number": 1}, {"number": 2}],
        "sections": [{"id": "section-001", "title": "Methods", "level": 3, "page": 1}],
        "paragraphs": [{"text": "bad \ufffd text", "page": 1}],
        "tables": [{"markdown": "| only |\n|---|", "page": 1}],
        "equations": [{"text": "", "page": 1}],
        "figures": [{"path": "fig.png", "caption": None, "page": 1}],
        "captions": [],
    }

    result = evaluate_document_quality(
        document,
        page_text=[
            {"page": 1, "character_count": 10, "has_text": True},
            {"page": 2, "character_count": 0, "has_text": False},
        ],
        minimum_characters_per_page=20,
    )

    assert result["passed"] is False
    assert result["llm_allowed"] is False
    assert {issue["code"] for issue in result["issues"]} == {
        "empty_page_text", "low_character_density", "garbled_characters",
        "empty_document_page", "section_hierarchy", "malformed_table", "empty_equation",
        "figure_caption_mismatch",
    }


def test_quality_gate_detects_garbled_section_titles() -> None:
    result = evaluate_document_quality(
        {
            "pages": [{"number": 1}],
            "sections": [{"id": "section-001", "title": "Bad \ufffd title", "level": 1, "page": 1}],
            "paragraphs": [], "tables": [], "equations": [], "figures": [], "captions": [],
        },
        page_text=[{"page": 1, "character_count": 50, "has_text": True}],
    )

    assert [issue["code"] for issue in result["issues"]] == ["garbled_characters"]


def test_quality_gate_rejects_missing_and_empty_structured_pages() -> None:
    inspected = [
        {"page": 1, "character_count": 50, "has_text": True},
        {"page": 2, "character_count": 50, "has_text": True},
    ]
    missing = evaluate_document_quality(
        {"pages": [{"number": 1}], "sections": [{"title": "One", "level": 1, "page": 1}],
         "paragraphs": [], "tables": [], "equations": [], "figures": [], "captions": []},
        page_text=inspected,
    )
    empty = evaluate_document_quality(
        {"pages": [{"number": 1}, {"number": 2}], "sections": [{"title": "One", "level": 1, "page": 1}],
         "paragraphs": [], "tables": [], "equations": [], "figures": [], "captions": []},
        page_text=inspected,
    )

    assert "page_coverage" in [issue["code"] for issue in missing["issues"]]
    assert "empty_document_page" in [issue["code"] for issue in empty["issues"]]
    assert missing["llm_allowed"] is False
    assert empty["llm_allowed"] is False


def test_quality_gate_detects_garbling_in_tables_and_figure_text() -> None:
    result = evaluate_document_quality(
        {
            "pages": [{"number": 1}],
            "sections": [{"title": "Title", "level": 1, "page": 1}],
            "paragraphs": [],
            "tables": [{"markdown": "| a\ufffd | b |\n|---|---|\n| 1 | 2 |", "page": 1}],
            "equations": [],
            "figures": [{"path": "fig.png", "alt_text": "bad \ufffd alt", "caption": "Figure 1.", "page": 1}],
            "captions": [{"text": "Figure 1.", "page": 1}],
        },
        page_text=[{"page": 1, "character_count": 50, "has_text": True}],
    )

    assert [issue["code"] for issue in result["issues"]] == ["garbled_characters"]
