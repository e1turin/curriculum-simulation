import datetime
from typing import cast

import pytest
from perfetto.protos.perfetto.trace.perfetto_trace_pb2 import (
    DebugAnnotation,
    Trace,
    TracePacket,
    TrackEvent,
)

from curriculum.perfetto import PerfettoTraceBuild, build_perfetto_trace


def _parse_trace(data: bytes) -> Trace:
    trace = Trace()
    trace.ParseFromString(data)
    return trace


def _annotation_value(annotation: DebugAnnotation) -> object:
    value_field = annotation.WhichOneof("value")
    return getattr(annotation, value_field) if value_field else None


def _annotations(event: TrackEvent) -> dict[str, object]:
    return {
        annotation.name: _annotation_value(annotation)
        for annotation in event.debug_annotations
    }


def _course_packets(trace: Trace, event_type: int) -> list[TracePacket]:
    return [
        packet
        for packet in trace.packet
        if packet.HasField("track_event")
        and packet.track_event.type == event_type
        and "curriculum" in packet.track_event.categories
    ]


@pytest.fixture
def curriculum_data() -> dict[str, object]:
    return {
        "schema_version": "1.2",
        "document": {
            "title": "Study plan",
            "program": "Software Engineering",
            "source_file": "plan.pdf",
            "language": "en",
        },
        "temporal_model": {
            "unit": "semester",
            "first_semester": 1,
            "last_semester": 3,
            "default_item_duration": 1,
            "duration_is_inferred": True,
        },
        "records": [
            {
                "id": "record-0001",
                "order": 1,
                "source_page": 1,
                "semester_start": None,
                "name": "Block 1",
                "credits": 10,
                "hours": 360,
                "record_type": "block",
            },
            {
                "id": "record-0002",
                "order": 2,
                "source_page": 1,
                "semester_start": None,
                "name": "Core subjects",
                "credits": 10,
                "hours": 360,
                "record_type": "group_or_requirement",
                "block_id": "record-0001",
            },
            {
                "id": "record-0003",
                "order": 3,
                "source_page": 1,
                "semester_start": 1,
                "name": "Algorithms",
                "credits": 3,
                "hours": 108,
                "record_type": "curriculum_item",
                "block_id": "record-0001",
                "group_id": "record-0002",
            },
            {
                "id": "record-0004",
                "order": 4,
                "source_page": 2,
                "semester_start": 3,
                "duration_semesters": 2,
                "name": "Capstone",
                "credits": 6,
                "hours": 216,
                "record_type": "curriculum_item",
                "block_id": "record-0001",
                "group_id": "record-0002",
            },
        ],
    }


def _build_trace(
    data: dict[str, object],
    duration_ns: int = 100,
    academic_start_year: int | None = None,
) -> tuple[PerfettoTraceBuild, Trace]:
    result = build_perfetto_trace(
        data,
        semester_duration_ns=duration_ns,
        academic_start_year=academic_start_year,
    )
    return result, _parse_trace(result.data)


def test_creates_native_slices_with_analysis_metadata(
    curriculum_data: dict[str, object],
) -> None:
    result, trace = _build_trace(curriculum_data)
    begins = _course_packets(trace, TrackEvent.TYPE_SLICE_BEGIN)
    course_track_uuids = {packet.track_event.track_uuid for packet in begins}
    ends = [
        packet
        for packet in trace.packet
        if packet.HasField("track_event")
        and packet.track_event.type == TrackEvent.TYPE_SLICE_END
        and packet.track_event.track_uuid in course_track_uuids
    ]

    assert (result.scheduled_record_count, result.unscheduled_record_count) == (2, 2)
    assert len(begins) == len(ends) == 2
    assert [packet.timestamp for packet in begins] == [0, 200]
    assert [packet.timestamp for packet in ends] == [100, 400]

    event = begins[0].track_event
    args = _annotations(event)
    assert event.name == "Algorithms"
    assert event.correlation_id_str == "record-0003"
    assert list(event.categories) == ["curriculum", "curriculum_item"]
    assert args["record_id"] == "record-0003"
    assert args["block_id"] == "record-0001"
    assert args["block_name"] == "Block 1"
    assert args["group_id"] == "record-0002"
    assert args["group_name"] == "Core subjects"
    assert args["semester_end_exclusive"] == 2
    assert args["duration_is_inferred"] is True
    assert args["credits"] == 3
    assert args["source_page"] == 1
    assert args["source_file"] == "plan.pdf"
    assert args["program"] == "Software Engineering"
    assert args["curriculum_schema_version"] == "1.2"

    capstone_args = _annotations(begins[1].track_event)
    assert capstone_args["duration_is_inferred"] is False
    assert capstone_args["semester_end_exclusive"] == 5


def test_emits_curriculum_metadata_event(
    curriculum_data: dict[str, object],
) -> None:
    result, trace = _build_trace(curriculum_data)
    metadata = next(
        packet.track_event
        for packet in trace.packet
        if packet.HasField("track_event")
        and packet.track_event.type == TrackEvent.TYPE_INSTANT
    )
    args = _annotations(metadata)

    assert result.data
    assert metadata.name == "Curriculum export"
    assert args["scheduled_record_count"] == 2
    assert args["unscheduled_record_count"] == 2
    assert args["document_group_count"] == 1
    assert args["semester_duration_ns"] == 100


def test_groups_course_tracks_by_document_groups(
    curriculum_data: dict[str, object],
) -> None:
    records = cast(list[dict[str, object]], curriculum_data["records"])
    records.append(
        {
            "id": "record-0005",
            "order": 5,
            "semester_start": 1,
            "name": "Databases",
            "record_type": "curriculum_item",
            "block_id": "record-0001",
            "group_id": "record-0002",
        }
    )
    _, trace = _build_trace(curriculum_data)
    descriptors = {
        packet.track_descriptor.uuid: packet.track_descriptor
        for packet in trace.packet
        if packet.HasField("track_descriptor")
    }
    block = next(d for d in descriptors.values() if d.name == "Block 1")
    group = next(d for d in descriptors.values() if d.name == "Core subjects")
    group_children = [
        descriptor
        for descriptor in descriptors.values()
        if descriptor.parent_uuid == group.uuid
    ]

    assert group.parent_uuid == block.uuid
    assert {descriptor.name for descriptor in group_children} == {
        "0003 · Algorithms",
        "0004 · Capstone",
        "0005 · Databases",
    }
    assert block.child_ordering == block.EXPLICIT
    assert group.child_ordering == group.EXPLICIT


def test_joins_non_overlapping_semester_items_for_the_same_subject(
    curriculum_data: dict[str, object],
) -> None:
    records = cast(list[dict[str, object]], curriculum_data["records"])
    records.append(
        {
            "id": "record-0005",
            "order": 5,
            "semester_start": 2,
            "name": "Algorithms",
            "record_type": "curriculum_item",
            "block_id": "record-0001",
            "group_id": "record-0002",
        }
    )

    _, trace = _build_trace(curriculum_data)
    descriptor = next(
        packet.track_descriptor
        for packet in trace.packet
        if packet.HasField("track_descriptor")
        and packet.track_descriptor.name == "0003/0005 · Algorithms"
    )
    events = [
        packet
        for packet in _course_packets(trace, TrackEvent.TYPE_SLICE_BEGIN)
        if packet.track_event.track_uuid == descriptor.uuid
    ]

    assert [packet.timestamp for packet in events] == [0, 100]
    assert [packet.track_event.correlation_id_str for packet in events] == [
        "record-0003",
        "record-0005",
    ]
    assert [
        _annotations(packet.track_event)["semester_start"] for packet in events
    ] == [
        1,
        2,
    ]


def test_keeps_overlapping_items_on_separate_tracks(
    curriculum_data: dict[str, object],
) -> None:
    records = cast(list[dict[str, object]], curriculum_data["records"])
    records.append(
        {
            "id": "record-0005",
            "order": 5,
            "semester_start": 1,
            "name": "Algorithms",
            "record_type": "curriculum_item",
            "block_id": "record-0001",
            "group_id": "record-0002",
        }
    )

    _, trace = _build_trace(curriculum_data)
    algorithm_descriptors = [
        packet.track_descriptor
        for packet in trace.packet
        if packet.HasField("track_descriptor")
        and packet.track_descriptor.name.endswith(" · Algorithms")
    ]

    assert {descriptor.name for descriptor in algorithm_descriptors} == {
        "0003 · Algorithms",
        "0005 · Algorithms",
    }


def test_adds_sequential_semester_schedule_track(
    curriculum_data: dict[str, object],
) -> None:
    _, trace = _build_trace(curriculum_data)
    descriptors = {
        packet.track_descriptor.uuid: packet.track_descriptor
        for packet in trace.packet
        if packet.HasField("track_descriptor")
    }
    schedule = next(d for d in descriptors.values() if d.name == "Semesters")
    begins = [
        packet
        for packet in trace.packet
        if packet.HasField("track_event")
        and packet.track_event.type == TrackEvent.TYPE_SLICE_BEGIN
        and packet.track_event.track_uuid == schedule.uuid
    ]
    ends = [
        packet
        for packet in trace.packet
        if packet.HasField("track_event")
        and packet.track_event.type == TrackEvent.TYPE_SLICE_END
        and packet.track_event.track_uuid == schedule.uuid
    ]

    assert [packet.track_event.name for packet in begins] == [
        "Semester 1",
        "Semester 2",
        "Semester 3",
        "Semester 4",
    ]
    assert [packet.timestamp for packet in begins] == [0, 100, 200, 300]
    assert [packet.timestamp for packet in ends] == [100, 200, 300, 400]


def test_maps_semesters_to_academic_calendar(
    curriculum_data: dict[str, object],
) -> None:
    _, trace = _build_trace(curriculum_data, academic_start_year=2024)
    day_ns = 86_400_000_000_000
    begins = _course_packets(trace, TrackEvent.TYPE_SLICE_BEGIN)
    course_track_uuids = {packet.track_event.track_uuid for packet in begins}
    ends = [
        packet
        for packet in trace.packet
        if packet.HasField("track_event")
        and packet.track_event.type == TrackEvent.TYPE_SLICE_END
        and packet.track_event.track_uuid in course_track_uuids
    ]
    snapshot = next(
        packet.clock_snapshot
        for packet in trace.packet
        if packet.HasField("clock_snapshot")
    )
    clocks = {clock.clock_id: clock.timestamp for clock in snapshot.clocks}

    assert [packet.timestamp for packet in begins] == [0, 365 * day_ns]
    assert [packet.timestamp for packet in ends] == [153 * day_ns, 668 * day_ns]
    assert all(packet.timestamp_clock_id == 11 for packet in begins + ends)
    assert snapshot.primary_trace_clock == 11
    assert clocks[11] == 0
    assert clocks[1] == int(
        datetime.datetime(2024, 9, 1, tzinfo=datetime.UTC).timestamp() * 1_000_000_000
    )

    metadata = next(
        packet.track_event
        for packet in trace.packet
        if packet.HasField("track_event")
        and packet.track_event.type == TrackEvent.TYPE_INSTANT
    )
    assert _annotations(metadata)["timeline_model"] == "academic_calendar"
    assert _annotations(metadata)["academic_start_date"] == "2024-09-01"
    assert _annotations(metadata)["academic_end_date"] == "2026-06-30"

    schedule_uuid = next(
        packet.track_descriptor.uuid
        for packet in trace.packet
        if packet.HasField("track_descriptor")
        and packet.track_descriptor.name == "Semesters"
    )
    first_semester = next(
        packet.track_event
        for packet in trace.packet
        if packet.HasField("track_event")
        and packet.track_event.type == TrackEvent.TYPE_SLICE_BEGIN
        and packet.track_event.track_uuid == schedule_uuid
    )
    assert _annotations(first_semester)["calendar_start_date"] == "2024-09-01"
    assert _annotations(first_semester)["calendar_end_date"] == "2025-01-31"


def test_honors_explicit_default_duration_provenance(
    curriculum_data: dict[str, object],
) -> None:
    temporal_model = cast(dict[str, object], curriculum_data["temporal_model"])
    temporal_model["duration_is_inferred"] = False

    _, trace = _build_trace(curriculum_data)
    event = _course_packets(trace, TrackEvent.TYPE_SLICE_BEGIN)[0].track_event

    assert _annotations(event)["duration_is_inferred"] is False


def test_accepts_legacy_v1_input_and_infers_nearest_group() -> None:
    legacy = {
        "schema_version": "1.0",
        "document": {},
        "records": [
            {
                "order": 6,
                "semester_start": None,
                "name": "Legacy group",
                "record_type": "group_or_requirement",
            },
            {"order": 7, "semester_start": 2, "name": "Legacy course"},
        ],
    }

    result = build_perfetto_trace(legacy)
    trace = _parse_trace(result.data)
    event = _course_packets(trace, TrackEvent.TYPE_SLICE_BEGIN)[0].track_event
    args = _annotations(event)

    assert args["record_id"] == "record-0007"
    assert args["group_id"] == "record-0006"
    assert args["group_name"] == "Legacy group"
    assert args["duration_semesters"] == 1


def test_rejects_academic_start_year_before_unix_epoch(
    curriculum_data: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="1970 or later"):
        build_perfetto_trace(curriculum_data, academic_start_year=1969)


def test_rejects_non_positive_semester(
    curriculum_data: dict[str, object],
) -> None:
    records = cast(list[dict[str, object]], curriculum_data["records"])
    records[2]["semester_start"] = 0

    with pytest.raises(ValueError, match="semester_start"):
        build_perfetto_trace(curriculum_data)


def test_rejects_unsupported_major_schema(
    curriculum_data: dict[str, object],
) -> None:
    curriculum_data["schema_version"] = "2.0"

    with pytest.raises(ValueError, match="unsupported"):
        build_perfetto_trace(curriculum_data)


def test_rejects_duplicate_record_ids(
    curriculum_data: dict[str, object],
) -> None:
    records = cast(list[dict[str, object]], curriculum_data["records"])
    records[3]["id"] = "record-0003"

    with pytest.raises(ValueError, match="duplicate record id"):
        build_perfetto_trace(curriculum_data)
