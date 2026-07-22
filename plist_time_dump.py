import os
import sys
import plistlib
import csv
import re
from collections import namedtuple
from datetime import datetime, timezone, timedelta

# Requires Python 3.11+: we rely on the modernized datetime.fromisoformat() to parse
# 'Z' suffixes, offsets without a colon (e.g. -0500), and basic-format times. Earlier
# interpreters silently fail to parse some of those, producing wrong/missing results.
MIN_PYTHON = (3, 11)
if sys.version_info < MIN_PYTHON:
    sys.exit(
        f"plist_time_dump requires Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer "
        f"(running {sys.version_info.major}.{sys.version_info.minor})."
    )

# Define the script version number
SCRIPT_VERSION = "2.0"

# Epoch constants for the various timestamp conventions we try to decode.
EPOCH_1601 = datetime(1601, 1, 1, tzinfo=timezone.utc)  # Windows FILETIME
EPOCH_1904 = datetime(1904, 1, 1, tzinfo=timezone.utc)  # HFS+ epoch
EPOCH_2001 = datetime(2001, 1, 1, tzinfo=timezone.utc)  # Apple Cocoa / Core Data (CFAbsoluteTime)

# Canonical ISO 8601 output format used throughout the report.
ISO_OUT = "%Y-%m-%dT%H:%M:%S.%fZ"

# Key names that make a field a "likely timestamp" regardless of its value's confidence.
DATE_KEY_RE = re.compile(r'date|time', re.IGNORECASE)

# Custom Apple-ish format: YYYY-MM-DD_HHMMSS-#### / +#### (trailing timezone offset).
CUSTOM_RE = re.compile(r'^(\d{4}-\d{2}-\d{2})_(\d{6})([+-]\d{4})$')

# Patterns used by --deepscan to find timestamps embedded *inside* larger strings
# (filenames, paths, log fragments) rather than only whole-value timestamps.
CUSTOM_SUBSTR_RE = re.compile(r'\d{4}-\d{2}-\d{2}_\d{6}[+-]\d{4}')
ISO_SUBSTR_RE = re.compile(
    r'\d{4}-\d{2}-\d{2}(?:[T _]\d{2}:?\d{2}:?\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?'
)
EPOCH_SUBSTR_RE = re.compile(r'(?<!\d)\d{9,19}(?:\.\d+)?(?!\d)')

# A single decoded interpretation of a raw value.
Candidate = namedtuple("Candidate", "iso fmt confidence dt")
# A row destined for the report.
Record = namedtuple("Record", "iso original fmt key confidence context dt")

# unit -> (report label, base confidence). Numeric epochs are inherently ambiguous, so
# well-anchored Unix variants rank above the Apple epochs, and HFS+ (historically noisy
# in this tool) ranks lowest.
UNIT_META = {
    "unix_s":  ("Unix_seconds", "high"),
    "unix_ms": ("Unix_milliseconds", "high"),
    "unix_ns": ("Unix_nanoseconds", "high"),
    "cocoa":   ("Cocoa_CoreData_2001", "medium"),
    "hfs":     ("HFS+_1904", "low"),
}

# unit -> (min, max) magnitude that value must fall in for the unit to even be considered.
# Overlaps are intentional: they are the genuine Unix-vs-Cocoa ambiguity we want to surface.
UNIT_RANGE = {
    "unix_s":  (1e8, 1e10),
    "unix_ms": (1e11, 1e13),
    "unix_ns": (1e17, 1e19),
    "cocoa":   (1e7, 4e9),
    "hfs":     (2.5e9, 4.3e9),
}

CONF_WEIGHT = {"high": 3, "medium": 2, "low": 1}


# ---------------------------------------------------------------------------
# Decoding helpers
# ---------------------------------------------------------------------------

def _epoch_to_dt(n, unit):
    """Convert a numeric epoch value to an aware UTC datetime, or None if out of range."""
    try:
        if unit == "unix_s":
            return datetime.fromtimestamp(n, tz=timezone.utc)
        if unit == "unix_ms":
            return datetime.fromtimestamp(n / 1000.0, tz=timezone.utc)
        if unit == "unix_ns":
            return datetime.fromtimestamp(n / 1e9, tz=timezone.utc)
        if unit == "cocoa":
            return EPOCH_2001 + timedelta(seconds=n)
        if unit == "hfs":
            return EPOCH_1904 + timedelta(seconds=n)
    except (OverflowError, OSError, ValueError):
        return None
    return None


def _numeric_candidates(n):
    """Return [(dt, label, confidence)] for every plausible epoch interpretation of n."""
    out = []
    for unit, (lo, hi) in UNIT_RANGE.items():
        if lo <= n < hi:
            dt = _epoch_to_dt(n, unit)
            if dt is not None:
                label, conf = UNIT_META[unit]
                out.append((dt, label, conf))
    return out


def _try_iso(s):
    """Parse an ISO 8601 string into an aware UTC datetime, or None."""
    t = s.strip()
    if t.endswith("Z"):
        t = t[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(t)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _try_custom(s):
    """Parse the custom YYYY-MM-DD_HHMMSS-#### format into aware UTC, or None."""
    m = CUSTOM_RE.match(s.strip())
    if not m:
        return None
    date_part, time_part, tz_part = m.groups()
    try:
        naive = datetime.strptime(f"{date_part}_{time_part}", "%Y-%m-%d_%H%M%S")
    except ValueError:
        return None
    sign = -1 if tz_part[0] == "-" else 1
    offset = timedelta(hours=int(tz_part[1:3]), minutes=int(tz_part[3:5])) * sign
    # Local time expressed with `offset` from UTC -> UTC = local - offset.
    return naive.replace(tzinfo=timezone.utc) - offset


def _try_float(s):
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def _score(cand):
    """Rank a (dt, label, confidence) candidate for default single-best selection."""
    dt, _label, conf = cand
    now = datetime.now(timezone.utc)
    w = CONF_WEIGHT[conf]
    if now.year - 15 <= dt.year <= now.year + 2:  # recency bonus toward the plausible core
        w += 2
    return w


def _scan_substrings(text):
    """Find timestamps embedded within a larger string (deepscan only).

    Returns [(dt, label, confidence)]. Matches are heuristic, so confidence is
    downgraded relative to whole-value decodes.
    """
    raw = []
    consumed = []  # spans already claimed by a more specific pattern

    def overlaps(start, end):
        return any(not (end <= s or start >= e) for s, e in consumed)

    # Most specific first: the custom YYYY-MM-DD_HHMMSS±HHMM format.
    for m in CUSTOM_SUBSTR_RE.finditer(text):
        dt = _try_custom(m.group())
        if dt is not None:
            raw.append((dt, "Custom_format", "medium"))
            consumed.append(m.span())

    # ISO 8601 (incl. bare dates); skip anything already claimed above.
    for m in ISO_SUBSTR_RE.finditer(text):
        if overlaps(*m.span()):
            continue
        dt = _try_iso(m.group())
        if dt is not None:
            raw.append((dt, "ISO_8601", "medium"))
            consumed.append(m.span())

    # Numeric epoch runs; plausibility gating happens later in _finalize.
    for m in EPOCH_SUBSTR_RE.finditer(text):
        if overlaps(*m.span()):
            continue
        num = _try_float(m.group())
        if num is not None:
            for dt, label, _conf in _numeric_candidates(num):
                raw.append((dt, label, "low"))

    return raw


def _finalize(raw, deepscan):
    """Gate candidates by a plausibility window and reduce to the reported set."""
    now_year = datetime.now(timezone.utc).year
    lo = 1970 if deepscan else 1990
    hi = now_year + (5 if deepscan else 2)
    plausible = [t for t in raw if lo <= t[0].year <= hi]
    if not plausible:
        return []
    chosen = plausible if deepscan else [max(plausible, key=_score)]

    out, seen = [], set()
    for dt, label, conf in chosen:
        dt = dt.astimezone(timezone.utc)
        iso = dt.strftime(ISO_OUT)
        dedupe_key = (iso, label)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        out.append(Candidate(iso, label, conf, dt))
    return out


def interpret_value(value, deepscan=False):
    """Return the list of plausible Candidate interpretations for a single leaf value."""
    # Native plist <date> objects arrive already parsed by plistlib (naive == UTC).
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return _finalize([(dt.astimezone(timezone.utc), "Plist_date", "high")], deepscan)

    # bool is an int subclass; never a timestamp.
    if isinstance(value, bool):
        return []

    if isinstance(value, (int, float)):
        return _finalize(_numeric_candidates(float(value)), deepscan)

    if isinstance(value, str):
        s = value.strip()
        if not s:
            return []
        raw = []
        dt = _try_iso(s)
        if dt is not None:
            raw.append((dt, "ISO_8601", "high"))
        dt = _try_custom(s)
        if dt is not None:
            raw.append((dt, "Custom_format", "high"))
        if not raw:  # only try numeric epochs when it isn't already a recognizable date string
            num = _try_float(s)
            if num is not None:
                raw.extend(_numeric_candidates(num))
        # deepscan: when the whole value isn't itself a timestamp, look for timestamps
        # embedded inside it (filenames, paths, log fragments).
        if deepscan and not raw:
            raw.extend(_scan_substrings(value))
        return _finalize(raw, deepscan)

    return []


# ---------------------------------------------------------------------------
# Validation & range filtering
# ---------------------------------------------------------------------------

def validate_timestamp(iso_str, years=15):
    """Validate an already-formatted ISO timestamp. Returns (is_valid, reason)."""
    try:
        dt = datetime.strptime(iso_str, ISO_OUT).replace(tzinfo=timezone.utc)
    except ValueError:
        return False, "invalid_format"

    now = datetime.now(timezone.utc)
    issues = []
    if dt > now:
        issues.append("future_date")
    if dt.year < 1970:
        issues.append("pre_1970")
    if dt.year < now.year - years:
        issues.append("too_old")
    elif dt.year > now.year + years:
        issues.append("too_future")

    if issues:
        return False, ",".join(issues)
    return True, "valid"


def _parse_day(s):
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)


class DateRangeFilter:
    """Inclusive UTC date-range filter built from the --on/--before/--after/--between flags."""

    def __init__(self, on=None, before=None, after=None, between=None):
        self.start = None  # inclusive lower bound (aware UTC)
        self.end = None    # inclusive upper bound (aware UTC)

        if on:
            day = _parse_day(on)
            self.start = day
            self.end = day + timedelta(days=1) - timedelta(microseconds=1)
        if after:
            self.start = _parse_day(after)
        if before:
            self.end = _parse_day(before) + timedelta(days=1) - timedelta(microseconds=1)
        if between:
            a, b = _parse_day(between[0]), _parse_day(between[1])
            if a > b:
                a, b = b, a
            self.start = a
            self.end = b + timedelta(days=1) - timedelta(microseconds=1)

        self.active = any([on, before, after, between])

    def matches(self, dt):
        if not self.active:
            return True
        if self.start and dt < self.start:
            return False
        if self.end and dt > self.end:
            return False
        return True


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def _orig_str(value):
    """Render a raw value for the Original Value column."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, bytes):
        return value[:64].hex()
    return str(value)


def _context_snippet(parent_dict, current_key, limit=200):
    """bulk_extractor-style context: the sibling scalar key/values around a timestamp."""
    if parent_dict is None:
        return ""
    parts = []
    for k, v in parent_dict.items():
        if k == current_key:
            continue
        if isinstance(v, (dict, list, bytes)):
            continue
        if isinstance(v, datetime):
            v = v.isoformat()
        parts.append(f"{k}={v}")
    snippet = "; ".join(parts)
    if len(snippet) > limit:
        snippet = snippet[: limit - 1] + "…"
    return snippet


def _try_embedded_plist(value):
    """If value is an embedded serialized plist (bplist bytes or inline XML), parse it."""
    if isinstance(value, bytes):
        head = value.lstrip()[:8]
        if head.startswith(b"bplist") or head.startswith(b"<?xml"):
            try:
                return plistlib.loads(value)
            except Exception:
                return None
        return None
    if isinstance(value, str):
        stripped = value.lstrip()
        if stripped.startswith("<?xml") and "plist" in value[:256].lower():
            try:
                return plistlib.loads(value.encode("utf-8"))
            except Exception:
                return None
    return None


def _walk(value, key, key_path, parent_dict, options, depth, records):
    """Recursively walk parsed plist data, collecting timestamp Records."""
    if isinstance(value, dict):
        for k, v in value.items():
            child = f"{key_path}/{k}" if key_path else k
            _walk(v, k, child, value, options, depth, records)
        return
    if isinstance(value, list):
        for i, item in enumerate(value):
            child = f"{key_path}[{i}]"
            _walk(item, None, child, None, options, depth, records)
        return

    # Embedded plist recursion (binary bplist blobs or inline-XML strings).
    if not options.nonest and depth < options.nestdepth:
        embedded = _try_embedded_plist(value)
        if embedded is not None:
            _walk(embedded, key, f"{key_path}→[embedded]", None,
                  options, depth + 1, records)
            return

    # Leaf value.
    candidates = interpret_value(value, options.deepscan)
    if not candidates:
        return

    key_is_date = bool(key and DATE_KEY_RE.search(key))
    context = "" if options.nocontext else _context_snippet(parent_dict, key)

    for c in candidates:
        # In default (non-deepscan) mode keep the noise down: only accept values whose key
        # looks temporal, native <date> objects, or high-confidence epoch decodes.
        if not options.deepscan and not (
            key_is_date or c.fmt == "Plist_date" or c.confidence == "high"
        ):
            return
        records.append(
            Record(c.iso, _orig_str(value), c.fmt, key_path, c.confidence, context, c.dt)
        )


def extract_records(plist_data, options):
    records = []
    _walk(plist_data, None, "", None, options, 0, records)
    return records


# ---------------------------------------------------------------------------
# File / directory processing
# ---------------------------------------------------------------------------

def get_file_type(plist_path):
    """Determine if the file is a plist or a bplist."""
    try:
        with open(plist_path, "rb") as file:
            header = file.read(8)
            if header.startswith(b"bplist"):
                return "bplist"
            if header.startswith(b"<?xml"):
                return "plist"
            return "unknown"
    except Exception as e:
        print(f"Error determining file type: {e}")
        return "error"


def build_headers(options):
    headers = ["UTC Timestamp", "Original Value", "Timestamp Format", "Key"]
    if not options.nocontext:
        headers.append("Context")
    headers += ["File Type", "File Name", "Full Path"]
    if options.deepscan:
        headers += ["Confidence", "Validation"]
    elif options.validate:
        headers.append("Validation")
    return headers


def process_file(plist_path, csv_writer, options):
    file_type = get_file_type(plist_path)

    try:
        with open(plist_path, "rb") as plist_file:
            plist_data = plistlib.load(plist_file)
    except (plistlib.InvalidFileException, ValueError):
        return
    except Exception as e:
        print(f"Skipping unreadable file {plist_path}: {e}")
        return

    records = extract_records(plist_data, options)
    full_path = os.path.abspath(plist_path)
    file_name = os.path.basename(plist_path)

    for r in records:
        if not options.date_filter.matches(r.dt):
            continue

        is_valid, reason = validate_timestamp(r.iso)
        # --validate alone drops anything that fails validation; --deepscan shows all.
        if options.validate and not options.deepscan and not is_valid:
            continue

        row = [r.iso, r.original, r.fmt, r.key]
        if not options.nocontext:
            row.append(r.context)
        row += [file_type, file_name, full_path]
        if options.deepscan:
            row += [r.confidence, reason]
        elif options.validate:
            row.append(reason)
        csv_writer.writerow(row)

    print(f"Evaluating: {plist_path}")


def process_directory(directory_path, output_file_path, options):
    with open(output_file_path, "w", newline="", encoding="utf-8") as output_file:
        csv_writer = csv.writer(output_file, delimiter="\t")
        csv_writer.writerow(build_headers(options))

        for root, _, files in os.walk(directory_path):
            for file in files:
                if file.lower().endswith((".plist", ".bplist")):
                    process_file(os.path.join(root, file), csv_writer, options)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

Options = namedtuple(
    "Options", "validate deepscan nocontext nonest nestdepth date_filter"
)


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description=(
            "Extract timestamps from PList files and convert them to ISO 8601 UTC "
            f"(Version {SCRIPT_VERSION})."
        )
    )
    parser.add_argument("directory_to_search", help="The directory path to search for PList files.")
    parser.add_argument("output_file_path", help="The path for the output TSV file.")
    parser.add_argument(
        "--validate", action="store_true",
        help="Mark/verify timestamps and drop entries that fail validation.",
    )
    parser.add_argument(
        "--deepscan", action="store_true",
        help="Maximum-intensity scan: search all fields with looser matching, detect "
             "timestamps embedded inside larger strings (filenames, paths, log fragments), "
             "and report everything including low-confidence and failed-validation rows "
             "(adds Confidence/Validation columns; may emit multiple candidates per value).",
    )
    parser.add_argument("--on", metavar="YYYY-MM-DD", help="Only timestamps on this UTC day.")
    parser.add_argument("--after", metavar="YYYY-MM-DD", help="Only timestamps on/after this UTC day.")
    parser.add_argument("--before", metavar="YYYY-MM-DD", help="Only timestamps on/before this UTC day.")
    parser.add_argument(
        "--between", nargs=2, metavar=("START", "END"),
        help="Only timestamps between two UTC days (inclusive).",
    )
    parser.add_argument("--nocontext", action="store_true", help="Omit the Context column for quicker/leaner runs.")
    parser.add_argument("--nonest", action="store_true", help="Skip embedded/nested plist extraction (fast triage).")
    parser.add_argument("--nestdepth", type=int, default=5, help="Max embedded-plist recursion depth (default 5).")
    args = parser.parse_args()

    # Validate mutually exclusive range flag combinations.
    if args.on and (args.before or args.after or args.between):
        parser.error("--on cannot be combined with --before/--after/--between.")
    if args.between and (args.before or args.after):
        parser.error("--between cannot be combined with --before/--after.")

    try:
        date_filter = DateRangeFilter(
            on=args.on, before=args.before, after=args.after, between=args.between
        )
    except ValueError:
        parser.error("Date arguments must be in YYYY-MM-DD format.")

    options = Options(
        validate=args.validate,
        deepscan=args.deepscan,
        nocontext=args.nocontext,
        nonest=args.nonest,
        nestdepth=max(0, args.nestdepth),
        date_filter=date_filter,
    )

    process_directory(args.directory_to_search, args.output_file_path, options)
    print(f"Processing complete. Results exported to {args.output_file_path}")


if __name__ == "__main__":
    main()
