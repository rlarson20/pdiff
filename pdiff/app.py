"""pdiff: step through the diffs between consecutive commits of a branch.

Pick a branch, then a start and an end commit. pdiff walks the first-parent
chain A -> B -> C -> D -> E and shows `diff A B`, `diff B C`, ... one at a time.
Navigation never wraps: you can't go back from the first diff or forward
from the last one.
"""

from __future__ import annotations

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
from textual.screen import Screen
from textual.widgets import Footer, Input, OptionList, Static
from textual.widgets.option_list import Option

COMMIT_FMT = "%H%x1f%P%x1f%h%x1f%an%x1f%ad%x1f%s"
MAX_COMMITS = 5000  # cap on the commit picker so huge repos stay snappy
MAX_CONTEXT = 99


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
    if git("merge-base", "--is-ancestor", a, e, check=False).returncode != 0:
        if git("merge-base", "--is-ancestor", e, a, check=False).returncode != 0:
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


@dataclass(frozen=True)
class DiffView:
    text: Text
    file_offsets: tuple[int, ...]  # line index of each `diff --git` header


def file_offsets(plain: str) -> tuple[int, ...]:
    return tuple(i for i, ln in enumerate(plain.split("\n")) if ln.startswith("diff --git"))


@lru_cache(maxsize=64)
def load_diff(a: str, b: str, ignore_ws: bool, word: bool, context: int) -> DiffView:
    args = ["diff", "--no-ext-diff", "--color=always", f"-U{context}"]
    if ignore_ws:
        args.append("-w")
    if word:
        args.append("--word-diff=color")
    text = Text.from_ansi(git(*args, a, b).stdout)
    text.rstrip()
    if not text.plain:
        return DiffView(Text("(no changes)", style="dim"), ())
    return DiffView(text, file_offsets(text.plain))


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
# diff viewer
# --------------------------------------------------------------------------


class Body(ScrollableContainer):
    can_focus = False


class Info(VerticalScroll):
    can_focus = False


class DiffScreen(Screen[None]):
    BINDINGS = [
        Binding("l,right,right_square_bracket", "next", "Next"),
        Binding("h,left,left_square_bracket", "prev", "Prev"),
        Binding("n", "file(1)", "File↓"),
        Binding("N", "file(-1)", "File↑"),
        Binding("f", "toggle_full", "Full"),
        Binding("i", "toggle_details", "Info"),
        Binding("s", "toggle_stat", "Stat"),
        Binding("w", "toggle_ws", "Ws"),
        Binding("d", "toggle_word", "Word"),
        Binding("plus,equals", "context(1)", "+Ctx"),
        Binding("minus", "context(-1)", "-Ctx"),
        Binding("r", "repick", "Re-pick"),
        Binding("j,down", "scroll('down')", show=False),
        Binding("k,up", "scroll('up')", show=False),
        Binding("space,pagedown", "scroll('page_down')", show=False),
        Binding("b,pageup", "scroll('page_up')", show=False),
        Binding("g,home", "scroll('home')", show=False),
        Binding("G,end", "scroll('end')", show=False),
        Binding("H", "scroll('left')", show=False),
        Binding("L", "scroll('right')", show=False),
    ]

    def __init__(self, repo: str, branch: str, chain: list[Commit]) -> None:
        super().__init__()
        self.repo = repo
        self.branch = branch
        self.chain = chain
        self.idx = 0  # diff is chain[idx] -> chain[idx + 1]
        self.full = False
        self.ignore_ws = False
        self.word = False
        self.context = 3
        self.show_stat = False
        self.show_details = False
        self.file_idx: int | None = None
        self.view = DiffView(Text(), ())
        self.header_plain = ""
        self.status_plain = ""

    @property
    def steps(self) -> int:
        return len(self.chain) - 1

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
        self.view = load_diff(a.sha, b.sha, self.ignore_ws, self.word, self.context)
        self.file_idx = None

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
        n = len(self.view.file_offsets)
        where = f"file {self.file_idx + 1}/{n}" if self.file_idx is not None else f"{n} files"
        self.status_plain = (
            f"whitespace: {'ignored' if self.ignore_ws else 'shown'}   "
            f"word-diff: {'on' if self.word else 'off'}   "
            f"context: {self.context}   {where}"
        )
        self.query_one("#status", Static).update(Text(self.status_plain, style="dim"))

    # -- navigation (never wraps) -------------------------------------------

    def step(self, delta: int) -> None:
        was_full, self.full = self.full, False
        new = self.idx + delta
        if not 0 <= new < self.steps:
            self.notify("last diff" if delta > 0 else "first diff")
            if was_full:
                self.render_view()
            return
        self.idx = new
        self.render_view()

    def action_next(self) -> None:
        self.step(1)

    def action_prev(self) -> None:
        self.step(-1)

    def action_file(self, delta: int) -> None:
        offsets = self.view.file_offsets
        if not offsets:
            return
        if self.file_idx is None:  # start from the file at the current scroll position
            cur = bisect_right(offsets, int(self.query_one(Body).scroll_y)) - 1
        else:
            cur = self.file_idx
        new = max(0, min(cur + delta, len(offsets) - 1))
        self.file_idx = new
        self.query_one(Body).scroll_to(y=offsets[new], animate=False)
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
    """
    BINDINGS = [Binding("q", "quit", "Quit")]

    def __init__(self, repo: str) -> None:
        super().__init__()
        self.repo = repo
        self.branch = ""
        self.commits: list[Commit] = []

    def on_mount(self) -> None:
        self.show_branches()

    def show_branches(self) -> None:
        self.push_screen(BranchPicker(list_branches()), self.on_branch)

    def on_branch(self, branch: str | None) -> None:
        if branch is None:
            self.exit()
            return
        commits = list_commits(branch)
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
        self.push_screen(DiffScreen(self.repo, self.branch, chain), lambda _: self.show_commits())


def main() -> None:
    try:
        root = repo_root()
    except RuntimeError as exc:
        print(f"pdiff: {exc}", file=sys.stderr)
        sys.exit(1)
    PDiffApp(root.name).run()


if __name__ == "__main__":
    main()
