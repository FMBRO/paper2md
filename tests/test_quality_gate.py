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
        "section_hierarchy", "malformed_table", "empty_equation",
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
