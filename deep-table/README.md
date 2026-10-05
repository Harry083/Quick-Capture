# Deep Table

A desktop application for **forensic examination of SQLite databases and LevelDB stores**: the live data,
the records that were deleted or overwritten but not yet destroyed, the order things happened in, and the
BLOBs, timestamps and serialised values apps hide in their columns. Everything you bookmark goes into a report
with the evidence hashes.

- **SQLite:** live tables, carved deleted rows, WAL and rollback-journal history.
- **LevelDB:** Chrome / Edge / Electron **Local Storage**, **Session Storage** and **IndexedDB** decoded into
  tables (IndexedDB values become JSON), plus older and deleted values still in the log and table files. Any
  other LevelDB store opens as raw key/value records.

It is a companion to [Quick Capture](../README.md) and is built the same way: a native window using the operating
system's web engine through [pywebview](https://pywebview.flowrl.com/), with the same vanilla HTML/CSS/JS style kit
and no build step. **No web server runs and no network port is opened.**

## Evidence handling

The originals are never opened by SQLite, and never written.

1. When you open a database, its `-wal`, `-journal` and `-shm` companions are found automatically.
2. Each file is hashed (MD5, SHA-1, SHA-256) and copied into a private temp folder. The copies are hashed
   again, and the case refuses to open if they don't match (e.g. if an app was writing to the file).
3. Deep Table parses the copies itself (see `backend/sqlite_format.py`). For each **view** of the database it
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
| **Recovered records as a database** | The recovered records are rebuilt into a database of their own (View → *Recovered records*): one table per source table, with the original columns plus `dt_status`, `dt_source`, `dt_location`, `dt_orig_rowid`, `dt_confidence` and `dt_also_found_in`. You can browse, search, join and report on them as if they were live. |
| **Search all** | One search across every column of every table, without writing SQL. Text matches whatever the case, the same bytes are found inside BLOBs, and recovered deleted records are searched too. Matching cells are highlighted, and each result links to its table already filtered. |
| **WAL timeline** | Replays the WAL one transaction at a time, from the main file onwards, and lists every row each commit **inserted**, **updated** (with before → after for the changed columns) or **deleted**. Each change is shown with the row's own timestamp for timing context, and you can jump to the database as of that commit or bookmark the change. |
| **WAL & journal** | Header, salts and checkpoint sequence; every frame with its page, owning table, commit and state (valid, uncommitted, bad checksum, older salt); transactions with the pages they touched; one click to view the database as of any commit. |
| **Timestamps** | Auto-detected per column and shown in UTC: Unix s/ms/µs/ns, Mac absolute (Cocoa) s and ns, WebKit/Chrome, Windows FILETIME, HFS+, .NET ticks, Julian day, GPS. Override any column, or click a number to see every plausible reading. |
| **BLOB & text viewer** | Identifies the type from magic bytes and previews it: images (JPEG, PNG, GIF, WebP, BMP…), binary/XML plists with NSKeyedArchiver archives resolved into plain structures, JSON, HTML, text, gzip/zlib/jsonlz4/LZ4/Snappy streams (decompressed, then identified again), embedded SQLite databases (schema), schema-less protobuf, plus a hex dump, entropy, hashes and a strings count. |
| **Drill into embedded data** | A decode chain for data wrapped in other data, with steps you can undo: **Base64, hex text, URL encoding, UTF-16, zlib, raw deflate, GZip, Snappy, LZ4 / Mozilla jsonlz4, Brotli** (if the `brotli` package is installed), **plist, protobuf, MessagePack, Bencode, XOR 0xFF, strings**. Transforms that look applicable are highlighted. Any long text cell opens in the same viewer, so a Base64 value in a TEXT column can be unwrapped too. Save the result at any step. |
| **BLOB export** | Exports every BLOB in a column to its own file, named `table-rowid-column.ext` with the extension taken from the detected type, plus a manifest CSV with sizes, types, MD5 and SHA-256. It works on recovered records too (via the rebuilt database). |
| **SQL** | A read-only SQL editor (Ctrl+Enter), saved queries, and a **query builder** that suggests joins from foreign keys and naming conventions (`x_id → x`, Core Data's `ZFOO → ZFOO.Z_PK`) and can convert timestamp columns in the SQL itself. |
| **Pages** | A map of every page coloured by type (table/index leaf and interior, overflow, freelist, pointer map), with pages that have free space or were changed in the WAL marked. Click one for its header, cells and a hex view coloured by region (header, cell pointers, cells, freeblocks, unallocated). |
| **Bookmarks & report** | Bookmark any row (live, query result or recovered) with a label and a note. The HTML/JSON report holds the case details, the evidence files and their hashes, the database header, WAL/journal summary, every bookmark with its values, saved queries re-run at report time, recovered deleted records with provenance, and the query log. CSV export is available for any table, query or recovered set, with an extra UTC column next to each timestamp. |

## LevelDB and browser storage

Open a LevelDB **folder** (the *Folder…* button), or any file inside one, e.g.:

| Store | Typical path (Chrome profile; Edge, Brave and Electron apps are alike) |
|---|---|
| Local Storage | `Default/Local Storage/leveldb` |
| Session Storage | `Default/Session Storage` |
| IndexedDB | `Default/IndexedDB/https_web.whatsapp.com_0.indexeddb.leveldb` |

Every file in the folder is hashed, copied and verified, the same as for SQLite. The folder's digest in the
report is the SHA-256 of the sorted list of file hashes. Deep Table reads the copies with its own LevelDB
reader (`backend/leveldb.py`):

- **Files:** `.log` files with CRC32C checks and records split across blocks, `.ldb`/`.sst` tables with
  Snappy-compressed blocks, and the `MANIFEST` (comparator, last sequence, and which files are still part of
  the current version; leftovers are flagged as obsolete).
- **States:** every record from every file gets one. LevelDB never edits in place, so for each key the
  highest sequence number is **live**, earlier puts are **older versions**, and puts followed by a tombstone
  are **deleted**. The same record found in both a log and a table is shown once, with both locations.

Chromium stores are then decoded (`backend/chromium.py`) into a database Deep Table builds itself, so Tables,
Search all, Query, bookmarks and reports work as they do for SQLite:

| Table | What's in it |
|---|---|
| `local_storage`, `local_storage_origins` | origin, key and value (Latin-1 / UTF-16 decoded); per-origin size and last-modified time |
| `session_storage` | origin, key and value, with the namespace and map they belong to |
| `indexeddb_records` | database, object store, key (decoded IDB key) and **value as JSON**, decoded from V8's serialisation format: objects, arrays, strings, numbers, BigInt, Date, Map, Set, RegExp, ArrayBuffer and typed arrays, Error, and Blob/File references. Query it with `json_extract(value, '$.field')`; the original bytes are in `value_raw`. |
| `indexeddb_databases`, `indexeddb_object_stores` | names, versions, key paths |
| `leveldb_records` | every raw record from every file: sequence, state, put/delete, key, value, file, offset, checksum |

The decoded tables hold the **live** records. Older versions and deleted values appear in **Recovered** with
their sequence number and file, and in the rebuilt *Deleted and older records* view. **LevelDB & timeline**
lists the files and every surviving put and delete in sequence order, with before → after for updates.
Compactions drop superseded records, so the timeline is what's left, not complete history.

Session Storage is cleared when a browsing session ends, so its values often survive **only** as deleted
records. The test suite uses stores written by a real Chromium (`tests/fixtures/`): it checks that deleted
Local Storage, Session Storage and IndexedDB values come back, and that every IndexedDB value decodes.

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
  on-page part is still carved), and other stores (Realm, the Windows registry). LevelDB values Chromium
  moved into external blob files are referenced but not read.
  Formats such as ABX, LZFSE and Java serialisation are identified but not decoded.

## Requirements

- Python 3.10+ (it uses only the standard library plus pywebview)
- A system web engine:
  - Windows 10/11: Edge WebView2, which is already installed.
  - macOS: nothing extra.
  - Linux: GTK and WebKit2GTK (e.g. `sudo apt install python3-gi gir1.2-webkit2-4.1`), or
    `pip install "pywebview[qt]"`.

No Administrator or root rights are needed. Deep Table only reads files you can already open.

## Setup and run

```bash
cd deep-table
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
python -m PyInstaller --clean DeepTable.spec
```

This builds a single file, `dist/DeepTable.exe` (`dist/DeepTable` on Linux/macOS), with the Deep Table icon.
On Linux, copy it and `deeptable.png` to `/opt/DeepTable/` and install `deep-table.desktop` into
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
deep-table/
├── backend/
│   ├── api.py            the methods the window calls (window.pywebview.api.*)
│   ├── session.py        a case: evidence copies + hashes, views, rows, SQL, search, recovery, timeline, bookmarks
│   ├── sqlite_format.py  pure-Python SQLite parser: header, b-trees, records, overflow, freelist, WAL, journal
│   ├── recovery.py       deleted-record carving (freeblocks, unallocated, freelist) and table signatures
│   ├── history.py        rows from WAL frames and journal pages
│   ├── leveldb.py        pure-Python LevelDB reader: logs, tables (Snappy), MANIFEST, record states
│   ├── chromium.py       Local Storage, Session Storage, IndexedDB keys/metadata and V8 value decoding
│   ├── leveldb_case.py   builds the queryable database, recovered records and timeline for a LevelDB store
│   ├── decoders.py       timestamps, BLOB type detection, plist / NSKeyedArchiver, protobuf, hex
│   ├── transforms.py     decode chain: Base64, hex, zlib/deflate/gzip, Snappy, LZ4, MessagePack, Bencode, ...
│   └── report.py         HTML / JSON report and CSV export
├── frontend/             vanilla HTML/CSS/JS UI; styles.css + fonts/ are the shared tool style kit
├── tests/                pytest suite (python -m pytest tests); fixtures/ holds real Chromium stores
├── app.py                entry point: opens the native window (no server, no port)
├── DeepTable.spec       PyInstaller one-file build
├── deeptable.ico/.png   the app icon
└── deep-table.desktop   Linux menu launcher
```

## License

MIT — see [LICENSE](../../LICENSE).
