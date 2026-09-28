"""Command line entry point: `incidentpilot <command>`."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .config import load_config
from .ingest import normalize
from .pipeline import IncidentPilot
from .rag import RunbookIndex


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)-7s %(name)s: %(message)s",
    )


def cmd_index(args: argparse.Namespace) -> int:
    config = load_config()
    index = RunbookIndex(config.index_path)
    count = index.build(Path(args.runbooks or config.runbook_dir))
    print(f"indexed {count} chunks from {args.runbooks or config.runbook_dir} "
          f"using the '{index.embedder.name}' embedder -> {config.index_path}")
    index.close()
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    config = load_config()
    index = RunbookIndex(config.index_path)
    if index.size == 0:
        index.build(config.runbook_dir)
    for hit in index.search(args.query, top_k=args.top_k):
        print(f"[{hit.score:.5f}] {hit.runbook_id} :: {hit.heading}")
        print("    " + hit.text.strip().replace("\n", "\n    ")[:400])
        print()
    index.close()
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    config = load_config()
    payload = json.loads(Path(args.alert).read_text(encoding="utf-8"))
    alerts = normalize(payload)
    if not alerts:
        print("no firing alerts in that payload", file=sys.stderr)
        return 1

    pilot = IncidentPilot(config)
    use_agent = not args.no_agent
    if use_agent and not config.has_anthropic_key:
        print("note: no ANTHROPIC_API_KEY found -- running the heuristic ranker only",
              file=sys.stderr)

    exit_code = 0
    for alert in alerts:
        outcome = pilot.respond(alert, use_agent=use_agent, repo_url=args.repo_url or "")
        diag = outcome["diagnosis"]
        print(f"\n=== {outcome['incident_id']} - {alert.summary_line}")
        print(f"root cause : {diag['root_cause']}")
        print(f"commit     : {diag['offending_sha'] or '(none identified)'}")
        print(f"confidence : {diag['confidence']:.0%}"
              f"{'  [NEEDS HUMAN]' if diag['needs_human'] else ''}")
        print(f"users hit  : ~{outcome['impact']['affected_users_point']:,}")
        print(f"report     : {outcome['report_path']}")
        print(f"slack      : {outcome['payload_path']}"
              f"{' (posted)' if outcome.get('posted') else ' (not posted)'}")
        if diag["needs_human"]:
            exit_code = 2
    return exit_code


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run("incidentpilot.server:app", host=args.host, port=args.port, reload=args.reload)
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    config = load_config()
    print(f"repo            : {config.repo_path}  (exists: {(config.repo_path / '.git').exists()})")
    print(f"runbooks        : {config.runbook_dir}  ({len(list(Path(config.runbook_dir).glob('*.md')))} files)")
    print(f"topology        : {config.topology_path}  (exists: {config.topology_path.exists()})")
    print(f"index           : {config.index_path}")
    print(f"model           : {config.model}  (effort {config.effort}, max {config.max_agent_turns} turns)")
    print(f"anthropic key   : {'present' if config.has_anthropic_key else 'MISSING (heuristic mode)'}")
    print(f"voyage key      : {'present' if __import__('os').environ.get('VOYAGE_API_KEY') else 'absent (hashed embeddings)'}")
    print(f"slack posting   : {'ENABLED' if config.slack_post else 'disabled (dry-run)'}")
    print(f"output dir      : {config.out_dir}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="incidentpilot", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p_index = sub.add_parser("index", help="build the runbook index")
    p_index.add_argument("--runbooks", help="directory of markdown runbooks")
    p_index.set_defaults(func=cmd_index)

    p_search = sub.add_parser("search", help="query the runbook index")
    p_search.add_argument("query")
    p_search.add_argument("--top-k", type=int, default=5)
    p_search.set_defaults(func=cmd_search)

    p_replay = sub.add_parser("replay", help="run one alert payload end to end")
    p_replay.add_argument("alert", help="path to an alert JSON payload")
    p_replay.add_argument("--no-agent", action="store_true", help="heuristic ranker only")
    p_replay.add_argument("--repo-url", help="base repo URL for commit links in the report")
    p_replay.set_defaults(func=cmd_replay)

    p_serve = sub.add_parser("serve", help="run the webhook receiver")
    p_serve.add_argument("--host", default="0.0.0.0")
    p_serve.add_argument("--port", type=int, default=8080)
    p_serve.add_argument("--reload", action="store_true")
    p_serve.set_defaults(func=cmd_serve)

    p_doctor = sub.add_parser("doctor", help="show resolved configuration")
    p_doctor.set_defaults(func=cmd_doctor)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _configure_logging(args.verbose)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
