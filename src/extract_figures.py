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
    text = markdown_path.read_text(encoding="utf-8")

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

    markdown_path.write_text(_IMG_LINK.sub(_rewrite, text), encoding="utf-8")
    return markdown_path
