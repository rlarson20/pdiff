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
