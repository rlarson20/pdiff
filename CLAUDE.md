# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Managed with `uv` (Python >=3.12, single dependency: `textual`; dev: `pytest`).

```sh
uv run pdiff                                  # run the TUI (must be launched from inside a git repo)
uv run pytest                                 # all tests
uv run pytest tests/test_pdiff.py::test_name  # single test
```

No linter/formatter is configured; the code uses `# fmt: skip` markers on hand-aligned blocks, so don't reformat wholesale.

## Architecture

pdiff is a Textual TUI that steps through diffs between consecutive commits of a git branch. All code lives in one module, `pdiff/app.py` (`pdiff/__init__.py` re-exports `main`), in three layers:

1. **Git layer** (top of the file): shells out to the `git` CLI via the `git()` helper (no libgit2/GitPython). `commit_chain(a, e)` is the core: it accepts the two commits in either order, verifies ancestry with `merge-base --is-ancestor`, and walks `--first-parent --ancestry-path`. It raises `ValueError` if the result isn't a single first-parent chain, which is how merges are kept as clean parent→child diffs. `load_diff`/`load_stat` are `lru_cache`d and return Rich `Text` parsed from git's ANSI colour output (`Text.from_ansi`), so git, not pdiff, produces the diff colouring.
2. **Pickers**: `Picker` is a generic filterable `OptionList` screen; `BranchPicker` and `CommitPicker` subclass it. `CommitPicker` selects start then end commit and dismisses with a `(sha, sha)` tuple.
3. **`DiffScreen` + `PDiffApp`**: `PDiffApp` is a small state machine driven by screen-dismiss callbacks (`on_branch` → `on_commits` → `DiffScreen`, and `r`/back returns up the chain). `DiffScreen` holds the chain and a step index; toggles (whitespace, word-diff, context lines, stat, details, full start→end diff) change the arguments passed to the cached `load_diff` and re-render. Navigation intentionally never wraps.

## Tests

`tests/test_pdiff.py` builds real throwaway git repos in `tmp_path` (linear and merged fixtures, `monkeypatch.chdir` into them, since the git layer uses the cwd) and drives the Textual UI with its async pilot via `asyncio`. Tests need `git` on `PATH`.

## Project notes

`plan.md` is the author's TODO list (colour-scheme config files, CLI args, packaging, alternative commit-selection strategies besides first-parent `git log`); none of these exist yet.
