# Todo

Open work, roughly by priority. Move finished items to `completed.md`.

## Before the first public release
- [ ] Build and test the image on an array-less setup with a Btrfs pool
- [ ] Test on a system with a classic array and parity (`/mnt/diskN`, `/dev/mdXpY`)
- [ ] Test with a ZFS pool with a dataset per share
- [ ] Verify the `shares.ini` field names (`useCache`, `cachePool`, and Unraid 7's secondary storage) on Unraid 6.12 and 7; Settings › Paths per container shows "unknown" when they do not match
- [ ] Fill in the support link (`<Support>` in the template) once the forum thread exists
- [ ] Support thread on the Unraid forum and submission to Community Applications

## Features
- [ ] Test SAS drives and HBAs; validate the `smartctl -n standby` fallback
- [ ] Optional: notifications (Unraid notification or webhook) for spin-ups outside a configured window
- [ ] Optional: set an "expected night window" per pool and highlight deviations
- [ ] Export a period as CSV from the UI
- [ ] Group containers by Docker Compose project (`project` is already in `/data/containers.json`)
- [ ] Bytes per container: only possible for direct `/mnt/diskN` or pool mappings, see `docs/decisions.md`

## Design (from the 2026-10-16 audit, medium priority)
- [ ] Use one word for the hardware: the UI mixes "drive" and "disk" (headings say drives, the table and filter say disk)
- [ ] Period buttons on a phone scroll horizontally with a hidden scrollbar, so "All" is cut off without a hint; show a fade or let them wrap
- [ ] Full file paths in Active drives and the live feed are only reachable by hover or tap, not by keyboard

## Technical
- [ ] Health endpoint (`/health`) with poller and watcher status, used by the Docker HEALTHCHECK
- [ ] Make `via_user_share` faster with many processes (cache fd scans per second)
- [ ] Multi-arch image not needed (Unraid is amd64), but document it
