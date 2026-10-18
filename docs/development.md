# Development

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest -q

npm install
npm test
```

- **Backend** (`tests/test_*.py`): all host paths (`/proc`, `/sys/block`, `/proc/self/mounts`,
  `disks.ini`) and the data directory are redirected to a temporary folder by the `sd` fixture,
  which also pins `TZ` to `Europe/Amsterdam` so day boundaries are predictable. External commands
  (`hdparm`, `fatrace`) are not called; the logic around them can be tested on its own
  (`parse_hdparm`, `poll_once`, `handle`, `LINE`, `migrate`).
- **Frontend** (`tests/js/`): the dashboard runs in jsdom with a mocked `fetch` that serves
  `days.json` and day files.

## Running locally without Unraid

With a fake `hdparm` and your own `disks.ini` you can run the whole app on a laptop:

```bash
mkdir -p /tmp/sd/{emhttp,data,bin}
printf '["disk1"]\ndevice="sda"\n' > /tmp/sd/emhttp/disks.ini
printf '#!/bin/sh\necho " drive state is:  active/idle"\n' > /tmp/sd/bin/hdparm && chmod +x /tmp/sd/bin/hdparm
PATH=/tmp/sd/bin:$PATH DATA_DIR=/tmp/sd/data EMHTTP_DIR=/tmp/sd/emhttp \
  WATCH=/tmp PORT=8089 python3 app/spindash.py
```

`fatrace` needs root and a Linux kernel; without `fatrace` the watcher logs an error and the rest
keeps working. Old-format files (`spin.csv`, `who.csv`, …) placed in `DATA_DIR` are migrated on
start, which is a quick way to try the dashboard on real data. For the frontend only: open
`http://localhost:8089/?demo`. Force a language with `?lang=de` (en, nl, fr, de, es); otherwise the
UI follows the browser language or the last chosen language (`localStorage`, key `spindash.lang`).
The card layout from Settings is stored per browser under `spindash.cards`: a JSON list of
`{id, hidden}` in display order (ids `kpi`, `lanes`, `days`, `hist`, `causes`, `src`, `actR`, `actW`, `rest`, `feed`, `events`); unknown ids are
ignored and missing cards are added at the end. The old id `act` (one Active drives card) is read as
`actR` and `actW` in its place, with the same `hidden`. On a wide screen every card with `data-half`
(all but `kpi`, `lanes` and `events`) takes half the width; below 820px every card is full width.
Key figures, Spin-ups per day, Spin-ups by hour (at least the last 7 days) and Spin duration and rest are
computed from `data/spins.csv` (all days), per lane as on the timeline (a merged pool counts once); only the
spinning hours in Key figures add up the physical drives. "Woke quickly" in Spin duration and rest counts
spin-ups within 30 minutes of going to standby (`QUICK_MS`). The comparison uses the period of the same length right before the shown one,
and only when the data covers all of it; "All" has no comparison. Lane grouping is stored under `spindash.group` as
`{merge}` (default `true`, pools and the array together); it only changes the view, the day files stay per drive.
Hidden paths are a JSON list of patterns (`*` anything, `?` one character), stored on the server in
`DATA_DIR/spindash-settings.json` via `/data/settings.json` (see `docs/data-formats.md`), so every browser
sees the same list. The frontend loads them before the first data fetch and saves them on every change.
A copy stays in `localStorage` under `spindash.hide`; it is used in `?demo` mode and when the server
cannot be reached, and moved to the server once if the server has no list yet. A failed save is
shown under the field.
A pattern with a leading `/` matches the full path and its `/mnt/user/...` form; without it, the path inside
the share. Hidden activity is filtered out after loading (`data.allAct` → `data.act`), also for live events.

## README screenshots

The screenshots in `docs/images/` must always match the current dashboard (see `CLAUDE.md`). They are
made from the sample data (`?demo`, English, the 7-day view) at 1200 px wide and 2× scale, in light and
dark, with `scripts/screenshots.mjs`:

```bash
npm install --no-save playwright && npx playwright install chromium
node scripts/screenshots.mjs
```

It writes `screenshot-light.png` / `screenshot-dark.png` (the whole page) and
`active-drives-live-activity-light.png` / `active-drives-live-activity.png` (the reading, writing, spin
duration and live activity cards). Playwright is deliberately not in `package.json`, so CI and a normal
`npm install` do not need it. Check the new images before committing and update the alt texts in the README.

## Testing on Unraid

```bash
docker build -t spin-dashboard:dev .
docker run -d --name spin-dashboard-dev --privileged --pid=host -p 8090:8089 \
  -v /mnt:/mnt:ro,slave -v /var/local/emhttp:/emhttp:ro \
  -v /var/run/docker.sock:/var/run/docker.sock:ro \
  -v /mnt/user/appdata/spin-dashboard-dev:/data \
  spin-dashboard:dev
docker logs -f spin-dashboard-dev
```

Note: do not run two instances with the same `DATA_DIR`.
