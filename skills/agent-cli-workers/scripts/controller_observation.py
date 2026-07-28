#!/usr/bin/env python3
"""Privacy-minimized controller-side routing observations."""

import argparse
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import uuid


SCHEMA_VERSION = 1
ROUTES = ("direct", "grok", "codex")
TASK_CLASSES = (
    "unknown",
    "research",
    "investigation",
    "review",
    "implementation",
    "test-repair",
    "refactor",
    "other",
)
WAIT_MODES = ("none", "blocking", "parallel")
PARALLEL_WORK_CLASSES = (
    "none",
    "implementation",
    "review",
    "research",
    "testing",
    "integration",
    "other",
)
VERIFICATION_OUTCOMES = ("passed", "failed", "not-run")
DECISION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
DB_COLUMNS = (
    "decision_id",
    "requested_at",
    "task_class",
    "predicted_route",
    "predicted_direct_seconds",
    "wait_mode",
    "parallel_work_class",
    "verified_at",
    "actual_route",
    "verification",
    "rework_count",
)


class ObservationError(Exception):
    def __init__(self, error: str, message: str, exit_code: int = 2):
        super().__init__(message)
        self.error = error
        self.message = message
        self.exit_code = exit_code


def fail(error: str, message: str, exit_code: int = 2) -> None:
    raise ObservationError(error, message, exit_code)


def emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=True, sort_keys=True))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_utc(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None


def sql_values(values: tuple[str, ...]) -> str:
    return ", ".join(repr(value) for value in values)


def observation_root() -> Path:
    override = os.environ.get("AGENT_CLI_WORKERS_OBSERVATION_DIR")
    if override:
        candidate = Path(override).expanduser()
    else:
        home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        candidate = home / "state" / "agent-cli-workers" / "observations"
    if candidate.is_symlink():
        fail("unsafe_observation_dir", "observation directory must not be a symlink", 5)
    try:
        candidate.mkdir(parents=True, exist_ok=True, mode=0o700)
        root = candidate.resolve()
        info = root.stat()
    except OSError as exc:
        fail("observation_dir_unavailable", f"cannot initialize observation directory: {exc}", 5)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        fail("unsafe_observation_dir", "observation directory must be owned by this user", 5)
    if stat.S_IMODE(info.st_mode) & 0o077:
        fail("insecure_observation_dir", "observation directory is accessible to other users", 5)
    return root


def database_path(root: Path) -> Path:
    path = root / "observations.sqlite3"
    if path.is_symlink():
        fail("unsafe_observation_path", "observation database must not be a symlink", 5)
    if path.exists():
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            fail("unsafe_observation_path", "observation database must be an owned regular file", 5)
        if stat.S_IMODE(info.st_mode) & 0o077:
            fail("insecure_observation_path", "observation database is accessible to other users", 5)
    return path


def open_database() -> sqlite3.Connection:
    path = database_path(observation_root())
    try:
        connection = sqlite3.connect(path, timeout=5)
        connection.row_factory = sqlite3.Row
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            connection.executescript(
                f"""
                CREATE TABLE observations (
                    decision_id TEXT PRIMARY KEY,
                    requested_at TEXT NOT NULL,
                    task_class TEXT NOT NULL CHECK (task_class IN ({sql_values(TASK_CLASSES)})),
                    predicted_route TEXT NOT NULL CHECK (predicted_route IN ({sql_values(ROUTES)})),
                    predicted_direct_seconds REAL CHECK (predicted_direct_seconds > 0),
                    wait_mode TEXT NOT NULL CHECK (wait_mode IN ({sql_values(WAIT_MODES)})),
                    parallel_work_class TEXT NOT NULL CHECK (parallel_work_class IN ({sql_values(PARALLEL_WORK_CLASSES)})),
                    verified_at TEXT,
                    actual_route TEXT CHECK (actual_route IN ({sql_values(ROUTES)})),
                    verification TEXT CHECK (verification IN ({sql_values(VERIFICATION_OUTCOMES)})),
                    rework_count INTEGER CHECK (rework_count >= 0),
                    CHECK (
                        (predicted_route = 'direct' AND wait_mode = 'none') OR
                        (predicted_route != 'direct' AND wait_mode IN ('blocking', 'parallel'))
                    ),
                    CHECK (
                        (wait_mode = 'parallel' AND parallel_work_class != 'none') OR
                        (wait_mode != 'parallel' AND parallel_work_class = 'none')
                    ),
                    CHECK (
                        (verified_at IS NULL AND actual_route IS NULL AND verification IS NULL AND rework_count IS NULL) OR
                        (verified_at IS NOT NULL AND actual_route IS NOT NULL AND verification IS NOT NULL AND rework_count IS NOT NULL)
                    )
                );
                PRAGMA user_version = {SCHEMA_VERSION};
                """
            )
        elif version != SCHEMA_VERSION:
            connection.close()
            fail("observation_schema_unsupported", "observation database schema is unsupported", 5)
        database_path(path.parent)
        return connection
    except (OSError, sqlite3.DatabaseError) as exc:
        fail("observation_database_unavailable", f"cannot open observation database: {exc}", 5)


def validate_decision_id(decision_id: str) -> None:
    if not DECISION_ID_RE.fullmatch(decision_id):
        fail("invalid_decision_id", "decision id contains unsafe characters")


def validate_begin(args: argparse.Namespace) -> None:
    if (args.predicted_route == "direct") != (args.wait_mode == "none"):
        fail("invalid_wait_mode", "direct requires wait-mode=none; delegation does not")
    if args.wait_mode == "parallel" and args.parallel_work_class == "none":
        fail("parallel_work_required", "parallel delegation must name its parallel work class")
    if args.wait_mode != "parallel" and args.parallel_work_class != "none":
        fail("unexpected_parallel_work", "parallel work is valid only for parallel waiting")
    predicted = args.predicted_direct_seconds
    if predicted is not None and (not math.isfinite(predicted) or predicted <= 0):
        fail("invalid_predicted_direct_seconds", "predicted direct seconds must be positive and finite")


def validate_row(row: sqlite3.Row) -> None:
    requested = parse_utc(row["requested_at"])
    predicted = row["predicted_direct_seconds"]
    valid_prediction = predicted is None or (
        isinstance(predicted, (int, float)) and math.isfinite(predicted) and predicted > 0
    )
    valid_wait = (row["predicted_route"] == "direct" and row["wait_mode"] == "none") or (
        row["predicted_route"] in {"grok", "codex"} and row["wait_mode"] in {"blocking", "parallel"}
    )
    valid_work = (row["wait_mode"] == "parallel" and row["parallel_work_class"] != "none") or (
        row["wait_mode"] != "parallel" and row["parallel_work_class"] == "none"
    )
    valid_enums = (
        row["task_class"] in TASK_CLASSES
        and row["predicted_route"] in ROUTES
        and row["wait_mode"] in WAIT_MODES
        and row["parallel_work_class"] in PARALLEL_WORK_CLASSES
    )
    complete = row["verified_at"] is not None
    valid_completion = (
        not complete
        and row["actual_route"] is None
        and row["verification"] is None
        and row["rework_count"] is None
    ) or (
        complete
        and row["actual_route"] in ROUTES
        and row["verification"] in VERIFICATION_OUTCOMES
        and isinstance(row["rework_count"], int)
        and row["rework_count"] >= 0
    )
    if (
        requested is None
        or not valid_enums
        or not valid_prediction
        or not valid_wait
        or not valid_work
        or not valid_completion
    ):
        fail("observation_corrupt", f"observation {row['decision_id']!r} is corrupt", 5)
    if complete and parse_utc(row["verified_at"]) is None:
        fail("observation_corrupt", f"observation {row['decision_id']!r} is corrupt", 5)


def public_record(row: sqlite3.Row) -> dict:
    validate_row(row)
    record = {key: row[key] for key in DB_COLUMNS}
    if row["verified_at"] is None:
        record["state"] = "open"
        return record
    requested = parse_utc(row["requested_at"])
    verified = parse_utc(row["verified_at"])
    assert requested is not None and verified is not None
    record["state"] = "complete"
    record["end_to_end_seconds"] = max(0.0, round((verified - requested).total_seconds(), 6))
    return record


def cmd_begin(args: argparse.Namespace) -> int:
    validate_begin(args)
    decision_id = args.decision_id or f"decision-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:8]}"
    validate_decision_id(decision_id)
    connection = open_database()
    try:
        with connection:
            connection.execute(
                """INSERT INTO observations
                   (decision_id, requested_at, task_class, predicted_route,
                    predicted_direct_seconds, wait_mode, parallel_work_class)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    decision_id,
                    utc_now(),
                    args.task_class,
                    args.predicted_route,
                    args.predicted_direct_seconds,
                    args.wait_mode,
                    args.parallel_work_class,
                ),
            )
        row = connection.execute("SELECT * FROM observations WHERE decision_id = ?", (decision_id,)).fetchone()
    except sqlite3.IntegrityError:
        fail("observation_exists", f"observation {decision_id!r} already exists", 4)
    finally:
        connection.close()
    emit(public_record(row))
    return 0


def cmd_finish(args: argparse.Namespace) -> int:
    validate_decision_id(args.decision_id)
    if args.rework_count < 0:
        fail("invalid_rework_count", "rework count must be non-negative")
    connection = open_database()
    try:
        with connection:
            row = connection.execute("SELECT * FROM observations WHERE decision_id = ?", (args.decision_id,)).fetchone()
            if row is None:
                fail("observation_not_found", f"observation {args.decision_id!r} was not found", 4)
            validate_row(row)
            if row["verified_at"] is not None:
                fail("observation_already_finished", f"observation {args.decision_id!r} is complete", 4)
            connection.execute(
                """UPDATE observations
                   SET verified_at = ?, actual_route = ?, verification = ?, rework_count = ?
                   WHERE decision_id = ?""",
                (utc_now(), args.actual_route, args.verification, args.rework_count, args.decision_id),
            )
        row = connection.execute("SELECT * FROM observations WHERE decision_id = ?", (args.decision_id,)).fetchone()
    finally:
        connection.close()
    emit(public_record(row))
    return 0


def percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower, upper = math.floor(position), math.ceil(position)
    value = ordered[lower]
    if lower != upper:
        value += (ordered[upper] - value) * (position - lower)
    return round(value, 6)


def duration_summary(values: list[float]) -> dict:
    return {"count": len(values), "p50_seconds": percentile(values, 0.5), "p90_seconds": percentile(values, 0.9)}


def increment(mapping: dict, value: str) -> None:
    mapping[value] = mapping.get(value, 0) + 1


def cmd_report(args: argparse.Namespace) -> int:
    connection = open_database()
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=args.since_days)).isoformat().replace("+00:00", "Z")
        rows = connection.execute(
            "SELECT * FROM observations WHERE requested_at >= ? ORDER BY requested_at",
            (cutoff,),
        ).fetchall()
    finally:
        connection.close()
    report = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": utc_now(),
        "since_days": args.since_days,
        "observations": len(rows),
        "completed": 0,
        "open": 0,
        "corrupt_observations": 0,
        "by_task_class": {},
        "by_predicted_route": {},
        "by_actual_route": {},
        "by_verification": {},
        "by_wait_mode": {},
        "by_parallel_work_class": {},
        "prediction": {"denominator": "completed", "matched": 0, "mismatched": 0},
        "rework": {"denominator": "completed", "total": 0, "with_rework": 0},
    }
    durations = []
    route_durations = {route: [] for route in ROUTES}
    wait_durations = {wait: [] for wait in WAIT_MODES}
    direct_prediction_errors = []
    for row in rows:
        try:
            record = public_record(row)
        except ObservationError:
            report["corrupt_observations"] += 1
            continue
        for key, field in (
            ("by_task_class", "task_class"),
            ("by_predicted_route", "predicted_route"),
            ("by_wait_mode", "wait_mode"),
            ("by_parallel_work_class", "parallel_work_class"),
        ):
            increment(report[key], record[field])
        if record["state"] == "open":
            report["open"] += 1
            continue
        report["completed"] += 1
        increment(report["by_actual_route"], record["actual_route"])
        increment(report["by_verification"], record["verification"])
        prediction_key = "matched" if record["predicted_route"] == record["actual_route"] else "mismatched"
        report["prediction"][prediction_key] += 1
        report["rework"]["total"] += record["rework_count"]
        report["rework"]["with_rework"] += int(record["rework_count"] > 0)
        duration = record["end_to_end_seconds"]
        durations.append(duration)
        route_durations[record["actual_route"]].append(duration)
        wait_durations[record["wait_mode"]].append(duration)
        predicted = record["predicted_direct_seconds"]
        if record["actual_route"] == "direct" and predicted is not None:
            direct_prediction_errors.append(abs(duration - predicted))
    completed = report["completed"]
    report["prediction"]["match_rate"] = round(report["prediction"]["matched"] / completed, 6) if completed else None
    report["rework"]["rate"] = round(report["rework"]["with_rework"] / completed, 6) if completed else None
    report["end_to_end"] = duration_summary(durations)
    report["duration_by_actual_route"] = {key: duration_summary(value) for key, value in route_durations.items() if value}
    report["duration_by_wait_mode"] = {key: duration_summary(value) for key, value in wait_durations.items() if value}
    errors = duration_summary(direct_prediction_errors)
    report["direct_prediction_error"] = {
        "count": errors["count"],
        "p50_absolute_error_seconds": errors["p50_seconds"],
        "p90_absolute_error_seconds": errors["p90_seconds"],
    }
    emit(report)
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Record controller routing observations")
    commands = root.add_subparsers(dest="command", required=True)
    begin = commands.add_parser("begin")
    begin.add_argument("--decision-id")
    begin.add_argument("--task-class", required=True, choices=TASK_CLASSES)
    begin.add_argument("--predicted-route", required=True, choices=ROUTES)
    begin.add_argument("--predicted-direct-seconds", type=float)
    begin.add_argument("--wait-mode", required=True, choices=WAIT_MODES)
    begin.add_argument("--parallel-work-class", choices=PARALLEL_WORK_CLASSES, default="none")
    begin.set_defaults(func=cmd_begin)
    finish = commands.add_parser("finish")
    finish.add_argument("decision_id")
    finish.add_argument("--actual-route", required=True, choices=ROUTES)
    finish.add_argument("--verification", required=True, choices=VERIFICATION_OUTCOMES)
    finish.add_argument("--rework-count", type=int, default=0)
    finish.set_defaults(func=cmd_finish)
    report = commands.add_parser("report")
    report.add_argument("--since-days", type=float, default=30)
    report.set_defaults(func=cmd_report)
    return root


def main() -> int:
    os.umask(0o077)
    try:
        args = parser().parse_args()
        if hasattr(args, "since_days") and (not math.isfinite(args.since_days) or args.since_days < 0):
            fail("invalid_since_days", "since-days must be finite and non-negative")
        return args.func(args)
    except ObservationError as exc:
        emit({"error": exc.error, "message": exc.message})
        return exc.exit_code
    except KeyboardInterrupt:
        emit({"error": "interrupted", "message": "operation interrupted"})
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
