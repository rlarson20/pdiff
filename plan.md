TODO:

Stages run from the current state to a deployable, defensible portfolio project. Each stage should leave `main` releasable.

## Stage 1: Foundation (make it a real CLI tool)
Goal: runnable from anywhere with predictable behavior, and safe to keep building on.
- [] `--repo PATH` / `-C PATH` arg: point the app at a different repo instead of the cwd (git layer currently relies on the cwd, so thread a `cwd` through the `git()` helper rather than `os.chdir`; update the `lru_cache`d loaders so the repo is part of the cache key)
- [] cli args to change default behavior (whitespace, word-diff, context lines, stat, etc.)
- [] range from refs: `pdiff main..feature`, skipping the pickers
- [] clear errors: not a git repo, bad ref, non-linear history, empty range
- [] large-diff safety: truncate/lazy-render huge diffs, skip binary files, bound the `lru_cache`
- [] CI: GitHub Actions running pytest on Linux and macOS, Python 3.12 and 3.13
- [] split `pdiff/app.py` into modules (git layer, pickers, screens) once it stops fitting in your head

## Stage 2: Daily-driver features (make it worth using)
Goal: the review workflow is better than `git log -p`.
- [] jump (`g`) to commit N or a SHA
- [] search (`/`) commit messages or diff content (`git log -S`/`-G` style)
- [] file filter: limit the walk to commits touching a path/glob, optionally show only that path's hunks
- [] per-file and per-hunk navigation within a step (`]`/`[`)
- [] stat overview as a navigator: list of steps with `--stat` summaries, jump to a step
- [] mini-map / progress bar of the whole chain (clickable; Textual already gives us mouse input)
- [] copy/yank (`y`): SHA, current diff, permalink
- [] open the current step in `$EDITOR` at file and line
- [] `?` help overlay generated from the bindings

## Stage 3: Configuration and polish
Goal: it fits into people's existing terminal setups.
- [] config file (`~/.config/pdiff/config.toml`) for defaults, theme, keybindings (same schema as the CLI args)
- [] color scheme support and some default templates (json/toml/yml for colorschemes?)
- [] remappable keybindings
- [] pager / external diff tool support (`delta`, `difftastic`)
- [] snapshot tests for the TUI

## Stage 4: Packaging and release (deployable)
Goal: `uvx pdiff` / `pipx install pdiff` works for a stranger.
- [] packaging for distribution: pyproject metadata, entry point, license, version
- [] publish to PyPI (try TestPyPI first), tagged releases via CI
- [] README overhaul: install, quickstart, GIF/screencast, key bindings, config reference
- [] CHANGELOG and a versioning policy
- [] optional: Homebrew tap

## Stage 5: Differentiators (make it defensible)
Goal: features that set pdiff apart from `tig`, `lazygit`, and `git log -p`, with a story to tell in an interview.
- [] other commit-selection strategies besides the current git-log (squash-aware grouping for `fixup!`/`squash!`, group by author/day)
- [] compare two versions of a branch's history (rebase / force-push), like `git range-diff`
- [] reviewed markers: mark commits as reviewed, persist per branch, resume where you left off
- [] `pdiff --pr 123` (via `gh`)
- [] annotations: attach notes to a commit/hunk, export as markdown
- [] per-step commit-quality hints: flag very large commits, many unrelated files, empty messages
- [] write-up / design notes: why first-parent chains, how merges stay clean, tradeoffs vs. existing tools

## Stage 6: Extended features (as time allows)
- [] bookmarks: pin interesting steps and jump between them
- [] blame at the current step: who last touched a hunk's lines, jump to the introducing commit
- [] auto-refresh: watch the branch and append new commits to the chain (during rebase/fetch)
- [] reflog / "since last viewed" mode
- [] export the walk-through as a patch series or HTML
- [] non-TUI mode: `--plain` / `--json` to print the chain and diffs for scripting and CI
- [] rename awareness (`--follow` style) and readable submodule pointer bumps
