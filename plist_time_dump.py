import os
import sys
import sqlite3
import plistlib
import csv
import re
from collections import namedtuple
from datetime import datetime, timezone, timedelta
from pathlib import Path

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
SCRIPT_VERSION = "3.0"

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
    "unix_s":     ("Unix_seconds", "high"),
    "unix_ms":    ("Unix_milliseconds", "high"),
    "unix_ns":    ("Unix_nanoseconds", "high"),
    "cocoa":      ("Cocoa_CoreData_2001", "medium"),
    "cocoa_ms":   ("Cocoa_milliseconds_2001", "medium"),
    "cocoa_ns":   ("Cocoa_nanoseconds_2001", "medium"),
    "hfs":        ("HFS+_1904", "low"),
}

# unit -> (min, max) magnitude that value must fall in for the unit to even be considered.
# Overlaps are intentional: they are the genuine Unix-vs-Cocoa ambiguity we want to surface.
#
# The Cocoa sub-second units matter more than their "medium" confidence suggests: iOS stores
# iMessage `message.date` (sms.db) as NANOSECONDS SINCE 2001, and read as nanoseconds since
# 1970 the same value decodes to a plausible-looking date exactly 31 years early
# (709936697517449984 -> 1992-06-30 instead of the true 2023-07-01). Without cocoa_ns the
# tool emitted a confident wrong answer on one of the most-examined artifacts in mobile
# forensics. Both readings are generated; _score's recency weighting settles it in default
# mode, and --deepscan shows both.
UNIT_RANGE = {
    "unix_s":     (1e8, 1e10),
    "unix_ms":    (1e11, 1e13),
    "unix_ns":    (1e17, 1e19),
    "cocoa":      (1e7, 4e9),
    "cocoa_ms":   (1e10, 1.5e12),
    "cocoa_ns":   (1e16, 1.5e18),
    "hfs":        (2.5e9, 4.3e9),
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
        if unit == "cocoa_ms":
            return EPOCH_2001 + timedelta(seconds=n / 1000.0)
        if unit == "cocoa_ns":
            return EPOCH_2001 + timedelta(seconds=n / 1e9)
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


def _archiver_classname(obj, objects):
    """Resolve an NSKeyedArchiver object's $class UID to its $classname string."""
    cls = obj.get("$class")
    if isinstance(cls, plistlib.UID) and 0 <= cls.data < len(objects):
        cls_obj = objects[cls.data]
        if isinstance(cls_obj, dict):
            return cls_obj.get("$classname")
    return None


def _resolve_nskeyedarchiver(parsed, max_depth=50, budget=None):
    """Resolve an NSKeyedArchiver archive into a plain dict/list/scalar tree.

    Returns None if `parsed` is not an NSKeyedArchiver archive. NSDate objects are
    converted to datetimes (from NS.time seconds-since-2001) so they decode as
    high-confidence Plist_date; an out-of-range NS.time (e.g. 1e18) is returned as
    the raw number instead of raising, via the same overflow-safe _epoch_to_dt used
    elsewhere -- a corrupt/adversarial embedded archive must never abort the whole
    directory walk. Cycles are broken via a per-path visited set; recursion is
    bounded by max_depth (object-graph depth, distinct from the embedded-plist
    nesting bounded by --nestdepth).

    Resolved objects are memoized so an object reachable via multiple references
    (a shared/diamond object graph) is resolved once instead of once per incoming
    path. Without this, a chain of N objects each referencing the next twice
    resolves in O(2**N) time -- max_depth bounds recursion *depth*, not the number
    of paths, so it does not stop this blowup in practice.

    The memo is keyed by **(idx, depth)**, not idx alone, and a result is cached
    whenever it contains no `<cycle:>` sentinel -- regardless of whether it
    contains a `<maxdepth:>` sentinel. The two sentinels have different
    path-dependence, which is the crux of this scheme:

    - `<maxdepth:idx>` comes purely from `depth < 0`, which never consults
      `visiting`. For a fixed idx and a fixed remaining `depth`, it is exactly
      the same result on every path that reaches it, so it (and anything built
      on top of it) is safe to cache *as long as the cache key includes depth*:
      a path that reaches the same idx with a different remaining budget can
      legitimately resolve further (or less far), and the key must not conflate
      those two cases. This is what fixes the case where the object graph is
      deeper than max_depth: previously, hitting the depth cutoff anywhere
      marked *every* ancestor back to the root "unclean" (regardless of depth),
      so nothing on that path was ever cached and the resolver reverted to a
      full, uncapped O(2**max_depth) blowup the moment any part of the graph
      exceeded max_depth by even one level -- keying on (idx, depth) instead of
      idx lets a depth-truncated result still be reused by every other
      reference site that hits the same idx at the same remaining depth
      (which is exactly what happens at every level of a shared/diamond graph),
      without ever conflating it with a resolution that had a different depth
      budget to work with.
    - `<cycle:idx>` comes from idx already being in `visiting`, i.e. an ancestor
      on *this specific call stack*. `visiting` only tracks the current DFS
      path (not every object ever visited), so whether a given idx triggers a
      cycle sentinel or resolves normally can depend on which path reached it.
      Cycle-tainted results must therefore never be cached for reuse on a
      different path -- unchanged from before, and (idx, depth) keying does not
      relax this: caching is still gated on the absence of a `<cycle:>`
      sentinel anywhere beneath the result.

    Residual, deliberately-accepted edge case (the same shape already accepted
    for the idx-only cache in the previous fix wave, and unaffected by adding
    depth to the key): two different paths reaching the same idx at the same
    remaining depth can still have different `visiting` ancestor sets. If the
    first path's resolution happens not to loop back into its own ancestors
    anywhere in idx's subtree, it caches a fully-resolved value; a later path
    with a different ancestor set that *would* have hit one of its own
    ancestors somewhere in that same subtree instead reuses the cached value.
    This can only make that second path's output *more* resolved than a strict
    per-path recomputation would have been (reusing a value some other,
    already-completed, terminating resolution legitimately produced) -- never
    wrong, never non-terminating, never a crash. It has no bearing on
    termination, which is the property this fix must actually guarantee.

    Bounded resolver work budget (fix wave 3)
    ------------------------------------------
    Unlike `<maxdepth:>`, a `<cycle:>` sentinel is inherently path-dependent
    (it comes from `visiting`, the current DFS ancestor set) and can never be
    blessed as safe to cache -- doing so risks reusing a "cycle" answer on a
    different path that wouldn't actually have looped. A single
    self-referencing object therefore permanently taints every one of its
    ancestors' cacheability, all the way to the root: every ancestor ANDs in
    that `False`, so nothing on that path is ever memoized and the resolver
    reverts to the full O(2**max_depth) blowup memoization was supposed to
    eliminate -- CONFIRMED via a `make_diamond_archive`-shaped graph whose
    bottom object references itself: depth 14=0.052s, 16=0.144s, 18=0.682s,
    20=3.229s (~4.5x per +2 depth, extrapolating to "never returns" by
    max_depth=50). Crucially, this happens *inside* this function, before
    `_walk`'s own node-visit budget (_WalkBudget) ever gets a chance to run,
    so that budget cannot help here.

    To bound this regardless of memoizability, `resolve_obj` also consumes
    one unit from `budget` (a _WalkBudget) on every entry, *before* doing any
    work or recursing into children -- mirroring `_walk`'s own
    `budget.consume()`-first pattern. Once exhausted, it returns a
    `<truncated:idx>` sentinel immediately, without recursing further, just
    like hitting `depth < 0`. This caps the *total number of object
    resolutions attempted* across the whole call (cache hits and cycle hits
    are already O(1) and consume no budget), regardless of how much taint
    prevents memoization from helping -- recursion itself, not memoization,
    is what guarantees termination in the worst (all-tainted) case.

    Like `<maxdepth:>`, `<truncated:>` is always safe to cache
    (cacheable=True): `budget.remaining` only ever decreases within one
    top-level call, so once it reaches zero it stays zero for the rest of
    the call -- a cached truncated result can never be "wrong" later, because
    a hypothetical fresh recomputation of the same (idx, depth) at any later
    point would see an equally-or-more exhausted budget and truncate at
    least as eagerly. Conversely, a cache hit that returns an earlier,
    more-complete (non-truncated) resolution computed before the budget ran
    out is the same accepted "more resolved than a strict recomputation"
    relaxation already documented above for (idx, depth) reuse across
    differing `visiting` sets -- never wrong, never non-terminating.

    `budget` defaults to a fresh, generously-sized _WalkBudget when not
    supplied (e.g. when tests call this function directly), matching the
    same "a caller that forgets to thread one through still gets a bounded
    call, never an unbounded one" default `_walk`/`_process_leaf` already
    use. The normal embedded-plist call site (_process_leaf) threads its own
    per-source budget in, so resolver work and walk work both count against
    one combined per-source cap.

    Two-tier memo: idx-only for depth-independent ("pure") results (fix wave 3)
    -----------------------------------------------------------------------
    Keying the memo by (idx, depth) (rather than idx alone) is necessary for
    correctness when a subtree's resolution actually depends on how much
    depth/budget remains -- but most shared subtrees in practice do NOT
    depend on that: an object whose own resolution never hits a
    `<maxdepth:>` or `<truncated:>` sentinel anywhere beneath it would
    resolve to the *exact same value* no matter how much additional depth
    or budget the caller had to spare, because it never needed any of the
    extra. Keying such a result by depth anyway means a subtree reachable
    at N distinct remaining-depth values gets fully re-resolved and
    re-materialized N times instead of once -- CONFIRMED on a crafted "hub"
    archive (a 45-level wrapper chain, each level referencing
    `[next_wrapper, shared]`, where `shared` is a single ~3000-element
    array reached at 45 different remaining depths, one per wrapper level):
    keying by (idx, depth) alone measured 4.5s / ~21.6MB peak (tracemalloc)
    from an 0.11MB blob, versus 0.1s / ~0.5MB when `shared` is resolved
    once and reused -- roughly a 45-48x time-and-memory multiplier, scaling
    with the number of distinct depths a shared subtree is reached at (up
    to `max_depth + 1` in the worst case). This is a real cost multiplier
    on ordinary (non-adversarial, acyclic) shared object graphs, not merely
    an adversarial-input concern, and fix wave 3's resolver work budget
    (above) does not on its own bound it: the total work for a hub-shaped
    archive like the one above is well under a generous budget, so the
    budget never engages and the multiplier goes through unchecked.

    The fix: track a second boolean, `pure`, alongside `cacheable`, meaning
    "this value contains no `<maxdepth:>` and no `<truncated:>` sentinel
    anywhere beneath it" (propagated by ANDing across children exactly like
    `cacheable` is). A result with `cacheable and pure` is cached in a
    *second*, idx-only memo (`memo_pure`), consulted before the
    (idx, depth)-keyed one; every other reference to the same idx --
    regardless of what remaining depth or budget it arrives with -- reuses
    it immediately, dropping the hub case above from 45 materializations to
    1. A result that is `cacheable` but NOT `pure` (contains a `<maxdepth:>`
    or `<truncated:>` sentinel somewhere) keeps using the (idx, depth)-keyed
    memo exactly as wave 2 established, since such a result genuinely can
    differ given a different remaining depth/budget.

    Why this is safe: `<maxdepth:>` is excluded from `pure` because it is
    (by construction, see above) the one sentinel whose value is a direct
    function of the `depth` parameter -- a deeper remaining budget can
    legitimately resolve further, so an idx-only cache entry could return a
    stale, less-resolved value for a caller with more depth to spare than
    whoever populated the cache. `<truncated:>` is conservatively excluded
    from `pure` too (even though, per its own section above, a
    budget-exhaustion result happens to be safe to reuse regardless of
    depth): keeping it out of the stronger idx-only tier costs nothing in
    the cases this fix targets (a `pure`, i.e. fully clean, `shared` node
    in a hub-shaped graph never hits either sentinel at all) and avoids
    needing to separately re-justify promoting a budget-dependent result
    into a cache tier whose entire premise is "safe at any depth." A
    `<cycle:>` result is never written to either memo (`cacheable=False`
    still gates both), unchanged from before, and the `idx in visiting`
    cycle check still runs *before* the `memo_pure` lookup -- so a genuine
    self-reference on the current path is still always caught, even for an
    idx that has an unrelated, already-cached `memo_pure` entry from a
    completely different (non-self-referencing) path. Reusing that
    unrelated entry in that situation is the same accepted "may return a
    more-resolved value than a strict per-path recomputation, never wrong,
    never non-terminating" relaxation already established above for
    (idx, depth) reuse across differing `visiting` sets -- this just
    extends the same, already-accepted risk shape to the idx-only tier too.
    """
    if not (isinstance(parsed, dict)
            and parsed.get("$archiver") == "NSKeyedArchiver"
            and isinstance(parsed.get("$objects"), list)
            and "$top" in parsed):
        return None
    if budget is None:
        budget = _WalkBudget("<unspecified source>")
    objects = parsed["$objects"]
    # idx -> fully-resolved value, for results that are both cacheable (no
    # <cycle:> anywhere) AND pure (no <maxdepth:>/<truncated:> anywhere) --
    # safe to reuse regardless of the caller's remaining depth/budget.
    memo_pure = {}
    # (idx, depth) -> resolved value, for cacheable-but-not-pure results
    # (contain a <maxdepth:>/<truncated:> sentinel somewhere, so a different
    # remaining depth/budget could legitimately resolve differently). See
    # docstring above for why both tiers are needed and why this is safe.
    memo = {}

    def resolve_ref(ref, visiting, depth):
        """Returns (value, cacheable, pure, vdepth). `cacheable` is False only
        if a `<cycle:>` sentinel was produced anywhere within `value`. `pure`
        is False only if a `<maxdepth:>` or `<truncated:>` sentinel was
        produced anywhere within `value` -- neither makes a result
        uncacheable, but only a `cacheable and pure` result is safe to
        reuse regardless of remaining depth/budget; see the docstring above
        for why. `vdepth` is the materialized structural depth of `value`,
        used to keep max_depth a hard bound across memo reuse."""
        if isinstance(ref, plistlib.UID):
            idx = ref.data
            # Charge EVERY reference, including memo hits, cycle returns, and
            # out-of-range refs: each costs a real call, so charging only
            # resolve_obj entries made the true cost O(budget x fanout) rather
            # than the O(budget) this budget is supposed to guarantee.
            if not budget.consume():
                return f"<truncated:{idx}>", True, False, 0
            if not (0 <= idx < len(objects)):
                return None, True, True, 0
            if idx in visiting:
                return f"<cycle:{idx}>", False, False, 0
            if idx in memo_pure:
                value, vdepth = memo_pure[idx]
                # Reuse only when the cached subtree fits within the depth
                # remaining HERE. A memo hit consumes no depth, so splicing in
                # a subtree deeper than `depth` would let the resolved tree
                # exceed max_depth without bound -- which compounds across
                # levels and ends in an uncaught RecursionError during _walk.
                if vdepth <= depth:
                    return value, True, True, vdepth
            # NOT an `else`: when a memo_pure entry exists but does not fit, the
            # depth-keyed tier is still the right place to look. Gating this on
            # `else` disabled memoization entirely below the fit threshold --
            # every reference re-resolved the whole subtree, measured ~1000x the
            # work and enough to exhaust the node budget on legitimate archives.
            key = (idx, depth)
            if key in memo:
                value, vdepth = memo[key]
                return value, True, False, vdepth
            value, cacheable, pure, vdepth = resolve_obj(
                idx, visiting | {idx}, depth - 1)
            if cacheable:
                if pure:
                    memo_pure[idx] = (value, vdepth)
                else:
                    memo[(idx, depth)] = (value, vdepth)
            return value, cacheable, pure, vdepth
        return ref, True, True, 0

    def resolve_obj(idx, visiting, depth):
        if depth < 0:
            return f"<maxdepth:{idx}>", True, False, 0
        obj = objects[idx]
        if obj == "$null":
            return None, True, True, 0
        if not isinstance(obj, dict):
            # primitive object: string / number / data / etc.
            return obj, True, True, 0
        name = _archiver_classname(obj, objects)
        if name in ("NSDictionary", "NSMutableDictionary"):
            cacheable = True
            pure = True
            child = 0
            keys = []
            for k in obj.get("NS.keys", []):
                kv, kc, kp, kd = resolve_ref(k, visiting, depth)
                keys.append(kv)
                cacheable = cacheable and kc
                pure = pure and kp
                child = max(child, kd)
            vals = []
            for v in obj.get("NS.objects", []):
                vv, vc, vp, vd = resolve_ref(v, visiting, depth)
                vals.append(vv)
                cacheable = cacheable and vc
                pure = pure and vp
                child = max(child, vd)
            return {str(k): v for k, v in zip(keys, vals)}, cacheable, pure, child + 1
        if name in ("NSArray", "NSMutableArray", "NSSet", "NSMutableSet"):
            cacheable = True
            pure = True
            child = 0
            vals = []
            for v in obj.get("NS.objects", []):
                vv, vc, vp, vd = resolve_ref(v, visiting, depth)
                vals.append(vv)
                cacheable = cacheable and vc
                pure = pure and vp
                child = max(child, vd)
            return vals, cacheable, pure, child + 1
        if name in ("NSString", "NSMutableString"):
            return obj.get("NS.string"), True, True, 0
        if name in ("NSData", "NSMutableData"):
            return obj.get("NS.data"), True, True, 0
        if name == "NSDate":
            t = obj.get("NS.time")
            # bool is an int subclass; never a timestamp (see interpret_value).
            # Without this, NS.time=True would decode as 2001-01-01T00:00:01Z.
            if isinstance(t, (int, float)) and not isinstance(t, bool):
                dt = _epoch_to_dt(t, "cocoa")
                return (dt if dt is not None else t), True, True, 0
            return t, True, True, 0
        if name in ("NSNumber", "NSValue"):
            for k in ("NS.intValue", "NS.doubleValue", "NS.numberValue"):
                if k in obj:
                    return obj[k], True, True, 0
            return None, True, True, 0
        # Generic object: resolve every field except archiver metadata.
        cacheable = True
        pure = True
        child = 0
        result = {}
        for k, v in obj.items():
            if k == "$class":
                continue
            rv, rc, rp, rd = resolve_ref(v, visiting, depth)
            result[k] = rv
            cacheable = cacheable and rc
            pure = pure and rp
            child = max(child, rd)
        return result, cacheable, pure, child + 1

    top = parsed["$top"]
    if isinstance(top, dict):
        out = {}
        for k, v in top.items():
            val, _cacheable, _pure, _vdepth = resolve_ref(v, set(), max_depth)
            out[str(k)] = val
        return out
    val, _cacheable, _pure, _vdepth = resolve_ref(top, set(), max_depth)
    return val


# Upper bound on the total number of nodes _walk will visit across one
# top-level call (one plist file, or one SQLite cell's embedded-plist
# expansion) -- see _WalkBudget below for why this exists.
#
# This does NOT protect against ordinary forensic data being large: it exists
# solely to bound adversarial/aliased object graphs (a crafted NSKeyedArchiver
# DAG designed to re-expand combinatorially -- see _resolve_nskeyedarchiver
# and _WalkBudget's docstrings) to a large, finite constant instead of letting
# that blowup run unchecked. Ordinary multi-megabyte forensic plists (an
# iTunes/Music Library.xml, an iOS Manifest.plist, LaunchServices/bundle
# caches, sysdiagnose output, ...) are routine and are NOT adversarial -- they
# are exactly the timestamp-dense files this tool exists to scan, and they
# must not be truncated by this budget. MEASURED node density: a compact
# bplist can hit a 100,000-node budget at only ~1.15 MB, and a realistic
# bplist with long unique strings/paths at ~3.79 MB -- both comfortably
# smaller than routine multi-MB artifacts, which is why the limit below is
# 1,000,000 (a real directory walk visiting that many *total* nodes for one
# single embedded structure in one single file would itself be unusual) and
# not 100,000. Cost: the budget bounds total work at O(limit) -- every
# reference is charged, including memo and cycle hits -- so exhausting a
# 1,000,000-node budget on an adversarial graph settles in the low seconds.
# (Charging only on cache misses made the real cost O(limit x fanout), where
# an attacker picks the fanout: a 1.6 KB crafted file measured 105s.)
MAX_WALK_NODES = 1_000_000

# Sentinel written to the "Timestamp Format" column (schema/column count is
# UNCHANGED -- see _emit_truncation_row) marking a row that notes a source
# whose walk/resolve was cut short by the node-visit budget. Exists so
# truncation is visible to an analyst reading only the TSV output, not just
# a stdout warning that may have scrolled past, been redirected away, or
# never been captured at all -- for a forensic tool, a truncated source
# being invisible in the evidence file itself is a real gap: a downstream
# reader of the TSV alone would otherwise have no way to know a source was
# incomplete.
TRUNCATION_MARKER = "TRUNCATED_NODE_BUDGET"

# Cap on how many distinct per-source truncation warnings _WalkBudget prints
# to stdout during one run before falling silent (see _TruncationTracker).
# An adversarial or heavily corrupted directory (many crafted files/cells)
# must not be able to flood stdout with one warning line per source; the
# TRUNCATION_MARKER row (above) remains the authoritative, complete record
# of every truncated source regardless of how many warnings were printed.
MAX_PRINTED_TRUNCATION_WARNINGS = 10


class _TruncationTracker:
    """Mutable, run-scoped counter of how many distinct sources have had
    their _WalkBudget exhausted (i.e. truncated output) during one
    process_directory() call (or a standalone caller's own equivalent).

    Used only to (a) cap the number of per-source warnings _WalkBudget
    prints to stdout at MAX_PRINTED_TRUNCATION_WARNINGS, and (b) produce one
    run-level summary note if any source truncated. It is never consulted
    by _WalkBudget.consume() for anything that affects *what* gets
    truncated: each source's own budget (limit/remaining) stays completely
    independent of every other source's, exactly as before -- this only
    ever changes whether a given exhaustion gets a line printed about it,
    never whether the exhaustion itself happens or how much of that source
    got walked.

    Defaults to None (untracked -- every warning gets printed, uncapped)
    when a caller doesn't supply one, matching every other _WalkBudget-
    adjacent default in this module, so counts never leak between unrelated
    calls (e.g. two independent test cases, or a process_file call made
    outside of process_directory).
    """

    __slots__ = ("truncated_sources",)

    def __init__(self):
        self.truncated_sources = 0

    def note_truncated(self):
        """Record one more truncated source. Returns True if a per-source
        warning should still be printed for it (still under the cap),
        False once warnings are suppressed for the rest of this run."""
        self.truncated_sources += 1
        return self.truncated_sources <= MAX_PRINTED_TRUNCATION_WARNINGS

    def summary(self):
        """A one-line, run-level note if any source truncated this run
        (fix wave 3, Important-5), or None if none did."""
        if self.truncated_sources == 0:
            return None
        # Deliberately does not name a single cause: sources are counted here
        # for the node-visit budget, the interpreter recursion limit, and an
        # aborted database scan alike.
        return (
            f"Note: {self.truncated_sources} source(s) had truncated output "
            f"during this run -- see '{TRUNCATION_MARKER}' rows in the output "
            f"file for exactly which ones."
        )


class _WalkBudget:
    """Mutable node-visit budget shared across one top-level _walk/_process_leaf
    call tree.

    Memoizing _resolve_nskeyedarchiver (previous fix wave) makes the *resolver*
    O(1) per shared node, but its output is an aliased DAG: a shared node is
    the very same object at every reference site that points to it (e.g.
    `tree["root"][0] is tree["root"][1]`). _walk has no memo of its own -- it
    builds a distinct key_path string per path and happily re-descends into
    that same shared object once per incoming path -- so the O(2**depth)
    blowup the resolver's memoization was supposed to eliminate simply moved
    one layer up the call stack, into the walk. Fixing only the resolver made
    the resolver fast (confirmed: ~0.0002s even at depth 48) while
    extract_records as a whole stayed exponential (confirmed: depth 20 ~6.4s,
    depth 22 ~25s, ~4x per +2 depth) because of this.

    This budget bounds the total number of nodes _walk will visit, so a
    re-expanded aliased DAG cannot blow up the *walk* combinatorially either:
    once exhausted, _walk stops descending (returns) instead of recursing
    further. It never raises; the analyst gets one warning line plus whatever
    records were already collected up to that point, rather than losing the
    whole scan to a hang. Truncation is also surfaced in the TSV itself (see
    TRUNCATION_MARKER / _emit_truncation_row) via the `truncated` property
    below, not only via the stdout warning.

    Deliberately scoped per top-level entry point -- a fresh instance per
    extract_records() call (one plist file) and per SQLite cell (one column
    value) in process_sqlite_file -- rather than shared globally across an
    entire directory or database scan. A large but *legitimate* scan (many
    independent files, or many independent rows/columns, none of which is an
    aliased DAG) must never accumulate node-visits toward truncating later,
    unrelated, non-adversarial output; only a single pathological structure
    should ever be able to exhaust its own budget. `tracker` (a
    _TruncationTracker) is the one deliberate exception, and only for
    whether a stdout warning gets *printed* -- never for the budget/limit
    itself; see _TruncationTracker.
    """

    __slots__ = ("remaining", "limit", "_warned", "_source", "_tracker")

    def __init__(self, source, limit=None, tracker=None):
        # `limit` resolves the current MAX_WALK_NODES at call time (rather
        # than a value frozen into the default argument at module-import
        # time) so tests can monkeypatch the module constant to exercise
        # truncation deterministically and fast, without needing an actual
        # multi-second adversarial archive to exhaust a million-node budget.
        if limit is None:
            limit = MAX_WALK_NODES
        self.remaining = limit
        self.limit = limit
        self._warned = False
        self._source = source
        self._tracker = tracker

    @property
    def truncated(self):
        """True once this budget has been exhausted at least once -- i.e.
        the walk/resolve for this source was cut short and its collected
        output is incomplete. Callers use this to emit a TRUNCATION_MARKER
        row so truncation is visible in the TSV, not only in a stdout
        warning."""
        return self._warned

    def consume(self):
        """Charge one node visit. Returns True if the walk may proceed, False
        if the budget is exhausted (caller must stop descending without
        recursing further). Prints at most one warning per exhausted budget
        -- never one per node -- and, once more than
        MAX_PRINTED_TRUNCATION_WARNINGS sources have truncated during this
        run (per `tracker`), suppresses further per-source prints (printing
        one "further warnings suppressed" line exactly once instead)."""
        if self.remaining <= 0:
            if not self._warned:
                self._warned = True
                if self._tracker is not None:
                    if self._tracker.note_truncated():
                        self._print_warning()
                    elif self._tracker.truncated_sources == MAX_PRINTED_TRUNCATION_WARNINGS + 1:
                        print(
                            f"Warning: more than {MAX_PRINTED_TRUNCATION_WARNINGS} "
                            f"sources have had truncated output during this run "
                            f"-- further per-source truncation warnings are "
                            f"suppressed for the rest of this run (see "
                            f"'{TRUNCATION_MARKER}' rows in the output file "
                            f"for the complete list)."
                        )
                else:
                    self._print_warning()
            return False
        self.remaining -= 1
        return True

    def _print_warning(self):
        print(
            f"Warning: node-visit budget ({self.limit}) exhausted "
            f"while walking {self._source} -- output for this source "
            f"is truncated."
        )


def _walk(value, key, key_path, parent_dict, options, depth, records, budget=None):
    """Recursively walk parsed plist data, collecting timestamp Records.

    `budget` (a _WalkBudget) bounds the total number of nodes visited across
    this whole top-level call -- see _WalkBudget for why this is necessary
    even though _resolve_nskeyedarchiver's own results are memoized. Defaults
    to a fresh (generous) budget when not supplied, so a caller that forgets
    to thread one through still gets a bounded walk rather than an unbounded
    one.
    """
    if budget is None:
        budget = _WalkBudget("<unspecified source>")
    if not budget.consume():
        return
    if isinstance(value, dict):
        for k, v in value.items():
            child = f"{key_path}/{k}" if key_path else k
            _walk(v, k, child, value, options, depth, records, budget)
        return
    if isinstance(value, list):
        for i, item in enumerate(value):
            child = f"{key_path}[{i}]"
            _walk(item, None, child, None, options, depth, records, budget)
        return
    _process_leaf(value, key, key_path, parent_dict, options, depth, records, budget)


def _process_leaf(value, key, key_path, parent_scalars, options, depth, records, budget=None):
    """Decode a single leaf value into Records, recursing into embedded plists.

    Used by both the plist walker and the SQLite scanner. `parent_scalars` is a
    dict (or None) of sibling scalar values, used for the Context column.
    `budget` is forwarded both to `_resolve_nskeyedarchiver` (bounds the
    resolver's own work; see its docstring for why it needs this too) and to
    any recursive _walk call this leaf triggers (an embedded plist); see
    _WalkBudget. Defaults to a fresh budget when not supplied, matching
    _walk's default.
    """
    if budget is None:
        budget = _WalkBudget("<unspecified source>")
    # Embedded plist recursion (binary bplist blobs or inline-XML strings).
    if not options.nonest and depth < options.nestdepth:
        embedded = _try_embedded_plist(value)
        if embedded is not None:
            resolved = _resolve_nskeyedarchiver(embedded, budget=budget)
            tree = resolved if resolved is not None else embedded
            _walk(tree, key, f"{key_path}→[embedded]", None,
                  options, depth + 1, records, budget)
            return

    candidates = interpret_value(value, options.deepscan)
    if not candidates:
        return

    key_is_date = bool(key and DATE_KEY_RE.search(key))
    context = "" if options.nocontext else _context_snippet(parent_scalars, key)

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


def extract_records(plist_data, options, source="<data>", budget=None,
                    records=None):
    """`budget` may be supplied by the caller (process_file does, so it can
    inspect `budget.truncated` afterward and emit a TRUNCATION_MARKER row);
    defaults to a fresh, untracked budget otherwise, matching every other
    _WalkBudget default in this module.

    `records` may likewise be a caller-owned list, which is appended to in
    place. That lets a caller keep everything collected so far even when the
    walk dies partway (RecursionError on a pathological graph) -- partial
    evidence still belongs in the report."""
    if budget is None:
        budget = _WalkBudget(source)
    if records is None:
        records = []
    _walk(plist_data, None, "", None, options, 0, records, budget)
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


def get_file_kind(path):
    """Classify a file by magic bytes: 'plist', 'bplist', 'sqlite', or 'unknown'."""
    try:
        with open(path, "rb") as f:
            header = f.read(16)
    except Exception as e:
        print(f"Error reading {path}: {e}")
        return "unknown"
    if header.startswith(b"bplist"):
        return "bplist"
    if header.startswith(b"SQLite format 3\x00"):
        return "sqlite"
    stripped = header.lstrip()
    if stripped.startswith(b"<?xml") or stripped.startswith(b"<plist"):
        return "plist"
    return "unknown"


def build_headers(options):
    # Source identification comes before Key/Context so the wide, free-text
    # columns (Context especially, which can carry embedded newlines from
    # message bodies) sit at the right-hand end and don't push the columns an
    # analyst scans first off the screen.
    headers = ["UTC Timestamp", "Original Value", "Timestamp Format",
               "File Type", "File Name", "Full Path", "Key"]
    if not options.nocontext:
        headers.append("Context")
    if options.deepscan:
        headers += ["Confidence", "Validation"]
    elif options.validate:
        headers.append("Validation")
    return headers


def _emit_records(records, file_type, source_path, csv_writer, options):
    """Filter, validate, and write one TSV row per surviving Record."""
    full_path = os.path.abspath(source_path)
    file_name = os.path.basename(source_path)
    for r in records:
        if not options.date_filter.matches(r.dt):
            continue
        is_valid, reason = validate_timestamp(r.iso)
        # --validate alone drops anything that fails validation; --deepscan shows all.
        if options.validate and not options.deepscan and not is_valid:
            continue
        row = [r.iso, r.original, r.fmt, file_type, file_name, full_path, r.key]
        if not options.nocontext:
            row.append(r.context)
        if options.deepscan:
            row += [r.confidence, reason]
        elif options.validate:
            row.append(reason)
        csv_writer.writerow(row)


def _emit_truncation_row(csv_writer, options, file_type, source_path, key_hint):
    """Write one clearly-marked row noting that `source_path` (or, for a
    SQLite cell, the specific `key_hint` identifying which cell) had its
    walk/resolve cut short by the node-visit budget (fix wave 3, Important-4).

    Reuses the exact same column layout build_headers/_emit_records produce
    -- schema/column count is unchanged -- with sentinel values in the
    timestamp-ish fields (TRUNCATION_MARKER in "Timestamp Format") so it can
    never be mistaken for a real decoded timestamp. Written directly
    (bypassing _emit_records) so it is never silently dropped by
    --validate's fails-validation filter or a --on/--before/--after/
    --between date-range filter the way a normal Record would be: an
    analyst who filtered to one specific day must still be told that a
    source's output was incomplete, not have that fact filtered away too.
    """
    full_path = os.path.abspath(source_path)
    file_name = os.path.basename(source_path)
    row = ["", "", TRUNCATION_MARKER, file_type, file_name, full_path, key_hint]
    if not options.nocontext:
        row.append("")
    if options.deepscan:
        row += ["", "truncated"]
    elif options.validate:
        row.append("truncated")
    csv_writer.writerow(row)


def process_file(plist_path, csv_writer, options, tracker=None):
    file_type = get_file_type(plist_path)

    try:
        with open(plist_path, "rb") as plist_file:
            plist_data = plistlib.load(plist_file)
    except (plistlib.InvalidFileException, ValueError):
        return
    except Exception as e:
        print(f"Skipping unreadable file {plist_path}: {e}")
        return

    budget = _WalkBudget(plist_path, tracker=tracker)
    # A pathological object graph can still nest deeply enough to exhaust the
    # interpreter stack. Catch it per source: one crafted file must never abort
    # the directory walk and silently leave later evidence unscanned.
    recursion_hit = False
    # Caller-owned list so anything collected before a mid-walk RecursionError
    # is still reported rather than discarded.
    records = []
    try:
        extract_records(plist_data, options, source=plist_path, budget=budget,
                        records=records)
    except RecursionError:
        recursion_hit = True
        # Route through the tracker so this shares the run-wide warning cap and
        # is counted in the run summary, exactly like a budget truncation.
        if tracker is None or tracker.note_truncated():
            print(f"Recursion limit hit while walking {plist_path} -- "
                  f"output for this source is truncated.")
    _emit_records(records, file_type, plist_path, csv_writer, options)
    if budget.truncated or recursion_hit:
        hint = ("<truncated: recursion limit exceeded>" if recursion_hit
                else "<truncated: node-visit budget exceeded>")
        _emit_truncation_row(csv_writer, options, file_type, plist_path, hint)
    print(f"Evaluating: {plist_path}")


def process_sqlite_file(db_path, csv_writer, options, tracker=None):
    """Scan a SQLite database read-only for timestamps in columns and embedded plists."""
    try:
        uri = Path(db_path).resolve().as_uri() + "?mode=ro&immutable=1"
        conn = sqlite3.connect(uri, uri=True)
    except Exception as e:
        print(f"Skipping unreadable database {db_path}: {e}")
        return

    # Decode TEXT leniently; BLOBs still arrive as bytes.
    conn.text_factory = lambda b: b.decode("utf-8", "replace")
    records = []
    truncated_keys = []
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\'"
        )
        tables = [row[0] for row in cur.fetchall()]

        for table in tables:
            # Double-quoted identifier with embedded double quotes escaped by doubling.
            # Without this, a table literally named e.g. foo"bar breaks the f-string
            # quoting, the query raises, and the whole table is silently skipped
            # (missed evidence) -- rather than just having its name escaped correctly.
            quoted = '"' + table.replace('"', '""') + '"'
            try:
                cur.execute(f'PRAGMA table_info({quoted})')
                cols = [r[1] for r in cur.fetchall()]
            except sqlite3.DatabaseError:
                continue

            has_rowid = True
            try:
                cur.execute(f'SELECT rowid, * FROM {quoted}')
            except sqlite3.DatabaseError:
                has_rowid = False
                try:
                    cur.execute(f'SELECT * FROM {quoted}')
                except sqlite3.DatabaseError:
                    continue

            # Stream row-by-row (iterate the cursor) rather than fetchall(): real
            # forensic databases can be hundreds of MB to GB, and materializing an
            # entire table in memory risks OOM.
            for idx, row in enumerate(cur):
                if has_rowid:
                    rid, values = row[0], row[1:]
                else:
                    rid, values = idx, row
                parent = dict(zip(cols, values))
                for col, val in zip(cols, values):
                    if val is None:
                        continue
                    key_path = f"{table}.{col}(rowid={rid})"
                    # Fresh budget per cell (not shared across the whole table/
                    # database): see _WalkBudget -- a large but legitimate scan
                    # of many independent rows/columns must never accumulate
                    # toward truncating later, unrelated output. `tracker` is
                    # shared (run-scoped) purely for warning-cap/summary
                    # purposes -- see _TruncationTracker.
                    budget = _WalkBudget(f"{db_path}:{key_path}", tracker=tracker)
                    # Catch RecursionError per cell: one crafted BLOB must never
                    # abort the scan and silently leave later evidence unscanned.
                    try:
                        _process_leaf(val, col, key_path, parent, options, 0,
                                      records, budget)
                    except RecursionError:
                        # Route through the tracker so this shares the run-wide
                        # warning cap and is counted in the run summary.
                        if tracker is None or tracker.note_truncated():
                            print(f"Recursion limit hit while walking "
                                  f"{db_path}:{key_path} -- output for this "
                                  f"cell is truncated.")
                        truncated_keys.append(key_path)
                        continue
                    if budget.truncated:
                        truncated_keys.append(key_path)
    except sqlite3.DatabaseError as e:
        # Emit whatever was collected before the failure rather than discarding
        # it -- partial evidence still belongs in the report. The scan of this
        # database stopped early, so mark it: a reader of the TSV alone must be
        # able to see the database was abandoned partway, not mistake the rows
        # already written for a complete scan.
        if tracker is None or tracker.note_truncated():
            print(f"Stopping scan of database {db_path}: {e}")
        truncated_keys.append("<truncated: database error>")
    finally:
        conn.close()

    _emit_records(records, "sqlite", db_path, csv_writer, options)
    for key_path in truncated_keys:
        _emit_truncation_row(csv_writer, options, "sqlite", db_path, key_path)
    print(f"Evaluating: {db_path}")


def process_directory(directory_path, output_file_path, options):
    # One tracker shared across the whole run (fix wave 3, Important-4/5):
    # caps per-source stdout truncation warnings at MAX_PRINTED_TRUNCATION_
    # WARNINGS and, if any source truncated, prints one run-level summary
    # note at the end. Purely a reporting concern -- it never affects any
    # individual source's own node-visit budget/limit; see
    # _TruncationTracker.
    tracker = _TruncationTracker()
    with open(output_file_path, "w", newline="", encoding="utf-8") as output_file:
        csv_writer = csv.writer(output_file, delimiter="\t")
        csv_writer.writerow(build_headers(options))

        for root, _, files in os.walk(directory_path):
            for file in files:
                path = os.path.join(root, file)
                kind = get_file_kind(path)
                if kind in ("plist", "bplist"):
                    process_file(path, csv_writer, options, tracker=tracker)
                elif kind == "sqlite":
                    process_sqlite_file(path, csv_writer, options, tracker=tracker)

    summary = tracker.summary()
    if summary:
        print(summary)


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
