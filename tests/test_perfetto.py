from __future__ import annotations

import unittest

from curriculum.perfetto import build_perfetto_trace


class PerfettoTraceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.data = {
            "schema_version": "1.1",
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
                    "semester_start": 1,
                    "name": "Algorithms",
                    "credits": 3,
                    "hours": 108,
                    "record_type": "curriculum_item",
                },
                {
                    "id": "record-0003",
                    "order": 3,
                    "source_page": 2,
                    "semester_start": 3,
                    "duration_semesters": 2,
                    "name": "Capstone",
                    "credits": 6,
                    "hours": 216,
                    "record_type": "curriculum_item",
                },
            ],
        }

    def test_creates_complete_slices_with_analysis_metadata(self) -> None:
        trace = build_perfetto_trace(self.data, semester_duration_us=100)
        slices = [event for event in trace["traceEvents"] if event["ph"] == "X"]

        self.assertEqual(len(slices), 2)
        self.assertEqual(slices[0]["ts"], 0)
        self.assertEqual(slices[0]["dur"], 100)
        self.assertEqual(slices[1]["ts"], 200)
        self.assertEqual(slices[1]["dur"], 200)
        self.assertNotEqual(slices[0]["tid"], slices[1]["tid"])

        args = slices[0]["args"]
        self.assertEqual(args["record_id"], "record-0002")
        self.assertEqual(args["semester_end_exclusive"], 2)
        self.assertTrue(args["duration_is_inferred"])
        self.assertEqual(args["credits"], 3)
        self.assertEqual(args["source_page"], 1)
        self.assertEqual(args["source_file"], "plan.pdf")
        self.assertEqual(args["program"], "Software Engineering")
        self.assertEqual(args["curriculum_schema_version"], "1.1")

        self.assertFalse(slices[1]["args"]["duration_is_inferred"])
        self.assertEqual(slices[1]["args"]["semester_end_exclusive"], 5)
        self.assertEqual(
            trace["otherData"]["curriculum"]["unscheduled_record_count"],
            1,
        )

    def test_groups_tracks_by_semester(self) -> None:
        self.data["records"].append(
            {
                "id": "record-0004",
                "order": 4,
                "semester_start": 1,
                "name": "Databases",
            }
        )
        trace = build_perfetto_trace(self.data)
        process_names = [
            event["args"]["name"]
            for event in trace["traceEvents"]
            if event["ph"] == "M" and event["name"] == "process_name"
        ]
        semester_one_slices = [
            event
            for event in trace["traceEvents"]
            if event["ph"] == "X" and event["pid"] == 1
        ]

        self.assertEqual(process_names, ["Semester 1", "Semester 3"])
        self.assertEqual(len(semester_one_slices), 2)
        self.assertEqual(len({event["tid"] for event in semester_one_slices}), 2)

    def test_honors_explicit_default_duration_provenance(self) -> None:
        self.data["temporal_model"]["duration_is_inferred"] = False

        trace = build_perfetto_trace(self.data)
        event = next(e for e in trace["traceEvents"] if e["ph"] == "X")

        self.assertFalse(event["args"]["duration_is_inferred"])

    def test_accepts_legacy_v1_input(self) -> None:
        legacy = {
            "schema_version": "1.0",
            "document": {},
            "records": [
                {
                    "order": 7,
                    "semester_start": 2,
                    "name": "Legacy course",
                }
            ],
        }

        trace = build_perfetto_trace(legacy)
        event = next(e for e in trace["traceEvents"] if e["ph"] == "X")

        self.assertEqual(event["args"]["record_id"], "record-0007")
        self.assertEqual(event["args"]["duration_semesters"], 1)

    def test_rejects_non_positive_semester(self) -> None:
        self.data["records"][1]["semester_start"] = 0

        with self.assertRaisesRegex(ValueError, "semester_start"):
            build_perfetto_trace(self.data)

    def test_rejects_unsupported_major_schema(self) -> None:
        self.data["schema_version"] = "2.0"

        with self.assertRaisesRegex(ValueError, "unsupported"):
            build_perfetto_trace(self.data)

    def test_rejects_duplicate_record_ids(self) -> None:
        self.data["records"][2]["id"] = "record-0002"

        with self.assertRaisesRegex(ValueError, "duplicate id"):
            build_perfetto_trace(self.data)


if __name__ == "__main__":
    unittest.main()
