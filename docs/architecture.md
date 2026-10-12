# Architecture

```
┌──────────────────────── container (privileged, --pid=host) ────────────────────────┐
│                                                                                    │
│  poller ── hdparm -C / smartctl -n standby ──► days/<day>.csv  (spin, state)       │
│     └── /emhttp/disks.ini + /sys/block/*/rotational ──► days/<day>.csv  (disk)     │
│                                                                                    │
│  watcher ── fatrace -c per HDD mount ──► queue ──► handle() ──► days/<day>.csv     │
│                                         classify(pid): /proc/<pid>/cgroup, stat     │
│                                         container_name(): docker.sock              │
│                                         shfs → via_user_share(): /proc/*/fd       │
│                                                                                    │
│  housekeeping ── prune_days() every 6 hours; migrate() and relabel_existing()      │
│                  on start                                                          │
│                                                                                    │
│  web ── ThreadingHTTPServer :8089 ── /  → static/index.html                        │
│                                      /data/days.json, /data/days/<day>.csv         │
│                                      /data/stream  (SSE: elke who-regel live)      │
└────────────────────────────────────────────────────────────────────────────────────┘
          ▲ /mnt (ro,slave)   ▲ /var/local/emhttp (ro)   ▲ docker.sock (ro)   ▼ /data (rw)
```

## Components

**Poller.** Every `POLL_INTERVAL` seconds. Determines the HDDs from `disks.ini` (only
`rotational=1`), polls their state and writes changes as `spin` lines. On the first poll after
start and on the first poll of each day it writes the current state of every disk as `state`
lines, plus the disk names as `disk` lines.

**Watcher.** Determines the mounts to watch: `/mnt/<name>` for array disks (`disk1`) and
pools (pool member `trunk2` belongs to `/mnt/trunk`), plus every mount below them (ZFS datasets).
Each mount gets a `fatrace -c` with filter `O+WD<` (open, write, create, delete, move). `fatrace` uses
`FAN_MARK_FILESYSTEM`, so access from other mount namespaces (the host, other containers) is
seen as well. Every minute it checks whether the mounts changed or a `fatrace` stopped.

**Classification.** Processes in `IGNORE_PROCS` (default `cache_dirs`), or with such a process as
an ancestor, are not logged. After that the order is: container (cgroup) → stopped process →
SMB/NFS → shell (sshd, ttyd, login) → shfs → btrfs → mover → cron → webGUI → process name. Opens by
`shfs` are re-examined 0.4 s later: which other process has the same file open via `/mnt/user`
(inode comparison)? The source of a new PID is already determined in the reader thread
(`classify_early`) and remembered per PID for `PID_TTL` (30 s), so events of a process that has
stopped in the meantime get the same source; "stopped" itself is not remembered. See
*Ignoring Cache Dirs* in `decisions.md`.

**Deduplication.** The same path + kind + name + action only once per `DEDUP_SECONDS` (600).
SMB/NFS is summarised into one line per share. The action is part of the key so that a read and a
write of the same file within the window each appear once.

**Storage.** All lines go to the day file of their own timestamp (`append_lines`). Old day files
are removed as a whole. On start, `migrate()` converts files from older versions; see
`docs/data-formats.md`.

**Frontend.** It fetches `days.json` and only the day files the shown period needs: the window
plus one day on each side (a cause just after midnight, a session that ends the next day), and
always today and yesterday for the live feed. Fetched days stay in memory; browsing back or
choosing "All" fetches the missing days on demand. Every minute it refetches `days.json` and the
live days (conditionally, 304 when unchanged). It merges `activity` lines (old inotify data) and `who` lines into one activity stream
and links it to spin-ups. `state` lines set the state but are never a spin-up. The period is
last hour, 24 hours, 7 days, 30 days or all; `offset` shifts the window back by whole periods.
Texts come from `I18N` (en, nl, fr, de, es) via `t()`; dates via `Intl` in the locale of the chosen
language. The `(al gestopt)` suffix in who lines is translated when shown; the data format itself
stays unchanged.

Below the spin-up analysis, the *Active drives* panel shows per drive the top write/read sources and
files over the period (also without spin-ups), and a *Live activity* feed lists the most recent file
events newest-first with an R/W marker and a relative time. The feed is **live**: the web server keeps
a Server-Sent Events stream (`/data/stream`) open per browser, and `write_who()` pushes each new line
onto every open stream the moment it is logged. So the feed updates instantly instead of waiting for
the next minute fetch; the periodic fetch stays the source of truth and re-syncs on (re)load. New
items get a short highlight, the feed re-renders every 30 s so the "x min ago" labels stay current,
and hovering a truncated file path shows the full path in an overlay.

## Why one process

This used to be a User Script (`spinmon.sh`), an inotify watcher, nginx and a separate
`spin-who` container. One process with threads makes installation through Community Applications
possible and prevents parts from stopping independently of each other.
