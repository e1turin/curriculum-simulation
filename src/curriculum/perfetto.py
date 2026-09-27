"""Convert curriculum JSON into Perfetto's JSON trace-event format."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any


DEFAULT_SEMESTER_DURATION_US = 1_000_000


def _positive_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _schema_major(data: dict[str, Any]) -> int:
    version = data.get("schema_version")
    if not isinstance(version, str):
        raise ValueError("schema_version must be a string")

    try:
        return int(version.split(".", maxsplit=1)[0])
    except ValueError as exc:
        raise ValueError(f"invalid schema_version: {version!r}") from exc


def build_perfetto_trace(
    data: dict[str, Any],
    *,
    semester_duration_us: int = DEFAULT_SEMESTER_DURATION_US,
) -> dict[str, Any]:
    """Build a Perfetto-compatible JSON trace from parsed curriculum data.

    Perfetto timestamps are microseconds. Because the source describes time in
    semesters rather than wall-clock dates, ``semester_duration_us`` is a
    display scale: by default one semantic semester is represented by one
    trace second. The semantic semester values are retained in event args.
    """
    if _schema_major(data) != 1:
        raise ValueError(
            f"unsupported curriculum schema_version: {data['schema_version']!r}"
        )

    semester_duration_us = _positive_int(
        semester_duration_us,
        "semester_duration_us",
    )

    records = data.get("records")
    if not isinstance(records, list):
        raise ValueError("records must be an array")

    temporal_model = data.get("temporal_model", {})
    if not isinstance(temporal_model, dict):
        raise ValueError("temporal_model must be an object")
    if temporal_model.get("unit", "semester") != "semester":
        raise ValueError("only semester-based temporal models are supported")

    default_duration = _positive_int(
        temporal_model.get("default_item_duration", 1),
        "temporal_model.default_item_duration",
    )
    default_duration_is_inferred = temporal_model.get(
        "duration_is_inferred",
        True,
    )
    if not isinstance(default_duration_is_inferred, bool):
        raise ValueError("temporal_model.duration_is_inferred must be a boolean")

    scheduled: list[tuple[dict[str, Any], int, int, int]] = []
    seen_orders: set[int] = set()

    for index, record in enumerate(records, start=1):
        if not isinstance(record, dict):
            raise ValueError(f"records[{index - 1}] must be an object")

        semester = record.get("semester_start")
        if semester is None:
            continue

        semester = _positive_int(
            semester,
            f"records[{index - 1}].semester_start",
        )
        order = _positive_int(
            record.get("order", index),
            f"records[{index - 1}].order",
        )
        if order in seen_orders:
            raise ValueError(f"duplicate order among scheduled records: {order}")
        seen_orders.add(order)

        duration = _positive_int(
            record.get("duration_semesters", default_duration),
            f"records[{index - 1}].duration_semesters",
        )
        scheduled.append((record, semester, duration, order))

    if not scheduled:
        raise ValueError("the curriculum has no records with semester_start")

    first_semester = min(semester for _, semester, _, _ in scheduled)
    model_first_semester = temporal_model.get("first_semester")
    if model_first_semester is not None:
        model_first_semester = _positive_int(
            model_first_semester,
            "temporal_model.first_semester",
        )
        if model_first_semester > first_semester:
            raise ValueError(
                "temporal_model.first_semester is later than a scheduled record"
            )
        first_semester = model_first_semester

    document = data.get("document", {})
    if not isinstance(document, dict):
        raise ValueError("document must be an object")

    trace_events: list[dict[str, Any]] = []
    semesters = sorted({semester for _, semester, _, _ in scheduled})

    for semester in semesters:
        trace_events.extend(
            [
                {
                    "name": "process_name",
                    "ph": "M",
                    "pid": semester,
                    "tid": 0,
                    "args": {"name": f"Semester {semester}"},
                },
                {
                    "name": "process_sort_index",
                    "ph": "M",
                    "pid": semester,
                    "tid": 0,
                    "args": {"sort_index": semester},
                },
            ]
        )

    schema_version = data["schema_version"]
    seen_record_ids: set[str] = set()
    for record, semester, duration_semesters, order in scheduled:
        name = record.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError(f"scheduled record {order} must have a non-empty name")

        record_id = record.get("id", f"record-{order:04d}")
        if not isinstance(record_id, str) or not record_id:
            raise ValueError(f"scheduled record {order} must have a non-empty id")
        if record_id in seen_record_ids:
            raise ValueError(f"duplicate id among scheduled records: {record_id!r}")
        seen_record_ids.add(record_id)

        trace_events.extend(
            [
                {
                    "name": "thread_name",
                    "ph": "M",
                    "pid": semester,
                    "tid": order,
                    "args": {"name": f"{order:04d} · {name}"},
                },
                {
                    "name": "thread_sort_index",
                    "ph": "M",
                    "pid": semester,
                    "tid": order,
                    "args": {"sort_index": order},
                },
                {
                    "name": name,
                    "cat": f"curriculum,{record.get('record_type', 'unknown')}",
                    "ph": "X",
                    "pid": semester,
                    "tid": order,
                    "ts": (semester - first_semester) * semester_duration_us,
                    "dur": duration_semesters * semester_duration_us,
                    "args": {
                        "record_id": record_id,
                        "record_order": order,
                        "record_type": record.get("record_type", "unknown"),
                        "semester_start": semester,
                        "semester_end_exclusive": semester + duration_semesters,
                        "duration_semesters": duration_semesters,
                        "duration_is_inferred": (
                            "duration_semesters" not in record
                            and default_duration_is_inferred
                        ),
                        "credits": record.get("credits"),
                        "hours": record.get("hours"),
                        "source_page": record.get("source_page"),
                        "source_file": document.get("source_file"),
                        "document_title": document.get("title"),
                        "program": document.get("program"),
                        "language": document.get("language"),
                        "curriculum_schema_version": schema_version,
                    },
                },
            ]
        )

    return {
        "traceEvents": trace_events,
        "displayTimeUnit": "ms",
        "otherData": {
            "curriculum": {
                "schema_version": schema_version,
                "program": document.get("program"),
                "source_file": document.get("source_file"),
                "semantic_time_unit": "semester",
                "first_semester": first_semester,
                "semester_duration_us": semester_duration_us,
                "scheduled_record_count": len(scheduled),
                "unscheduled_record_count": len(records) - len(scheduled),
            }
        },
    }


def convert_file(
    input_path: pathlib.Path,
    output_path: pathlib.Path,
    *,
    semester_duration_us: int = DEFAULT_SEMESTER_DURATION_US,
) -> dict[str, Any]:
    """Read curriculum JSON, write a Perfetto JSON trace, and return it."""
    try:
        data = json.loads(input_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in {input_path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError("curriculum JSON root must be an object")

    trace = build_perfetto_trace(
        data,
        semester_duration_us=semester_duration_us,
    )
    output_path.write_text(
        json.dumps(trace, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return trace


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert curriculum JSON to a Perfetto JSON trace.",
    )
    parser.add_argument("input", type=pathlib.Path, help="Input curriculum JSON")
    parser.add_argument(
        "-o",
        "--output",
        type=pathlib.Path,
        help="Output trace (default: <input>.perfetto.json)",
    )
    parser.add_argument(
        "--semester-duration-us",
        type=int,
        default=DEFAULT_SEMESTER_DURATION_US,
        help=(
            "Perfetto display scale in microseconds per semester "
            f"(default: {DEFAULT_SEMESTER_DURATION_US})"
        ),
    )
    args = parser.parse_args()

    input_path = args.input.resolve()
    output_path = (
        args.output.resolve()
        if args.output
        else input_path.with_name(f"{input_path.stem}.perfetto.json")
    )

    if not input_path.is_file():
        parser.error(f"input file does not exist: {input_path}")
    if input_path == output_path:
        parser.error("input and output paths must be different")

    try:
        trace = convert_file(
            input_path,
            output_path,
            semester_duration_us=args.semester_duration_us,
        )
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    metadata = trace["otherData"]["curriculum"]
    print(f"Input:       {input_path}")
    print(f"Output:      {output_path}")
    print(f"Slices:      {metadata['scheduled_record_count']}")
    print(f"Unscheduled: {metadata['unscheduled_record_count']}")


if __name__ == "__main__":
    main()
