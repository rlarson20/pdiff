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

```sh
pdiff                        # pick a branch, then a start and end commit
pdiff main~10..main          # skip the pickers; read like `git diff A..B`
pdiff ..feature              # an empty side means HEAD
pdiff -C ~/src/other -w -U 1 # another repo, ignoring whitespace, 1 context line
```

| option | effect |
|---|---|
| `-C PATH` | run as if started in `PATH` |
| `-w`, `--ignore-whitespace` | start with whitespace ignored |
| `--word-diff` | start with word-diff on |
| `-U N`, `--context N` | context lines (default 3) |
| `--stat` / `--details` | start with `--stat` / commit details shown |
| `--full` | start on the full start→end diff |
| `--max-lines N` | render at most N lines per step until `a` is pressed; `0` = no cap (default 5000) |

A range must be a single first-parent chain with A older than B. Otherwise
pdiff exits with an error, e.g. `HEAD is newer than HEAD~2; did you mean HEAD~2..HEAD?`.

## Keys

| key | action |
|---|---|
| `l` `→` `]` / `h` `←` `[` | next / previous diff |
| `j` `k` `space` `b` `g` `G` | scroll (`H` `L` scroll sideways) |
| `:` | go to step N, or the step that introduces a SHA/ref |
| `n` / `N` | next / previous file |
| `}` / `{` | next / previous hunk |
| `f` | toggle the full start→end diff |
| `i` / `s` | toggle commit details / `--stat` |
| `w` / `d` | toggle ignore-whitespace / word-diff |
| `+` / `-` | more / fewer context lines |
| `a` | show all lines of a truncated diff |
| `y` / `Y` / `p` | copy SHA / diff as a patch / commit permalink |
| `r` | re-pick commits |
| `?` | help |
| `q` | quit |

In the pickers, `/` focuses the filter box, `Enter` selects (first the start
commit, then the end), and `Esc` goes back.

## Test

```sh
uv run pytest
```
