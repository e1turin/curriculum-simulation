# Curriculum parser and Perfetto exporter

This project extracts a university study plan from PDF into source-faithful JSON and converts scheduled curriculum items into a native [Perfetto](https://ui.perfetto.dev/) protobuf trace.

## Requirements

- Python 3.14+
- [uv](https://docs.astral.sh/uv/)
- `pdftotext` from Poppler for PDF parsing (`brew install poppler` on macOS)

## Parse a curriculum

```sh
uv run curriculum resources/09.03.04.pdf -o curriculum.json
```

The curriculum JSON schema is currently `1.2`. Its `records` remain flat and in printed PDF order because text extraction does not reliably expose nesting between choice-group rows. Inventing that deeper hierarchy would make the data less reliable.

Version 1.1 introduced deterministic record IDs and an explicit semester-based temporal model. Version 1.2 adds document grouping references:

- `block_id` points to the nearest preceding `block` row;
- `group_id` on each scheduled item points to the nearest preceding `group_or_requirement` row;
- grouping continues across PDF page boundaries and resets when a new block starts;
- `duration_semesters` remains available as an optional per-record override.

Rows without `semester_start` are aggregate blocks, requirements, or choice groups. Their credits must not be added to scheduled rows without interpreting the curriculum's choice structure.

## Create a Perfetto timeline

```sh
uv run curriculum-perfetto curriculum.json -o curriculum.pftrace
```

Open `curriculum.pftrace` in <https://ui.perfetto.dev/> with **Open trace file**.

The exporter uses the official `perfetto` Python SDK to write native TrackEvent protobuf packets:

- curriculum tracks are grouped as `document block → nearest document group → course`, independently of semester;
- scheduled rows with the same `group_id` and course name share one child track when their semester intervals do not overlap, so a subject distributed across semesters appears on one lane;
- distinct or concurrent curriculum items remain on separate child tracks, with each source row represented by its own `TYPE_SLICE_BEGIN` and `TYPE_SLICE_END` events;
- a separate top-level `Semesters` track contains consecutive `Semester N` slices showing the semantic time windows;
- unscheduled aggregate rows are excluded from course slices and counted on a `Curriculum export` metadata event;
- every course slice carries typed `DebugAnnotation` arguments for subject/block/group identity, record ID/order/type, semantic semester range, duration provenance, credits, hours, source page/file, program, language, and schema version;
- subject identities (`group_id + course name`) are emitted as event correlation IDs, which makes Perfetto render every semester slice of one subject with the same deterministic color; source record IDs remain available as annotations.

In Perfetto SQL, debug annotations are available through the event's argument set with keys such as `debug.record_id`, `debug.credits`, and `debug.semester_start`:

```sql
SELECT
  name,
  dur,
  EXTRACT_ARG(arg_set_id, 'debug.record_id') AS record_id,
  EXTRACT_ARG(arg_set_id, 'debug.group_name') AS document_group,
  EXTRACT_ARG(arg_set_id, 'debug.semester_start') AS semester,
  EXTRACT_ARG(arg_set_id, 'debug.credits') AS credits
FROM slice
WHERE category GLOB 'curriculum,*'
ORDER BY ts, name;
```

Native Perfetto timestamps use nanoseconds, while the input only has semantic semesters. The default compact display mapping is therefore **one semester = 1,000,000,000 nanoseconds (one trace second)**. This is only a visualization scale: use `semester_start`, `semester_end_exclusive`, and `duration_semesters` annotations for analysis. The scale can be changed without changing semantics:

```sh
uv run curriculum-perfetto curriculum.json \
  --semester-duration-ns 10000000000 \
  -o curriculum.pftrace
```

To use an academic calendar instead, provide its starting year. Autumn semesters run from September 1 through January 31, spring semesters from February 1 through June 30, and summer breaks remain visible as gaps. A single Perfetto clock snapshot correlates the relative timeline with UTC wall time:

```sh
uv run curriculum-perfetto curriculum.json \
  --academic-start-year 2024 \
  -o curriculum.perfetto
```

Calendar dates are included only on the export metadata event and semester-window slices; course slices retain the semantic semester annotations without duplicating calendar metadata.

The exporter accepts all schema v1.x inputs. For v1.0/v1.1 it derives missing record IDs, temporal defaults, and nearest-preceding document groups where needed.

## Test

```sh
uv run pytest -v
```
