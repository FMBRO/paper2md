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


def test_normalize_references_heading_promotes_variants() -> None:
    assert normalize_references_heading("References\n[1] ...") == "## References\n[1] ..."
    assert normalize_references_heading("# References\n[1] ...") == "## References\n[1] ..."
    assert normalize_references_heading("**References**\n[1] ...") == "## References\n[1] ..."


def test_normalize_equations_converts_brackets_to_dollars() -> None:
    src = "Intro.\n\n\\[\ny = Ax\n\\]\n\nMore."
    assert normalize_equations(src) == "Intro.\n\n$$\ny = Ax\n$$\n\nMore."


def test_normalize_equations_leaves_existing_dollar_blocks() -> None:
    src = "$$\ny = Ax\n$$"
    assert normalize_equations(src) == src


def test_normalize_captions_figure_variants() -> None:
    assert normalize_captions("Figure 1: Overview.") == "**Figure 1.** Overview."
    assert normalize_captions("Fig. 2. Comparison.") == "**Figure 2.** Comparison."
    assert normalize_captions("**Figure 3.** Already normalized.") == "**Figure 3.** Already normalized."


def test_normalize_captions_table_variants() -> None:
    assert normalize_captions("Table 1: Results.") == "**Table 1.** Results."
    assert normalize_captions("Table 2. Numbers.") == "**Table 2.** Numbers."


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
