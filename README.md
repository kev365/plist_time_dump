# Plist_Time_Dump - PList Timestamp Extractor
This project aims to bring unique visibility to mobile forensics investigations by enhancing the ability to seekout timestamps from various plists and export contextual information about them, for review.

** Note: This is merely a side project for learning some coding skills (with an AI assist) and something that seemed like an interesting personal challenge. Please don't rely on this tool for expert information.

This Python script recursively walks a directory of PList files, extracts timestamps from a wide range of formats, converts them to ISO 8601 (UTC), captures surrounding context, and exports the results to a tab-separated values (TSV) file.

## Features

- Searches keys whose name contains "time" or "date", **and** now also detects timestamps by
  value across the whole file.
- Recognizes many timestamp encodings:
  - ISO 8601 strings (with or without `Z`/offset)
  - Native plist `<date>` objects
  - Unix epoch seconds, milliseconds, and nanoseconds (integer, string, or fractional)
  - Apple Cocoa / Core Data time (seconds since 2001-01-01)
  - HFS+ time (seconds since 1904-01-01)
  - Custom `YYYY-MM-DD_HHMMSS-####` timezone format
- Recurses into **embedded/nested plists** (binary `bplist` blobs and inline-XML plists), e.g.
  `NSKeyedArchiver` archives stored in `<data>` fields.
- Captures **context** (the sibling key/values around each timestamp), similar in spirit to
  bulk_extractor's context column.
- Optional **date-range filtering** (on a day, before/after a day, or between two days).
- Optional **`--deepscan`** mode that searches harder and reports everything, including
  low-confidence and failed-validation entries, and finds timestamps embedded *inside*
  larger strings (filenames, paths, log fragments) rather than only whole-value timestamps.
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

The script generates a TSV file. The default columns are:

- `UTC Timestamp` — ISO 8601 formatted timestamp (UTC assumed; verify independently).
- `Original Value` — the raw value **that was decoded into the timestamp** (the source leaf,
  not a neighboring key's value).
- `Timestamp Format` — the detected format/epoch used for the conversion (helps verification).
- `Key` — the key path where the timestamp was found. Embedded plists are marked with
  `→[embedded]` in the path.
- `Context` — a truncated snapshot of the sibling scalar key/values around the timestamp
  (omitted when `--nocontext` is used).
- `File Type` — `plist`, `bplist`, or `unknown`.
- `File Name` — name of the PList file.
- `Full Path` — full path to the PList file.

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

Note: `--validate` (without `--deepscan`) drops rows that fail validation. `--deepscan` keeps
them and flags the reason instead.

### Sample Output

| UTC Timestamp | Original Value | Timestamp Format | Key | Context | File Type | File Name |
|---|---|---|---|---|---|---|
| 2022-10-15T08:30:00.000000Z | 2022-10-15 08:30:00 | ISO_8601 | created_date | count=42; label=... | plist | data.plist |
| 2021-12-03T12:10:00.000000Z | 1638533400 | Unix_seconds | timestamp | ... | plist | records.plist |
| 2023-03-08T20:26:40.000000Z | 700000000 | Cocoa_CoreData_2001 | lastCocoaDate | ... | plist | app.plist |
| 2015-03-04T05:06:07.000000Z | 2015-03-04T05:06:07Z | ISO_8601 | payload→[embedded]/date | ... | plist | archive.plist |

Author: Kevin Stokes

Notes:
- All timestamps are treated as UTC; always verify against the source artifact.
- Numeric epoch values are ambiguous by nature. Default mode reports the single most plausible
  interpretation; `--deepscan` surfaces every plausible interpretation so nothing is hidden.
