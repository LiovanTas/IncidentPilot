"""Build stand-in demo replays for the curated real-world incidents, without an API key.

Placeholders until real ones are recorded with `incidentpilot demo-record`, which overwrites
them. Everything here runs for real -- the alert is normalized, the commit window is
correlated and ranked, and every tool the agent calls reads the actual repository -- except
the model: its reasoning and choices are scripted below, written knowing the answer.

So a stand-in shows what an investigation looks like, not how well the agent does. Every
recording says so: its model is labelled SCRIPTED STAND-IN, which the page shows on the
replay chip, the run footer, the Slack brief and the known-answer box, and it carries no
cost or token usage.

    py -3.13 eval/demo_standins.py            # all curated real-world incidents
    py -3.13 eval/demo_standins.py RW-go-05   # just one
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from incidentpilot.agent import DiagnosisAgent  # noqa: E402
from incidentpilot.demo import runs as runs_mod  # noqa: E402
from incidentpilot.demo import scenarios as scenario_mod  # noqa: E402
from incidentpilot.demo.workspace import Pilots, repo_available  # noqa: E402

STAND_IN_MODEL = "claude-sonnet-5 (SCRIPTED STAND-IN)"
RECORDED_AT = "2026-11-24T12:00:00+00:00"


def step(thinking: str | None, tool: str, **args) -> tuple[str | None, str, dict]:
    return thinking, tool, args


# Per incident: the turns (what the model "thinks", then which tool it calls), and the
# diagnosis it submits. Short SHAs are resolved by the tools exactly as the model's would be.
SCRIPTS: dict[str, dict] = {
    "RW-moby-03": {
        "turns": [
            step("Case-insensitive filesystems are the default on macOS and Windows. Breakage "
                 "that only shows up there usually means two paths that differ only by case. "
                 "The ranker's top pick is a batch of Windows test changes; reading it first.",
                 "get_commit_diff", sha="2af5034ce8"),
            step("Test edits and one error message. Nothing adds or renames a path, so it "
                 "can't collide with anything. Ruled out. I want a commit that creates a new "
                 "top-level path: #10 moves autogen/dockerversion into a new package.",
                 "get_commit_diff", sha="d5cd032a86"),
            step("It creates a top-level version/ directory. Checking whether the repo already "
                 "has something named VERSION at the root.",
                 "get_file_history", path="VERSION", limit=3),
        ],
        "verdict": {
            "root_cause": "d5cd032a86 adds a top-level version/ directory, and the repository "
                          "already has a VERSION file at its root. On a case-insensitive "
                          "filesystem those are the same name, so checkouts and builds collide.",
            "confidence": 0.84,
            "reasoning": "The symptom is specific to case-insensitive filesystems, which points "
                         "at a path collision rather than a logic bug. Of the twelve suspects, "
                         "only this one creates a new top-level path, and its name differs from "
                         "the existing VERSION file only by case.",
            "evidence": [
                "d5cd032a86 adds version/version_lib.go and moves autogen/dockerversion to version",
                "VERSION already exists at the repository root",
                "2af5034ce8 (the ranker's pick) only edits tests and an error message",
            ],
            "ruled_out": ["2af5034ce8: Windows test changes; adds no new paths"],
            "remediation": ["Revert d5cd032a86",
                            "Re-land the package under a name that doesn't clash with VERSION"],
        },
    },
    "RW-grafana-01": {
        "turns": [
            step("The report names goavro. Only one commit in the window brings goavro in, and "
                 "it is also the ranker's top pick. Reading it to see what it adds.",
                 "get_commit_diff", sha="413be3a6a0"),
            step("It vendors github.com/linkedin/goavro and golang/snappy and imports goavro "
                 "from pkg/extensions/main.go, so every Grafana build now compiles it. Checking "
                 "the runner-up so the verdict doesn't just echo the ranker.",
                 "get_commit_diff", sha="047abc87c2"),
        ],
        "verdict": {
            "root_cause": "413be3a6a0 vendors github.com/linkedin/goavro and imports it from "
                          "pkg/extensions/main.go, so every Grafana build now compiles goavro, "
                          "which does not build for 32-bit targets.",
            "confidence": 0.9,
            "reasoning": "The report names the library. This is the only commit in the window "
                         "that adds it, and the import in pkg/extensions puts it in every "
                         "build. The runner-up is a TypeScript UI change and can't affect Go "
                         "compilation.",
            "evidence": [
                "413be3a6a0 adds vendor/github.com/linkedin/goavro and an import in pkg/extensions/main.go",
                "go.mod gains the goavro requirement in the same commit",
                "047abc87c2 only touches packages/grafana-ui TypeScript",
            ],
            "ruled_out": ["047abc87c2: UI-only TypeScript change"],
            "remediation": ["Revert 413be3a6a0",
                            "Gate the goavro import behind a build tag that excludes 32-bit targets"],
        },
    },
    "RW-moby-01": {
        "turns": [
            step("The docker/cli formatting code renders whatever the engine API returns, so "
                 "I'm looking for a change to an API response, not to the CLI. The top pick "
                 "only edits AUTHORS. #6 changes the /info response; reading it.",
                 "get_commit_diff", sha="5d2006256f"),
            step("#6 drops BridgeNfIptables and BridgeNfIp6tables from GET /info for API 1.49 "
                 "and up. The CLI's info output reads those fields. #4 documents API 1.49; "
                 "checking it to rule it out.",
                 "get_commit_diff", sha="adb9e9135a"),
        ],
        "verdict": {
            "root_cause": "5d2006256f removes BridgeNfIptables and BridgeNfIp6tables from the "
                          "GET /info response for API 1.49 and up. docker/cli's info formatting "
                          "still reads them, so its output breaks against the new API version.",
            "confidence": 0.86,
            "reasoning": "A formatting failure in a separate client points at a change in the "
                         "data the engine sends. This is the only suspect that changes an API "
                         "response shape. The others are tests, docs, tracing and vendoring.",
            "evidence": [
                "5d2006256f omits two /info fields for API >= 1.49 (api/types/system/info.go)",
                "Older API versions keep returning them, so only new clients see the change",
                "adb9e9135a is documentation only",
            ],
            "ruled_out": ["ece7e02b86: AUTHORS file only",
                          "adb9e9135a: API documentation only"],
            "remediation": ["Revert 5d2006256f",
                            "Update docker/cli to handle the missing fields, then re-land"],
        },
    },
    "RW-prometheus-01": {
        "turns": [
            step("The report says the build refuses to work on Mac OS X and pastes a go build "
                 "command with -ldflags. The ranker's top pick changes build flags in "
                 "Makefile.INCLUDE.",
                 "get_commit_diff", sha="0e9e3e068d"),
            step("It adds -linkmode external and -extldflags '-static' with static leveldb and "
                 "snappy libraries. Apple's linker can't produce fully static binaries, which "
                 "fits 'refuses to build on Mac OS X'. Checking the only other build change in "
                 "the window.",
                 "get_commit_diff", sha="c7052ede27"),
        ],
        "verdict": {
            "root_cause": "0e9e3e068d adds -linkmode external and -extldflags '-static' to the "
                          "build flags. macOS does not support fully static linking, so the "
                          "build fails there.",
            "confidence": 0.88,
            "reasoning": "The pasted command in the report carries the new ldflags, and this "
                         "is the only commit that changes them. The other build change only "
                         "adjusts tarball rules.",
            "evidence": [
                "Makefile.INCLUDE gains -linkmode external and -extldflags '-static ...'",
                "The failing command in the report uses those flags",
                "c7052ede27 only edits tarball targets in the Makefile",
            ],
            "ruled_out": ["c7052ede27: tarball rule fixups only"],
            "remediation": ["Revert 0e9e3e068d",
                            "Apply the static flags only on Linux"],
        },
    },
    "RW-go-05": {
        "turns": [
            step("Failing only on big-endian machines and riscv64 points at code that has "
                 "assembly on amd64 and arm64 and a generic Go fallback elsewhere, where byte "
                 "order matters. The top pick is comment grammar; reading it to rule it out.",
                 "get_commit_diff", sha="1d187fd0be"),
            step("Grammar fixes only. Nothing in the shortlist adds per-architecture code, so "
                 "the cause is probably outside it. A new random generator is a common place "
                 "for exactly that split; checking recent changes to math/rand/v2.",
                 "get_file_history", path="src/math/rand/v2", limit=6),
            step("6382893890 adds ChaCha8: assembly for amd64 and arm64, a generic "
                 "implementation for every other architecture. Reading it.",
                 "get_commit_diff", sha="6382893890"),
        ],
        "verdict": {
            "root_cause": "6382893890 adds the ChaCha8 generator, with assembly for amd64 and "
                          "arm64 and a generic implementation everywhere else. The failures are "
                          "on exactly the platforms that use the generic path, and the "
                          "big-endian ones point to a byte-order bug in it.",
            "confidence": 0.8,
            "reasoning": "The ranker's shortlist missed it: none of its twelve commits adds "
                         "code that differs by architecture. File history on math/rand/v2 "
                         "found the commit that does, and its platform split matches the "
                         "failure pattern exactly.",
            "evidence": [
                "Failures only on big-endian platforms and riscv64, not amd64 or arm64",
                "6382893890 ships assembly for amd64 and arm64 and a generic fallback",
                "None of the shortlisted commits changes per-architecture code",
            ],
            "ruled_out": ["1d187fd0be: comment grammar fixes only"],
            "remediation": ["Revert 6382893890",
                            "Add a big-endian builder run to the ChaCha8 tests before re-landing"],
        },
    },
    "RW-go-04": {
        "turns": [
            step("The panic says gotypesalias is not listed in godebugs.All. So something now "
                 "reports the gotypesalias setting, and the compiler's table doesn't know it. "
                 "That rules out the top pick, an os change. #2, #3 and #6 all touch "
                 "gotypesalias; reading #2 first.",
                 "get_commit_diff", sha="15cec430d7"),
            step("#2 flips the default in types2 but adds no godebug tracking. #3 says it "
                 "tracks non-default gotypesalias behaviour, which means reporting the setting "
                 "through the godebug package.",
                 "get_commit_diff", sha="c51f6c6257"),
        ],
        "verdict": {
            "root_cause": "c51f6c6257 makes types2, which is compiled into the compiler, report "
                          "non-default gotypesalias uses through the godebug package. The panic "
                          "is godebug refusing a setting it can't find in godebugs.All, which "
                          "happens when this code runs against a table without the entry, as "
                          "in a bootstrap build.",
            "confidence": 0.82,
            "reasoning": "The panic message names the mechanism: a godebug setting looked up "
                         "in godebugs.All. Three suspects touch gotypesalias, but only this "
                         "one starts reporting it through godebug, and it edits the table in "
                         "the same commit.",
            "evidence": [
                "Panic: godebug: Value of name not listed in godebugs.All: gotypesalias",
                "c51f6c6257 adds gotypesalias tracking in types2 and go/types",
                "The same commit edits src/internal/godebugs/table.go",
            ],
            "ruled_out": ["ca94e9e223: os.File ReadDir locking, unrelated to the compiler",
                          "15cec430d7: flips the default but adds no godebug tracking"],
            "remediation": ["Revert c51f6c6257",
                            "Re-land with the tracking guarded for bootstrap toolchains"],
        },
    },
    "RW-go-03": {
        "turns": [
            step("CockroachDB is network-heavy and runs on many cores, so a scheduler or "
                 "network-poller change is the likeliest cause of a benchmark failure. The top "
                 "pick says it only affects tests; reading it to be sure.",
                 "get_commit_diff", sha="7ce45a014c"),
            step("Test-only, as it says. #7 changes how findRunnable polls the network; "
                 "reading it.",
                 "get_commit_diff", sha="352dd2d932"),
            step("#7 lets only one P poll the network at a time. #2 also changes the runtime, "
                 "its lock implementation; checking it before deciding.",
                 "get_commit_diff", sha="7a177114df"),
        ],
        "verdict": {
            "root_cause": "352dd2d932 lets only one P poll the network at a time in "
                          "findRunnable. On a many-core, network-heavy server like CockroachDB, "
                          "other Ps can no longer pick up ready network work themselves, which "
                          "fits a benchmark that suddenly fails.",
            "confidence": 0.72,
            "reasoning": "The workload points at the scheduler's network path, and this is the "
                         "only suspect that changes it. The runtime lock change is possible, "
                         "but it would show up across benchmarks rather than in one "
                         "network-bound one, so I'm less sure than usual and say so.",
            "evidence": [
                "352dd2d932 adds sched.pollingNet so only one P does non-blocking netpoll",
                "CockroachDB is network-bound and runs on many cores",
                "7ce45a014c only changes a test helper",
            ],
            "ruled_out": ["7ce45a014c: affects runtime tests only",
                          "7a177114df: lock implementation change; would not single out one network-bound benchmark"],
            "remediation": ["Revert 352dd2d932",
                            "Benchmark netpoll contention on many-core machines before re-landing"],
        },
    },
}


def _message(content: list) -> SimpleNamespace:
    return SimpleNamespace(content=content, stop_reason="tool_use", stop_details=None,
                           usage=SimpleNamespace(input_tokens=0, output_tokens=0,
                                                 cache_read_input_tokens=0,
                                                 cache_creation_input_tokens=0))


class _Stream:
    def __init__(self, message):
        self.message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self.message


class ScriptedClient:
    """Stands in for the Anthropic client: returns the scripted turns in order, at roughly
    the pace of a real turn."""

    def __init__(self, turns: list[SimpleNamespace]):
        self.turns = list(turns)
        self.messages = SimpleNamespace(stream=self._stream)
        self.beta = self.messages

    def _stream(self, **_kwargs):
        message = self.turns.pop(0)
        thinking = sum(len(b.thinking) for b in message.content if b.type == "thinking")
        time.sleep(2.0 + thinking / 220)
        return _Stream(message)


def _turns(scenario, script: dict) -> list[SimpleNamespace]:
    b = SimpleNamespace
    out = []
    for i, (thinking, tool, args) in enumerate(script["turns"]):
        blocks = [b(type="thinking", thinking=thinking)] if thinking else []
        blocks.append(b(type="tool_use", id=f"s{i}", name=tool, input=args))
        out.append(_message(blocks))
    verdict = {
        "offending_sha": scenario.answer_sha, "runbook_ids": [],
        "rollback_command": f"git revert --no-edit {scenario.answer_sha[:10]}",
        "needs_human": False, **script["verdict"],
    }
    out.append(_message([b(type="tool_use", id="final", name="submit_diagnosis", input=verdict)]))
    return out


def build(scenario, pilots: Pilots) -> Path:
    script = SCRIPTS[scenario.id]
    pilot = pilots.get(scenario.repo)
    agent = DiagnosisAgent(model="claude-sonnet-5", client=ScriptedClient(_turns(scenario, script)),
                           show_reasoning=True)
    agent._use_fallbacks = False
    pilot._agent = agent
    run = runs_mod.Run(scenario, "live")
    os.environ["ANTHROPIC_API_KEY"] = "scripted-stand-in"  # takes the live path; no request is made
    try:
        runs_mod.RunManager(pilots=pilots).execute_live(run)
    finally:
        os.environ.pop("ANTHROPIC_API_KEY", None)
        pilot._agent = None
    errors = [e["message"] for e in run.events if e["type"] == "error"]
    if errors:
        raise RuntimeError(errors[0])
    events = [e for e in run.events if e["type"] not in ("answer", "end")]
    for e in events:
        if e["type"] == "done":
            e.update(model=STAND_IN_MODEL, cost_usd=None, usage=None)
            for block in e.get("slack") or []:
                for item in block.get("elements") or []:
                    if isinstance(item.get("text"), str):
                        item["text"] = item["text"].replace("source: agent",
                                                            "source: scripted stand-in")
    path = runs_mod.recording_path(scenario.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema": runs_mod.RECORDING_SCHEMA, "scenario": scenario.id,
        "recorded_at": RECORDED_AT, "model": STAND_IN_MODEL, "stand_in": True,
        "events": events,
    }, indent=1), encoding="utf-8")
    return path


def main(argv: list[str]) -> int:
    if os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is set: record real replays instead, with "
              "`incidentpilot demo-record`.", file=sys.stderr)
        return 1
    os.environ["GIT_NO_LAZY_FETCH"] = "1"
    pilots = Pilots()
    chosen = [s for s in scenario_mod.load(None) if s.id in SCRIPTS and (not argv or s.id in argv)]
    for scenario in chosen:
        if not repo_available(scenario.repo):
            print(f"skip {scenario.id}: no local clone of {scenario.repo}", file=sys.stderr)
            continue
        verdict_sha = SCRIPTS[scenario.id]["verdict"].get("offending_sha")
        assert verdict_sha is None, "scripts take the answer from the scenario"
        path = build(scenario, pilots)
        print(f"{scenario.id:18} -> {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
