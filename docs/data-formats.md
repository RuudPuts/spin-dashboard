# Data formats

All data lives in `DATA_DIR/days` (default `/data/days`, on the host usually
`/mnt/user/appdata/spin-dashboard/days`): **one CSV per day**, `YYYY-MM-DD.csv`. The day is the
container's local date (`TZ`, passed in by Unraid). Plain text, no header.

## Lines

Every line starts with epoch seconds and a type, followed by fields per type.

```
1791496800,disk,sdd,trunk
1791496800,state,sdd,standby
1791529042,spin,sdd,active
1791529032,who,container,sonarr,16690,geopend,/mnt/trunk/Media/series/
1791530846,spin,sdd,standby
```

| Type | Fields after `epoch,type` | Meaning |
|---|---|---|
| `disk` | device, name | Name from `disks.ini` (`parity`, `disk1`, `trunk`, `trunk2`). On the first poll of every day and whenever the names change. |
| `state` | device, `active`/`standby` | Measured state at that moment, **not a change**: on the first poll of a day and after a start. Never counts as a spin-up. |
| `spin` | device, `active`/`standby` | State change between two polls. `active` after `standby` is a spin-up. |
| `who` | kind, name, pid, action, path | Access to a file or folder (fatrace). See below. |
| `activity` | disk, event, path | Old inotify data (`OPEN+ISDIR`, `MODIFY`, …). Only written by the migration. |

Thanks to the `disk` and `state` lines at the top, every day file can be read on its own: the
names and the state at the start of the day are in it, even when the previous day has already
been removed.

### who

| Field | Content |
|---|---|
| kind | `container`, `unraid`, `gebruiker` (user), `onbekend` (unknown) |
| name | Container name, `SMB-share`, `shell (ssh)`, `mover`, `webGUI`, process name, … (commas replaced by spaces). Ends in ` (al gestopt)` ("already stopped") when the process was gone. |
| pid | PID (host namespace) |
| action | `geopend` (opened for reading), `geschreven` (written, or opened with write intent), `aangemaakt` (created), `verwijderd` (deleted), `verplaatst` (moved) |
| path | **Folders end in `/`.** May contain commas; always the last field. |

These values are Dutch and part of the format; the frontend translates them when shown.

**Read vs. write.** The action carries the direction. `geopend` is a read-only open; `geschreven`
comes from a fatrace `W` event (a write to an already-open file) or from an open whose flags
include write intent (fatrace shows `OW`/`ORW`). Create, delete and move are also writes. Older
day files only know `geopend` for every open, so those lines carry no direction: the frontend then
shows no read/write badge. No migration is needed — old and new lines live side by side.

**Deduplication.** The same path + kind + name + action is logged at most once per
`DEDUP_SECONDS` (600). Because the action is part of the key, a read and a write of the same file
within that window each get their own line.

## Web

| URL | Content |
|---|---|
| `/data/days.json` | `{"days": ["2026-10-08", "2026-10-09"], "bytes": 13042817, "sizes": [6521400, 6521417]}`: the days in ascending order, the total size and the size per day (uncompressed). The frontend uses them to fetch only the days it shows and to report loading progress. Older versions sent only `days` (and later `bytes`); the frontend then counts finished days instead of bytes. |
| `/data/days/YYYY-MM-DD.csv` | One day file, gzip-compressed when the browser sends `Accept-Encoding: gzip` (about 8× smaller on real data). Each response has an `ETag`; a request with a matching `If-None-Match` gets a `304` without a body. Any other path under `/data/` returns 404. |
| `/data/spins.csv` | Only the `disk`, `state` and `spin` lines of every day file, days in ascending order, same line format. A few KB per day instead of megabytes, so the frontend can compute key figures, the comparison with the previous period and the spin-ups-per-day trend over all days without fetching every day file. Gzip and `ETag`/`304` like the day files; the ETag changes when any day file changes. Older servers return 404; the frontend then uses the days it has loaded. |
| `/data/settings.json` | Dashboard settings shared by every browser. `GET` returns `{"hide": ["/mnt/user/appdata/*", "*.log"]}`, or `{}` when nothing has been saved yet (the frontend then moves its old `spindash.hide` list from `localStorage` to the server once). `POST` with `Content-Type: application/json` and a JSON object saves it and returns what was stored. Only known keys are kept (unknown ones are ignored, so older and newer frontends can share the file). `hide`: a list of at most 200 strings of at most 500 characters without control characters; whitespace is trimmed, empty and duplicate entries are dropped. Invalid input returns `400` with `{"error": "..."}`, another content type `415`, a body over 64 KiB `413`. |
| `/data/stream` | Server-Sent Events. Every `who` line is pushed the moment it is logged (`data: <line>`), with a `: ping` every 30 s to keep the connection alive. Powers the live feed; the day files stay the source of truth. |

## Settings file

`DATA_DIR/spindash-settings.json` holds what `/data/settings.json` returns: a JSON object, today only
`{"hide": [...]}` (hidden-path patterns, see `docs/development.md`). It is written atomically (via
`spindash-settings.json.tmp`) and both names are skipped by the watcher, like the day files. A missing
or unreadable file counts as "nothing saved". New keys may be added later; readers ignore keys they
do not know.

## Retention

Every 6 hours, day files older than `RETENTION_DAYS` (default 30) are removed as a whole.

## Migration from the old format

Until October 2026 everything was stored in separate files in `DATA_DIR`:

| File | Line | Becomes |
|---|---|---|
| `spin.csv` | `2026-10-01T04:15:07+02:00,/dev/sdd,active` | `spin` |
| `disks.csv` | `sdd,trunk` | `disk` at the top of every migrated day |
| `who.csv` | `epoch,kind,name,pid,action,path` | `who` |
| `activity.csv` | `epoch,disk,EVENT,path` | `activity` |

`migrate()` runs on every start. If old files are present, they are converted into day files
(with `disk` and `state` lines per day, computed from the spin history), merged with whatever was
already in the day files, and then moved to `DATA_DIR/legacy/`. Running it again is safe: duplicate
lines are merged. Lines that cannot be converted (such as syslog lines in `spin.csv`) are skipped
and counted in the log; they stay in `legacy/`.
