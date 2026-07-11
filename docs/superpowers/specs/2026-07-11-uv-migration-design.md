# uv Migration Design

## Goal

Use uv as the only Python environment and dependency-management entry point
without changing the paper conversion pipeline's runtime behavior.

## Design

- Declare runtime dependencies in `pyproject.toml` and test-only dependencies
  in the `dev` dependency group.
- Standardize the project on Python 3.12 through `.python-version` and the
  project `requires-python` constraint.
- Commit `uv.lock` so local and CI environments resolve the same dependency
  versions.
- Move the existing pytest configuration into `pyproject.toml` and remove the
  superseded requirements and pytest configuration files.
- Document `uv sync` and `uv run` as the supported commands. Tesseract,
  Tkinter, and other OS dependencies remain external to uv.

## Verification

Generate and validate the lockfile, create the uv-managed environment, and run
the complete pytest suite through `uv run`.
