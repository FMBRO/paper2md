import pytest


def test_metadata_normalizes_doi_and_uses_it_as_canonical_identity() -> None:
    from src.research_models import PaperMetadata

    metadata = PaperMetadata(doi="https://doi.org/10.1000/ABC.Def ")

    assert metadata.doi == "10.1000/abc.def"
    assert metadata.canonical_identity(pdf_sha256="ABC") == "doi:10.1000/abc.def"


def test_metadata_falls_back_through_arxiv_zotero_and_pdf_identity() -> None:
    from src.research_models import PaperMetadata

    assert PaperMetadata(arxiv_id="https://arxiv.org/abs/2401.01234v2").canonical_identity() == "arxiv:2401.01234"
    assert PaperMetadata(zotero_library_id="0", zotero_item_key="ABCD1234").canonical_identity() == "zotero:0:ABCD1234"
    assert PaperMetadata().canonical_identity(pdf_sha256="A" * 64) == f"sha256:{'a' * 64}"


def test_metadata_normalizes_an_arxiv_prefixed_identifier() -> None:
    from src.research_models import PaperMetadata

    assert PaperMetadata(arxiv_id="arXiv:2401.01234v2").arxiv_id == "2401.01234"


def test_input_spec_requires_a_nonempty_source() -> None:
    from src.research_models import InputKind, InputSpec

    with pytest.raises(ValueError, match="source"):
        InputSpec(kind=InputKind.DOI, source="  ")
