"""Replaying a past incident must not see the future, and must date commits by when
they landed rather than when they were written."""

from __future__ import annotations

import os
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from incidentpilot.gitctx import GitError, GitRepo, rank_candidates
from incidentpilot.models import Alert

T0 = datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc)
ONSET = T0 + timedelta(hours=100, minutes=30)


def _git(repo, *args, when=None):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
           "GIT_CONFIG_NOSYSTEM": "1"}
    if when is not None:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = when.isoformat()
    out = subprocess.run(["git", "-C", str(repo), "-c", "commit.gpgsign=false",
                          "-c", "core.hooksPath=", *args],
                         capture_output=True, text=True, env=env, check=True)
    return out.stdout.strip()


def _commit(repo, path, text, message, when):
    (repo / path).write_text(text, encoding="utf-8")
    _git(repo, "add", path)
    _git(repo, "commit", "-q", "-m", message, when=when)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    """main:   c1 (T0) -- c2 (T0+2h) -- M (T0+100h, merges the PR) -- revert (T0+102h)
       PR:     pr (written T0+1h, four days before it merged)
    The alert fires at T0+100h30m."""
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _commit(r, "config.py", "POOL = 50\n", "initial", T0)
    _git(r, "checkout", "-q", "-b", "pr")
    pr = _commit(r, "config.py", "POOL = 5\n", "perf: right-size the pool", T0 + timedelta(hours=1))
    _git(r, "checkout", "-q", "main")
    _commit(r, "README", "docs\n", "docs: readme", T0 + timedelta(hours=2))
    _git(r, "merge", "-q", "--no-ff", "-m", "Merge pull request #7", "pr",
         when=T0 + timedelta(hours=100))
    revert = _commit(r, "config.py", "POOL = 50\n",
                     f'Revert "perf: right-size the pool"\n\nThis reverts commit {pr}.',
                     T0 + timedelta(hours=102))
    return {"path": r, "pr": pr, "revert": revert}


def _alert():
    return Alert(fingerprint="t", source="generic", service="svc", severity="sev1",
                 title="pool exhausted", started_at=ONSET)


def test_the_pinned_view_cannot_see_the_revert(repo):
    past = GitRepo(repo["path"]).at(ONSET + timedelta(minutes=30))
    history = past.file_history("config.py")
    assert "Revert" not in history
    assert "right-size the pool" in history


def test_the_pinned_view_refuses_to_show_a_future_commit(repo):
    past = GitRepo(repo["path"]).at(ONSET + timedelta(minutes=30))
    with pytest.raises(GitError, match="does not exist"):
        past.show(repo["revert"])
    with pytest.raises(GitError, match="does not exist"):
        past.file_at(repo["revert"], "config.py")
    assert "POOL = 5" in past.show(repo["pr"])  # the past is still readable


def test_unpinned_head_does_see_it(repo):
    """The control: without pinning, the answer is sitting right there."""
    assert "Revert" in GitRepo(repo["path"]).file_history("config.py")


def test_a_merged_pr_is_dated_by_its_merge_not_its_authoring(repo):
    """The PR commit was written 99 hours before the alert, outside a 72h window by its
    own date. It merged 30 minutes before the alert, which is what matters."""
    past = GitRepo(repo["path"]).at(ONSET + timedelta(minutes=30))
    commits = past.commits_in_window(_alert(), lookback_hours=72)
    by_sha = {c.sha: c for c in commits}
    assert repo["pr"] in by_sha, "a change merged just before the alert must be a candidate"
    landed = by_sha[repo["pr"]]
    assert landed.landed_at == T0 + timedelta(hours=100)
    assert landed.authored_at == T0 + timedelta(hours=1)
    assert all(c.sha != repo["revert"] for c in commits)


def test_recency_scores_the_merge_time(repo):
    past = GitRepo(repo["path"]).at(ONSET + timedelta(minutes=30))
    ranked = rank_candidates(_alert(), past.commits_in_window(_alert(), 72), {}, ["pool"])
    pr = next(c for c in ranked if c.sha == repo["pr"])
    assert pr.signals["recency"] > 0.9, pr.rationale
    assert "30m before onset" in " ".join(pr.rationale)


def test_before_the_repo_existed_is_an_error_not_a_silent_head(repo):
    with pytest.raises(GitError):
        GitRepo(repo["path"]).at(T0 - timedelta(days=1))
