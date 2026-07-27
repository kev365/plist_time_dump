# plist_time_dump - PList Timestamp Extractor
This project aims to bring unique visibility to mobile forensics investigations by enhancing the ability to seekout timestamps from various plists and export contextual information about them, for review.

** Note: This is merely a side project for learning some coding skills (with an AI assist) and something that seemed like an interesting personal challenge. Please don't rely on this tool for expert information.

This Python script recursively walks a directory, identifies plists and SQLite databases by
their file header (not their extension), extracts timestamps from a wide range of formats,
converts them to ISO 8601 (UTC), records what each timestamp sits next to, and exports the
results to a tab-separated values (TSV) file.

## Features

- Finds timestamps both by **key name** (keys containing "time" or "date") and by **value**,
  anywhere in the file.
- Recognizes many timestamp encodings:
  - ISO 8601 strings (with or without `Z`/offset)
  - Native plist `<date>` objects
  - Unix epoch seconds, milliseconds, and nanoseconds (integer, string, or fractional)
  - Apple Cocoa / Core Data time — seconds, **milliseconds, and nanoseconds** since
    2001-01-01. The sub-second variants matter: iOS stores iMessage `message.date`
    (`sms.db`) as nanoseconds since 2001, and reading such a value against the Unix epoch
    yields a plausible-looking date exactly **31 years early**
  - HFS+ time (seconds since 1904-01-01)
  - Custom `YYYY-MM-DD_HHMMSS-####` timezone format
- Recurses into **embedded/nested plists** (binary `bplist` blobs and inline-XML plists), e.g.
  `NSKeyedArchiver` archives stored in `<data>` fields or SQLite BLOB columns. A per-source
  node-visit budget (1,000,000) bounds deliberately hostile object graphs — an aliased or
  self-referencing archive built to blow up recursive traversal. Ordinary multi-MB forensic
  plists are not expected to reach it; if anything ever does, that one source is truncated and
  **marked in the TSV** rather than hanging or killing the rest of the scan. See
  **Truncated sources** below.
- Records **context** for each timestamp — the other fields of the record it belongs to (see
  [What `Context` actually is](#what-context-actually-is); it is structural, not byte-proximity).
- Optional **date-range filtering** (on a day, before/after a day, or between two days).
- Optional **`--deepscan`** mode that searches harder and reports everything, including
  low-confidence and failed-validation entries, and finds timestamps embedded *inside*
  larger strings (filenames, paths, log fragments) rather than only whole-value timestamps.
- Scans **SQLite databases** (detected by header, any extension): finds timestamps stored in
  columns and inside embedded plists in BLOB/TEXT values, including **NSKeyedArchiver** archives
  (object graph resolved so archived dates get meaningful key paths). Databases are opened
  **read-only and immutable** — the tool never modifies a scanned database. typedstream and
  other non-plist BLOBs are skipped.
- Outputs the results to a tab-separated values (TSV) file.

## Requirements

- **Python 3.11 or newer** (standard library only — no external packages). 3.11+ is required
  for its `datetime.fromisoformat()` support of `Z` suffixes, offsets without a colon
  (e.g. `-0500`), and basic-format times. The script exits with a clear message on older
  interpreters. Tested on 3.13.
- PList files (commonly used on Apple platforms)

## Usage

1. Clone this repository or download the script file.
2. Open your terminal or command prompt.
3. Run the script with the following command:

> python plist_time_dump.py [directory_to_search] [output_file_path] [options]

- `[directory_to_search]`: The directory path to search for PList files.
- `[output_file_path]`: The path for the output TSV file.

### Options

| Flag | Description |
|------|-------------|
| `--validate` | Verify timestamps and drop entries that fail validation (adds a `Validation` column). |
| `--deepscan` | Maximum-intensity scan: search all fields with looser matching, **detect timestamps embedded inside larger strings** (filenames, paths, log fragments), and report everything including low-confidence and failed-validation rows. Adds `Confidence` and `Validation` columns, and may emit multiple candidate rows for ambiguous numeric values. Higher recall, more false positives. |
| `--on YYYY-MM-DD` | Only timestamps that fall on this UTC day. |
| `--after YYYY-MM-DD` | Only timestamps on or after this UTC day. |
| `--before YYYY-MM-DD` | Only timestamps on or before this UTC day. |
| `--between START END` | Only timestamps between two UTC days (inclusive). |
| `--nocontext` | Omit the `Context` column for quicker/leaner runs. |
| `--nonest` | Skip embedded/nested plist extraction (fast triage). |
| `--nestdepth N` | Maximum embedded-plist recursion depth (default 5). |

`--on` cannot be combined with `--before`/`--after`/`--between`; `--between` cannot be combined
with `--before`/`--after`. Use `--before` + `--after` together for a custom window.

## Example Usage

```
python plist_time_dump.py /path/to/plist/files output.tsv
python plist_time_dump.py /path/to/plist/files output.tsv --validate
python plist_time_dump.py /path/to/plist/files output.tsv --deepscan
python plist_time_dump.py /path/to/plist/files output.tsv --on 2022-12-10
python plist_time_dump.py /path/to/plist/files output.tsv --after 2022-01-01 --before 2023-01-01
python plist_time_dump.py /path/to/plist/files output.tsv --between 2021-01-01 2023-01-01
python plist_time_dump.py /path/to/plist/files output.tsv --nonest --nocontext
```

## Output

The script generates a TSV file. The default columns, in order:

- `UTC Timestamp` — ISO 8601 formatted timestamp (UTC assumed; verify independently).
- `Original Value` — the raw value **that was decoded into the timestamp** (the source leaf,
  not a neighboring key's value).
- `Timestamp Format` — the detected format/epoch used for the conversion (helps verification).
- `File Type` — `plist`, `bplist`, `sqlite`, or `unknown`.
- `File Name` — name of the source file.
- `Full Path` — full path to the source file.
- `Key` — the key path where the timestamp was found. Embedded plists are marked with
  `→[embedded]` in the path.
- `Context` — the containing record's *other* scalar fields (omitted with `--nocontext`).
  See the note below on exactly what this is and is not.

The two wide, free-text columns (`Key`, `Context`) are last so they don't push the columns
you scan first off the screen.

> **Parse the output as real CSV with a tab delimiter, not by splitting on tabs.**
> Values taken verbatim from evidence can contain tabs and newlines — SMS bodies especially.
> Those fields are correctly quoted, so a real CSV reader keeps each record in one row, but
> `awk -F'\t'` / `cut` will mis-split them. In Excel, import via **Data ▸ Get Data ▸ From
> Text/CSV** (delimiter: Tab), which honours the quoting and keeps a multi-line value inside a
> single cell; double-clicking the file uses the older importer and is less reliable for that.

### What `Context` actually is

`Context` is the **containing record's other scalar fields** — structural siblings, not bytes
surrounding the value on disk:

- **SQLite** — the other columns of the *same row*.
- **plist dictionary** — the other keys of the *same dictionary*.

Current limitations, worth knowing before relying on it:

- Only **scalar** siblings are included; nested dictionaries, arrays and BLOBs are skipped.
- It is **truncated to 200 characters** (with `…`). On real iOS data this bites often — 62% of
  rows in a sysdiagnose sample were truncated — because one long free-text sibling (a message
  body) can consume the whole budget.
- A timestamp that is an **element of an array** has no dictionary parent, so its `Context` is
  empty (e.g. `PurgeEvents/…Assets[0]/Date`).
- No ancestor/parent context is included; the `Key` path is what locates the value in the tree.

- For SQLite sources, `File Type` is `sqlite` and `Key` is `Table.Column(rowid=N)` (plus
  `→[embedded]/...` for timestamps found inside a BLOB/TEXT plist). For a `WITHOUT ROWID`
  table (which has no real SQLite rowid), `N` is a synthetic 0-based row index assigned in
  scan order, not an actual `rowid` column value — don't mistake `rowid=0` there for a real,
  independently-queryable rowid.

Additional columns:

- `Confidence` (with `--deepscan`) — `high` / `medium` / `low`, reflecting how unambiguous the
  decode is. Whole-value decodes are `high`; timestamps found inside larger strings are
  `medium` (patterned) or `low` (numeric runs). Numeric epochs are inherently ambiguous, so
  `--deepscan` may report more than one interpretation of the same value (e.g. a number that is
  plausibly both Unix and Cocoa time).
- `Validation` (with `--validate` or `--deepscan`) — validation status or issues found.

When validation is active, the `Validation` column may show:
- `future_date` — Timestamps after the current date
- `pre_1970` — Timestamps before 1970
- `too_old` — Timestamps more than 15 years in the past
- `too_future` — Timestamps more than 15 years in the future
- `invalid_format` — Values that don't match expected formats
- `valid` — Timestamps that pass all validation checks (still verify independently)
- `truncated` — Not a timestamp: marks a `TRUNCATED_NODE_BUDGET` row (see Truncation above),
  meaning that source's output is incomplete

Note: `--validate` (without `--deepscan`) drops rows that fail validation. `--deepscan` keeps
them and flags the reason instead.

### Truncated sources

If a single source (one plist file, or one SQLite cell's embedded-plist expansion) hits the
node-visit budget described above, that source's output is truncated — and this is marked
**directly in the TSV**, not only as a console warning: a row is written for that source with
`Timestamp Format` set to `TRUNCATED_NODE_BUDGET` (`UTC Timestamp`/`Original Value` left blank,
since there is no decoded value), so it survives being scrolled past on the console and is not
silently lost if stdout wasn't captured. This row is written unconditionally — it is never
dropped by `--validate` or by a `--on`/`--before`/`--after`/`--between` date-range filter the
way a normal row would be, since a source's incompleteness needs to stay visible regardless of
what you're currently filtering for. The schema/column count is unchanged: the marker just
reuses the normal columns. The truncated source is identified by `File Name`/`Full Path`; for
SQLite, `Key` additionally names the specific `Table.Column(rowid=N)` cell that was truncated
(for a plist file `Key` holds a fixed `<truncated: ...>` literal, since the whole file is the
source, and that literal also says *why* it stopped — the node-visit budget or the
interpreter's recursion limit). On the SQLite path `Key` carries the cell identifier rather
than a reason, except when the whole database scan was abandoned on a database error, which is
marked `<truncated: database error>`.

To keep a run with many truncated sources from flooding the console, only the first 10
per-source warnings are printed to stdout (plus one line noting that further warnings are
suppressed); the TSV's `TRUNCATED_NODE_BUDGET` rows remain the complete, authoritative record of
every truncated source regardless of how many warnings were printed. A run with at least one
truncated source also prints a one-line summary (source count) at the end.

### Sample Output

(`Full Path` omitted here for width.)

| UTC Timestamp | Original Value | Timestamp Format | File Type | File Name | Key | Context |
|---|---|---|---|---|---|---|
| 2022-10-15T08:30:00.000000Z | 2022-10-15 08:30:00 | ISO_8601 | plist | data.plist | created_date | count=42; label=… |
| 2021-12-03T12:10:00.000000Z | 1638533400 | Unix_seconds | plist | records.plist | timestamp | id=7; kind=sync |
| 2023-03-08T20:26:40.000000Z | 700000000 | Cocoa_CoreData_2001 | bplist | app.plist | lastCocoaDate | bundle=com.example |
| 2015-03-04T05:06:07.000000Z | 2015-03-04T05:06:07Z | ISO_8601 | plist | archive.plist | payload→[embedded]/date | name=archive |
| 2023-07-01T20:38:17.517450Z | 709936697517449984 | Cocoa_nanoseconds_2001 | sqlite | sms.db | message.date(rowid=1) | guid=7F002C80-…; text=… |

## Testing

Stdlib `unittest`, no dependencies. Run every suite:

```
for t in tests/test_*.py; do python "$t"; done
```

`tests/make_sqlite_fixtures.py` regenerates the SQLite fixture database used for manual
end-to-end checks (the generated `tests/sqlite_fixtures/` is git-ignored).

## Known limitations

- **Bare numbers under a non-temporal key can be false positives.** A value is accepted on
  "high confidence" alone even when its key name isn't date-like, which on real iOS data is
  roughly 60% precise. Genuine finds this catches include `last_modified`, `endOfDay` and
  `__expiration`; false positives include AudioUnit FourCC codes (`auDescType` = `1635283826`
  = `'aufx'`, not a 2021 timestamp), App Store `itemId`s, `ChemID`, `AssetSize` and version
  numbers. Treat numeric hits under non-temporal keys as needing triage.
- **UTC is assumed** where a value carries no timezone. Explicit offsets (`Z`, `±HH:MM`,
  `±HHMM`) are parsed and converted correctly; a naive local timestamp is reported as-is.
- **Numeric epochs are inherently ambiguous.** Default mode reports the single most plausible
  interpretation; `--deepscan` surfaces every plausible one so nothing is hidden.
- **`Context` has gaps** — scalars only, truncated at 200 characters, and empty for array
  elements. See [What `Context` actually is](#what-context-actually-is).
- Encrypted content (e.g. an encrypted iOS backup's payload files) cannot be parsed; those
  files simply don't match any known header and are skipped.

Always verify a timestamp against the source artifact before relying on it.

Author: Kevin Stokes
