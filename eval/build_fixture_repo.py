"""Materialize the replay corpus as a real git repository.

Builds `eval/fixtures/repo` by writing the baseline tree, then replaying every cause and
decoy commit in chronological order with authored dates matching the scenario timeline.
Writes `eval/incidents.json` mapping each incident's alert payload to the SHA of its
ground-truth cause.

    py -3.13 eval/build_fixture_repo.py
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from scenarios import BASE, INCIDENTS, all_commits, baseline_files  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT / "eval" / "fixtures" / "repo"
INCIDENTS_JSON = ROOT / "eval" / "incidents.json"


class BuildError(RuntimeError):
    pass


def git(*args: str, env_extra: dict[str, str] | None = None) -> str:
    env = {**os.environ}
    # Keep the build hermetic: a developer's global git config must not change the SHAs.
    env.update({
        "GIT_CONFIG_NOSYSTEM": "1",
        "HOME": str(REPO),
        "GIT_TERMINAL_PROMPT": "0",
    })
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        ["git", "-C", str(REPO), *args],
        capture_output=True, text=True, env=env, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        raise BuildError(f"git {' '.join(args)}\n{proc.stdout}\n{proc.stderr}")
    return proc.stdout.strip()


def write_file(rel: str, content: str) -> None:
    path = REPO / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def apply_edits(spec: dict) -> list[str]:
    touched: list[str] = []
    for rel, content in (spec.get("new_files") or {}).items():
        write_file(rel, content)
        touched.append(rel)
    for rel, find, replace in (spec.get("edits") or []):
        path = REPO / rel
        if not path.exists():
            raise BuildError(f"{spec.get('message', '')[:40]!r}: {rel} does not exist")
        text = path.read_text(encoding="utf-8")
        if find not in text:
            raise BuildError(
                f"anchor not found in {rel} for commit {spec.get('message', '')[:60]!r}\n"
                f"  looking for: {find[:120]!r}"
            )
        path.write_text(text.replace(find, replace, 1), encoding="utf-8", newline="\n")
        touched.append(rel)
    return touched


def commit(message: str, author: str, when, extra_env: dict[str, str] | None = None) -> str:
    stamp = when.isoformat()
    email = author.lower().replace(" ", ".").replace("'", "") + "@checkout-platform.dev"
    env = {
        "GIT_AUTHOR_NAME": author, "GIT_AUTHOR_EMAIL": email, "GIT_AUTHOR_DATE": stamp,
        "GIT_COMMITTER_NAME": author, "GIT_COMMITTER_EMAIL": email, "GIT_COMMITTER_DATE": stamp,
    }
    if extra_env:
        env.update(extra_env)
    git("add", "-A", env_extra=env)
    git("commit", "-q", "--no-gpg-sign", "-m", message, env_extra=env)
    return git("rev-parse", "HEAD", env_extra=env)


def _force_remove(func, path, _exc):
    """git writes its object files read-only, which blocks rmtree on Windows."""
    os.chmod(path, stat.S_IWRITE)
    func(path)


def rmtree(path: Path) -> None:
    if not path.exists():
        return
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_force_remove)
    else:
        shutil.rmtree(path, onerror=_force_remove)


def build_repo() -> dict[tuple[str, str], str]:
    rmtree(REPO)
    REPO.mkdir(parents=True)

    git("init", "-q", "-b", "main")
    git("config", "user.name", "checkout-platform")
    git("config", "user.email", "bot@checkout-platform.dev")
    git("config", "commit.gpgsign", "false")

    for rel, content in baseline_files().items():
        write_file(rel, content)
    commit("chore: initial import of the checkout platform monorepo",
           "Platform Bot", BASE - timedelta(days=30))

    shas: dict[tuple[str, str], str] = {}
    for spec in all_commits():
        apply_edits(spec)
        sha = commit(spec["message"], spec["author"], spec["at"])
        shas[(spec["incident_id"], spec["role"])] = (
            shas.get((spec["incident_id"], spec["role"]), "") or sha
        )
        if spec["role"] == "cause":
            shas[(spec["incident_id"], "cause")] = sha
    return shas


def build_incidents(shas: dict[tuple[str, str], str]) -> list[dict]:
    records = []
    for incident in INCIDENTS:
        sha = shas[(incident["id"], "cause")]
        records.append({
            "incident_id": incident["id"],
            "ground_truth_sha": sha,
            "ground_truth_subject": incident["cause"]["message"].splitlines()[0],
            "alert": {
                "fingerprint": incident["id"],
                "source": "generic",
                "service": incident["service"],
                "severity": incident["severity"],
                "title": incident["title"],
                "description": incident["description"],
                "started_at": incident["onset"].isoformat().replace("+00:00", "Z"),
                "labels": incident["labels"],
                "metrics": incident["metrics"],
            },
        })
    return records


def main() -> int:
    shas = build_repo()
    total = int(git("rev-list", "--count", "HEAD"))
    records = build_incidents(shas)
    INCIDENTS_JSON.write_text(json.dumps(records, indent=2), encoding="utf-8")

    print(f"built {REPO} with {total} commits")
    print(f"wrote {len(records)} incidents to {INCIDENTS_JSON}")
    print("\nground truth:")
    for record in records:
        print(f"  {record['incident_id']}  {record['ground_truth_sha'][:10]}  "
              f"{record['ground_truth_subject'][:64]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
