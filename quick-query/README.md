# Quick Query

A desktop application for **forensic examination of SQLite databases**: the live tables, the rows SQLite has
deleted but not yet overwritten, earlier versions of rows from the write-ahead log and rollback journal, and
the BLOBs and timestamps apps hide in their columns. Everything you bookmark goes into a report with the
evidence hashes.

It is the SQLite counterpart to [Quick Capture](../README.md) and is built the same way: a native window using the
operating system's web engine through [pywebview](https://pywebview.flowrl.com/), with the same vanilla
HTML/CSS/JS style kit and no build step. **No web server runs and no network port is opened.**

## Evidence handling

The originals are never opened by SQLite, and never written.

1. When you open a database, its `-wal`, `-journal` and `-shm` companions are found automatically.
2. Each file is hashed (MD5, SHA-1, SHA-256) and copied into a private temp folder. The copies are hashed
   again, and the case refuses to open if they don't match (e.g. if an app was writing to the file).
3. Quick Query parses the copies itself (see `backend/sqlite_format.py`). For each **view** of the database it
   builds its own page image: the main file only, the main file plus the WAL, the state at any single WAL
   commit, or the state before a journalled transaction. Each image is marked as a rollback-journal database,
   so SQLite never looks for or replays a WAL, and is opened `mode=ro&immutable=1` with `query_only` on.
4. An authorizer refuses every write, `ATTACH`, and state-changing `PRAGMA`. Queries time out after 60 s and
   can be stopped.
5. The temp folder is deleted when the case is closed.

So there is no checkpoint, no WAL truncation and no `-shm` file next to your evidence. The test suite checks
the originals' hashes and modification times before and after a full session.

## Features

| | |
|---|---|
| **Tables** | Every table and view with row counts, paging, sorting and full-text search across columns, plus the `CREATE` statement. |
| **Views of the database** | With a WAL: *current* (as an app sees it), *main file only*, or *as of commit N* for every transaction in the WAL. With a journal: the database *before* the journalled transaction. |
| **Deleted record recovery** | Carves records from freeblocks (where SQLite puts deleted cells), unallocated space in every b-tree page, including interior pages that were leaves before a split, freelist pages (trunk and leaf), freeblocks a defragmentation left unlinked, WAL frames (current and earlier checkpoint cycles), journal pages, and main-file rows the WAL has since replaced. Each record is classified **deleted**, **older version** or **live copy** by comparing it with the live table. Each record shows its page and offset, a confidence rating, which columns had to be inferred, and every other place it was found. |
| **WAL & journal** | Header, salts and checkpoint sequence; every frame with its page, owning table, commit and state (valid, uncommitted, bad checksum, older salt); transactions with the pages they touched; one click to view the database as of any commit. |
| **Timestamps** | Auto-detected per column and shown in UTC: Unix s/ms/µs/ns, Mac absolute (Cocoa) s and ns, WebKit/Chrome, Windows FILETIME, HFS+, .NET ticks, Julian day, GPS. Override any column, or click a number to see every plausible reading. |
| **BLOB viewer** | Identifies the type from magic bytes and previews it: images (JPEG, PNG, GIF, WebP, BMP…), binary/XML plists with NSKeyedArchiver archives resolved into plain structures, JSON, text, gzip/zlib (decompressed, then identified again), schema-less protobuf, plus a hex dump. Save any BLOB to a file. |
| **SQL** | A read-only SQL editor (Ctrl+Enter), saved queries, and a **query builder** that suggests joins from foreign keys and naming conventions (`x_id → x`, Core Data's `ZFOO → ZFOO.Z_PK`) and can convert timestamp columns in the SQL itself. |
| **Pages** | A map of every page coloured by type (table/index leaf and interior, overflow, freelist, pointer map), with pages that have free space or were changed in the WAL marked. Click one for its header, cells and a hex view coloured by region (header, cell pointers, cells, freeblocks, unallocated). |
| **Bookmarks & report** | Bookmark any row (live, query result or recovered) with a label and a note. The HTML/JSON report holds the case details, the evidence files and their hashes, the database header, WAL/journal summary, every bookmark with its values, saved queries re-run at report time, recovered deleted records with provenance, and the query log. CSV export is available for any table, query or recovered set, with an extra UTC column next to each timestamp. |

### What recovery can and can't do

- If the app used `secure_delete`, freed space is zeroed. The overview says so (*free space is zeroed*). The
  WAL and journal may still hold earlier copies.
- SQLite reuses freed space and rebalances pages, so part of what was deleted is usually overwritten. The test
  suite checks that every deleted row whose bytes are still in the file comes back, and that no row that
  wasn't deleted is reported as deleted.
- A freeblock overwrites a deleted cell's first 4 bytes. That usually costs the rowid and the first column's
  type. An `INTEGER PRIMARY KEY` column costs nothing to infer. Any other lost column is inferred from the
  bytes left over and marked with `*`.
- Records are matched to tables by column count and the types each column's affinity allows. Tables with one
  or two columns match random bytes easily, so those carves are only accepted on strong evidence.
- Not covered yet: deleted index entries, deleted records whose payload spilled onto overflow pages (the
  on-page part is still carved), and non-SQLite stores (LevelDB, Realm).

## Requirements

- Python 3.10+ (it uses only the standard library plus pywebview)
- A system web engine:
  - Windows 10/11: Edge WebView2, which is already installed.
  - macOS: nothing extra.
  - Linux: GTK and WebKit2GTK (e.g. `sudo apt install python3-gi gir1.2-webkit2-4.1`), or
    `pip install "pywebview[qt]"`.

No Administrator or root rights are needed. Quick Query only reads files you can already open.

## Setup and run

```bash
cd quick-query
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt     # Windows
.venv\Scripts\python.exe app.py
```

```bash
.venv/bin/python app.py /path/to/sms.db      # a path on the command line opens straight away
```

`--debug` enables the web inspector.

## Build a standalone app

```bash
python -m pip install pyinstaller
python -m PyInstaller --clean QuickQuery.spec
```

This builds a single file, `dist/QuickQuery.exe` (`dist/QuickQuery` on Linux/macOS), with the Quick Query icon.
On Linux, copy it and `quickquery.png` to `/opt/QuickQuery/` and install `quick-query.desktop` into
`~/.local/share/applications/` (it registers for SQLite files). PyInstaller builds for the OS it runs on.

## Tests

```bash
python -m pip install pytest
python -m pytest tests
```

The tests build real databases with the `sqlite3` module and check the following:

- The parser reads every serial type and overflow chain exactly as SQLite does.
- Deleted rows come back from freeblocks, unallocated space, freelist pages, the WAL (including frames from an
  earlier checkpoint cycle) and a `PERSIST` journal.
- Each WAL commit view has the right rows.
- No query can write, attach or change settings.
- The evidence files are byte-for-byte unchanged afterwards.

## Project structure

```
quick-query/
├── backend/
│   ├── api.py            the methods the window calls (window.pywebview.api.*)
│   ├── session.py        a case: evidence copies + hashes, views, rows, SQL, recovery, bookmarks
│   ├── sqlite_format.py  pure-Python SQLite parser: header, b-trees, records, overflow, freelist, WAL, journal
│   ├── recovery.py       deleted-record carving (freeblocks, unallocated, freelist) and table signatures
│   ├── history.py        rows from WAL frames and journal pages
│   ├── decoders.py       timestamps, BLOB type detection, plist / NSKeyedArchiver, protobuf, hex
│   └── report.py         HTML / JSON report and CSV export
├── frontend/             vanilla HTML/CSS/JS UI; styles.css + fonts/ are the shared tool style kit
├── tests/                pytest suite (python -m pytest tests)
├── app.py                entry point: opens the native window (no server, no port)
├── QuickQuery.spec       PyInstaller one-file build
├── quickquery.ico/.png   the app icon
└── quick-query.desktop   Linux menu launcher
```

## License

MIT — see [LICENSE](../../LICENSE).
