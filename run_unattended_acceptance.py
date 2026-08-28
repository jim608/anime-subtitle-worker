from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from acceptance.collector import collect_observations, observation_summary
from acceptance.harness import (
    AcceptanceInputError,
    evaluate_acceptance,
    read_json_object,
    report_json,
    validate_plan,
)
from acceptance.planner import prepare_corpus_plan
from config import ConfigError, load_config
from safe_files import atomic_write_text, sha256_file


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Prepare, validate, or evaluate the fixed 100-video unattended acceptance corpus. "
            "Schema v2 also verifies committed completed-MKV receipts and final media. "
            "Preparation previews by default and never executes planned faults. Collection only "
            "writes the requested observations file; other modes are read-only unless an explicit "
            "output path is supplied."
        )
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--prepare-plan", action="store_true")
    mode.add_argument("--validate-manifest", metavar="PLAN_JSON", type=Path)
    mode.add_argument("--evaluate", metavar="PLAN_JSON", type=Path)
    mode.add_argument("--collect", metavar="PLAN_JSON", type=Path)
    parser.add_argument(
        "--observations",
        type=Path,
        help="Required with --evaluate or --collect; collection refuses overwrite",
    )
    parser.add_argument(
        "--plan-output",
        type=Path,
        help="Optional with --prepare-plan; creation is atomic and refuses overwrite",
    )
    parser.add_argument("--config", type=Path, required=True, help="Worker config used for exact policy identity")
    parser.add_argument("--report", type=Path, help="Optional report JSON path; stdout is the read-only default")
    parser.add_argument(
        "--overwrite-report",
        action="store_true",
        help="Allow replacing an existing --report file",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
        if args.prepare_plan:
            if args.observations is not None:
                raise AcceptanceInputError("--observations is not valid with --prepare-plan")
            if args.report is not None or args.overwrite_report:
                raise AcceptanceInputError("--report options are not valid with --prepare-plan")
            payload = prepare_corpus_plan(config)
            plan = payload.get("plan")
            if payload.get("ready") is not True or not isinstance(plan, dict):
                if args.plan_output is not None:
                    payload["requested_plan_output"] = str(args.plan_output)
                    payload["write_performed"] = False
                sys.stdout.write(report_json(payload))
                return 2
            if args.plan_output is None:
                sys.stdout.write(report_json(payload))
                return 0
            rendered_plan = report_json(plan)
            _write_new_plan(args.plan_output, rendered_plan)
            summary = {key: value for key, value in payload.items() if key != "plan"}
            summary.update(
                {
                    "preview": False,
                    "write_performed": True,
                    "plan_output": str(args.plan_output),
                    "plan_sha256": sha256_file(args.plan_output),
                }
            )
            sys.stdout.write(report_json(summary))
            return 0
        if args.plan_output is not None:
            raise AcceptanceInputError("--plan-output is only valid with --prepare-plan")
        if args.collect is not None:
            if args.observations is None:
                raise AcceptanceInputError("--observations is required with --collect")
            if args.report is not None or args.overwrite_report:
                raise AcceptanceInputError("--report options are not valid with --collect")
            observations = collect_observations(args.collect, config)
            _write_new_observations(args.observations, report_json(observations))
            summary = observation_summary(observations)
            sys.stdout.write(
                json.dumps(
                    {
                        "mode": "collect",
                        "readonly_sources": True,
                        "observations": str(args.observations),
                        **summary,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )
            return 0 if summary["complete"] is True else 2
        if args.validate_manifest is not None:
            if args.observations is not None:
                raise AcceptanceInputError("--observations is only valid with --evaluate")
            plan = read_json_object(args.validate_manifest)
            errors = validate_plan(plan, config)
            payload = {
                "mode": "validate-manifest",
                "readonly": True,
                "valid": not errors,
                "errors": errors,
                "plan": str(args.validate_manifest),
                "plan_sha256": sha256_file(args.validate_manifest),
            }
            exit_code = 0 if not errors else 2
        else:
            if args.observations is None:
                raise AcceptanceInputError("--observations is required with --evaluate")
            payload = evaluate_acceptance(args.evaluate, args.observations, config)
            exit_code = 0 if payload.get("qualified") is True else 2
        rendered = report_json(payload)
        if args.report is None:
            sys.stdout.write(rendered)
        else:
            if args.report.exists() and not args.overwrite_report:
                raise AcceptanceInputError(
                    f"report already exists; use --overwrite-report to replace it: {args.report}"
                )
            atomic_write_text(args.report, rendered, encoding="utf-8")
            sys.stdout.write(f"report={args.report}\n")
        return exit_code
    except (AcceptanceInputError, ConfigError, OSError, ValueError) as exc:
        sys.stderr.write(f"acceptance error: {exc}\n")
        return 2


def _write_new_observations(path: Path, content: str) -> None:
    _write_new_file(path, content, label="observations")


def _write_new_plan(path: Path, content: str) -> None:
    _write_new_file(path, content, label="plan")


def _write_new_file(path: Path, content: str, *, label: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise AcceptanceInputError(f"{label} already exists; refusing overwrite: {path}") from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
