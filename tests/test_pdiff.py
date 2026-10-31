"""Tests for pdiff."""

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from pdiff import app as pdiff


def run(repo: Path, *args: str) -> str:
    env = {
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
        "PATH": os.environ["PATH"], "HOME": str(repo),
    }  # fmt: skip
    return subprocess.run(
        ["git", *args], cwd=repo, env=env, check=True, capture_output=True, text=True
    ).stdout.strip()


def commit(repo: Path, name: str, body: str) -> str:
    (repo / name).write_text(body)
    run(repo, "add", name)
    run(repo, "commit", "-m", f"add {name}")
    return run(repo, "rev-parse", "HEAD")


@pytest.fixture
def linear(tmp_path, monkeypatch) -> tuple[Path, list[str]]:
    run(tmp_path, "init", "-b", "main")
    shas = [commit(tmp_path, f"{n}.txt", f"{n}\n") for n in "abcde"]
    monkeypatch.chdir(tmp_path)
    return tmp_path, shas


@pytest.fixture
def merged(tmp_path, monkeypatch) -> tuple[Path, list[str], str]:
    """main: A B C, side branch S off B, merged into main as M, then D."""
    run(tmp_path, "init", "-b", "main")
    a = commit(tmp_path, "a.txt", "a\n")
    b = commit(tmp_path, "b.txt", "b\n")
    run(tmp_path, "checkout", "-b", "side")
    side = commit(tmp_path, "s.txt", "s\n")
    run(tmp_path, "checkout", "main")
    c = commit(tmp_path, "c.txt", "c\n")
    run(tmp_path, "merge", "--no-ff", "-m", "merge side", "side")
    m = run(tmp_path, "rev-parse", "HEAD")
    d = commit(tmp_path, "d.txt", "d\n")
    monkeypatch.chdir(tmp_path)
    return tmp_path, [a, b, c, m, d], side


# -- git layer ---------------------------------------------------------------


def test_commit_chain_linear(linear):
    _, shas = linear
    assert [c.sha for c in pdiff.commit_chain(shas[0], shas[4])] == shas


def test_commit_chain_accepts_reversed_args(linear):
    _, shas = linear
    assert [c.sha for c in pdiff.commit_chain(shas[3], shas[1])] == shas[1:4]


def test_commit_chain_same_commit_rejected(linear):
    _, shas = linear
    with pytest.raises(ValueError):
        pdiff.commit_chain(shas[2], shas[2])


def test_commit_chain_unrelated_rejected(linear):
    repo, shas = linear
    run(repo, "checkout", "--orphan", "other")
    other = commit(repo, "x.txt", "x\n")
    with pytest.raises(ValueError):
        pdiff.commit_chain(shas[0], other)


def test_commit_chain_stays_on_first_parent(merged):
    _, shas, side = merged
    chain = [c.sha for c in pdiff.commit_chain(shas[0], shas[4])]
    assert chain == shas
    assert side not in chain


def test_list_commits_first_parent_only(merged):
    _, shas, side = merged
    assert [c.sha for c in pdiff.list_commits("main")] == shas[::-1]


def test_list_branches_current_first(linear):
    repo, _ = linear
    run(repo, "branch", "zzz")
    run(repo, "branch", "aaa")
    assert pdiff.list_branches()[0] == "main"


def test_file_offsets():
    text = "diff --git a/x b/x\n+1\ndiff --git a/y b/y\n+2"
    assert pdiff.file_offsets(text) == (0, 2)


def test_load_diff_shows_added_file(linear):
    _, shas = linear
    view = pdiff.load_diff(shas[0], shas[1], False, False, 3)
    assert "b.txt" in view.text.plain
    assert len(view.file_offsets) == 1


def test_load_diff_empty(linear):
    _, shas = linear
    view = pdiff.load_diff(shas[0], shas[0], False, False, 3)
    assert view.text.plain == "(no changes)"
    assert view.file_offsets == ()


def test_repo_root_outside_repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    with pytest.raises(RuntimeError):
        pdiff.repo_root()


# -- UI ----------------------------------------------------------------------


async def open_diff(pilot):
    await pilot.pause()
    await pilot.press("enter")  # branch: main
    await pilot.pause()
    await pilot.press("end", "enter")  # oldest commit as start
    await pilot.press("home", "enter")  # newest commit as end
    await pilot.pause()
    return pilot.app.screen


def test_walk_has_no_wraparound(linear):
    async def go():
        app = pdiff.PDiffApp("repo")
        async with app.run_test(size=(120, 40)) as pilot:
            screen = await open_diff(pilot)
            assert isinstance(screen, pdiff.DiffScreen)
            assert "step 1/4" in screen.header_plain

            await pilot.press("h")  # can't go back from the first diff
            assert screen.idx == 0

            for _ in range(6):
                await pilot.press("l")
            assert screen.idx == 3
            assert "step 4/4" in screen.header_plain

            await pilot.press("h")
            assert screen.idx == 2

    asyncio.run(go())


def test_full_toggle_and_flags(linear):
    async def go():
        app = pdiff.PDiffApp("repo")
        async with app.run_test(size=(120, 40)) as pilot:
            screen = await open_diff(pilot)
            await pilot.press("f")
            assert screen.full and "FULL" in screen.header_plain
            await pilot.press("f")
            assert not screen.full
            await pilot.press("w", "d", "plus")
            assert screen.ignore_ws and screen.word and screen.context == 4
            await pilot.press("minus", "minus", "minus", "minus", "minus", "minus")
            assert screen.context == 0
            await pilot.press("i", "s")
            assert screen.show_details and screen.show_stat

    asyncio.run(go())


def test_file_jump_in_full_view(linear):
    async def go():
        app = pdiff.PDiffApp("repo")
        async with app.run_test(size=(120, 40)) as pilot:
            screen = await open_diff(pilot)
            await pilot.press("f")
            assert len(screen.view.file_offsets) == 4
            await pilot.press("n", "n")  # starts in file 0, so n n lands on file 2
            assert screen.file_idx == 2
            await pilot.press("N", "N", "N")
            assert screen.file_idx == 0

    asyncio.run(go())


def test_quit_key_exits(linear):
    async def go():
        app = pdiff.PDiffApp("repo")
        async with app.run_test(size=(120, 40)) as pilot:
            await open_diff(pilot)
            await pilot.press("q")
            await pilot.pause()
        assert app.return_code in (0, None)

    asyncio.run(go())


def test_repick_returns_to_commit_picker(linear):
    async def go():
        app = pdiff.PDiffApp("repo")
        async with app.run_test(size=(120, 40)) as pilot:
            await open_diff(pilot)
            await pilot.press("r")
            await pilot.pause()
            assert isinstance(app.screen, pdiff.CommitPicker)

    asyncio.run(go())


def test_filter_typing_does_not_quit(linear):
    async def go():
        app = pdiff.PDiffApp("repo")
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("slash", "q")
            await pilot.pause()
            assert app.screen.query_text == "q"
            assert app.is_running

    asyncio.run(go())


# -- ranges and the command line ---------------------------------------------


@pytest.mark.parametrize(
    ("spec", "refs"),
    [("a..b", ("a", "b")), ("a..", ("a", "HEAD")), ("..b", ("HEAD", "b")), ("..", ("HEAD", "HEAD"))],
)
def test_parse_range(spec, refs):
    assert pdiff.parse_range(spec) == refs


@pytest.mark.parametrize("spec", ["main", "a...b"])
def test_parse_range_rejects(spec):
    with pytest.raises(ValueError):
        pdiff.parse_range(spec)


def test_range_chain_reads_like_git_diff(linear):
    _, shas = linear
    assert [c.sha for c in pdiff.range_chain("HEAD~3..HEAD~1")] == shas[1:4]
    assert [c.sha for c in pdiff.range_chain("HEAD~2..")] == shas[2:]


@pytest.mark.parametrize(
    ("spec", "msg"),
    [
        ("HEAD..HEAD~2", "did you mean HEAD~2..HEAD?"),
        ("nope..HEAD", "unknown revision 'nope'"),
        ("HEAD..main", "empty range"),
        ("--all..HEAD", "unknown revision '--all'"),
    ],
)
def test_range_chain_errors(linear, spec, msg):
    with pytest.raises(ValueError, match=msg.replace("?", r"\?")):
        pdiff.range_chain(spec)


def test_range_chain_through_merge_stays_first_parent(merged):
    _, shas, side = merged
    assert [c.sha for c in pdiff.range_chain("main~3..main")] == shas[1:]
    with pytest.raises(ValueError, match="first-parent"):
        pdiff.range_chain(f"{side}..main")


def test_build_app_options_and_dash_c(linear, tmp_path_factory, monkeypatch):
    repo, shas = linear
    monkeypatch.chdir(tmp_path_factory.mktemp("elsewhere"))
    app = pdiff.build_app(["-C", str(repo), "-w", "-U", "7", "--stat", "--max-lines", "0", "HEAD~2.."])
    assert app.repo == repo.name
    assert [c.sha for c in app.chain] == shas[2:]
    assert app.opts == pdiff.Options(ignore_ws=True, context=7, show_stat=True, max_lines=0)


def test_main_reports_errors(linear, tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        pdiff.main(["-C", str(tmp_path / "missing")])
    assert exc.value.code == 1
    assert capsys.readouterr().err.startswith("pdiff: -C ")

    with pytest.raises(SystemExit):
        pdiff.main(["HEAD..nope"])
    assert "unknown revision 'nope'" in capsys.readouterr().err


def test_build_app_empty_repo(tmp_path, monkeypatch):
    run(tmp_path, "init", "-b", "main")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(RuntimeError, match="no commits"):
        pdiff.build_app([])


def test_context_arg_bounds():
    with pytest.raises(SystemExit):
        pdiff.parse_args(["-U", "100"])
    with pytest.raises(SystemExit):
        pdiff.parse_args(["--max-lines", "-1"])


@pytest.mark.parametrize(
    ("remote", "url"),
    [
        ("https://github.com/o/r.git", "https://github.com/o/r"),
        ("https://user@gitlab.com/g/sub/r", "https://gitlab.com/g/sub/r"),
        ("git@github.com:o/r.git", "https://github.com/o/r"),
        ("ssh://git@host.example:2222/o/r.git", "https://host.example/o/r"),
        ("/srv/git/r.git", None),
        ("file:///srv/git/r.git", None),
    ],
)
def test_web_url(remote, url):
    assert pdiff.web_url(remote) == url


def test_commit_url(linear):
    repo, shas = linear
    assert pdiff.commit_url(shas[0]) is None
    run(repo, "remote", "add", "origin", "git@github.com:o/r.git")
    assert pdiff.commit_url(shas[0]) == f"https://github.com/o/r/commit/{shas[0]}"


# -- diff views --------------------------------------------------------------


def test_hunk_offsets():
    text = "diff --git a/x b/x\n@@ -1 +1 @@\n-a\n+b\n@@ -9 +9 @@\n-c"
    assert pdiff.hunk_offsets(text) == (1, 4)


def test_truncate():
    plain = "\n".join(f"line {i}" for i in range(10))
    view = pdiff.DiffView(pdiff.Text(plain), (0, 6), (1, 7), 10)
    assert pdiff.truncate(view, 0) is view
    assert pdiff.truncate(view, 10) is view
    cut = pdiff.truncate(view, 4)
    assert cut.text.plain.startswith("line 0\nline 1\nline 2\nline 3\n\n… 6 more lines")
    assert (cut.file_offsets, cut.hunk_offsets, cut.lines) == ((0,), (1,), 4)


@pytest.fixture
def hunky(tmp_path, monkeypatch) -> tuple[Path, list[str]]:
    """Two commits; the second changes two far-apart lines of a 40-line file."""
    run(tmp_path, "init", "-b", "main")
    lines = [f"{i}\n" for i in range(40)]
    first = commit(tmp_path, "x.txt", "".join(lines))
    lines[0], lines[-1] = "first\n", "last\n"
    second = commit(tmp_path, "x.txt", "".join(lines))
    monkeypatch.chdir(tmp_path)
    return tmp_path, [first, second]


# -- UI: new features ----------------------------------------------------------


def range_app(spec: str, **opts) -> pdiff.PDiffApp:
    return pdiff.PDiffApp("repo", pdiff.Options(**opts), pdiff.range_chain(spec), spec)


def test_range_mode_skips_pickers_and_applies_options(linear):
    async def go():
        app = range_app("HEAD~4..HEAD", word=True, context=5, full=True)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, pdiff.DiffScreen)
            assert screen.word and screen.context == 5 and screen.full
            assert "HEAD~4..HEAD" in screen.header_plain
            await pilot.press("r")  # no picked commits to return to: branch picker
            await pilot.pause()
            assert isinstance(app.screen, pdiff.BranchPicker)

    asyncio.run(go())


def test_goto_step_number_sha_and_miss(linear):
    _, shas = linear

    async def go():
        app = range_app("HEAD~4..HEAD")
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            screen = app.screen
            await pilot.press("colon", "3", "enter")
            await pilot.pause()
            assert screen.idx == 2
            await pilot.press("colon", *shas[1][:8], "enter")  # introduced by step 1
            await pilot.pause()
            assert screen.idx == 0
            await pilot.press("colon", "9", "enter")  # out of range, not a ref
            await pilot.pause()
            assert screen.idx == 0 and app.screen is screen
            await pilot.press("colon", "escape")
            await pilot.pause()
            assert app.screen is screen

    asyncio.run(go())


def test_hunk_navigation(hunky):
    async def go():
        app = range_app("HEAD~1..HEAD", context=1)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            screen = app.screen
            assert len(screen.view.hunk_offsets) == 2
            await pilot.press("right_curly_bracket")
            assert screen.hunk_idx == 0 and screen.file_idx == 0
            await pilot.press("right_curly_bracket", "right_curly_bracket")
            assert screen.hunk_idx == 1  # clamped, no wrap
            await pilot.press("left_curly_bracket")
            assert screen.hunk_idx == 0
            assert "hunk 1/2" in screen.status_plain

    asyncio.run(go())


def test_truncation_and_show_all(hunky):
    async def go():
        app = range_app("HEAD~1..HEAD", context=99, max_lines=10)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            screen = app.screen
            assert screen.truncated and screen.view.lines == 10
            assert "showing 10/" in screen.status_plain
            assert len(screen.view.hunk_offsets) == 1
            await pilot.press("a")
            assert not screen.truncated and screen.show_all
            await pilot.press("a")
            assert screen.truncated

    asyncio.run(go())


def test_yank_sha_patch_and_link(hunky):
    repo, shas = hunky

    async def go():
        app = range_app("HEAD~1..HEAD")
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("y")
            assert app.clipboard == shas[1]
            await pilot.press("Y")
            assert app.clipboard.startswith("diff --git a/x.txt b/x.txt")
            assert "\x1b" not in app.clipboard
            clip = app.clipboard
            await pilot.press("p")  # no remote: warns, clipboard untouched
            assert app.clipboard == clip
            run(repo, "remote", "add", "origin", "https://github.com/o/r.git")
            await pilot.press("p")
            assert app.clipboard == f"https://github.com/o/r/commit/{shas[1]}"

    asyncio.run(go())


def test_help_overlay_lists_bindings(linear):
    async def go():
        app = range_app("HEAD~4..HEAD")
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("question_mark")
            await pilot.pause()
            assert isinstance(app.screen, pdiff.HelpScreen)
            text = str(app.screen.query_one("#help-text", pdiff.Static).render())
            for label in ("Next step", "Next hunk", "Go to step N, SHA or ref", "Copy SHA", "Quit"):
                assert label in text
            await pilot.press("question_mark")
            await pilot.pause()
            assert isinstance(app.screen, pdiff.DiffScreen)

    asyncio.run(go())
