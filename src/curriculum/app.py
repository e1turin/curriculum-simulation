
"""
Convert a curriculum PDF of the form

    Semester | Module / discipline name | Credits | Hours

into machine-readable JSON.

Example:
    python curriculum_to_json.py academic_plan.pdf
    python curriculum_to_json.py academic_plan.pdf -o curriculum.json

Requires:
    pdftotext (Poppler)

Ubuntu/Debian:
    sudo apt install poppler-utils

macOS:
    brew install poppler
"""


import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

# Matches rows such as:
#   3   Архитектура компьютера                    3   108
#       Факультетский модуль                     19   684
#
# The semester is optional.
ROW_RE = re.compile(
    r"^\s*"
    r"(?:(\d+)\s+)?"  # optional semester
    r"(.+?)"  # name
    r"\s+(\d+)"  # credits
    r"\s+(\d+)"  # hours
    r"\s*$"
)


# These are table/document headers, not curriculum records.
IGNORED_NAMES = {
    "Учебный план",
    "Семестры старта",
    "Семестр старта",
    "Наименование модулей, дисциплин, практики и аттестации",
    "Трудоемкость в з.ед",
    "Трудоемкость в з. ед.",
    "Трудоемкость в час.",
    "Трудоемкость в час",
}


def extract_pdf_text(pdf_path: Path) -> str:
    """
    Extract text while approximately preserving table layout.
    """
    if shutil.which("pdftotext") is None:
        raise RuntimeError(
            "pdftotext is not installed.\n"
            "Install Poppler:\n"
            "  Ubuntu/Debian: sudo apt install poppler-utils\n"
            "  macOS: brew install poppler"
        )

    process = subprocess.run(
        ["pdftotext", "-layout", "-enc", "UTF-8", str(pdf_path), "-"],
        capture_output=True,
        check=False,
        text=True,
        encoding="utf-8",
    )

    if process.returncode != 0:
        raise RuntimeError(f"pdftotext failed:\n{process.stderr}")

    return process.stdout


def normalize_name(name: str) -> str:
    """
    Normalize whitespace without modifying actual course names.
    """
    return " ".join(name.split())


def classify_record(name: str, semester: int | None) -> str:
    """
    Conservative structural classification.

    Because PDFs usually do not expose table styling or nesting reliably,
    we avoid inventing hierarchy that is not explicitly represented.
    """
    if re.match(r"^Блок\s+\d+\.", name, re.IGNORECASE):
        return "block"
    return "curriculum_item" if semester is not None else "group_or_requirement"


def infer_document_metadata(pages: list[str]) -> dict:
    """
    Try to infer the document title and educational program from page 1.
    """
    if not pages:
        return {}

    lines = [normalize_name(line) for line in pages[0].splitlines() if line.strip()]
    title = next((line for line in lines if line.lower() == "учебный план"), None)
    program = next((line for line in lines if line.startswith("ОП ")), None)
    return {
        key: value
        for key, value in {"title": title, "program": program}.items()
        if value is not None
    }


def parse_curriculum(text: str, source_name: str) -> dict:
    """
    Parse extracted PDF text into JSON-compatible Python structures.
    """
    pages = [p for p in text.split("\f") if p.strip()]

    metadata = infer_document_metadata(pages)

    records = []

    for page_num, page in enumerate(pages, start=1):
        for raw_line in page.splitlines():
            if not (match := ROW_RE.match(raw_line.rstrip())):
                continue

            semester_text, raw_name, credits_text, hours_text = match.groups()

            name = normalize_name(raw_name)

            if name in IGNORED_NAMES:
                continue

            # Ignore accidental recognition of headings as numeric rows.
            if not name:
                continue

            semester = int(semester_text) if semester_text is not None else None

            credits = int(credits_text)
            hours = int(hours_text)

            order = len(records) + 1
            record = {
                "id": f"record-{order:04d}",
                "order": order,
                "source_page": page_num,
                "semester_start": semester,
                "name": name,
                "credits": credits,
                "hours": hours,
                "record_type": classify_record(name, semester),
            }

            records.append(record)

    active_block_id = active_group_id = None
    for record in records:
        match record["record_type"]:
            case "block":
                active_block_id, active_group_id = record["id"], None
            case "group_or_requirement":
                record["block_id"] = active_block_id
                active_group_id = record["id"]
            case "curriculum_item":
                record["block_id"] = active_block_id
                record["group_id"] = active_group_id

    block_totals = [record for record in records if record["record_type"] == "block"]

    scheduled_semesters = [
        record["semester_start"]
        for record in records
        if record["semester_start"] is not None
    ]

    result = {
        "schema_version": "1.2",
        "document": {
            **metadata,
            "source_file": source_name,
            "source_pages": len(pages),
            "language": "ru",
        },
        "columns": {
            "semester_start": "Семестры старта",
            "name": ("Наименование модулей, дисциплин, практики и аттестации"),
            "credits": "Трудоемкость в з.ед",
            "hours": "Трудоемкость в час.",
        },
        "temporal_model": {
            "unit": "semester",
            "first_semester": min(scheduled_semesters, default=None),
            "last_semester": max(scheduled_semesters, default=None),
            "default_item_duration": 1,
            "duration_is_inferred": True,
            "duration_note": (
                "The source contains only a start semester; curriculum "
                "items are assumed to occupy one semester unless a record "
                "defines duration_semesters."
            ),
        },
        "totals": {
            "credits": sum(x["credits"] for x in block_totals),
            "hours": sum(x["hours"] for x in block_totals),
            "derived_from": "sum of printed top-level block totals",
        },
        "statistics": {
            "records": len(records),
            "blocks": len(block_totals),
            "courses_or_semester_items": sum(
                1 for x in records if x["record_type"] == "curriculum_item"
            ),
            "groups_or_requirements": sum(
                1 for x in records if x["record_type"] == "group_or_requirement"
            ),
            "grouped_curriculum_items": sum(
                1
                for x in records
                if x["record_type"] == "curriculum_item"
                and x["group_id"] is not None
            ),
        },
        "notes": [
            "Records preserve the printed row order of the PDF.",
            "id is deterministic for a record's position in that order.",
            ("semester_start is null when the source row does not contain a semester."),
            (
                "Choice groups can contain alternatives, so the credits "
                "of child rows must not automatically be summed."
            ),
            (
                "block_id links records to the nearest preceding block row; "
                "group_id links curriculum items to the nearest preceding "
                "group_or_requirement row."
            ),
            (
                "These links preserve document grouping but do not invent "
                "nesting between group_or_requirement rows."
            ),
        ],
        "records": records,
    }

    return result


def validate(data: dict, hours_per_credit: int = 36) -> list[dict]:
    """
    Perform useful sanity checks.

    A mismatch does not prevent JSON generation because some curricula
    can legitimately contain exceptional rows.
    """
    warnings = []

    for record in data["records"]:
        credits = record["credits"]
        hours = record["hours"]

        if credits == 0 and hours == 0:
            continue

        expected_hours = credits * hours_per_credit

        if hours != expected_hours:
            warnings.append(
                {
                    "order": record["order"],
                    "name": record["name"],
                    "credits": credits,
                    "hours": hours,
                    "expected_hours": expected_hours,
                    "source_page": record["source_page"],
                }
            )

    return warnings


def convert(
    pdf_path: Path,
    output_path: Path,
) -> None:
    text = extract_pdf_text(pdf_path)

    data = parse_curriculum(
        text=text,
        source_name=pdf_path.name,
    )

    warnings = validate(data)

    data["validation"] = {
        "hours_per_credit_assumption": 36,
        "mismatch_count": len(warnings),
        "mismatches": warnings,
    }

    output_path.write_text(
        json.dumps(
            data,
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    print(f"Input:   {pdf_path}")
    print(f"Output:  {output_path}")
    print(f"Records: {len(data['records'])}")
    print(
        f"Totals:  {data['totals']['credits']} credits, {data['totals']['hours']} hours"
    )

    if warnings:
        print(f"Warning: {len(warnings)} rows do not satisfy credits × 36 = hours.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Convert curriculum PDF to JSON.")

    parser.add_argument(
        "pdf",
        type=Path,
        help="Input curriculum PDF",
    )

    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Output JSON file",
    )

    args = parser.parse_args()

    pdf_path = args.pdf.resolve()

    if not pdf_path.is_file():
        parser.error(f"input file does not exist: {pdf_path}")
    if pdf_path.suffix.lower() != ".pdf":
        parser.error("input file must be a PDF")

    output_path = (
        args.output.resolve() if args.output else pdf_path.with_suffix(".json")
    )

    try:
        convert(pdf_path, output_path)
    except (OSError, RuntimeError, TypeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
