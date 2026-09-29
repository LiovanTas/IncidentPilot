"""Git correlation: pull commits that landed inside the blast window and rank them
against the alert.

The ranker is deliberately explainable -- every point a commit scores comes with a
rationale string, which is what the agent reads. This is the offline baseline; the
Claude agent arbitrates on top of it.
"""

from __future__ import annotations

import math
import os
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from .models import Alert, CommitCandidate, parse_ts

REC = "\x1e"
FIELD = "\x1f"
_LOG_FORMAT = f"{REC}%H{FIELD}%an{FIELD}%aI{FIELD}%s{FIELD}%b{FIELD}"

# Commit-message / path patterns that historically precede incidents.
RISK_PATTERNS: dict[str, tuple[str, float]] = {
    "migration":   (r"\b(migrat|schema|alter table|drop column|backfill|ddl)\w*", 0.9),
    "config":      (r"\b(config|flag|feature[_-]?flag|toggle|values\.ya?ml)\w*", 0.7),
    "dependency":  (r"\b(bump|upgrade|downgrade|dependenc|lockfile|package\.json|requirements)\w*", 0.65),
    "concurrency": (r"\b(pool|thread|worker|concurren|lock|mutex|race)\w*", 0.7),
    "timeouts":    (r"\b(timeout|deadline|retry|retries|backoff|circuit)\w*", 0.75),
    "caching":     (r"\b(cache|ttl|evict|invalidat|redis|memcach)\w*", 0.6),
    "auth":        (r"\b(auth|token|jwt|oauth|session|permission|scope)\w*", 0.6),
    "query":       (r"\b(index|query|join|orm|pagination)\w*", 0.65),
    "release":     (r"\b(release|deploy|rollout|canary|cutover)\w*", 0.5),
    "revert":      (r"^revert\b", 0.4),
}

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Alerts lag the change that caused them, so the window reaches this far past onset.
LEAD_MINUTES = 30.0

# A merge of a long-lived branch can bring in hundreds of commits; beyond this many from
# one merge, the rest are dropped rather than flooding the candidate list.
MAX_MERGE_EXPANSION = 50


class GitError(RuntimeError):
    pass


class GitRepo:
    """Thin, read-only wrapper over the git CLI, viewed from one point in history.

    `ref` is the newest commit this view can see. In production it is HEAD: when an alert
    has just fired, nothing newer exists. When replaying a past incident it must be the
    commit that was current when the alert fired (see `at`), because the repository's later
    history contains the answer -- a revert or a fix that names the guilty commit. Every
    query walks backwards from `ref`, and a commit that is not an ancestor of it is refused.
    """

    def __init__(self, path: Path | str, ref: str = "HEAD", timeout: int = 120):
        self.path = Path(path)
        self.ref = ref
        # Seconds per git call. Generous enough for any local query; a first read of a large
        # window from a partial clone downloads file contents and needs far longer.
        self.timeout = timeout
        if not (self.path / ".git").exists():
            raise GitError(f"not a git repository: {self.path}")

    def _run(self, *args: str, stdin: str | None = None) -> str:
        proc = subprocess.run(
            ["git", "-C", str(self.path), *args], input=stdin, capture_output=True, text=True,
            timeout=self.timeout, encoding="utf-8", errors="replace",
        )
        if proc.returncode != 0:
            raise GitError(f"git {' '.join(args[:4])} failed: {proc.stderr.strip()}")
        return proc.stdout

    # ------------------------------------------------------------------ the past

    def at(self, when: datetime) -> "GitRepo":
        """The repository as it stood at `when`: the newest commit on the main line that had
        landed by then. Nothing after it is visible through the returned view."""
        sha = self._run("rev-list", "-1", "--first-parent", f"--before={when.isoformat()}",
                        self.ref).strip()
        if not sha:
            raise GitError(f"no commits on the main line before {when.isoformat()}")
        return GitRepo(self.path, ref=sha, timeout=self.timeout)

    def _require_visible(self, sha: str) -> None:
        if self.ref == "HEAD":
            return
        # Commits are always local, even in a blob:none partial clone. A SHA that is not
        # local does not exist as far as this incident is concerned -- and letting git go
        # and look for it on the remote can pull hundreds of thousands of objects.
        proc = subprocess.run(
            ["git", "-C", str(self.path), "merge-base", "--is-ancestor", sha, self.ref],
            capture_output=True, text=True, env={**os.environ, "GIT_NO_LAZY_FETCH": "1"},
        )
        if proc.returncode != 0:
            raise GitError(f"commit {sha} does not exist in the repository as of this incident")

    # ------------------------------------------------------------------ queries

    def commits_in_window(self, alert: Alert, lookback_hours: float,
                          lead_minutes: float = LEAD_MINUTES) -> list[CommitCandidate]:
        """Every commit that *landed on the main line* between (onset - lookback) and
        (onset + lead), stamped with when it landed.

        Landing time, not author time, is what an incident correlates with. On GitHub a
        commit can be written days before it merges, and in a merge-commit workflow even its
        committer date is the branch date -- so a change merged two hours before the alert
        could be dated five days earlier and fall outside the window entirely. Walking the
        first-parent line finds what actually shipped; a merge's commits are dated by the
        merge.
        """
        landed = self.landed_in_window(alert, lookback_hours, lead_minutes)
        if not landed:
            return []

        raw = self._run("log", "--no-walk=unsorted", "--stdin",
                        f"--pretty=format:{_LOG_FORMAT}", "--numstat", stdin="\n".join(landed))
        commits = _parse_log(raw)
        for commit in commits:
            commit.landed_at = parse_ts(landed[commit.sha])
        return commits

    def landed_in_window(self, alert: Alert, lookback_hours: float,
                         lead_minutes: float = LEAD_MINUTES) -> dict[str, str]:
        """SHA -> ISO landing time for everything that shipped in the window. Metadata only,
        so it never downloads file contents from a partial clone."""
        since = alert.started_at - timedelta(hours=lookback_hours)
        until = alert.started_at + timedelta(minutes=lead_minutes)
        mainline = self._run(
            "log", self.ref, "--first-parent", f"--since={since.isoformat()}",
            f"--until={until.isoformat()}", f"--format=%H{FIELD}%P{FIELD}%cI",
        )
        landed: dict[str, str] = {}
        for line in mainline.splitlines():
            if not line.strip():
                continue
            sha, parents, landed_at = line.split(FIELD)
            parent_list = parents.split()
            if len(parent_list) <= 1:
                landed.setdefault(sha, landed_at)
                continue
            branch = self._run("log", "--no-merges", f"--max-count={MAX_MERGE_EXPANSION}",
                               "--format=%H", f"{parent_list[0]}..{sha}")
            for branch_sha in branch.split():
                landed.setdefault(branch_sha, landed_at)
        return landed

    def show(self, sha: str, max_chars: int = 6000) -> str:
        """Unified diff for a commit, truncated so it cannot blow up the context window."""
        self._require_visible(sha)
        out = self._run("show", "--stat", "--patch", "--no-color", sha)
        if len(out) > max_chars:
            out = out[:max_chars] + f"\n... [diff truncated at {max_chars} chars]"
        return out

    def file_at(self, sha: str, path: str, max_chars: int = 8000) -> str:
        """A whole file as it stood at `sha` -- the context a diff hunk leaves out."""
        self._require_visible(sha)
        out = self._run("show", f"{sha}:{path}")
        if len(out) > max_chars:
            out = out[:max_chars] + f"\n... [file truncated at {max_chars} chars]"
        return out

    def file_history(self, path: str, limit: int = 10) -> str:
        return self._run("log", self.ref, f"-{limit}", "--pretty=format:%h %aI %an %s", "--", path)

    def blame_summary(self, path: str, limit: int = 40) -> str:
        try:
            out = self._run("blame", "--line-porcelain", "-L", f"1,{limit}", self.ref, "--", path)
        except GitError as exc:
            return f"blame unavailable: {exc}"
        authors: dict[str, int] = {}
        for line in out.splitlines():
            if line.startswith("author "):
                authors[line[7:]] = authors.get(line[7:], 0) + 1
        ranked = sorted(authors.items(), key=lambda kv: -kv[1])
        return "\n".join(f"{name}: {count} lines" for name, count in ranked)


def _parse_log(raw: str) -> list[CommitCandidate]:
    commits: list[CommitCandidate] = []
    for record in raw.split(REC):
        if not record.strip():
            continue
        header, _, numstat = record.partition("\n")
        parts = header.split(FIELD)
        if len(parts) < 4:
            continue
        sha, author, authored, subject = parts[0], parts[1], parts[2], parts[3]
        body = parts[4] if len(parts) > 4 else ""
        files, insertions, deletions = [], 0, 0
        for line in numstat.splitlines():
            cols = line.split("\t")
            if len(cols) != 3:
                continue
            add, rem, path = cols
            files.append(path.strip())
            insertions += int(add) if add.isdigit() else 0
            deletions += int(rem) if rem.isdigit() else 0
        commits.append(CommitCandidate(
            sha=sha.strip(), author=author.strip(), authored_at=parse_ts(authored.strip()),
            subject=subject.strip(), body=body.strip(), files=files,
            insertions=insertions, deletions=deletions,
        ))
    return commits


# --------------------------------------------------------------------------- scoring


def services_for_files(files: list[str], topology: dict) -> list[str]:
    """Map changed paths onto owning services via topology path prefixes."""
    owners: dict[str, None] = {}
    for name, spec in topology.get("services", {}).items():
        for prefix in spec.get("paths", []):
            if any(f.startswith(prefix) for f in files):
                owners[name] = None
                break
    return list(owners)


def _recency_signal(commit: CommitCandidate, alert: Alert) -> tuple[float, str]:
    delta_h = (alert.started_at - commit.shipped_at).total_seconds() / 3600.0
    if delta_h < -0.5:
        return 0.0, "landed after the alert fired"
    if delta_h < 0:
        delta_h = 0.0
    # Exponential decay: a change that landed just before onset is the prime suspect.
    value = math.exp(-delta_h / 8.0)
    if delta_h <= 2:
        note = f"landed {delta_h * 60:.0f}m before onset"
    else:
        note = f"landed {delta_h:.1f}h before onset"
    return value, note


def _ownership_signal(commit: CommitCandidate, alert: Alert, topology: dict) -> tuple[float, str]:
    spec = topology.get("services", {}).get(alert.service, {})
    own_paths = spec.get("paths", [])
    deps = set(spec.get("depends_on", []))
    shared = set(topology.get("shared_paths", []))

    if not commit.files:
        return 0.0, ""
    direct = sum(1 for f in commit.files if any(f.startswith(p) for p in own_paths))
    dep_paths = [p for d in deps for p in topology.get("services", {}).get(d, {}).get("paths", [])]
    indirect = sum(1 for f in commit.files if any(f.startswith(p) for p in dep_paths))
    shared_hits = sum(1 for f in commit.files if any(f.startswith(p) for p in shared))
    n = len(commit.files)

    value = min(1.0, (direct / n) + 0.55 * (indirect / n) + 0.45 * (shared_hits / n))
    notes = []
    if direct:
        notes.append(f"touches {direct}/{n} files owned by {alert.service}")
    if indirect:
        notes.append(f"touches {indirect} files in upstream deps ({', '.join(sorted(deps))})")
    if shared_hits:
        notes.append(f"touches {shared_hits} shared-library files")
    return value, "; ".join(notes)


def _keyword_signal(commit: CommitCandidate, keywords: list[str]) -> tuple[float, str]:
    if not keywords:
        return 0.0, ""
    blob = " ".join([commit.subject, commit.body, " ".join(commit.files)]).lower()
    tokens = set(_TOKEN_RE.findall(blob))
    hits = [k for k in keywords if k in tokens or k in blob]
    if not hits:
        return 0.0, ""
    value = min(1.0, len(hits) / max(3.0, len(keywords) * 0.5))
    return value, "alert terms present in commit: " + ", ".join(sorted(hits)[:6])


def _risk_signal(commit: CommitCandidate) -> tuple[float, str, list[str]]:
    blob = " ".join([commit.subject, commit.body, " ".join(commit.files)]).lower()
    matched: list[tuple[str, float]] = []
    for label, (pattern, weight) in RISK_PATTERNS.items():
        if re.search(pattern, blob):
            matched.append((label, weight))
    base = max((w for _, w in matched), default=0.0)
    # Big diffs are riskier, with sharply diminishing returns.
    churn_bonus = min(0.35, math.log10(max(commit.churn, 1)) / 10.0)
    value = min(1.0, base + churn_bonus)
    labels = [label for label, _ in sorted(matched, key=lambda kv: -kv[1])]
    note = ""
    if labels:
        note = "risk class: " + ", ".join(labels)
    if commit.churn > 300:
        note = (note + "; " if note else "") + f"large diff ({commit.churn} lines)"
    return value, note, labels


WEIGHTS = {"recency": 0.30, "ownership": 0.34, "keyword": 0.21, "risk": 0.15}


def rank_candidates(alert: Alert, commits: list[CommitCandidate], topology: dict,
                    keywords: list[str]) -> list[CommitCandidate]:
    """Score and sort commits in place, then return them."""
    for commit in commits:
        recency, r_note = _recency_signal(commit, alert)
        ownership, o_note = _ownership_signal(commit, alert, topology)
        keyword, k_note = _keyword_signal(commit, keywords)
        risk, x_note, _labels = _risk_signal(commit)

        commit.services = services_for_files(commit.files, topology)
        commit.signals = {
            "recency": round(recency, 4),
            "ownership": round(ownership, 4),
            "keyword": round(keyword, 4),
            "risk": round(risk, 4),
        }
        commit.rationale = [n for n in (r_note, o_note, k_note, x_note) if n]
        commit.score = round(
            sum(WEIGHTS[name] * value for name, value in commit.signals.items()), 4
        )
        # A commit that cannot have caused the alert scores zero regardless of anything else.
        if recency == 0.0:
            commit.score = 0.0

    commits.sort(key=lambda c: (-c.score, -c.shipped_at.timestamp()))
    return commits


def collect_candidates(repo: GitRepo, alert: Alert, topology: dict, keywords: list[str],
                       lookback_hours: float, limit: int) -> list[CommitCandidate]:
    commits = repo.commits_in_window(alert, lookback_hours)
    ranked = rank_candidates(alert, commits, topology, keywords)
    return [c for c in ranked if c.score > 0][:limit]
