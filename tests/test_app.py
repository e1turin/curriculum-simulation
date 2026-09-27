from __future__ import annotations

import unittest

from curriculum.app import parse_curriculum


class ParseCurriculumTests(unittest.TestCase):
    def test_emits_ids_and_explicit_temporal_model(self) -> None:
        data = parse_curriculum(
            """Учебный план
ОП Test program
  Блок 1. Модули 3 108
1 Course one 3 108
\f2 Course two 4 144
""",
            "plan.pdf",
        )

        self.assertEqual(data["schema_version"], "1.1")
        self.assertEqual(
            [record["id"] for record in data["records"]],
            ["record-0001", "record-0002", "record-0003"],
        )
        self.assertEqual(
            data["temporal_model"],
            {
                "unit": "semester",
                "first_semester": 1,
                "last_semester": 2,
                "default_item_duration": 1,
                "duration_is_inferred": True,
                "duration_note": (
                    "The source contains only a start semester; curriculum "
                    "items are assumed to occupy one semester unless a record "
                    "defines duration_semesters."
                ),
            },
        )

    def test_empty_input_has_open_temporal_range(self) -> None:
        data = parse_curriculum("not a table", "empty.pdf")

        self.assertIsNone(data["temporal_model"]["first_semester"])
        self.assertIsNone(data["temporal_model"]["last_semester"])


if __name__ == "__main__":
    unittest.main()
