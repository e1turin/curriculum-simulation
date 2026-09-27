# Curriculum parser and Perfetto exporter

This project extracts a university study plan from PDF into source-faithful JSON and converts scheduled curriculum items into a [Perfetto](https://ui.perfetto.dev/) JSON trace.

## Requirements

- Python 3.14+
- [uv](https://docs.astral.sh/uv/)
- `pdftotext` from Poppler for PDF parsing (`brew install poppler` on macOS)

## Parse a curriculum

```sh
uv run curriculum resources/09.03.04.pdf -o curriculum.json
```

The curriculum JSON schema is currently `1.1`. Its `records` remain flat and in printed PDF order because text extraction does not reliably expose the visual hierarchy or choice-group relationships. Inventing that hierarchy would make the data less reliable.

Version 1.1 adds:

- a deterministic `id` for correlating each source record with derived data;
- a `temporal_model` that records the semantic unit, observed semester range, and the explicit assumption that an item lasts one semester by default;
- support for a future per-record `duration_semesters` override without changing the default parser output.

Rows without `semester_start` are aggregate blocks, requirements, or choice groups. Their credits must not be added to scheduled rows without interpreting the curriculum's choice structure.

## Create a Perfetto timeline

```sh
uv run curriculum-perfetto curriculum.json -o curriculum.perfetto.json
```

Open `curriculum.perfetto.json` in <https://ui.perfetto.dev/> with **Open trace file**.

The exporter writes Chromium/Perfetto JSON trace events:

- each semester is a process group;
- every scheduled curriculum row is a complete (`X`) slice on its own track, so concurrent courses do not become falsely nested;
- unscheduled aggregate rows are excluded from slices and counted in `otherData.curriculum`;
- every slice includes the record ID/order/type, semantic semester range, credits, hours, source page/file, program, language, and schema version in `args`.

Perfetto timestamps must use physical units, while the input only has semantic semesters. The default display mapping is therefore **one semester = 1,000,000 microseconds (one trace second)**. This is only a visualization scale: use `semester_start`, `semester_end_exclusive`, and `duration_semesters` in event args for analysis. The scale can be changed without changing semantics:

```sh
uv run curriculum-perfetto curriculum.json \
  --semester-duration-us 10000000 \
  -o curriculum.perfetto.json
```

The exporter accepts both schema v1.0 and v1.1 inputs. For v1.0 it derives missing record IDs from `order` and uses the one-semester default.

## Test

```sh
uv run python -m unittest discover -s tests -v
```
