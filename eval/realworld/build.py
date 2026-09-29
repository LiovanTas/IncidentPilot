"""Build the real-world benchmark from revert commits in public repositories.

When someone commits "Revert X" with "This reverts commit <sha>", X is a change a
developer judged bad enough to undo -- ground truth labelled by the people who knew. This
turns such pairs into replayable incidents:

  alert     the revert's stated reason, with anything that names the answer removed
  onset     one hour before the revert landed
  repo      pinned to onset, so the revert (and anything else later) does not exist
  answer    X

Every selection rule is fixed in this file and applied newest-first, and every candidate
the rules drop is counted in FUNNEL.md -- so the benchmark can be audited for
cherry-picking rather than trusted.

What this measures, honestly: regression attribution in real codebases. Many reverts
undo changes that broke CI or a feature, not a production outage, so the alerts read
more like bug reports than pager messages. It is a harder and more honest test of
"which change did this?" than the synthetic suite, not a replay of production incidents.

    py -3.13 eval/realworld/build.py              # clone if needed, mine, warm, write
    py -3.13 eval/realworld/build.py --per-repo 5
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "src"))

from incidentpilot.gitctx import LEAD_MINUTES, GitError, GitRepo  # noqa: E402
from incidentpilot.models import Alert, parse_ts  # noqa: E402

REPOS_DIR = HERE / "repos"
OUT = HERE / "incidents.json"
FUNNEL = HERE / "FUNNEL.md"

REPOS = {
    "prometheus": "https://github.com/prometheus/prometheus.git",
    "moby": "https://github.com/moby/moby.git",
    "grafana": "https://github.com/grafana/grafana.git",
    # Gerrit's revert template records a "Reason for revert:" -- the most reliably
    # human-written explanation of the lot.
    "go": "https://github.com/golang/go.git",
    "etcd": "https://github.com/etcd-io/etcd.git",
    "containerd": "https://github.com/containerd/containerd.git",
    "consul": "https://github.com/hashicorp/consul.git",
}

# ---- selection rules. Change them and the benchmark is a different benchmark. ----------
PER_REPO = 10
LOOKBACK_HOURS = 72
ONSET_BEFORE_REVERT = timedelta(hours=1)
# Reverted within minutes is almost always a CI break caught before merge settled -- not
# an incident. Reverted after a week is a change of heart, not a regression.
MIN_TIME_TO_REVERT = timedelta(minutes=90)
MAX_TIME_TO_REVERT = timedelta(days=7)
MIN_REASON_CHARS = 40
MIN_REASON_WORDS = 6
# With a handful of commits in the window the answer is nearly the only suspect. Real
# incidents in these repos land among dozens, so fewer than this is not a test of anything.
MIN_WINDOW_COMMITS = 10
MAX_DESCRIPTION_CHARS = 1500
# -----------------------------------------------------------------------------------------

REVERTS_RE = re.compile(r"This reverts commit ([0-9a-f]{7,40})")
BOILERPLATE = re.compile(
    r"^(revert\b|reverts\b|this reverts (commit|cl)|signed-off-by:|co-authored-by:|reviewed-by:|"
    r"change-id:|reviewed-on:|run-trybot:|trybot-result:|luci-trybot-result:|auto-submit:|"
    r"commit-queue:|bug:|\(cherry picked|cherry-pick|backport|(fixes|closes|updates|for):?\s*"
    r"([\w.-]+/[\w.-]+)?#|merge (pull request|branch)|release notes?:)", re.I,
)
# Gerrit puts the one line we most want behind a label containing the word "revert".
REASON_LABEL = re.compile(r"^reason for revert:\s*", re.I)
# Any line that talks about the revert itself describes the *change*, not the symptom. In
# squash-merged repos it is usually the revert PR's own title -- e.g.
# 'Alerting: Revert adding typesVersions to /alerting' -- which is the answer, restated.
REVERT_WORD = re.compile(r"\brevert(s|ed|ing)?\b", re.I)
# Sign-offs in any form, including moby's old 'Docker-DCO-1.1-Signed-off-by: ... (github: x)'.
SIGNOFF = re.compile(r"signed-off-by|docker-dco|\(github:\s*[\w-]+\)", re.I)
BULLET = re.compile(r"^[\s*\-\u2022>]+")
# An alert describes a failure. A revert whose explanation contains none of this language is
# not describing a symptom -- in squash-merged repos (grafana) the "reason" is often just the
# list of commits in the revert PR, which can be the fix, the answer reworded, or unrelated
# work. Found by reading the first build's alerts against their answers.
SYMPTOM = re.compile(
    r"\b(break|breaks|broke|broken|breaking|fail|fails|failed|failing|failure|error|errors|"
    r"panic|panics|panicking|crash|crashes|crashed|crashing|regress\w*|bug|bugs|buggy|issue|"
    r"issues|problem\w*|doesn'?t|does not|don'?t|cannot|can'?t|won'?t|unable|wrong|incorrect|"
    r"slow\w*|timeouts?|timed out|leak\w*|hang|hangs|hung|stuck|flak\w*|refuse\w*|missing|"
    r"invalid|mismatch\w*|conflict\w*|unusable|degrad\w*|nil pointer|segfault|oom)\b",
    re.I,
)
# A line shaped like a commit subject -- 'feat(x): ...', 'Alerting: Add ...', or an
# imperative like 'fix wording of ...' -- is somebody's change, not somebody's symptom, even
# when it contains a word like "error". Dropped unconditionally: an earlier version kept such
# lines if they mentioned a failure, and 'chore(renovate): ignore swc/core ... due to bugs'
# went through as an alert whose answer was the swc/core bump itself.
COMMIT_SUBJECT = re.compile(
    r"^((feat|fix|fixes|fixed|chore|refactor|docs?|tests?|build|ci|perf|style|add|adds|added|"
    r"remove|removes|removed|update|updates|updated|bump|bumps|use|move|rename|delete|clean|"
    r"cleanup|ignore|introduce|implement|enable|disable|improve|upgrade|downgrade|make|"
    r"switch|allow|avoid|skip|set|change|replace|convert|simplify|fmt|format|lint)\b"
    r"|[A-Z][\w./ -]{0,30}:\s+\S)",
    re.I,
)
HEX_RE = re.compile(r"\b[0-9a-f]{7,40}\b")
PR_RE = re.compile(r"\(?#\d+\)?|\b[\w.-]+/[\w.-]+#\d+")
# Gerrit change numbers ("Likely due to CL 564197") are a lookup key: the guilty commit's own
# message carries "Reviewed-on: .../c/go/+/564197", which the agent reads with every diff.
CL_RE = re.compile(r"\b(CLs?\s*\d+(\s*(,|and|&)\s*\d+)*|(go\.dev|golang\.org)/cl/\d+)\b", re.I)
# Name fragments too generic to count as identifying an author.
GENERIC_NAME_PARTS = {"bot", "renovate", "dependabot", "github", "actions", "copybara",
                      "gopherbot", "service", "account", "team", "release", "automation"}
URL_RE = re.compile(r"https?://\S+")
MENTION_RE = re.compile(r"@[\w-]+")


@dataclass
class Funnel:
    reverts: int = 0
    linked: int = 0
    target_exists: int = 0
    target_not_merge: int = 0
    target_not_revert: int = 0
    is_really_a_revert: int = 0
    timing_ok: int = 0
    has_reason: int = 0
    describes_symptom: int = 0
    no_leak: int = 0
    in_window: int = 0
    window_big_enough: int = 0
    not_duplicate: int = 0
    selected: int = 0
    leaks: list[str] = field(default_factory=list)


# Mining reads commit metadata only, which a blob:none clone already has in full. Without
# this, asking about a revert target that is not in local history (a commit on a branch
# that never merged) makes git silently fetch it from GitHub -- and with no negotiation,
# one such lookup on grafana pulled 569,429 objects before it was stopped.
NO_LAZY_FETCH = {**os.environ, "GIT_NO_LAZY_FETCH": "1"}


def git(repo: Path, *args: str, stdin: str | None = None) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], input=stdin, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", env=NO_LAZY_FETCH)
    if proc.returncode != 0:
        raise GitError(f"git {' '.join(args[:3])}: {proc.stderr.strip()[:300]}")
    return proc.stdout


def ensure_clone(name: str, url: str) -> Path:
    path = REPOS_DIR / name
    if (path / ".git").exists():
        return path
    REPOS_DIR.mkdir(parents=True, exist_ok=True)
    print(f"  cloning {url} (metadata only)...", flush=True)
    subprocess.run(["git", "clone", "--filter=blob:none", "--no-checkout", "--quiet", url,
                    str(path)], check=True)
    return path


def _normalise(text: str) -> str:
    text = PR_RE.sub(" ", text.lower())
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _scrub(text: str) -> str:
    """Remove identifiers. Applied identically to the alert text and to the subject it is
    compared against -- scrubbing only one side is what let '@types/core' become '/core'
    and slip past the leak check."""
    text = URL_RE.sub("", text)
    text = HEX_RE.sub("", text)
    text = CL_RE.sub("", text)
    text = PR_RE.sub("", text)
    text = MENTION_RE.sub("", text)
    return re.sub(r"\s{2,}", " ", text).strip(" -*:")


def clean_reason(body: str) -> str:
    """The revert's explanation with boilerplate and anything identifying removed."""
    kept = []
    for line in body.splitlines():
        stripped = BULLET.sub("", line).strip()  # bullets first, or '* Revert "x"' survives
        stripped = REASON_LABEL.sub("", stripped)
        if not stripped or BOILERPLATE.match(stripped):
            continue
        if REVERT_WORD.search(stripped) or SIGNOFF.search(stripped):
            continue
        if stripped.startswith('"') and stripped.endswith('"'):
            continue  # a quoted commit subject on a line of its own
        if COMMIT_SUBJECT.match(stripped):
            continue  # a change, not a symptom
        stripped = _scrub(stripped)
        if stripped:
            kept.append(stripped)
    return "\n".join(kept).strip()


def describes_symptom(reason: str) -> bool:
    return bool(SYMPTOM.search(reason))


def names_author(reason: str, author: str, repo_name: str) -> bool:
    """True if the alert mentions the guilty commit's author by name."""
    parts = {w.lower() for w in re.split(r"[^A-Za-z]+", author) if len(w) >= 4}
    parts -= GENERIC_NAME_PARTS | {repo_name.lower()}
    text = reason.lower()
    return any(re.search(rf"\b{re.escape(part)}\b", text) for part in parts)


def leaks_answer(reason: str, subject: str) -> bool:
    """True if the alert text would name the guilty commit. Subjects shorter than a few
    words ("fix", "wip") are too generic to count as a leak by substring alone."""
    norm_subject = _normalise(_scrub(subject))
    if len(norm_subject.split()) < 3:
        return False
    return norm_subject in _normalise(reason)


def mine(name: str, repo: Path, per_repo: int) -> tuple[list[dict], Funnel]:
    funnel = Funnel()
    raw = git(repo, "log", "HEAD", "--grep=This reverts commit",
              "--format=%H%x1f%cI%x1f%B%x1e")
    reverts = []
    for record in raw.split("\x1e"):
        if not record.strip():
            continue
        r_sha, r_time, body = record.strip().split("\x1f", 2)
        funnel.reverts += 1
        match = REVERTS_RE.search(body)
        if match:
            funnel.linked += 1
            reverts.append((r_sha, parse_ts(r_time), body, match.group(1)))

    # Resolve every target in one call; short SHAs and missing objects both come back here.
    check = git(repo, "cat-file", "--batch-check=%(objectname) %(objecttype)",
                stdin="\n".join(t for *_, t in reverts))
    resolved = {}
    for target, line in zip((t for *_, t in reverts), check.splitlines()):
        parts = line.split()
        if len(parts) == 2 and parts[1] == "commit":
            resolved[target] = parts[0]

    meta_raw = git(repo, "log", "--no-walk=unsorted", "--stdin",
                   "--format=%H%x1f%P%x1f%cI%x1f%an%x1f%s%x1e",
                   stdin="\n".join(set(resolved.values())))
    meta = {}
    for record in meta_raw.split("\x1e"):
        if record.strip():
            sha, parents, c_time, author, subject = record.strip().split("\x1f", 4)
            meta[sha] = (parents.split(), parse_ts(c_time), author, subject)

    selected: list[dict] = []
    seen_alerts: set[str] = set()
    base = GitRepo(repo)
    for r_sha, r_time, body, target in reverts:            # git log order: newest first
        if len(selected) >= per_repo:
            break
        x = resolved.get(target)
        if not x or x not in meta:
            continue
        funnel.target_exists += 1
        parents, x_time, x_author, subject = meta[x]
        if len(parents) > 1:
            continue
        funnel.target_not_merge += 1
        if REVERT_WORD.search(subject):
            continue
        funnel.target_not_revert += 1
        if not REVERT_WORD.search(body.splitlines()[0] if body else ""):
            continue
        funnel.is_really_a_revert += 1
        if not (MIN_TIME_TO_REVERT <= r_time - x_time <= MAX_TIME_TO_REVERT):
            continue
        funnel.timing_ok += 1

        reason = clean_reason(body)
        if len(reason) < MIN_REASON_CHARS or len(reason.split()) < MIN_REASON_WORDS:
            continue
        funnel.has_reason += 1
        if not describes_symptom(reason):
            continue
        funnel.describes_symptom += 1
        if leaks_answer(reason, subject) or names_author(reason, x_author, name):
            funnel.leaks.append(f"{r_sha[:10]}: {subject[:80]}")
            continue
        funnel.no_leak += 1

        onset = r_time - ONSET_BEFORE_REVERT
        title = reason.splitlines()[0][:120]
        alert = Alert(fingerprint=f"{name}-{r_sha[:10]}", source="generic", service=name,
                      severity="sev2", title=title, description=reason[:MAX_DESCRIPTION_CHARS],
                      started_at=onset, labels={"repo": name})
        try:
            view = base.at(onset + timedelta(minutes=LEAD_MINUTES))
            landed = view.landed_in_window(alert, LOOKBACK_HOURS)
        except GitError:
            continue
        # A fair chance: the guilty commit shipped inside the window the tool searches, and
        # the revert had not.
        if x not in landed or r_sha in landed:
            continue
        funnel.in_window += 1
        if len(landed) < MIN_WINDOW_COMMITS:
            continue
        funnel.window_big_enough += 1
        # Two reverts with the same explanation (a pair of commits undone together) make two
        # incidents with one alert and different answers: unanswerable by construction.
        key = _normalise(reason)
        if key in seen_alerts:
            continue
        seen_alerts.add(key)
        funnel.not_duplicate += 1

        selected.append({
            "incident_id": f"RW-{name}-{len(selected) + 1:02d}",
            "repo": name,
            "repo_url": REPOS[name],
            "ground_truth_sha": x,
            "ground_truth_subject": subject,
            "revert_sha": r_sha,
            "hours_to_revert": round((r_time - x_time).total_seconds() / 3600, 1),
            "window_commits": len(landed),
            "alert": {
                "fingerprint": alert.fingerprint,
                "source": "generic",
                "service": name,
                "severity": "sev2",
                "title": title,
                "description": alert.description,
                "started_at": onset.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                "labels": {"repo": name},
                "metrics": {},
            },
        })
        funnel.selected += 1
    return selected, funnel


def warm(repo: Path, record: dict) -> float:
    """Fetch every file the tool will read for this incident, once, so the eval measures
    the agent rather than lazy downloads from GitHub."""
    started = time.perf_counter()
    alert = Alert(fingerprint="warm", source="generic", service=record["repo"], severity="sev2",
                  title="", started_at=parse_ts(record["alert"]["started_at"]))
    # A first read downloads every changed file in the window, one commit per round trip --
    # several minutes for a 150-commit window. Afterwards it is all local.
    view = GitRepo(repo, timeout=3600).at(alert.started_at + timedelta(minutes=LEAD_MINUTES))
    view.commits_in_window(alert, LOOKBACK_HOURS)
    view.show(record["ground_truth_sha"])
    return time.perf_counter() - started


def write_funnel(funnels: dict[str, Funnel], records: list[dict]) -> None:
    lines = ["# Real-world benchmark: how the incidents were selected", "",
             "Every revert in each repository, filtered by the rules in `build.py` and taken "
             "newest-first until the per-repo cap. Nothing was hand-picked. Counts are "
             "cumulative: each column is what survived every rule to its left.", "",
             "| Repo | Reverts | Names its target | Target exists | Not a merge | Target not a revert "
             "| Is itself a revert "
             "| Reverted in 90m-7d | States a reason | Describes a symptom "
             "| Doesn't name the answer | Landed in the window | Window >= 10 commits "
             "| Not a duplicate | Selected |",
             "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: "
             "| ---: | ---: | ---: |"]
    for name, f in funnels.items():
        lines.append(f"| {name} | {f.reverts} | {f.linked} | {f.target_exists} | "
                     f"{f.target_not_merge} | {f.target_not_revert} | {f.is_really_a_revert} | "
                     f"{f.timing_ok} | "
                     f"{f.has_reason} | {f.describes_symptom} | {f.no_leak} | {f.in_window} "
                     f"| {f.window_big_enough} | {f.not_duplicate} | **{f.selected}** |")
    lines += ["", "The funnel stops early once a repo reaches its cap, so the middle columns "
              "count only the reverts examined before then, not every revert in the repo.", ""]
    lines += ["## Rejected for leaking the answer", "",
              "Alerts whose cleaned text still contained the guilty commit's subject line, or "
              "named its author.", ""]
    leaks = [f"- `{name}` {leak}" for name, f in funnels.items() for leak in f.leaks]
    lines += leaks or ["None."]
    lines += ["", "## Selected", "", "| Incident | Commits in window | Hours to revert | Alert title |",
              "| --- | ---: | ---: | --- |"]
    for r in records:
        title = r["alert"]["title"].replace("|", "\\|")[:90]
        lines.append(f"| {r['incident_id']} | {r['window_commits']} | {r['hours_to_revert']} | {title} |")
    FUNNEL.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--per-repo", type=int, default=PER_REPO)
    parser.add_argument("--no-warm", action="store_true", help="skip prefetching file contents")
    args = parser.parse_args()

    records: list[dict] = []
    funnels: dict[str, Funnel] = {}
    for name, url in REPOS.items():
        print(f"{name}:", flush=True)
        repo = ensure_clone(name, url)
        selected, funnel = mine(name, repo, args.per_repo)
        funnels[name] = funnel
        print(f"  {funnel.reverts} reverts -> {funnel.selected} selected", flush=True)
        if not args.no_warm:
            for record in selected:
                seconds = warm(repo, record)
                print(f"  warmed {record['incident_id']} ({record['window_commits']} commits in "
                      f"window) in {seconds:.0f}s", flush=True)
        records += selected

    OUT.write_text(json.dumps(records, indent=2), encoding="utf-8")
    write_funnel(funnels, records)
    print(f"\n{len(records)} incidents -> {OUT}\nselection funnel -> {FUNNEL}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
