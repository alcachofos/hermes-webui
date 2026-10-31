"""#8684: on the stable channel, a checkout whose tags are ALL prereleases keeps v0.52.544's behaviour.

#8147 (exp-v0.52.596) leaves prerelease tags (``v0.22.5+canary.N``, ``-rc``) out of the stable release list, its
``describe`` and its ahead-count, so a canary never outranks the release it precedes. In a checkout with no stable
release at all, that left an empty list: the check fell through to the branch comparison and the apply selected the
untagged branch tip (``origin/main``). Without a stable release, the stable channel now keeps v0.52.544's unfiltered
list, so the installed canary stays current and a newer published canary is still offered. With any stable release
present, #8147's filtering is unchanged.
"""

from __future__ import annotations

import os
import subprocess

import pytest

import api.updates as updates

CANARY_1 = "v0.22.5+canary.1"
CANARY_2 = "v0.22.5+canary.2"


def _git(repo, *args, date="2026-10-10T12:00:00Z"):
    env = dict(os.environ, GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
    return subprocess.run(["git", *args], cwd=str(repo), env=env, check=True, capture_output=True, text=True).stdout


def _repo(tmp_path, name, commits, *, untagged_tip=True):
    """``commits`` is a list of tag lists, one commit each (oldest first); an origin carries every commit."""
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t.co")
    _git(repo, "config", "user.name", "Test")
    for i, tags in enumerate(commits):
        date = f"2026-10-{10 + i:02d}T12:00:00Z"
        _git(repo, "commit", "-q", "--allow-empty", "-m", f"c{i}", date=date)
        for tag in tags:
            _git(repo, "tag", "-a", "-m", tag, tag, date=date)
    if untagged_tip:
        _git(repo, "commit", "-q", "--allow-empty", "-m", "untagged development work", date="2026-10-29T12:00:00Z")
    origin = tmp_path / f"{name}-origin.git"
    _git(tmp_path, "clone", "-q", "--bare", str(repo), str(origin))
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "fetch", "-q", "--tags", "origin")
    return repo


@pytest.mark.parametrize("target", ["webui", "agent"])
def test_only_canary_tags_at_the_newest_canary_is_up_to_date_not_the_branch_tip(tmp_path, target):
    repo = _repo(tmp_path, f"only-canary-{target}", [[CANARY_1], [CANARY_2]])
    _git(repo, "checkout", "-q", CANARY_2)
    info = updates._check_repo(repo, target, "stable")
    assert info["behind"] == 0
    assert updates._select_apply_compare_ref(repo, "stable", target) == CANARY_2


@pytest.mark.parametrize("target", ["webui", "agent"])
def test_only_canary_tags_still_offer_a_newer_published_canary(tmp_path, target):
    repo = _repo(tmp_path, f"canary-ahead-{target}", [[CANARY_1], [CANARY_2]])
    _git(repo, "checkout", "-q", CANARY_1)
    info = updates._check_repo(repo, target, "stable")
    assert info["behind"] == 1
    assert updates._select_apply_compare_ref(repo, "stable", target) == CANARY_2


def test_only_canary_tags_webui_past_its_tag_never_advances_to_the_branch(tmp_path):
    repo = _repo(tmp_path, "canary-head-past", [[CANARY_1]])
    _git(repo, "checkout", "-q", "main")  # past the canary, on the untagged tip
    assert updates._select_apply_compare_ref(repo, "stable", "webui") is None


def test_only_canary_tags_agent_past_its_tag_keeps_its_historical_branch_fallthrough(tmp_path):
    """The Agent tracks its branch past its tags (v0.52.544 does the same here)."""
    repo = _repo(tmp_path, "agent-head-past", [[CANARY_1]])
    _git(repo, "checkout", "-q", "main")
    assert updates._select_apply_compare_ref(repo, "stable", "agent") == "origin/main"


@pytest.mark.parametrize("target", ["webui", "agent"])
def test_a_stable_release_keeps_the_prerelease_filter(tmp_path, target):
    """#8147 unchanged: with a stable release present, a newer canary is not a stable update."""
    repo = _repo(tmp_path, f"stable-plus-canary-{target}", [["v0.22.4"], [CANARY_1]], untagged_tip=False)
    _git(repo, "checkout", "-q", "v0.22.4")
    assert updates._release_tags(repo, "stable") == ["v0.22.4"]
    info = updates._check_repo(repo, target, "stable")
    assert info["behind"] == 0
    assert updates._select_apply_compare_ref(repo, "stable", target) == "v0.22.4"


def test_only_canary_tags_use_the_unfiltered_version_order(tmp_path):
    repo = _repo(tmp_path, "canary-order", [[CANARY_1], [CANARY_2]])
    _git(repo, "checkout", "-q", CANARY_2)
    assert updates._release_tags(repo, "stable") == [CANARY_2, CANARY_1]
    assert updates._current_release_tag(repo, "stable") == CANARY_2
    assert updates._count_channel_tags_ahead(repo, "stable") == 1  # HEAD's own tag, as on v0.52.544


def test_repo_has_stable_release(tmp_path):
    only_canary = _repo(tmp_path, "has-release-no", [[CANARY_1]])
    with_release = _repo(tmp_path, "has-release-yes", [["v0.22.4"], [CANARY_1]])
    assert updates._repo_has_stable_release(only_canary) is False
    assert updates._repo_has_stable_release(with_release) is True
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()
    assert updates._repo_has_stable_release(not_a_repo) is True  # a git failure keeps the #8147 filter


def test_experimental_channel_is_unchanged(tmp_path):
    repo = _repo(tmp_path, "exp-channel", [["exp-v0.52.1"], ["exp-v0.52.2"]])
    _git(repo, "checkout", "-q", "exp-v0.52.1")
    assert updates._release_tags(repo, "experimental") == ["exp-v0.52.2", "exp-v0.52.1"]
    assert updates._channel_describe_filter("experimental", repo) == ["--match", "exp-v*"]
