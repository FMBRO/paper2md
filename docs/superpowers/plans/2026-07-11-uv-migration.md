# uv Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace pip/venv requirements-file setup with a reproducible uv project.

**Architecture:** `pyproject.toml` is the dependency and tool configuration source of truth, while `uv.lock` fixes the complete environment. Project commands run through `uv run`, and OS-level OCR/GUI dependencies remain external.

**Tech Stack:** Python 3.12, uv, pytest

## Global Constraints

- Preserve all existing runtime dependency lower bounds.
- Do not change pipeline runtime behavior.
- Keep Tesseract and Tkinter installation outside Python dependency management.

---

### Task 1: Define and lock the uv project

**Files:** Create `pyproject.toml`, `.python-version`, and `uv.lock`; modify `.gitignore`; delete `requirements.txt`, `requirements-dev.txt`, and `pytest.ini`.

- [ ] Confirm `uv lock --check` fails because `pyproject.toml` is absent.
- [ ] Add project metadata, runtime dependencies, the dev group, and pytest settings.
- [ ] Generate `uv.lock` with `uv lock`.
- [ ] Run `uv lock --check` and expect exit code 0.

### Task 2: Update documentation

**Files:** Modify `README.md` and `docs/spec.md`.

- [ ] Replace pip/venv setup with `uv sync` commands.
- [ ] Prefix Python entry points and pytest with `uv run`.
- [ ] Search maintained documentation for stale requirements-file commands.

### Task 3: Verify the migrated environment

**Files:** Verify `uv.lock` and test `tests/`.

- [ ] Run `uv sync` and expect a successful environment sync.
- [ ] Run `uv run pytest` and expect the full suite to pass.
- [ ] Run `git diff --check` and expect no whitespace errors.
