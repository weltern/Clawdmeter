"""A dev build can't pass itself off as the release.

Found on 2026-11-22: a local build from before a 3.1.0 fix said "3.1.0" in the
About box and in the exe's ProductVersion, and finding out took unpacking it.

Run with `python -m pytest tests/ -q`.
"""

from __future__ import annotations

import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest  # noqa: E402

import build_info  # noqa: E402
from build_info import BuildInfo, version_label  # noqa: E402


def test_labels():
    assert version_label("3.1.0", BuildInfo("3ec0425", False, True)) == "3.1.0 (3ec0425)"
    assert version_label("3.1.0", BuildInfo("1d4a1cc", False, False)) == "3.1.0+dev.1d4a1cc"
    assert version_label("3.1.0", BuildInfo("1d4a1cc", True, False)) == "3.1.0+dev.1d4a1cc.dirty"
    # Unknown is never shown as a bare version: that is the claim at issue.
    assert version_label("3.1.0", BuildInfo(None, False, False)) == "3.1.0+dev"
    # A "release" flag with local changes is not a release.
    assert version_label("3.1.0", BuildInfo("3ec0425", True, True)).startswith("3.1.0+dev")


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


@pytest.fixture
def repo(tmp_path):
    try:
        _git(tmp_path, "init", "-q")
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("git not available")
    (tmp_path / "f.txt").write_text("a")
    _git(tmp_path, "add", "f.txt")
    _git(tmp_path, "commit", "-qm", "c")
    return tmp_path


def test_untagged_commit_is_dev(repo):
    info = build_info.from_git("3.1.0", repo, env={})
    assert info.commit and not info.release and not info.dirty


def test_release_tag_is_release_until_edited(repo):
    _git(repo, "tag", "v3.1.0")
    assert build_info.from_git("3.1.0", repo, env={}).release
    (repo / "f.txt").write_text("b")
    info = build_info.from_git("3.1.0", repo, env={})
    assert info.dirty and not info.release


def test_a_different_tag_is_not_this_release(repo):
    _git(repo, "tag", "v3.0.1")
    assert not build_info.from_git("3.1.0", repo, env={}).release


def test_untracked_files_do_not_mark_dirty(repo):
    (repo / "scratch.log").write_text("x")
    assert not build_info.from_git("3.1.0", repo, env={}).dirty


def test_ci_tag_ref_counts_without_tag_objects(repo):
    env = {"GITHUB_REF_TYPE": "tag", "GITHUB_REF_NAME": "v3.1.0"}
    assert build_info.from_git("3.1.0", repo, env=env).release


def test_no_git_repo_is_unknown(tmp_path):
    assert build_info.from_git("3.1.0", tmp_path, env={}) == BuildInfo(None, False, False)


def test_stamp_round_trip(repo, tmp_path):
    out = tmp_path / "stamp.py"
    info = build_info.write_stamp("3.1.0", out, cwd=repo)
    ns = {}
    exec(out.read_text(), ns)
    assert (ns["COMMIT"], ns["DIRTY"], ns["RELEASE"]) == (info.commit, info.dirty, info.release)
