from typing import cast

import pytest

from curriculum.app import parse_curriculum


@pytest.fixture
def parsed_curriculum() -> dict[str, object]:
    return parse_curriculum(
        """Учебный план
ОП Test program
  Блок 1. Модули 7 252
  Group one 7 252
1 Course one 3 108
\f2 Course two 4 144
""",
        "plan.pdf",
    )


def test_emits_ids_and_explicit_temporal_model(
    parsed_curriculum: dict[str, object],
) -> None:
    records = cast(list[dict[str, object]], parsed_curriculum["records"]) 
    assert parsed_curriculum["schema_version"] == "1.2"
    assert [record["id"] for record in records] == [
        "record-0001",
        "record-0002",
        "record-0003",
        "record-0004",
    ]
    assert records[1]["block_id"] == "record-0001"
    assert records[2]["block_id"] == "record-0001"
    assert records[2]["group_id"] == "record-0002"
    assert records[3]["group_id"] == "record-0002"
    assert cast(dict[str, object], parsed_curriculum["temporal_model"]) == {
        "unit": "semester",
        "first_semester": 1,
        "last_semester": 2,
        "default_item_duration": 1,
        "duration_is_inferred": True,
        "duration_note": (
            "The source contains only a start semester; curriculum items are "
            "assumed to occupy one semester unless a record defines "
            "duration_semesters."
        ),
    }


def test_empty_input_has_open_temporal_range() -> None:
    data = parse_curriculum("not a table", "empty.pdf")

    temporal_model = cast(dict[str, object], data["temporal_model"])
    assert temporal_model["first_semester"] is None
    assert temporal_model["last_semester"] is None
