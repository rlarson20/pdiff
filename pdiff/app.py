"""pdiff: step through the diffs between consecutive commits of a branch.

Pick a branch, then a start and an end commit. pdiff walks the first-parent
chain A -> B -> C -> D -> E and shows `diff A B`, `diff B C`, ... one at a time.
Navigation never wraps: you can't go back from the first diff or forward
from the last one.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from bisect import bisect_right
from dataclasses import dataclass
from functools import lru_cache
from itertools import pairwise
from pathlib import Path
from typing import Any

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import ScrollableContainer, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Footer, Input, OptionList, Static
from textual.widgets.option_list import Option

COMMIT_FMT = "%H%x1f%P%x1f%h%x1f%an%x1f%ad%x1f%s"
MAX_COMMITS = 5000  # cap on the commit picker so huge repos stay snappy
MAX_CONTEXT = 99
DEFAULT_MAX_LINES = 5000  # per-step render cap; huge Statics make Textual crawl


# --------------------------------------------------------------------------
# git layer
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Commit:
    sha: str
    parent: str  # first parent, "" for a root commit
    short: str
    author: str
    date: str
    subject: str


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=check,
    )


def repo_root() -> Path:
    try:
        out = git("rev-parse", "--show-toplevel", check=False)
    except FileNotFoundError:
        raise RuntimeError("git not found on PATH") from None
    if out.returncode != 0:
        raise RuntimeError("not inside a git repository")
    return Path(out.stdout.strip())


def parse_commits(out: str) -> list[Commit]:
    commits = []
    for line in out.splitlines():
        sha, parents, short, author, date, subject = line.split("\x1f", 5)
        commits.append(
            Commit(sha, parents.split()[0] if parents else "", short, author, date, subject)
        )
    return commits


def list_branches() -> list[str]:
    """Local branches, current one first, then most recently committed."""
    out = git(
        "for-each-ref", "--sort=-committerdate", "--format=%(refname:short)", "refs/heads"
    ).stdout.split()
    current = git("branch", "--show-current").stdout.strip()
    if current in out:
        out.remove(current)
        out.insert(0, current)
    elif not current:  # detached HEAD
        out.insert(0, "HEAD")
    return out


def rev_parse(ref: str) -> str | None:
    """Full SHA of the commit `ref` names, or None if it doesn't name one."""
    if ref.startswith("-"):  # never let a ref be read as an option
        return None
    out = git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False)
    return out.stdout.strip() if out.returncode == 0 else None


def is_ancestor(a: str, b: str) -> bool:
    return git("merge-base", "--is-ancestor", a, b, check=False).returncode == 0


def list_commits(ref: str, limit: int = MAX_COMMITS) -> list[Commit]:
    out = git(
        "log", "--first-parent", f"--max-count={limit}", f"--format={COMMIT_FMT}",
        "--date=short", ref, "--",
    ).stdout  # fmt: skip
    return parse_commits(out)


def commit_chain(a: str, e: str) -> list[Commit]:
    """First-parent chain from a to e inclusive (oldest first).

    The arguments may be given in either order. Raises ValueError when the
    commits can't be walked as a single linear chain.
    """
    if a == e:
        raise ValueError("pick two different commits")
    if not is_ancestor(a, e):
        if not is_ancestor(e, a):
            raise ValueError("neither commit is an ancestor of the other")
        a, e = e, a
    start = parse_commits(
        git("show", "-s", f"--format={COMMIT_FMT}", "--date=short", a).stdout
    )
    rest = parse_commits(
        git(
            "log", "--first-parent", "--ancestry-path", "--reverse",
            f"--format={COMMIT_FMT}", "--date=short", f"{a}..{e}",
        ).stdout  # fmt: skip
    )
    chain = [*start, *rest]
    if len(chain) < 2 or any(cur.parent != prev.sha for prev, cur in pairwise(chain)):
        raise ValueError("start commit is not on the first-parent path of the end commit")
    return chain


def parse_range(spec: str) -> tuple[str, str]:
    """Split `A..B` into its refs; an omitted side means HEAD, as in git."""
    if "..." in spec:
        raise ValueError(f"{spec!r}: A...B isn't supported, use A..B")
    a, sep, b = spec.partition("..")
    if not sep:
        raise ValueError(f"{spec!r}: expected a range like A..B")
    return a or "HEAD", b or "HEAD"


def range_chain(spec: str) -> list[Commit]:
    """The chain for `A..B`, read the way `git diff A..B` reads it: A is the base."""
    refs = parse_range(spec)
    shas = []
    for ref in refs:
        sha = rev_parse(ref)
        if sha is None:
            raise ValueError(f"unknown revision {ref!r}")
        shas.append(sha)
    (a_ref, b_ref), (a, b) = refs, shas
    if a == b:
        raise ValueError(f"empty range: {a_ref} and {b_ref} are the same commit")
    if not is_ancestor(a, b):
        if is_ancestor(b, a):
            raise ValueError(f"{a_ref} is newer than {b_ref}; did you mean {b_ref}..{a_ref}?")
        raise ValueError(f"{a_ref} is not an ancestor of {b_ref}")
    return commit_chain(a, b)


def web_url(remote: str) -> str | None:
    """`https://host/owner/repo` for an https, ssh or scp-style remote URL."""
    m = re.fullmatch(
        r"(?:(?:https?|ssh|git)://)?(?:[^@/]+@)?([^/:]+)(?::\d+)?[:/]([^/].*?)(?:\.git)?/?",
        remote,
    )
    return f"https://{m[1]}/{m[2]}" if m else None


def commit_url(sha: str) -> str | None:
    """Web permalink for a commit on `origin` (or the only remote), if any."""
    remotes = git("remote").stdout.split()
    name = "origin" if "origin" in remotes else remotes[0] if len(remotes) == 1 else None
    if name is None:
        return None
    base = web_url(git("remote", "get-url", name).stdout.strip())
    return f"{base}/commit/{sha}" if base else None


@dataclass(frozen=True)
class DiffView:
    text: Text
    file_offsets: tuple[int, ...]  # line index of each `diff --git` header
    hunk_offsets: tuple[int, ...] = ()  # line index of each `@@` header
    lines: int = 0


def line_offsets(plain: str, prefix: str) -> tuple[int, ...]:
    return tuple(i for i, ln in enumerate(plain.split("\n")) if ln.startswith(prefix))


def file_offsets(plain: str) -> tuple[int, ...]:
    return line_offsets(plain, "diff --git")


def hunk_offsets(plain: str) -> tuple[int, ...]:
    return line_offsets(plain, "@@")


def truncate(view: DiffView, max_lines: int) -> DiffView:
    """The first `max_lines` lines of `view` plus a note, or `view` if it fits."""
    if not max_lines or view.lines <= max_lines:
        return view
    plain = view.text.plain
    cut = -1
    for _ in range(max_lines):
        cut = plain.index("\n", cut + 1)
    text = view.text[:cut]
    text.append(
        f"\n\n… {view.lines - max_lines} more lines not shown (a: show all)",
        style="bold yellow",
    )
    return DiffView(
        text,
        tuple(o for o in view.file_offsets if o < max_lines),
        tuple(o for o in view.hunk_offsets if o < max_lines),
        max_lines,
    )


@lru_cache(maxsize=64)
def load_diff(a: str, b: str, ignore_ws: bool, word: bool, context: int) -> DiffView:
    args = ["diff", "--no-ext-diff", "--color=always", f"-U{context}"]
    if ignore_ws:
        args.append("-w")
    if word:
        args.append("--word-diff=color")
    text = Text.from_ansi(git(*args, a, b).stdout)
    text.rstrip()
    plain = text.plain
    if not plain:
        return DiffView(Text("(no changes)", style="dim"), (), (), 1)
    return DiffView(text, file_offsets(plain), hunk_offsets(plain), plain.count("\n") + 1)


def load_patch(a: str, b: str, ignore_ws: bool, context: int) -> str:
    """Uncoloured, applyable `git diff` (no word-diff) for copying."""
    args = ["diff", "--no-ext-diff", "--no-color", f"-U{context}"]
    if ignore_ws:
        args.append("-w")
    return git(*args, a, b).stdout


@lru_cache(maxsize=64)
def load_stat(a: str, b: str) -> Text:
    out = git("diff", "--no-ext-diff", "--color=always", "--stat=120", a, b).stdout
    text = Text.from_ansi(out)
    text.rstrip()
    return text


@lru_cache(maxsize=64)
def load_details(sha: str) -> Text:
    return Text(git("show", "-s", "--format=fuller", sha).stdout.rstrip())


# --------------------------------------------------------------------------
# pickers
# --------------------------------------------------------------------------


class Picker(Screen[Any]):
    """Filterable list. Rows are (id, label); `/` focuses the filter box."""

    BINDINGS = [
        Binding("slash", "filter", "Filter"),
        Binding("escape", "back", "Back"),
    ]

    def __init__(self, heading: str, rows: list[tuple[str, str]]) -> None:
        super().__init__()
        self.heading = heading
        self.rows = rows
        self.query_text = ""

    def compose(self) -> ComposeResult:
        yield Static(id="picker-heading")
        yield Input(placeholder="filter (press / to focus, Esc to leave)", id="filter")
        yield OptionList(id="options")
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_heading()
        self.fill()
        self.query_one(OptionList).focus()

    def refresh_heading(self) -> None:
        self.query_one("#picker-heading", Static).update(Text(self.heading, style="bold"))

    def row_label(self, row_id: str, label: str) -> Text:
        return Text(label, no_wrap=True, overflow="ellipsis")

    def fill(self) -> None:
        terms = self.query_text.lower().split()
        options = [
            Option(self.row_label(row_id, label), id=row_id)
            for row_id, label in self.rows
            if all(t in label.lower() for t in terms)
        ]
        ol = self.query_one(OptionList)
        ol.clear_options()
        ol.add_options(options)
        if options:
            ol.highlighted = 0

    @on(Input.Changed)
    def _filter_changed(self, event: Input.Changed) -> None:
        self.query_text = event.value
        self.fill()

    @on(Input.Submitted)
    def _filter_submitted(self) -> None:
        self.query_one(OptionList).focus()

    @on(OptionList.OptionSelected)
    def _selected(self, event: OptionList.OptionSelected) -> None:
        if event.option.id is not None:
            self.choose(event.option.id)

    def action_filter(self) -> None:
        self.query_one(Input).focus()

    def action_back(self) -> None:
        if self.query_one(Input).has_focus:
            self.query_one(OptionList).focus()
        else:
            self.go_back()

    def choose(self, row_id: str) -> None:
        self.dismiss(row_id)

    def go_back(self) -> None:
        self.dismiss(None)


class BranchPicker(Picker):
    def __init__(self, branches: list[str]) -> None:
        super().__init__(
            "Pick a branch  (enter: select, /: filter, q: quit)",
            [(b, b) for b in branches],
        )


class CommitPicker(Picker):
    """Two-step picker: first Enter marks the start commit, second the end."""

    PICK_START = "Pick the START commit  (enter: select, /: filter, esc: back, q: quit)"
    PICK_END = "Pick the END commit  (enter: select, esc: unmark start, q: quit)"

    def __init__(self, branch: str, commits: list[Commit]) -> None:
        rows = [
            (c.sha, f"{c.short}  {c.date}  {c.subject}  — {c.author}") for c in commits
        ]
        super().__init__(f"{branch}: {self.PICK_START}", rows)
        self.branch = branch
        self.start: str | None = None

    def row_label(self, row_id: str, label: str) -> Text:
        mark = "● " if row_id == self.start else "  "
        return Text(mark + label, no_wrap=True, overflow="ellipsis")

    def choose(self, row_id: str) -> None:
        if self.start is None:
            self.start = row_id
            self.heading = f"{self.branch}: {self.PICK_END}"
            self.refresh_heading()
            self.fill()
        elif row_id == self.start:
            self.notify("Pick a different commit for the end", severity="warning")
        else:
            self.dismiss((self.start, row_id))

    def go_back(self) -> None:
        if self.start is None:
            self.dismiss(None)
        else:
            self.start = None
            self.heading = f"{self.branch}: {self.PICK_START}"
            self.refresh_heading()
            self.fill()


# --------------------------------------------------------------------------
# modals
# --------------------------------------------------------------------------


class JumpScreen(ModalScreen[str | None]):
    """Prompt for a step number, SHA or ref; dismisses with the raw text."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        yield Input(placeholder="step number, SHA or ref (Esc to cancel)", id="jump")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    @on(Input.Submitted)
    def _submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)

    def action_cancel(self) -> None:
        self.dismiss(None)


def help_text(app: App[Any], bindings: list[Binding]) -> Text:
    """Key table built from `bindings`: one row per description, keys joined."""
    rows: dict[str, list[str]] = {}
    for b in Binding.make_bindings(bindings):
        if label := b.tooltip or b.description:
            rows.setdefault(label, []).append(app.get_key_display(b))
    keys = {label: " ".join(k) for label, k in rows.items()}
    width = max(map(len, keys.values()), default=0)
    text = Text()
    for label, k in keys.items():
        text.append(f"{k:>{width}}", style="bold")
        text.append(f"  {label}\n")
    text.rstrip()
    return text


class HelpScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape,question_mark", "dismiss", "Close")]

    def __init__(self, bindings: list[Binding]) -> None:
        super().__init__()
        self.bindings = bindings

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="help"):
            yield Static(help_text(self.app, self.bindings), id="help-text")


# --------------------------------------------------------------------------
# diff viewer
# --------------------------------------------------------------------------


@dataclass
class Options:
    """Initial DiffScreen state. The CLI fills it in (and later a config file)."""

    ignore_ws: bool = False
    word: bool = False
    context: int = 3
    show_stat: bool = False
    show_details: bool = False
    full: bool = False
    max_lines: int = DEFAULT_MAX_LINES  # 0 = no cap


class Body(ScrollableContainer):
    can_focus = False


class Info(VerticalScroll):
    can_focus = False


class DiffScreen(Screen[None]):
    BINDINGS = [
        Binding("l,right,right_square_bracket", "next", "Next", tooltip="Next step"),
        Binding("h,left,left_square_bracket", "prev", "Prev", tooltip="Previous step"),
        Binding("colon", "goto", "Go to", show=False, tooltip="Go to step N, SHA or ref"),
        Binding("n", "seek('file', 1)", "File↓", tooltip="Next file"),
        Binding("N", "seek('file', -1)", "File↑", tooltip="Previous file"),
        Binding("right_curly_bracket", "seek('hunk', 1)", show=False, tooltip="Next hunk"),
        Binding("left_curly_bracket", "seek('hunk', -1)", show=False, tooltip="Previous hunk"),
        Binding("f", "toggle_full", "Full", tooltip="Toggle full start→end diff"),
        Binding("i", "toggle_details", "Info", tooltip="Toggle commit details"),
        Binding("s", "toggle_stat", "Stat", tooltip="Toggle --stat"),
        Binding("w", "toggle_ws", "Ws", tooltip="Toggle ignore-whitespace"),
        Binding("d", "toggle_word", "Word", tooltip="Toggle word-diff"),
        Binding("plus", "context(1)", "+Ctx", tooltip="More context lines"),
        Binding("equals", "context(1)", show=False),  # unshifted `+`; kept out of help
        Binding("minus", "context(-1)", "-Ctx", tooltip="Fewer context lines"),
        Binding("a", "toggle_all", show=False, tooltip="Show all lines of a truncated diff"),
        Binding("y", "yank('sha')", show=False, tooltip="Copy SHA"),
        Binding("Y", "yank('patch')", show=False, tooltip="Copy diff as a patch"),
        Binding("p", "yank('link')", show=False, tooltip="Copy commit permalink"),
        Binding("r", "repick", "Re-pick"),
        Binding("j,down", "scroll('down')", show=False, tooltip="Scroll down"),
        Binding("k,up", "scroll('up')", show=False, tooltip="Scroll up"),
        Binding("space,pagedown", "scroll('page_down')", show=False, tooltip="Page down"),
        Binding("b,pageup", "scroll('page_up')", show=False, tooltip="Page up"),
        Binding("g,home", "scroll('home')", show=False, tooltip="Top"),
        Binding("G,end", "scroll('end')", show=False, tooltip="Bottom"),
        Binding("H", "scroll('left')", show=False, tooltip="Scroll left"),
        Binding("L", "scroll('right')", show=False, tooltip="Scroll right"),
    ]

    def __init__(
        self, repo: str, branch: str, chain: list[Commit], opts: Options | None = None
    ) -> None:
        super().__init__()
        opts = opts or Options()
        self.repo = repo
        self.branch = branch
        self.chain = chain
        self.idx = 0  # diff is chain[idx] -> chain[idx + 1]
        self.full = opts.full
        self.ignore_ws = opts.ignore_ws
        self.word = opts.word
        self.context = max(0, min(opts.context, MAX_CONTEXT))
        self.show_stat = opts.show_stat
        self.show_details = opts.show_details
        self.max_lines = opts.max_lines
        self.show_all = False  # lift the max_lines cap for the current step
        self.total_lines = 0  # of the current diff, before truncation
        self.file_idx: int | None = None
        self.hunk_idx: int | None = None
        self.view = DiffView(Text(), ())
        self.header_plain = ""
        self.status_plain = ""

    @property
    def steps(self) -> int:
        return len(self.chain) - 1

    @property
    def truncated(self) -> bool:
        return self.view.lines < self.total_lines

    def compose(self) -> ComposeResult:
        yield Static(id="header")
        with Info(id="info"):
            yield Static(id="info-text")
        with Body(id="body"):
            yield Static(id="diff")
        yield Static(id="status")
        yield Footer()

    def on_mount(self) -> None:
        self.render_view()

    # -- rendering ---------------------------------------------------------

    def pair(self) -> tuple[Commit, Commit]:
        if self.full:
            return self.chain[0], self.chain[-1]
        return self.chain[self.idx], self.chain[self.idx + 1]

    def render_view(self) -> None:
        a, b = self.pair()
        whole = load_diff(a.sha, b.sha, self.ignore_ws, self.word, self.context)
        self.total_lines = whole.lines
        self.view = whole if self.show_all else truncate(whole, self.max_lines)
        self.file_idx = self.hunk_idx = None

        head = Text(no_wrap=True, overflow="ellipsis")
        head.append(f"{self.repo}", style="bold")
        head.append(f" · {self.branch}   ")
        if self.full:
            head.append(f"FULL {len(self.chain)} commits", style="bold magenta")
        else:
            head.append(f"step {self.idx + 1}/{self.steps}", style="bold cyan")
        head.append(f"   {a.short} → {b.short}\n", style="yellow")
        head.append(b.subject if not self.full else f"{a.subject}  …  {b.subject}")
        self.header_plain = head.plain
        self.query_one("#header", Static).update(head)

        self.query_one("#diff", Static).update(self.view.text)
        self.query_one(Body).scroll_home(animate=False)
        self.render_info(a, b)
        self.render_status()

    def render_info(self, a: Commit, b: Commit) -> None:
        parts: list[Text] = []
        if self.show_details:
            if self.full:
                lines = [f"{c.short}  {c.date}  {c.author}  {c.subject}" for c in self.chain]
                parts.append(Text("\n".join(lines)))
            else:
                parts.append(load_details(b.sha))
        if self.show_stat:
            parts.append(load_stat(a.sha, b.sha))
        info = self.query_one("#info")
        info.display = bool(parts)
        self.query_one("#info-text", Static).update(Text("\n\n").join(parts))

    def render_status(self) -> None:
        def where(idx: int | None, n: int, noun: str) -> str:
            return f"{noun} {idx + 1}/{n}" if idx is not None else f"{n} {noun}s"

        status = (
            f"whitespace: {'ignored' if self.ignore_ws else 'shown'}   "
            f"word-diff: {'on' if self.word else 'off'}   "
            f"context: {self.context}   "
            f"{where(self.file_idx, len(self.view.file_offsets), 'file')}   "
            f"{where(self.hunk_idx, len(self.view.hunk_offsets), 'hunk')}"
        )
        if self.truncated:
            status += f"   showing {self.view.lines}/{self.total_lines} lines (a: all)"
        self.status_plain = status
        self.query_one("#status", Static).update(Text(status, style="dim"))

    # -- navigation (never wraps) -------------------------------------------

    def step(self, delta: int) -> None:
        was_full, self.full = self.full, False
        new = self.idx + delta
        if not 0 <= new < self.steps:
            self.notify("last diff" if delta > 0 else "first diff")
            if was_full:
                self.show_all = False
                self.render_view()
            return
        self.idx = new
        self.show_all = False
        self.render_view()

    def action_next(self) -> None:
        self.step(1)

    def action_prev(self) -> None:
        self.step(-1)

    def action_goto(self) -> None:
        self.app.push_screen(JumpScreen(), self.goto)

    def resolve_step(self, target: str) -> int | None:
        """Step index for a 1-based step number, or for a SHA/ref in the chain.

        A commit maps to the step that introduces it; the start commit to step 1.
        """
        if target.isdigit() and 1 <= int(target) <= self.steps:
            return int(target) - 1
        sha = rev_parse(target)
        for i, c in enumerate(self.chain):
            if c.sha == sha:
                return max(i - 1, 0)
        return None

    def goto(self, target: str | None) -> None:
        if target is None:
            return
        idx = self.resolve_step(target)
        if idx is None:
            self.notify(f"no step matches {target!r}", severity="warning")
            return
        self.idx, self.full, self.show_all = idx, False, False
        self.render_view()

    def action_seek(self, kind: str, delta: int) -> None:
        """Jump to the next/previous file or hunk header, clamped at the ends."""
        offsets = self.view.file_offsets if kind == "file" else self.view.hunk_offsets
        if not offsets:
            return
        cur = self.file_idx if kind == "file" else self.hunk_idx
        if cur is None:  # start from the position at the current scroll offset
            cur = bisect_right(offsets, int(self.query_one(Body).scroll_y)) - 1
        y = offsets[max(0, min(cur + delta, len(offsets) - 1))]
        self.query_one(Body).scroll_to(y=y, animate=False)
        # keep both cursors in step, so `n` after `}` continues from the same spot
        files, hunks = self.view.file_offsets, self.view.hunk_offsets
        self.file_idx = max(bisect_right(files, y) - 1, 0) if files else None
        self.hunk_idx = bisect_right(hunks, y) - 1 if hunks and y >= hunks[0] else None
        self.render_status()

    def action_scroll(self, how: str) -> None:
        body = self.query_one(Body)
        match how:
            case "down":
                body.scroll_down(animate=False)
            case "up":
                body.scroll_up(animate=False)
            case "page_down":
                body.scroll_page_down(animate=False)
            case "page_up":
                body.scroll_page_up(animate=False)
            case "home":
                body.scroll_home(animate=False)
            case "end":
                body.scroll_end(animate=False)
            case "left":
                body.scroll_relative(x=-8, animate=False)
            case "right":
                body.scroll_relative(x=8, animate=False)

    # -- toggles -------------------------------------------------------------

    def action_toggle_full(self) -> None:
        self.full = not self.full
        self.show_all = False
        self.render_view()

    def action_toggle_details(self) -> None:
        self.show_details = not self.show_details
        self.render_info(*self.pair())

    def action_toggle_stat(self) -> None:
        self.show_stat = not self.show_stat
        self.render_info(*self.pair())

    def action_toggle_ws(self) -> None:
        self.ignore_ws = not self.ignore_ws
        self.render_view()

    def action_toggle_word(self) -> None:
        self.word = not self.word
        self.render_view()

    def action_context(self, delta: int) -> None:
        self.context = max(0, min(self.context + delta, MAX_CONTEXT))
        self.render_view()

    def action_toggle_all(self) -> None:
        if not (self.truncated or self.show_all):
            self.notify("nothing is truncated")
            return
        self.show_all = not self.show_all
        self.render_view()

    # -- copying -------------------------------------------------------------

    def action_yank(self, what: str) -> None:
        a, b = self.pair()
        match what:
            case "sha":
                text, done = b.sha, f"copied {b.short}"
            case "patch":
                text, done = load_patch(a.sha, b.sha, self.ignore_ws, self.context), "copied diff"
            case _:
                url = commit_url(b.sha)
                if url is None:
                    self.notify("no web remote to link to", severity="warning")
                    return
                text, done = url, "copied permalink"
        self.app.copy_to_clipboard(text)
        self.notify(done)

    def action_repick(self) -> None:
        self.dismiss(None)


# --------------------------------------------------------------------------
# app
# --------------------------------------------------------------------------


class PDiffApp(App[None]):
    CSS = """
    #picker-heading { height: 1; padding: 0 1; background: $boost; }
    #filter { margin: 0 1; }
    #options { height: 1fr; }
    #header { height: 2; padding: 0 1; background: $boost; }
    #info { display: none; height: auto; max-height: 12; padding: 0 1;
            border-bottom: solid $primary; }
    #body { height: 1fr; }
    #diff { width: auto; min-width: 100%; padding: 0 1; }
    #status { height: 1; padding: 0 1; }
    JumpScreen, HelpScreen { align: center middle; }
    #jump { width: 60; }
    #help { width: auto; height: auto; max-height: 90%; padding: 0 1;
            border: round $primary; background: $surface; }
    #help-text { width: auto; }
    """
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("question_mark", "help", "Help", tooltip="This help"),
    ]

    def __init__(
        self,
        repo: str,
        opts: Options | None = None,
        chain: list[Commit] | None = None,
        label: str = "",
    ) -> None:
        super().__init__()
        self.repo = repo
        self.opts = opts or Options()
        self.chain = chain  # given on the command line: skip the pickers
        self.label = label
        self.branch = ""
        self.commits: list[Commit] = []

    def on_mount(self) -> None:
        if self.chain:
            screen = DiffScreen(self.repo, self.label, self.chain, self.opts)
            self.push_screen(screen, lambda _: self.show_branches())
        else:
            self.show_branches()

    def action_help(self) -> None:
        if isinstance(self.screen, ModalScreen):
            return
        self.push_screen(HelpScreen([*type(self.screen).BINDINGS, *self.BINDINGS]))

    def show_branches(self) -> None:
        self.push_screen(BranchPicker(list_branches()), self.on_branch)

    def on_branch(self, branch: str | None) -> None:
        if branch is None:
            self.exit()
            return
        try:
            commits = list_commits(branch)
        except subprocess.CalledProcessError as exc:
            self.notify(exc.stderr.strip() or f"can't read {branch}", severity="error")
            commits = []
        if len(commits) < 2:
            self.notify(f"{branch} needs at least 2 commits", severity="warning")
            self.show_branches()
            return
        self.branch, self.commits = branch, commits
        self.show_commits()

    def show_commits(self) -> None:
        self.push_screen(CommitPicker(self.branch, self.commits), self.on_commits)

    def on_commits(self, picked: tuple[str, str] | None) -> None:
        if picked is None:
            self.show_branches()
            return
        try:
            chain = commit_chain(*picked)
        except (ValueError, subprocess.CalledProcessError) as exc:
            self.notify(str(exc), severity="error")
            self.show_commits()
            return
        screen = DiffScreen(self.repo, self.branch, chain, self.opts)
        self.push_screen(screen, lambda _: self.show_commits())


# --------------------------------------------------------------------------
# command line
# --------------------------------------------------------------------------


def int_in(lo: int, hi: int | None = None):
    def parse(value: str) -> int:
        try:
            n = int(value)
        except ValueError:
            raise argparse.ArgumentTypeError(f"not an integer: {value!r}") from None
        if n < lo or (hi is not None and n > hi):
            bound = f"{lo}..{hi}" if hi is not None else f">= {lo}"
            raise argparse.ArgumentTypeError(f"must be {bound}, got {n}")
        return n

    return parse


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="pdiff",
        description="Step through the diffs between consecutive commits of a git branch.",
    )
    p.add_argument(
        "range", nargs="?", metavar="A..B",
        help="open the walk from A to B directly, skipping the pickers; "
        "read like `git diff A..B` (A is the base, an empty side means HEAD)",
    )  # fmt: skip
    p.add_argument("-C", dest="repo", metavar="PATH", help="run as if started in PATH")
    p.add_argument("-w", "--ignore-whitespace", action="store_true", help="start with whitespace ignored")
    p.add_argument("--word-diff", action="store_true", help="start with word-diff on")
    p.add_argument("-U", "--context", type=int_in(0, MAX_CONTEXT), default=3, metavar="N", help="context lines (default: %(default)s)")
    p.add_argument("--stat", action="store_true", help="start with --stat shown")
    p.add_argument("--details", action="store_true", help="start with commit details shown")
    p.add_argument("--full", action="store_true", help="start on the full start→end diff")
    p.add_argument(
        "--max-lines", type=int_in(0), default=DEFAULT_MAX_LINES, metavar="N",
        help="render at most N lines per step until `a` is pressed; 0 for no cap "
        "(default: %(default)s)",
    )  # fmt: skip
    return p.parse_args(argv)


def build_app(argv: list[str] | None = None) -> PDiffApp:
    """Parse argv and resolve everything git-related; raises on user errors."""
    args = parse_args(argv)
    if args.repo is not None:
        try:
            os.chdir(args.repo)
        except OSError as exc:
            raise RuntimeError(f"-C {args.repo}: {exc.strerror}") from None
    root = repo_root()
    if args.range is not None:
        chain = range_chain(args.range)
    elif not list_branches():
        raise RuntimeError("repository has no commits")
    else:
        chain = None
    opts = Options(
        ignore_ws=args.ignore_whitespace, word=args.word_diff, context=args.context,
        show_stat=args.stat, show_details=args.details, full=args.full,
        max_lines=args.max_lines,
    )  # fmt: skip
    return PDiffApp(root.name, opts, chain=chain, label=args.range or "")


def main(argv: list[str] | None = None) -> None:
    try:
        app = build_app(argv)
    except (RuntimeError, ValueError) as exc:
        print(f"pdiff: {exc}", file=sys.stderr)
        sys.exit(1)
    app.run()


if __name__ == "__main__":
    main()
