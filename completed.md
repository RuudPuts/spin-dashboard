# Completed

Newest first.

## 2026-10-16: hidden paths
- Settings › Hidden paths: one wildcard pattern per line (`*` anything, `?` one character), remembered per browser (`spindash.hide`)
- `/mnt/user/appdata/*` also hides the same files seen as `/mnt/<pool>/appdata/...`; a pattern without a leading slash matches inside the share (`*.log`, `appdata/plex/*`)
- Hidden activity disappears from the feed, Active drives, the rankings, session lists and spin-up causes, also when it streams in live. The day files stay complete
- The field shows how many file events are hidden

## 2026-10-16: pools and the array as one lane
- Each pool and the array show as one lane, one event row and one entry in the disk filter. A pool lists its member devices under the name, the array its number of drives
- A combined lane spins when any of its drives spins; the tooltip says how many were spinning at most, e.g. "spinning (2 of 3)"
- One switch under Settings › Drives, on by default, remembered per browser (`spindash.group`). Frontend only: the day files are unchanged
- Clicking inside the settings dialog's own padding no longer closes it; only clicks on the backdrop do
- The first option of the events filter names what it covers: "All drives", "All pools" or "All drives and pools"

## 2026-10-16: settings
- A gear button at the top right (where the language choice was) opens Settings, a dialog with the language choice and the list of cards
- Each card can be hidden or shown and moved by dragging its handle (mouse or touch) or with the up/down buttons; the layout is stored in the browser (`spindash.cards`) and survives new cards being added. "Restore default layout" resets it
- The two half-width cards (spin-ups by hour, what wakes the drives) sit side by side when they are next to each other; on their own they take the full width
- Keyboard and screen reader friendly: Escape closes the dialog and returns focus to the gear, moving a card keeps focus on it and announces its new position

## 2026-10-16: accessibility fixes from a design audit
- Badge text (R/W in session lists and the live feed) now reaches at least 4.5:1 in both themes; it was as low as 1.8:1 (white on amber in dark mode). New tokens `--up-text` (amber that is readable as text) and `--on-accent` (text on coloured badges and the primary button)
- "User" labels and write events use `--up-text` instead of `--up` as text colour (was 3.3:1 in light mode)
- The disk timelines can be read with the keyboard: Tab to a lane, arrow keys step through its periods, Home/End jump to the first/last; the tooltip appears and a live region reads it out. Focus survives live updates
- Histogram bars have their own label for screen readers; axis and hour labels are hidden from them; the timeline SVG is hidden behind the lane's own label
- Info icons point to their explanation with `aria-describedby`, so screen readers hear it without hovering
- The kind of a source (container, user, Unraid) is also given as text, not only as the colour of its dot
- Larger hit areas: info icons 28×28, the file toggle in the events table, the "Spin-ups only" label, and the browse arrows
- All font sizes are in `rem`, so the page follows the browser's text size; nothing is smaller than 11px
- The live feed no longer wraps its time onto a second line (five items in a four-column grid)
- Live activity has its own panel below Active drives instead of being a sub-heading inside it

## 2026-10-12: fixed header layout
- Language choice top right next to the title; period and browse buttons always on their own row below it, so the menu no longer jumps when switching period
- On narrow screens the period buttons scroll horizontally instead of breaking their labels

## 2026-10-12: Cache Dirs scans of large folder trees
- Folder opens by an already stopped process without a source are not logged while Cache Dirs runs. fatrace reported these `find` runs as `unknown`, which slipped past the filter: on a real day 98.5% of all lines (46 MB of 46.5 MB), and the main reason the dashboard loaded slowly

## 2026-10-12: processing 60× faster, new loading screen
- The dashboard opens in the 24-hour view (3 day files instead of 9 on first load)
- Switching period or browsing back and forward shows the skeleton and the loading bar again, with the chosen button highlighted right away
- Finding the source of an access (`nearestWho`) scanned the whole list for a path on every call, which made processing quadratic; with 110,000 real lines it took about 2 minutes in jsdom. The who index is now sorted per path with a separate index for SMB/NFS shares, and lookups use binary search: under 2 seconds in jsdom
- The live stream inserts new lines in place instead of re-sorting all activity, and redraws at most every 300 ms after the first event
- Loading screen: a thin progress bar at the top, the current step under the title and a shimmering skeleton of the dashboard, instead of a separate panel

## 2026-10-12: faster loading
- Day files are sent gzip-compressed (about 8× smaller on real data) with an `ETag`, so unchanged days get a `304`
- The frontend fetches only the days of the shown period (plus a day of margin, plus today and yesterday); browsing back or "All" fetches the rest on demand, and the minute refresh only refetches the live days
- `days.json` includes the size per day
- Loading no longer stalls in a background tab, where `requestAnimationFrame` never fires

## 2026-10-12: loading progress
- Loading panel on first load: a progress bar that follows the downloaded bytes ("3 of 12 days loaded · 4.2 of 13.0 MB"), then "Processing the data" and "Building the dashboard"; the browser gets a frame between steps so the bar actually updates
- `days.json` includes the total size of the day files (`bytes`)
- Google Fonts no longer blocks the first paint

## 2026-10-11: live feed and browsing
- Live activity feed under Active drives: newest file events first, with R/W marker, source and a
  relative time; items new since the last refresh get a short highlight
- The feed **streams**: a Server-Sent Events endpoint (`/data/stream`) pushes every `who` line the
  moment it is logged, so the feed updates instantly instead of waiting for the minute fetch
- "Last hour" period toggle with 10-minute axis ticks
- Hovering a truncated file path in Active drives shows the full path in an overlay

## 2026-10-11: reads vs. writes and active drives
- `fatrace` filter `O+W+D<`: writes to already-open files are now logged, not just opens
- Read vs. write direction per access: `geschreven` (a `W` event or an open with write intent) next
  to `geopend` (read-only); the frontend shows an R/W badge per file and per event
- Dedup key extended with the action, so a read and a write of the same file each survive per window
- New "Active drives" panel: per drive the top write/read sources and files over the period, also
  when there were no spin-ups — answers "why is this drive never idle"
- Sample data includes write activity so the panel is visible in demo mode
- Format extended, not migrated: old `geopend` lines carry no direction and still render

## 2026-10-09: one file per day
- All data in `days/YYYY-MM-DD.csv` with lines `epoch,type,...` (disk, state, spin, who, activity)
- `state` lines at the start of each day and after a restart, so every day file can be read on its own; no false spin-ups
- Automatic migration of `spin.csv`, `disks.csv`, `who.csv` and `activity.csv`; old files moved to `legacy/`
- Retention per whole day; `/data/days.json` and `/data/days/<day>.csv` in the web server
- Markdown documentation translated to English
- Screenshot of the dashboard (sample data, light and dark) in the README
- Log messages in English; the start line shows the installed fatrace version instead of its usage text

## 2026-10-09: languages and browsing
- UI in English, Dutch, French, German and Spanish; defaults to the browser language, the choice is remembered
- Browse through time: per day (24 hours), per week (7 days) and per 30 days, with a "Now" button that is always visible and highlighted for the current period
- Shorter axis labels, thinned out on narrow screens
- Info icons with tooltips in "What wakes the drives" (root of a pool, unknown, shfs, already stopped) instead of explanation paragraphs
- Frontend tests run with `--test-force-exit`, so a failing test no longer hangs on the refresh timer

## 2026-10-08: Cache Dirs
- Processes in `IGNORE_PROCS` (default `cache_dirs`) and their children are not logged
- Source per PID determined on arrival and remembered for 30 s: events of short-lived processes no longer show up as `(al gestopt)`
- Cache Dirs also recognised via the command line and, as a safety net, by the same process name without a source
- README: `docker run` and Docker Compose examples, tip about Cache Dirs

## 2026-10-01: Unraid app
- Everything merged into one container (`app/spindash.py`): poller, fatrace watcher, web server, housekeeping
- Pools and disk names from `disks.ini` instead of `zpool`/`lsblk` on the host
- `smartctl -n standby` as a fallback for `hdparm`
- Our own writes are not logged
- Cleanup by age (`RETENTION_DAYS`) instead of by file size
- Community Applications template, icon, Dockerfile, CI (tests → image to GHCR)
- Unit tests: backend (pytest) and frontend (node:test + jsdom)
- Rankings count pool wake-ups as a single event
- Older per-file SMB lines are grouped under the share

## 2026-09-29 – 2026-09-30: cause and source
- `spin-who`: fanotify via `fatrace`, source per access (container / Unraid / user)
- Container detection regardless of cgroup namespace; container names via docker.sock
- Source behind `shfs` via inode comparison on `/proc/*/fd`
- Link older lines to containers after the fact on start
- SMB/NFS summarised per share
- Session list: all files per spin session, with time and source
- Events of pool members grouped into one row
- `who.csv` as a full source next to (and later instead of) `activity.csv`
- Labels without the "Unraid:" prefix; the kind is shown through colour

## 2026-09-28 – 2026-09-29: dashboard and collection
- Dashboard with a timeline per disk, histogram per hour, cause ranking and event table
- Live data via an nginx container, refreshed every minute
- `spinmon.sh`: spin state per minute, changes only; inotify watcher for the wake-up and sessions
- Pool detection for the array, XFS/Btrfs and ZFS
- Robust watcher: restarts, cleanup of orphans, clean stop via the process group
- Root folder listings recognised and explained ("Root of pool")
