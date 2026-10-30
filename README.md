# pdiff

Step through the diffs between consecutive commits of a git branch.

Pick a branch, then a start and an end commit. For a chain A→B→C→D→E, pdiff
shows `diff A B`, `diff B C`, `diff C D`, `diff D E` one at a time. Navigation
never wraps. Merges follow the first-parent path, so each step is a clean
parent→child diff.

## Dependencies

- [uv](https://docs.astral.sh/uv/) (installs Python 3.12+ and the Python deps)
- `git` on your `PATH`

## Run

```sh
uv run pdiff                 # from inside any git repo, using this project
uv tool install .            # or install `pdiff` on your PATH
```

## Keys

| key | action |
|---|---|
| `l` `→` `]` / `h` `←` `[` | next / previous diff |
| `j` `k` `space` `b` `g` `G` | scroll (`H` `L` scroll sideways) |
| `n` / `N` | next / previous file |
| `f` | toggle the full start→end diff |
| `i` / `s` | toggle commit details / `--stat` |
| `w` / `d` | toggle ignore-whitespace / word-diff |
| `+` / `-` | more / fewer context lines |
| `r` | re-pick commits |
| `q` | quit |

In the pickers, `/` focuses the filter box, `Enter` selects (first the start
commit, then the end), and `Esc` goes back.

## Test

```sh
uv run pytest
```
