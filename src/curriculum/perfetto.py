"""Convert curriculum JSON into a native Perfetto protobuf trace."""

import argparse
import datetime
import json
import pathlib
import sys
import uuid
from dataclasses import dataclass
from itertools import pairwise

from perfetto.protos.perfetto.trace.perfetto_trace_pb2 import (
    BUILTIN_CLOCK_REALTIME,
    BUILTIN_CLOCK_TRACE_FILE,
    DebugAnnotation,
    TrackDescriptor,
    TrackEvent,
)
from perfetto.trace_builder.proto_builder import TraceProtoBuilder

from curriculum.utils import require, require_type

type JsonObject = dict[str, object]
type RecordReference = tuple[JsonObject, int, str]

DEFAULT_SEMESTER_DURATION_NS = 1_000_000_000
NANOSECONDS_PER_SECOND = 1_000_000_000
TRUSTED_PACKET_SEQUENCE_ID = 1
_TRACK_UUID_NAMESPACE = uuid.UUID("28ad0a98-216d-5d42-91c9-5cbbb5609db7")


@dataclass(frozen=True, slots=True)
class PerfettoTraceBuild:
    """Serialized native trace and conversion statistics."""

    data: bytes
    scheduled_record_count: int
    unscheduled_record_count: int


@dataclass(frozen=True, slots=True)
class ScheduledRecord:
    """Validated timeline data and its document grouping context."""

    record: JsonObject
    record_id: str
    order: int
    name: str
    semester: int
    duration_semesters: int
    block_id: str | None
    block_name: str | None
    group_id: str | None
    group_name: str | None


def _positive_int(value: object, field: str) -> int:
    match value:
        case int() if not isinstance(value, bool) and value > 0:
            return value
        case _:
            raise ValueError(f"{field} must be a positive integer")


def _non_empty_string(value: object, field: str) -> str:
    match value:
        case str() if value:
            return value
        case _:
            raise ValueError(f"{field} must be a non-empty string")


def _object(value: object, field: str) -> JsonObject:
    return require_type(value, dict, f"{field} must be an object")


def _schema_version(data: JsonObject) -> str:
    version = _non_empty_string(data.get("schema_version"), "schema_version")
    try:
        major = int(version.split(".", maxsplit=1)[0])
    except ValueError as exc:
        raise ValueError(f"invalid schema_version: {version!r}") from exc
    require(major == 1, f"unsupported curriculum schema_version: {version!r}")
    return version


def _track_uuid(kind: str, identifier: object) -> int:
    value = uuid.uuid5(_TRACK_UUID_NAMESPACE, f"{kind}:{identifier}").int
    return value & ((1 << 63) - 1)


def _set_annotation_value(annotation: DebugAnnotation, value: object) -> None:
    match value:
        case bool():
            annotation.bool_value = value
        case int():
            annotation.int_value = value
        case float():
            annotation.double_value = value
        case str():
            annotation.string_value = value
        case _:
            annotation.legacy_json_value = json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
            )


def _add_annotations(event: TrackEvent, values: dict[str, object]) -> None:
    for name, value in values.items():
        if value is None:
            continue
        annotation = event.debug_annotations.add()
        annotation.name = name
        _set_annotation_value(annotation, value)


def build_perfetto_trace(
    data: JsonObject,
    *,
    semester_duration_ns: int = DEFAULT_SEMESTER_DURATION_NS,
    academic_start_year: int | None = None,
) -> PerfettoTraceBuild:
    """Build a native Perfetto TrackEvent trace from curriculum data.

    By default, semantic semesters use a compact uniform display scale. When
    ``academic_start_year`` is supplied, semesters use calendar boundaries and
    the trace clock is correlated with UTC wall time.
    """
    schema_version = _schema_version(data)
    semester_duration_ns = _positive_int(
        semester_duration_ns,
        "semester_duration_ns",
    )
    if academic_start_year is not None:
        academic_start_year = _positive_int(
            academic_start_year,
            "academic_start_year",
        )
        require(
            academic_start_year >= 1970,
            "academic_start_year must be 1970 or later",
        )

    records = require_type(data.get("records"), list, "records must be an array")
    temporal_model = _object(data.get("temporal_model", {}), "temporal_model")
    require(
        temporal_model.get("unit", "semester") == "semester",
        "only semester-based temporal models are supported",
    )
    default_duration = _positive_int(
        temporal_model.get("default_item_duration", 1),
        "temporal_model.default_item_duration",
    )
    default_duration_is_inferred = temporal_model.get(
        "duration_is_inferred",
        True,
    )
    if not isinstance(default_duration_is_inferred, bool):
        raise TypeError("temporal_model.duration_is_inferred must be a boolean")

    identified_records: list[tuple[JsonObject, str, int, str]] = []
    records_by_id: dict[str, RecordReference] = {}
    seen_orders: set[int] = set()
    for index, raw_record in enumerate(records, start=1):
        record = _object(raw_record, f"records[{index - 1}]")
        order = _positive_int(
            record.get("order", index),
            f"records[{index - 1}].order",
        )
        require(order not in seen_orders, f"duplicate record order: {order}")
        seen_orders.add(order)

        record_id = _non_empty_string(
            record.get("id", f"record-{order:04d}"),
            f"record {order} id",
        )
        require(record_id not in records_by_id, f"duplicate record id: {record_id!r}")

        name = _non_empty_string(record.get("name"), f"record {order} name")
        identified_records.append((record, record_id, order, name))
        records_by_id[record_id] = (record, order, name)

    def resolve_reference(
        record: JsonObject,
        field: str,
        fallback: str | None,
        expected_type: str,
        order: int,
    ) -> tuple[str | None, str | None]:
        reference = record.get(field, fallback)
        if reference is None:
            return None, None
        reference = _non_empty_string(reference, f"record {order} {field}")
        require(
            reference in records_by_id,
            f"record {order} references unknown {field}: {reference!r}",
        )
        referenced_record, _, referenced_name = records_by_id[reference]
        require(
            referenced_record.get("record_type") == expected_type,
            f"record {order} {field} must reference a {expected_type} record",
        )
        return reference, referenced_name

    scheduled: list[ScheduledRecord] = []
    active_block_id: str | None = None
    active_group_id: str | None = None
    for record, record_id, order, name in identified_records:
        semester = record.get("semester_start")
        record_type = record.get("record_type", "unknown")
        if semester is None:
            if record_type == "block":
                active_block_id = record_id
                active_group_id = None
            else:
                active_group_id = record_id
            continue

        semester = _positive_int(semester, f"record {order} semester_start")
        duration = _positive_int(
            record.get("duration_semesters", default_duration),
            f"record {order} duration_semesters",
        )
        block_id, block_name = resolve_reference(
            record,
            "block_id",
            active_block_id,
            "block",
            order,
        )
        group_id, group_name = resolve_reference(
            record,
            "group_id",
            active_group_id,
            "group_or_requirement",
            order,
        )
        scheduled.append(
            ScheduledRecord(
                record=record,
                record_id=record_id,
                order=order,
                name=name,
                semester=semester,
                duration_semesters=duration,
                block_id=block_id,
                block_name=block_name,
                group_id=group_id,
                group_name=group_name,
            )
        )

    require(scheduled, "the curriculum has no records with semester_start")

    first_semester = min(item.semester for item in scheduled)
    model_first_semester = temporal_model.get("first_semester")
    if model_first_semester is not None:
        model_first_semester = _positive_int(
            model_first_semester,
            "temporal_model.first_semester",
        )
        require(
            model_first_semester <= first_semester,
            "temporal_model.first_semester is later than a scheduled record",
        )
        first_semester = model_first_semester

    last_semester = max(
        item.semester + item.duration_semesters - 1 for item in scheduled
    )
    model_last_semester = temporal_model.get("last_semester")
    if model_last_semester is not None:
        model_last_semester = _positive_int(
            model_last_semester,
            "temporal_model.last_semester",
        )
        last_semester = max(last_semester, model_last_semester)

    document = _object(data.get("document", {}), "document")
    unscheduled_count = len(records) - len(scheduled)

    academic_epoch: datetime.date | None = None
    if academic_start_year is not None:
        academic_epoch = datetime.date(academic_start_year, 9, 1)

    def semester_dates(semester: int) -> tuple[datetime.date, datetime.date]:
        assert academic_start_year is not None
        academic_year = academic_start_year + (semester - 1) // 2
        if semester % 2 == 1:
            return (
                datetime.date(academic_year, 9, 1),
                datetime.date(academic_year + 1, 2, 1),
            )
        return (
            datetime.date(academic_year + 1, 2, 1),
            datetime.date(academic_year + 1, 7, 1),
        )

    def date_timestamp_ns(value: datetime.date) -> int:
        assert academic_epoch is not None
        return (value - academic_epoch).days * 86_400 * NANOSECONDS_PER_SECOND

    def semester_range_ns(semester: int, duration: int = 1) -> tuple[int, int]:
        if academic_epoch is None:
            start_ns = (semester - first_semester) * semester_duration_ns
            return start_ns, start_ns + duration * semester_duration_ns
        start_date, _ = semester_dates(semester)
        _, end_date = semester_dates(semester + duration - 1)
        return date_timestamp_ns(start_date), date_timestamp_ns(end_date)

    builder = TraceProtoBuilder()
    if academic_epoch is not None:
        clock_packet = builder.add_packet()
        clock_snapshot = clock_packet.clock_snapshot
        clock_snapshot.primary_trace_clock = BUILTIN_CLOCK_TRACE_FILE
        trace_clock = clock_snapshot.clocks.add()
        trace_clock.clock_id = BUILTIN_CLOCK_TRACE_FILE
        trace_clock.timestamp = 0
        realtime_clock = clock_snapshot.clocks.add()
        realtime_clock.clock_id = BUILTIN_CLOCK_REALTIME
        realtime_clock.timestamp = (
            int(
                datetime.datetime(
                    academic_epoch.year,
                    academic_epoch.month,
                    academic_epoch.day,
                    tzinfo=datetime.UTC,
                ).timestamp()
            )
            * NANOSECONDS_PER_SECOND
        )
    used_track_uuids: set[int] = set()

    def add_track_descriptor(
        track_uuid: int,
        *,
        name: str,
        description: str,
        parent_uuid: int | None = None,
        order: int | None = None,
        explicit_child_ordering: bool = False,
    ) -> None:
        require(track_uuid != 0, "generated track UUID must not be zero")
        require(
            track_uuid not in used_track_uuids,
            f"generated duplicate track UUID: {track_uuid}",
        )
        used_track_uuids.add(track_uuid)

        descriptor = builder.add_packet().track_descriptor
        descriptor.uuid = track_uuid
        descriptor.name = name
        descriptor.description = description
        descriptor.sibling_merge_behavior = TrackDescriptor.SIBLING_MERGE_BEHAVIOR_NONE
        if parent_uuid is not None:
            descriptor.parent_uuid = parent_uuid
        if order is not None:
            descriptor.sibling_order_rank = order
        if explicit_child_ordering:
            descriptor.child_ordering = TrackDescriptor.EXPLICIT

    def add_slice(
        *,
        track_uuid: int,
        start_ns: int,
        end_ns: int,
        name: str,
        categories: list[str],
        annotations: dict[str, object],
        correlation_id: str | None = None,
    ) -> None:
        begin_packet = builder.add_packet()
        begin_packet.timestamp = start_ns
        if academic_epoch is not None:
            begin_packet.timestamp_clock_id = BUILTIN_CLOCK_TRACE_FILE
        begin_packet.trusted_packet_sequence_id = TRUSTED_PACKET_SEQUENCE_ID
        begin_event = begin_packet.track_event
        begin_event.type = TrackEvent.TYPE_SLICE_BEGIN
        begin_event.track_uuid = track_uuid
        begin_event.name = name
        begin_event.categories.extend(categories)
        if correlation_id is not None:
            begin_event.correlation_id_str = correlation_id
        _add_annotations(begin_event, annotations)

        end_packet = builder.add_packet()
        end_packet.timestamp = end_ns
        if academic_epoch is not None:
            end_packet.timestamp_clock_id = BUILTIN_CLOCK_TRACE_FILE
        end_packet.trusted_packet_sequence_id = TRUSTED_PACKET_SEQUENCE_ID
        end_event = end_packet.track_event
        end_event.type = TrackEvent.TYPE_SLICE_END
        end_event.track_uuid = track_uuid

    metadata_track_uuid = _track_uuid("metadata", schema_version)
    add_track_descriptor(
        metadata_track_uuid,
        name="Curriculum metadata",
        description="Metadata for the source curriculum and this trace export.",
        order=0,
    )
    metadata_packet = builder.add_packet()
    metadata_packet.timestamp = 0
    if academic_epoch is not None:
        metadata_packet.timestamp_clock_id = BUILTIN_CLOCK_TRACE_FILE
    metadata_packet.trusted_packet_sequence_id = TRUSTED_PACKET_SEQUENCE_ID
    metadata_event = metadata_packet.track_event
    metadata_event.type = TrackEvent.TYPE_INSTANT
    metadata_event.track_uuid = metadata_track_uuid
    metadata_event.name = "Curriculum export"
    metadata_event.categories.append("curriculum.metadata")
    _add_annotations(
        metadata_event,
        {
            "curriculum_schema_version": schema_version,
            "document_title": document.get("title"),
            "program": document.get("program"),
            "source_file": document.get("source_file"),
            "language": document.get("language"),
            "semantic_time_unit": "semester",
            "timeline_model": (
                "academic_calendar"
                if academic_epoch is not None
                else "uniform_semesters"
            ),
            "first_semester": first_semester,
            "last_semester": last_semester,
            "semester_duration_ns": (
                semester_duration_ns if academic_epoch is None else None
            ),
            "academic_start_date": (
                academic_epoch.isoformat() if academic_epoch is not None else None
            ),
            "academic_end_date": (
                (
                    semester_dates(last_semester)[1] - datetime.timedelta(days=1)
                ).isoformat()
                if academic_epoch is not None
                else None
            ),
            "scheduled_record_count": len(scheduled),
            "unscheduled_record_count": unscheduled_count,
            "document_group_count": len(
                {item.group_id for item in scheduled if item.group_id is not None}
            ),
        },
    )

    semester_schedule_uuid = _track_uuid("semester-schedule", schema_version)
    add_track_descriptor(
        semester_schedule_uuid,
        name="Semesters",
        description="Sequential windows mapping trace time to academic semesters.",
        order=1,
    )
    for semester in range(first_semester, last_semester + 1):
        start_ns, end_ns = semester_range_ns(semester)
        calendar_start = calendar_end = None
        if academic_epoch is not None:
            start_date, end_date_exclusive = semester_dates(semester)
            calendar_start = start_date.isoformat()
            calendar_end = (end_date_exclusive - datetime.timedelta(days=1)).isoformat()
        add_slice(
            track_uuid=semester_schedule_uuid,
            start_ns=start_ns,
            end_ns=end_ns,
            name=f"Semester {semester}",
            categories=["curriculum.semester"],
            annotations={
                "semester": semester,
                "semester_start": semester,
                "semester_end_exclusive": semester + 1,
                "calendar_start_date": calendar_start,
                "calendar_end_date": calendar_end,
            },
        )

    block_track_uuids: dict[str, int] = {}
    block_ids = list(
        dict.fromkeys(item.block_id for item in scheduled if item.block_id is not None)
    )
    for block_id in block_ids:
        _, block_order, block_name = records_by_id[block_id]
        track_uuid = _track_uuid("block", block_id)
        block_track_uuids[block_id] = track_uuid
        add_track_descriptor(
            track_uuid,
            name=block_name,
            description=f"Document block {block_id}.",
            order=100 + block_order,
            explicit_child_ordering=True,
        )

    group_track_uuids: dict[str, int] = {}
    group_block_ids: dict[str, str | None] = {}
    for item in scheduled:
        if item.group_id is not None:
            group_block_ids.setdefault(item.group_id, item.block_id)
    for group_id, block_id in group_block_ids.items():
        _, group_order, group_name = records_by_id[group_id]
        track_uuid = _track_uuid("group", group_id)
        group_track_uuids[group_id] = track_uuid
        add_track_descriptor(
            track_uuid,
            name=group_name,
            description=f"Document group {group_id}.",
            parent_uuid=(block_track_uuids[block_id] if block_id is not None else None),
            order=group_order,
            explicit_child_ordering=True,
        )

    # A repeated name inside one document group represents one subject spread
    # across semesters. Different names may be concurrent choices in broad groups.
    candidate_tracks: dict[tuple[str, str], list[ScheduledRecord]] = {}
    course_tracks: list[list[ScheduledRecord]] = []
    for item in scheduled:
        if item.group_id is None:
            course_tracks.append([item])
            continue
        key = (item.group_id, item.name)
        if key not in candidate_tracks:
            candidate_tracks[key] = []
            course_tracks.append(candidate_tracks[key])
        candidate_tracks[key].append(item)

    non_overlapping_tracks: list[list[ScheduledRecord]] = []
    for items in course_tracks:
        timeline_order = sorted(items, key=lambda item: (item.semester, item.order))
        has_overlap = any(
            current.semester < previous.semester + previous.duration_semesters
            for previous, current in pairwise(timeline_order)
        )
        if has_overlap:
            non_overlapping_tracks.extend([item] for item in items)
        else:
            non_overlapping_tracks.append(items)

    for items in non_overlapping_tracks:
        first_item = items[0]
        timeline_order = sorted(items, key=lambda item: (item.semester, item.order))
        record_ids = [item.record_id for item in items]
        track_uuid = (
            _track_uuid("record", record_ids[0])
            if len(record_ids) == 1
            else _track_uuid("records", ",".join(record_ids))
        )
        parent_uuid = None
        if first_item.group_id is not None:
            parent_uuid = group_track_uuids[first_item.group_id]
        elif first_item.block_id is not None:
            parent_uuid = block_track_uuids[first_item.block_id]

        order_label = "/".join(f"{item.order:04d}" for item in items)
        source_pages = dict.fromkeys(
            item.record.get("source_page", "unknown") for item in items
        )
        add_track_descriptor(
            track_uuid,
            name=f"{order_label} · {first_item.name}",
            description=(
                f"Source records {', '.join(record_ids)}; "
                f"pages {', '.join(map(str, source_pages))}."
            ),
            parent_uuid=parent_uuid,
            order=first_item.order,
        )

        for item in timeline_order:
            record_type = item.record.get("record_type", "unknown")
            if not isinstance(record_type, str):
                record_type = str(record_type)

            start_ns, end_ns = semester_range_ns(
                item.semester,
                item.duration_semesters,
            )
            add_slice(
                track_uuid=track_uuid,
                start_ns=start_ns,
                end_ns=end_ns,
                name=item.name,
                categories=["curriculum", record_type],
                correlation_id=item.record_id,
                annotations={
                    "record_id": item.record_id,
                    "record_order": item.order,
                    "record_type": record_type,
                    "block_id": item.block_id,
                    "block_name": item.block_name,
                    "group_id": item.group_id,
                    "group_name": item.group_name,
                    "semester_start": item.semester,
                    "semester_end_exclusive": (item.semester + item.duration_semesters),
                    "duration_semesters": item.duration_semesters,
                    "duration_is_inferred": (
                        "duration_semesters" not in item.record
                        and default_duration_is_inferred
                    ),
                    "credits": item.record.get("credits"),
                    "hours": item.record.get("hours"),
                    "source_page": item.record.get("source_page"),
                    "source_file": document.get("source_file"),
                    "document_title": document.get("title"),
                    "program": document.get("program"),
                    "language": document.get("language"),
                    "curriculum_schema_version": schema_version,
                },
            )

    return PerfettoTraceBuild(
        data=builder.serialize(),
        scheduled_record_count=len(scheduled),
        unscheduled_record_count=unscheduled_count,
    )


def convert_file(
    input_path: pathlib.Path,
    output_path: pathlib.Path,
    *,
    semester_duration_ns: int = DEFAULT_SEMESTER_DURATION_NS,
    academic_start_year: int | None = None,
) -> PerfettoTraceBuild:
    """Read curriculum JSON and write a native Perfetto ``.pftrace`` file."""
    try:
        data = json.loads(input_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in {input_path}: {exc}") from exc

    require(isinstance(data, dict), "curriculum JSON root must be an object")
    result = build_perfetto_trace(
        data,
        semester_duration_ns=semester_duration_ns,
        academic_start_year=academic_start_year,
    )
    output_path.write_bytes(result.data)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert curriculum JSON to a native Perfetto trace.",
    )
    parser.add_argument("input", type=pathlib.Path, help="Input curriculum JSON")
    parser.add_argument(
        "-o",
        "--output",
        type=pathlib.Path,
        help="Output trace (default: <input>.pftrace)",
    )
    parser.add_argument(
        "--semester-duration-ns",
        type=int,
        default=DEFAULT_SEMESTER_DURATION_NS,
        help=(
            "Perfetto display scale in nanoseconds per semester "
            f"(default: {DEFAULT_SEMESTER_DURATION_NS})"
        ),
    )
    parser.add_argument(
        "--academic-start-year",
        type=int,
        help=(
            "Map semesters to an academic calendar beginning September 1 of "
            "this year; spring semesters end June 30"
        ),
    )
    args = parser.parse_args()

    input_path = args.input.resolve()
    output_path = (
        args.output.resolve()
        if args.output
        else input_path.with_name(f"{input_path.stem}.pftrace")
    )

    if not input_path.is_file():
        parser.error(f"input file does not exist: {input_path}")
    if input_path == output_path:
        parser.error("input and output paths must be different")

    try:
        result = convert_file(
            input_path,
            output_path,
            semester_duration_ns=args.semester_duration_ns,
            academic_start_year=args.academic_start_year,
        )
    except (OSError, TypeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    print(f"Input:       {input_path}")
    print(f"Output:      {output_path}")
    print(f"Slices:      {result.scheduled_record_count}")
    print(f"Unscheduled: {result.unscheduled_record_count}")


if __name__ == "__main__":
    main()
