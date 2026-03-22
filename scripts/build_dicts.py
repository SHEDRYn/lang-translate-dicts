#!/usr/bin/env python3
"""
build_dicts.py — builds offline dictionary .db files for the mobile app.

Sources:
  - WikDict CDN    (download.wikdict.com)     for en↔ru pairs
  - Hans Wehr dict (github: wizsk/arabic_lexicons) for ar↔en pairs
  - Pivot en→ru    (built en_ru.db)           for ar↔ru pairs

Output schema matches dictionaryService.ts expectations:
  translations(id, word, translation, transcription, example, example_translation)

Usage:
  python build_dicts.py                  # build all 6 pairs
  python build_dicts.py --pair en_ru     # build one specific pair
  python build_dicts.py --pair en_ru ru_en

Output files land in scripts/dicts/
Intermediate downloads are cached in scripts/cache/  (safe to delete)
"""

import argparse
import json
import re
import sqlite3
import sys
import urllib.request
from pathlib import Path

# ─── Config ────────────────────────────────────────────────────────────────────

ROOT = Path(__file__).parent
OUTPUT_DIR = ROOT / "dicts"
CACHE_DIR = ROOT / "cache"

WIKDICT_BASE = "https://download.wikdict.com/dictionaries/sqlite/2"
ARABIC_LEXICONS_DB_URL = (
    "https://raw.githubusercontent.com/wizsk/arabic_lexicons"
    "/main/assets/data/db/db.sqlite.zip"
)

# Each pair: (src_lang, tgt_lang, data_source)
ALL_PAIRS = [
    ("en", "ru", "wikdict"),
    ("ru", "en", "wikdict"),
    ("ar", "en", "hanswehr"),
    ("en", "ar", "hanswehr_rev"),   # reverse of ar_en
    ("ar", "ru", "pivot"),          # ar→en (Hans Wehr) → ru (WikDict)
    ("ru", "ar", "pivot_rev"),      # reverse of ar_ru
]

# ─── SQLite output helpers ──────────────────────────────────────────────────────

CREATE_SQL = """
CREATE TABLE translations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    word TEXT NOT NULL COLLATE NOCASE,
    translation TEXT NOT NULL,
    transcription TEXT,
    example TEXT,
    example_translation TEXT
);
CREATE INDEX idx_word ON translations(word COLLATE NOCASE);
"""


def create_output_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    conn.executescript(CREATE_SQL)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def flush_batch(conn: sqlite3.Connection, batch: list) -> None:
    if not batch:
        return
    conn.executemany(
        "INSERT INTO translations "
        "(word, translation, transcription, example, example_translation) "
        "VALUES (?, ?, ?, ?, ?)",
        batch,
    )
    conn.commit()


# ─── Download helpers ───────────────────────────────────────────────────────────

def _progress_hook(count, block_size, total):
    if total > 0:
        pct = min(100, int(count * block_size * 100 / total))
        downloaded_mb = min(count * block_size, total) / (1024 * 1024)
        total_mb = total / (1024 * 1024)
        print(
            f"\r    {pct}%  {downloaded_mb:.1f} / {total_mb:.1f} MB",
            end="",
            flush=True,
        )


def download_file(url: str, dest: Path) -> Path:
    if dest.exists() and dest.stat().st_size > 0:
        print(f"  [cached]  {dest.name}")
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"  [download] {url}")
    try:
        urllib.request.urlretrieve(url, dest, reporthook=_progress_hook)
        print()
    except Exception as e:
        if dest.exists():
            dest.unlink()
        raise RuntimeError(f"Failed to download {url}: {e}") from e
    return dest


# ─── WikDict source ─────────────────────────────────────────────────────────────

def build_from_wikdict(src_lang: str, tgt_lang: str, out_path: Path) -> int:
    """
    Download WikDict bilingual SQLite (v2 schema) and convert to the app's flat schema.

    WikDict v2 table: simple_translation
      written_rep TEXT  — the source word
      trans_list  TEXT  — translations separated by " | "
    """
    sqlite_name = f"{src_lang}-{tgt_lang}.sqlite3"
    url = f"{WIKDICT_BASE}/{sqlite_name}"
    cached = download_file(url, CACHE_DIR / sqlite_name)

    src_conn = sqlite3.connect(f"file:{cached}?mode=ro", uri=True)
    src_conn.row_factory = sqlite3.Row

    tables = {
        r[0]
        for r in src_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    if "simple_translation" not in tables:
        src_conn.close()
        raise RuntimeError(
            f"Unexpected WikDict schema: 'simple_translation' not found in {sqlite_name}. "
            f"Tables: {tables}"
        )

    out_conn = create_output_db(out_path)
    batch: list = []
    total = 0

    for row in src_conn.execute(
        "SELECT written_rep, trans_list FROM simple_translation "
        "WHERE written_rep IS NOT NULL AND trans_list IS NOT NULL "
        "ORDER BY rel_importance DESC NULLS LAST"
    ):
        word = (row["written_rep"] or "").strip()
        trans_raw = (row["trans_list"] or "").strip()
        if not word or not trans_raw:
            continue

        parts = [t.strip() for t in trans_raw.split("|") if t.strip()]
        if not parts:
            continue
        translation = "; ".join(parts[:6])

        batch.append((word, translation, None, None, None))
        total += 1

        if len(batch) >= 5000:
            flush_batch(out_conn, batch)
            batch.clear()
            print(f"\r    {total} entries...", end="", flush=True)

    flush_batch(out_conn, batch)
    out_conn.execute("VACUUM")
    out_conn.close()
    src_conn.close()

    print(f"\n    Wrote {total} entries → {out_path.name}")
    return total


# ─── Hans Wehr / Arabic Lexicons source ─────────────────────────────────────────

def get_hanswehr_db() -> Path:
    """Download and unzip the Arabic Lexicons database. Return path to .sqlite file."""
    zip_path = CACHE_DIR / "arabic_lexicons_db.zip"
    sqlite_path = CACHE_DIR / "arabic_lexicons.sqlite"

    if not sqlite_path.exists() or sqlite_path.stat().st_size == 0:
        download_file(ARABIC_LEXICONS_DB_URL, zip_path)
        print("  [unzip]  arabic_lexicons_db.zip")
        import zipfile
        with zipfile.ZipFile(zip_path) as zf:
            # The zip contains db.sqlite
            members = zf.namelist()
            db_member = next((m for m in members if m.endswith(".sqlite")), members[0])
            with zf.open(db_member) as src, open(sqlite_path, "wb") as dst:
                dst.write(src.read())
        print(f"  [extracted] {sqlite_path.name} ({sqlite_path.stat().st_size // 1024} KB)")

    return sqlite_path


# Arabic Unicode ranges for stripping Arabic text from Hans Wehr entries
_ARABIC_RE = re.compile(
    r"^[\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF\u200c-\u200f\s،؛؟!]+",
)
# Common abbreviations to skip when searching for English start
_SKIP_WORDS = {
    "pl", "pl.", "n", "n.", "f", "f.", "m", "m.", "adj", "adj.",
    "adv", "adv.", "v", "v.", "vs", "vs.", "a", "a.", "i", "ii",
    "also", "see", "cf", "cf.", "obs", "obs.", "arch", "arch.",
    "coll", "coll.", "dial", "dial.", "etc", "etc.", "e.g", "e.g.",
    "no.", "sg", "sg.", "un",
}


def extract_english_from_hanswehr(meanings: str) -> str:
    """
    Extract the English definition from a Hans Wehr meanings string.
    Format: "[ARABIC_WORD] [ROMANIZATION] [POSSIBLY pl./etc.] [ENGLISH]"
    """
    if not meanings:
        return ""

    # Strip leading Arabic text
    text = _ARABIC_RE.sub("", meanings).strip()

    if not text:
        return meanings.strip()

    # Find where English starts: first all-ASCII word that's not an abbreviation
    # and has length > 1
    words = text.split()
    for i, w in enumerate(words):
        clean = w.rstrip(".,;:()")
        # Skip words with non-ASCII chars (romanization like ābid, āfāqī)
        if not all(ord(c) < 128 for c in clean):
            continue
        # Skip known abbreviations
        if clean.lower() in _SKIP_WORDS:
            continue
        # Skip single characters
        if len(clean) <= 1:
            continue
        # This looks like an English word
        english = " ".join(words[i:])
        # Truncate at first Arabic character block (for entries with embedded Arabic)
        english = _ARABIC_RE.split(english)[0].strip()
        return english

    # Fallback: return cleaned text without leading Arabic
    return text


def load_hanswehr_entries(sqlite_path: Path) -> dict[str, str]:
    """
    Load Hans Wehr entries from the Arabic Lexicons SQLite.
    Returns dict: arabic_word → english_meaning
    """
    conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    entries: dict[str, str] = {}

    for row in conn.execute(
        "SELECT word, meanings FROM hanswehr "
        "WHERE word IS NOT NULL AND meanings IS NOT NULL"
    ):
        word = (row["word"] or "").strip()
        meanings = (row["meanings"] or "").strip()
        if not word or not meanings:
            continue

        english = extract_english_from_hanswehr(meanings)
        if not english:
            continue

        # If word already exists, keep the shorter/better definition
        if word not in entries or len(english) < len(entries[word]):
            entries[word] = english

    conn.close()
    return entries


def build_ar_en(out_path: Path) -> dict[str, str]:
    """Build ar_en.db from Hans Wehr and return the entries for pivot use."""
    sqlite_path = get_hanswehr_db()
    entries = load_hanswehr_entries(sqlite_path)

    out_conn = create_output_db(out_path)
    batch: list = []
    total = 0

    for word, english in entries.items():
        batch.append((word, english, None, None, None))
        total += 1
        if len(batch) >= 5000:
            flush_batch(out_conn, batch)
            batch.clear()

    flush_batch(out_conn, batch)
    out_conn.execute("VACUUM")
    out_conn.close()

    print(f"    Wrote {total} entries → {out_path.name}")
    return entries


def build_en_ar(ar_en_entries: dict[str, str], out_path: Path) -> dict[str, str]:
    """
    Build en_ar.db by reversing ar_en entries.
    For each arabic word and its english translation, extract English keywords
    and map them back to the Arabic word.
    Returns en→ar mapping for pivot use.
    """
    # en_word → list of (arabic_word, full_english_def)
    en_to_ar: dict[str, tuple[str, str]] = {}

    for arabic_word, english_def in ar_en_entries.items():
        # Extract first 1-3 meaningful English keywords from the definition
        # Split on commas, semicolons, spaces to get individual words
        keywords = re.split(r"[,;.()│\s]+", english_def)
        keywords = [k.strip().lower() for k in keywords if k.strip() and len(k.strip()) > 1]

        for kw in keywords[:3]:  # only first 3 keywords to avoid noise
            if kw not in en_to_ar:
                en_to_ar[kw] = (arabic_word, english_def)

    out_conn = create_output_db(out_path)
    batch: list = []
    total = 0

    for en_word, (arabic_word, english_def) in en_to_ar.items():
        batch.append((en_word, arabic_word, None, None, None))
        total += 1
        if len(batch) >= 5000:
            flush_batch(out_conn, batch)
            batch.clear()

    flush_batch(out_conn, batch)
    out_conn.execute("VACUUM")
    out_conn.close()

    print(f"    Wrote {total} entries → {out_path.name}")
    return {k: v[0] for k, v in en_to_ar.items()}  # en→ar


def load_db_dict(db_path: Path) -> dict[str, str]:
    """Load a built .db file into a word→translation dict."""
    if not db_path.exists():
        return {}
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    result = {
        row[0].strip().lower(): row[1].strip()
        for row in conn.execute(
            "SELECT word, translation FROM translations "
            "WHERE word IS NOT NULL AND translation IS NOT NULL"
        )
        if row[0] and row[1]
    }
    conn.close()
    return result


def build_ar_ru(ar_en_entries: dict[str, str], out_path: Path) -> dict[str, str]:
    """
    Build ar_ru.db using a pivot: Arabic→English (Hans Wehr) → Russian (WikDict).
    Returns ar→ru mapping for pivot_rev.
    """
    en_ru_path = OUTPUT_DIR / "en_ru.db"
    if not en_ru_path.exists():
        raise RuntimeError(
            "en_ru.db not found. Build en_ru first before ar_ru."
        )

    print(f"  Loading en_ru.db for pivot...")
    en_ru = load_db_dict(en_ru_path)
    print(f"  Loaded {len(en_ru)} en→ru entries")

    out_conn = create_output_db(out_path)
    batch: list = []
    total = 0
    ar_ru: dict[str, str] = {}

    for arabic_word, english_def in ar_en_entries.items():
        # Extract first English keyword from the definition
        keywords = re.split(r"[,;.()\s]+", english_def)
        keywords = [k.strip().lower() for k in keywords if k.strip() and len(k.strip()) > 1]

        russian = None
        for kw in keywords[:5]:
            if kw in en_ru:
                russian = en_ru[kw]
                break

        if not russian:
            continue

        ar_ru[arabic_word] = russian
        batch.append((arabic_word, russian, None, None, None))
        total += 1

        if len(batch) >= 5000:
            flush_batch(out_conn, batch)
            batch.clear()

    flush_batch(out_conn, batch)
    out_conn.execute("VACUUM")
    out_conn.close()

    print(f"\n    Wrote {total} entries → {out_path.name}")
    return ar_ru


def build_pivot_rev(forward_entries: dict[str, str], out_path: Path) -> int:
    """
    Build a reversed dictionary from forward_entries (e.g. ar_ru → ru_ar).
    forward: source_word → target_word
    reverse: target_word → source_word
    """
    # Reverse: target → source
    rev: dict[str, str] = {}
    for src, tgt in forward_entries.items():
        # tgt might be "привет; алё; ..." — use the first part
        first_tgt = tgt.split(";")[0].strip()
        if first_tgt and first_tgt not in rev:
            rev[first_tgt] = src

    out_conn = create_output_db(out_path)
    batch = [(tgt_word, src_word, None, None, None) for tgt_word, src_word in rev.items()]
    total = len(batch)
    flush_batch(out_conn, batch)
    out_conn.execute("VACUUM")
    out_conn.close()

    print(f"    Wrote {total} entries → {out_path.name}")
    return total


# ─── Main ────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--pair",
        nargs="+",
        metavar="PAIR",
        help="Build only these pairs (e.g. --pair en_ru ru_en). Default: all 6.",
    )
    args = parser.parse_args()

    requested: set[str] | None = None
    if args.pair:
        requested = set(args.pair)
        valid = {f"{s}_{t}" for s, t, _ in ALL_PAIRS}
        for p in requested:
            if p not in valid:
                print(f"ERROR: unknown pair '{p}'. Valid: {sorted(valid)}", file=sys.stderr)
                sys.exit(1)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    results: list[tuple[str, int, float]] = []

    # Cache for Arabic data (avoid re-loading)
    ar_en_entries: dict[str, str] | None = None
    en_ar_map: dict[str, str] | None = None
    ar_ru_map: dict[str, str] | None = None

    def need(pair_id: str) -> bool:
        return requested is None or pair_id in requested

    sep = "=" * 55

    # ── en_ru ──────────────────────────────────────────────────────────────────
    if need("en_ru"):
        out_path = OUTPUT_DIR / "en_ru.db"
        print(f"\n{sep}\n  en_ru.db  [wikdict]\n{sep}")
        n = build_from_wikdict("en", "ru", out_path)
        size_mb = out_path.stat().st_size / (1024 * 1024)
        print(f"  Size: {size_mb:.1f} MB")
        results.append(("en_ru", n, size_mb))

    # ── ru_en ──────────────────────────────────────────────────────────────────
    if need("ru_en"):
        out_path = OUTPUT_DIR / "ru_en.db"
        print(f"\n{sep}\n  ru_en.db  [wikdict]\n{sep}")
        n = build_from_wikdict("ru", "en", out_path)
        size_mb = out_path.stat().st_size / (1024 * 1024)
        print(f"  Size: {size_mb:.1f} MB")
        results.append(("ru_en", n, size_mb))

    # ── ar_en ──────────────────────────────────────────────────────────────────
    if need("ar_en") or need("en_ar") or need("ar_ru") or need("ru_ar"):
        out_path = OUTPUT_DIR / "ar_en.db"
        print(f"\n{sep}\n  ar_en.db  [Hans Wehr]\n{sep}")
        ar_en_entries = build_ar_en(out_path)
        size_mb = out_path.stat().st_size / (1024 * 1024)
        print(f"  Size: {size_mb:.1f} MB")
        if need("ar_en"):
            results.append(("ar_en", len(ar_en_entries), size_mb))

    # ── en_ar ──────────────────────────────────────────────────────────────────
    if need("en_ar") or need("ru_ar"):
        out_path = OUTPUT_DIR / "en_ar.db"
        print(f"\n{sep}\n  en_ar.db  [Hans Wehr reversed]\n{sep}")
        en_ar_map = build_en_ar(ar_en_entries or {}, out_path)
        size_mb = out_path.stat().st_size / (1024 * 1024)
        print(f"  Size: {size_mb:.1f} MB")
        if need("en_ar"):
            results.append(("en_ar", len(en_ar_map), size_mb))

    # ── ar_ru ──────────────────────────────────────────────────────────────────
    if need("ar_ru") or need("ru_ar"):
        out_path = OUTPUT_DIR / "ar_ru.db"
        print(f"\n{sep}\n  ar_ru.db  [Hans Wehr + WikDict pivot]\n{sep}")
        ar_ru_map = build_ar_ru(ar_en_entries or {}, out_path)
        size_mb = out_path.stat().st_size / (1024 * 1024)
        print(f"  Size: {size_mb:.1f} MB")
        if need("ar_ru"):
            results.append(("ar_ru", len(ar_ru_map), size_mb))

    # ── ru_ar ──────────────────────────────────────────────────────────────────
    if need("ru_ar"):
        out_path = OUTPUT_DIR / "ru_ar.db"
        print(f"\n{sep}\n  ru_ar.db  [ar_ru reversed]\n{sep}")
        n = build_pivot_rev(ar_ru_map or {}, out_path)
        size_mb = out_path.stat().st_size / (1024 * 1024)
        print(f"  Size: {size_mb:.1f} MB")
        results.append(("ru_ar", n, size_mb))

    # Summary
    if results:
        print(f"\n{'='*55}")
        print("  Summary")
        print(f"{'='*55}")
        for pack_id, n, size_mb in results:
            print(f"  {pack_id}.db  —  {n:,} entries  /  {size_mb:.1f} MB")
        print(f"\n  Output: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
