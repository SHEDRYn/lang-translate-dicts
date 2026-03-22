# lang-translate-dicts
Multilingual dictionaries with translations

Pre-built SQLite dictionary databases for offline bilingual lookups.
Ready to use in any mobile or desktop app — no conversion needed.

## Available Dictionaries

| File | Direction | Entries | Size | Source |
|------|-----------|---------|------|--------|
| `en_ru.db` | English → Russian | 93 515 | 7 MB | WikDict |
| `ru_en.db` | Russian → English | 126 405 | 10 MB | WikDict |
| `ar_en.db` | Arabic → English | 20 183 | 3 MB | Hans Wehr |
| `en_ar.db` | English → Arabic | 23 326 | 1 MB | Hans Wehr (reversed) |
| `ar_ru.db` | Arabic → Russian | 19 342 | 2 MB | Hans Wehr + WikDict pivot |
| `ru_ar.db` | Russian → Arabic | 7 085 | 1 MB | Pivot (reversed) |

## Download

All files are attached to the [latest release](../../releases/latest).

Direct URL pattern:
```
https://github.com/<user>/<repo>/releases/download/v1-dicts/<pair>.db
```

Example:
```
https://github.com/<user>/<repo>/releases/download/v1-dicts/en_ru.db
```

## Database Schema

Every `.db` file uses the same flat schema:

```sql
CREATE TABLE translations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    word TEXT NOT NULL COLLATE NOCASE,
    translation TEXT NOT NULL,
    transcription TEXT,
    example TEXT,
    example_translation TEXT
);
CREATE INDEX idx_word ON translations(word COLLATE NOCASE);
```

## Usage Example

```python
import sqlite3

conn = sqlite3.connect("en_ru.db")
row = conn.execute(
    "SELECT word, translation, transcription FROM translations WHERE word = ?",
    ("hello",)
).fetchone()
print(row)  # ('hello', 'привет; алё; добрый день', None)
```

## Rebuilding

The conversion script is located in `scripts/build_dicts.py`.

**Requirements:** Python 3.10+, no extra packages needed.

```bash
# Build all 6 pairs
python scripts/build_dicts.py

# Build specific pairs only
python scripts/build_dicts.py --pair en_ru ru_en
```

Source data is downloaded automatically on first run and cached in `scripts/cache/`.

**Data sources:**
- **WikDict** — `download.wikdict.com` (en↔ru), license: CC BY-SA
- **Hans Wehr** — `github.com/wizsk/arabic_lexicons` (ar↔en), license: GPL

## License

Dictionary data is derived from third-party sources:
- WikDict data: [Creative Commons BY-SA](https://creativecommons.org/licenses/by-sa/4.0/)
- Hans Wehr data: [GPL](https://www.gnu.org/licenses/gpl-3.0.html)

Scripts in this repository: MIT
