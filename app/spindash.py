#!/usr/bin/env python3
"""Spin Dashboard voor Unraid.

Eén proces met drie onderdelen:

  poller   peilt elke POLL_INTERVAL seconden de status van alle HDD's (hdparm -C, met
           smartctl als terugval) en schrijft alleen wijzigingen naar spin.csv.
           De namen (parity, disk1, pool, pool2) komen uit Unraid's disks.ini.
  watcher  draait per HDD-mount een `fatrace -c` (fanotify) en legt vast welk proces
           welk bestand opent: een container, Unraid zelf of een gebruiker (SMB/shell).
  docker   volgt start/stop/health van containers (ctr-regels) en levert per container de mounts
           met waar de share staat (shares.ini) voor /data/containers.json.
  web      serveert het dashboard en de CSV's, en bewaart de instellingen (verborgen paden).

Alles staat in DATA_DIR/days: één CSV per dag (YYYY-MM-DD.csv, lokale datum) met regels
`epoch,soort,...` (disk, state, spin, proc, who, activity, ctr). Oudere losse bestanden (spin.csv, disks.csv,
who.csv, activity.csv) worden bij het starten eenmalig naar dagbestanden gemigreerd.
"""
import gzip
import hashlib
import http.client
import http.server
import json
import os
import queue
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.parse
from datetime import datetime
from functools import partial

# ---------- instellingen ----------
PORT = int(os.environ.get("PORT", "8089"))
DATA = os.environ.get("DATA_DIR", "/data")
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
EMHTTP = os.environ.get("EMHTTP_DIR", "/emhttp")          # /var/local/emhttp van de host
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "60"))
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "30"))
DEDUP_S = int(os.environ.get("DEDUP_SECONDS", "600"))   # zelfde pad + bron maar 1x per 10 min
WATCH_OVERRIDE = os.environ.get("WATCH", "").split()   # optioneel: vaste lijst mounts
# processen (en hun kinderen) die niet gelogd worden; cache_dirs opent continu alle mappen
IGNORE_PROCS = set(os.environ.get("IGNORE_PROCS", "cache_dirs").split())
SHFS_DELAY = 0.4
PID_TTL = 30          # uitkomst van classify() per PID zo lang onthouden
IGNORE_TTL = 300      # zo lang na een genegeerd proces dezelfde naam zonder bron ook negeren
IGNORE_SCAN = 30      # zo vaak opnieuw kijken of er een IGNORE_PROCS-proces draait (s)

# Paden naar het hostsysteem; in tests te vervangen
PROC = "/proc"
SYS_BLOCK = "/sys/block"
MOUNTS_FILE = "/proc/self/mounts"
USER_SHARES = "/mnt/user"
DOCKER_SOCK = "/var/run/docker.sock"

DAYS = os.path.join(DATA, "days")
LEGACY = os.path.join(DATA, "legacy")
# oude losse bestanden; alleen nog gelezen door migrate()
SPIN = os.path.join(DATA, "spin.csv")
DISKS = os.path.join(DATA, "disks.csv")
WHO = os.path.join(DATA, "who.csv")
ACTIVITY = os.path.join(DATA, "activity.csv")
# instellingen uit het dashboard (verborgen paden), gedeeld door alle browsers
SETTINGS = os.path.join(DATA, "spindash-settings.json")
OWN_FILES = {"spin.csv", "disks.csv", "who.csv", "activity.csv",
             "spindash-settings.json", "spindash-settings.json.tmp"}
DAY_FILE = re.compile(r"^\d{4}-\d{2}-\d{2}\.csv(?:\.tmp)?$")
TYPES = ("disk", "state", "spin", "proc", "who", "activity", "ctr")   # volgorde binnen dezelfde seconde

write_lock = threading.Lock()
# live-verbindingen (Server-Sent Events): elke write_who zet de regel in alle wachtende queues
streams = []
streams_lock = threading.Lock()


def log(*a):
    print(time.strftime("%F %T"), *a, flush=True)


# =====================================================================================
# Dagbestanden
# =====================================================================================
def day_of(ts):
    return time.strftime("%Y-%m-%d", time.localtime(float(ts)))


def day_path(day):
    return os.path.join(DAYS, f"{day}.csv")


def day_start(day):
    return int(time.mktime(time.strptime(day, "%Y-%m-%d")))


def append_lines(lines):
    """Regels `epoch,soort,...` toevoegen aan het dagbestand van hun eigen tijdstip."""
    by_day = {}
    for line in lines:
        by_day.setdefault(day_of(line.split(",", 1)[0]), []).append(line + "\n")
    with write_lock:
        os.makedirs(DAYS, exist_ok=True)
        for day, ls in by_day.items():
            with open(day_path(day), "a") as f:
                f.writelines(ls)


def list_days():
    try:
        return sorted(n[:-4] for n in os.listdir(DAYS) if DAY_FILE.match(n) and n.endswith(".csv"))
    except OSError:
        return []


def days_index():
    """Inhoud van /data/days.json: de dagen, hun grootte en het totaal (ongecomprimeerd), zodat de
    frontend alleen de dagen van de getoonde periode ophaalt en daarbij voortgang kan tonen."""
    days, sizes = list_days(), []
    for d in days:
        try:
            sizes.append(os.path.getsize(day_path(d)))
        except OSError:
            sizes.append(0)
    return {"days": days, "bytes": sum(sizes), "sizes": sizes}


_gz_cache = {}        # pad -> (mtime_ns, grootte, gzip-bytes); oude dagen veranderen niet meer


def day_body(path, st, gz):
    """Inhoud van een dagbestand, gzip-gecomprimeerd als de browser dat accepteert."""
    if not gz:
        with open(path, "rb") as f:
            return f.read()
    hit = _gz_cache.get(path)
    if hit and hit[:2] == (st.st_mtime_ns, st.st_size):
        return hit[2]
    with open(path, "rb") as f:
        body = gzip.compress(f.read(), compresslevel=6, mtime=0)
    if len(_gz_cache) >= 64:
        _gz_cache.clear()
    _gz_cache[path] = (st.st_mtime_ns, st.st_size, body)
    return body


_spin_cache = {}      # pad -> (mtime_ns, grootte, spin-regels); alleen de disk/state/spin-regels
SPIN_TYPES = ("disk", "state", "spin")


def spin_lines(path, st):
    """De disk-, state- en spin-regels van één dagbestand. Klein (de who-regels zijn het grootste deel),
    zodat de frontend kerncijfers en de trend over alle dagen kan tonen zonder alles op te halen."""
    hit = _spin_cache.get(path)
    if hit and hit[:2] == (st.st_mtime_ns, st.st_size):
        return hit[2]
    out = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            p = line.split(",", 2)
            if len(p) == 3 and p[1] in SPIN_TYPES:
                out.append(line if line.endswith("\n") else line + "\n")
    text = "".join(out)
    _spin_cache[path] = (st.st_mtime_ns, st.st_size, text)
    return text


def spins_body():
    """Inhoud van /data/spins.csv: de spin-regels van alle dagen op volgorde, plus een ETag die
    verandert zodra een dagbestand verandert."""
    parts, tag, days = [], [], list_days()
    for d in days:
        path = day_path(d)
        try:
            st = os.stat(path)
            parts.append(spin_lines(path, st))
        except OSError:
            continue
        tag.append(f"{d}:{st.st_mtime_ns:x}:{st.st_size:x}")
    live = {day_path(d) for d in days}
    for path in [p for p in _spin_cache if p not in live]:      # opgeruimde dagen vergeten
        _spin_cache.pop(path, None)
    etag = '"s-%s"' % hashlib.sha1("|".join(tag).encode()).hexdigest()[:16]
    return "".join(parts).encode(), etag


def is_own_file(path):
    name = os.path.basename(path.rstrip("/"))
    return name in OWN_FILES or bool(DAY_FILE.match(name))


# =====================================================================================
# Schijven en mounts
# =====================================================================================
def read_disks_ini():
    """[(device, naam)] uit disks.ini; flash en lege slots overgeslagen."""
    path = os.path.join(EMHTTP, "disks.ini")
    out, name, seen = [], None, set()
    try:
        for line in open(path):
            line = line.strip()
            if line.startswith("["):
                name = line.strip('[]"')
            elif line.startswith("device=") and name and name != "flash":
                dev = line.split("=", 1)[1].strip('"')
                if dev and dev not in seen:
                    seen.add(dev)
                    out.append((dev, name))
    except OSError:
        pass
    return out


def is_rotational(dev):
    try:
        return open(f"{SYS_BLOCK}/{dev}/queue/rotational").read().strip() == "1"
    except OSError:
        return False


def mount_base(name):
    """disk1 -> disk1, trunk2 -> trunk (poolleden), parity -> None."""
    if name.startswith("parity"):
        return None
    return name if re.fullmatch(r"disk\d+", name) else re.sub(r"\d+$", "", name)


def hdd_devices():
    """{device: naam} voor alle draaiende schijven. Zonder disks.ini: alle sdX met rotational=1."""
    mapped = {d: n for d, n in read_disks_ini() if is_rotational(d)}
    if mapped:
        return mapped
    try:
        return {d: d for d in os.listdir(SYS_BLOCK) if d.startswith("sd") and is_rotational(d)}
    except OSError:
        return {}


def mounted():
    out = []
    for line in open(MOUNTS_FILE):
        out.append(line.split()[1].replace("\\040", " "))
    return out


def target_mounts():
    """Mounts van HDD's onder /mnt, inclusief ZFS-datasets daaronder."""
    if WATCH_OVERRIDE:
        roots = [r.rstrip("/") for r in WATCH_OVERRIDE]
    else:
        roots = sorted({f"/mnt/{b}" for b in map(mount_base, hdd_devices().values()) if b})
    mps = mounted()
    return sorted({mp for mp in mps for r in roots if mp == r or mp.startswith(r + "/")})


# =====================================================================================
# Poller: spin-status
# =====================================================================================
def parse_hdparm(out):
    """Uitvoer van `hdparm -C` -> 'active', 'standby' of None."""
    m = re.search(r"drive state is:\s+(\S+)", out)
    if not m:
        return None
    s = m.group(1)
    if s in ("standby", "sleeping"):
        return "standby"
    if s.startswith("active") or s.startswith("idle"):
        return "active"
    return None


def drive_state(dev):
    """'active', 'standby' of None. hdparm maakt de schijf niet wakker; smartctl -n standby ook niet."""
    try:
        out = subprocess.run(["hdparm", "-C", f"/dev/{dev}"], capture_output=True, text=True, timeout=15).stdout
        st = parse_hdparm(out)
        if st:
            return st
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:   # SAS en controllers waar hdparm niets zegt
        r = subprocess.run(["smartctl", "-n", "standby", "-i", f"/dev/{dev}"],
                           capture_output=True, text=True, timeout=15)
        if "STANDBY" in r.stdout.upper():
            return "standby"
        if r.returncode == 0:
            return "active"
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def poll_once(last, now=None):
    """Eén peiling. Schrijft naar het dagbestand:
    - `disk`-regels bij de eerste peiling van een dag of als de schijfnamen veranderen;
    - `spin` bij een statuswijziging;
    - `state` met de huidige status bij de eerste peiling van een dag of na het starten,
      zodat elk dagbestand op zichzelf leesbaar is. Een `state` is geen spin-up.
    `last` houdt de vorige status bij. Geeft het aantal spin- en state-regels terug."""
    now = int(time.time() if now is None else now)
    day, devs = day_of(now), hdd_devices()
    known = last.setdefault("st", {})
    new_day = last.get("day") != day
    lines = []
    if new_day or devs != last.get("disks"):
        lines += [f"{now},disk,{d},{n}" for d, n in sorted(devs.items())]
    n = 0
    for dev in sorted(devs):
        st = drive_state(dev)
        if not st:
            continue
        if dev in known and st != known[dev]:
            lines.append(f"{now},spin,{dev},{st}")
            n += 1
        elif dev not in known or new_day:
            lines.append(f"{now},state,{dev},{st}")
            n += 1
        known[dev] = st
    last["day"], last["disks"] = day, dict(devs)
    if lines:
        append_lines(lines)
    if not devs:
        log("No HDDs found. Is /var/local/emhttp mounted and is the container privileged?")
    return n


def poller():
    last = {}
    while True:
        try:
            poll_once(last)
        except Exception as e:
            log("poller error:", repr(e))
        time.sleep(POLL_INTERVAL)


# =====================================================================================
# Watcher: wie opent wat (fatrace)
# =====================================================================================
LINE = re.compile(r"^(\d+)\.\d+ (.*)\((\d+)\): ([RWOCD+<>]+)\s+(/.*?)(?: exe=.*)?$")
# W (schrijven) staat vóór O (openen): een open met schrijfintentie (OW/ORW) telt als schrijven
OPS = [("D", "verwijderd"), ("+", "aangemaakt"), ("<", "verplaatst"), (">", "verplaatst"),
       ("W", "geschreven"), ("O", "geopend")]
SHELLS = {"sshd": "ssh", "sshd-session": "ssh", "ttyd": "webterminal", "login": "console",
          "agetty": "console", "tmux: server": "tmux", "screen": "screen"}

events = queue.Queue(maxsize=20_000)
procs = []
recent = {}
names = {}
seen = {}             # pid -> (tijd, comm, uitkomst van classify)
ignored_comms = {}    # comm -> laatste keer dat een proces met die naam genegeerd werd
stats = {"written": 0, "dropped": 0}
MY_PID = str(os.getpid())


def proc_comm_ppid(pid):
    st = open(f"{PROC}/{pid}/stat").read()
    return st[st.index("(") + 1:st.rindex(")")], st[st.rindex(")") + 2:].split()[1]


def chain_of(pid, limit=15):
    chain = []
    while pid and pid not in ("0", "1") and len(chain) < limit:
        try:
            comm, pid = proc_comm_ppid(pid)
        except (OSError, ValueError, IndexError):
            break
        chain.append(comm)
    return chain


_ignore_seen = {"t": None, "on": False}


def ignore_active(now=None):
    """Draait er een proces uit IGNORE_PROCS (bv. cache_dirs)? Hooguit eens per IGNORE_SCAN opnieuw bekeken."""
    now = time.time() if now is None else now
    if _ignore_seen["t"] is not None and now - _ignore_seen["t"] < IGNORE_SCAN:
        return _ignore_seen["on"]
    on = False
    try:
        pids = [p for p in os.listdir(PROC) if p.isdigit()]
    except OSError:
        pids = []
    for p in pids:
        try:
            comm, _ = proc_comm_ppid(p)
        except (OSError, ValueError, IndexError):
            continue
        if comm in IGNORE_PROCS:
            on = True
            break
        try:
            args = open(f"{PROC}/{p}/cmdline", "rb").read().split(b"\0")[:2]
        except OSError:
            continue
        if any(os.path.basename(a.decode(errors="replace")) in IGNORE_PROCS for a in args):
            on = True
            break
    _ignore_seen.update(t=now, on=on)
    return on


def is_ignored(pid, limit=15):
    """Hoort dit proces (of een voorouder) bij IGNORE_PROCS? Kijkt naar de procesnaam en naar de
    opdrachtregel, voor scripts die als `bash /pad/naar/cache_dirs` gestart zijn."""
    while pid and pid not in ("0", "1") and limit:
        limit -= 1
        try:
            comm, ppid = proc_comm_ppid(pid)
        except (OSError, ValueError, IndexError):
            return False
        if comm in IGNORE_PROCS:
            return True
        try:
            args = open(f"{PROC}/{pid}/cmdline", "rb").read().split(b"\0")[:2]
        except OSError:
            args = []
        if any(os.path.basename(a.decode(errors="replace")) in IGNORE_PROCS for a in args):
            return True
        pid = ppid
    return False


def container_id(pid):
    try:
        cg = open(f"{PROC}/{pid}/cgroup").read()
    except OSError:
        return None
    # met een eigen cgroup-namespace ziet dit eruit als "0::/../<id>", zonder "docker/"
    m = re.search(r"(?:^|[/-])([0-9a-f]{64})(?:\.scope)?\s*$", cg, re.M)
    return m.group(1) if m else None


def docker_conn(timeout):
    """HTTP over de Docker-socket. timeout None: blijven wachten (de events-stroom)."""
    c = http.client.HTTPConnection("localhost", timeout=timeout)
    c.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    c.sock.settimeout(timeout)
    c.sock.connect(DOCKER_SOCK)
    return c


def docker_get(path, timeout=2):
    """JSON van een GET op de Docker-API, of None (geen socket, fout, andere status)."""
    try:
        c = docker_conn(timeout)
        c.request("GET", path)
        r = c.getresponse()
        body = r.read()
        c.close()
        return json.loads(body) if r.status == 200 else None
    except Exception:
        return None


def container_name(cid):
    if cid in names:
        return names[cid]
    info = docker_get(f"/containers/{cid}/json")
    name = (info or {}).get("Name", "").lstrip("/") or cid[:12]
    names[cid] = name
    return name


def classify(pid, comm, now=None):
    """Als classify_now(), maar onthoudt de uitkomst per PID. Een kortlevend proces (find van
    cache_dirs) is vaak al gestopt voordat al zijn events verwerkt zijn; de latere events krijgen
    zo dezelfde uitkomst als het eerste, toen het proces nog draaide.

    Vangnet: is kort geleden een proces met dezelfde naam genegeerd, dan wordt een proces met die
    naam zonder herkenbare bron (gestopt, of verweesd zodat de keten niet meer te volgen is) ook
    genegeerd. Een find vanuit een shell, SMB of cron heeft wel een bron en wordt gewoon gelogd."""
    now = time.time() if now is None else now
    hit = seen.get(pid)
    if hit and now - hit[0] < PID_TTL and hit[1] == comm:
        return hit[2]
    src = classify_now(pid, comm)
    if src is None:
        ignored_comms[comm] = now
    elif (src in (("unraid", comm), ("onbekend", f"{comm} (al gestopt)"))
          and now - ignored_comms.get(comm, -IGNORE_TTL) < IGNORE_TTL):
        src = None
    if src is None or src[0] != "onbekend":
        seen[pid] = (now, comm, src)
        if len(seen) > 10_000:
            for k, v in list(seen.items()):
                if now - v[0] >= PID_TTL:
                    seen.pop(k, None)
    return src


def classify_now(pid, comm):
    """(soort, naam) van een proces, of None als het genegeerd wordt (IGNORE_PROCS)."""
    cid = container_id(pid)
    if cid:
        return "container", container_name(cid)
    if not os.path.exists(f"{PROC}/{pid}"):
        return "onbekend", f"{comm} (al gestopt)"
    if is_ignored(pid):
        return None
    chain = [comm] + (chain_of(pid) or [comm])[1:]
    if "smbd" in chain:
        return "gebruiker", "SMB-share"
    if any(c.startswith("nfsd") for c in chain):
        return "gebruiker", "NFS-share"
    for c in chain:
        if c in SHELLS:
            return "gebruiker", f"shell ({SHELLS[c]})"
    if comm == "shfs":
        return "unraid", "shfs (via /mnt/user)"
    if comm.startswith("btrfs"):
        return "unraid", "btrfs"
    for c in chain:
        if c in ("mover", "move", "age_mover"):
            return "unraid", "mover"
        if c in ("crond", "cron"):
            return "unraid", "cron / User Scripts"
        if c.startswith("php") or c in ("emhttpd", "emhttp", "nginx"):
            return "unraid", "webGUI"
    return "unraid", comm


def via_user_share(path):
    """Wie heeft /mnt/pool/Share/x via /mnt/user/Share/x open? Vergelijkt inodes."""
    parts = path.split("/")
    if len(parts) < 5:
        return None
    try:
        want = os.stat(f"{USER_SHARES}/" + "/".join(parts[3:]))
    except OSError:
        return None
    key, tail = (want.st_dev, want.st_ino), "/" + parts[-1]
    for pid in os.listdir(PROC):
        if not pid.isdigit():
            continue
        fddir = f"{PROC}/{pid}/fd"
        try:
            fds = os.listdir(fddir)
        except OSError:
            continue
        for fd in fds:
            try:
                if not os.readlink(f"{fddir}/{fd}").endswith(tail):
                    continue
                st = os.stat(f"{fddir}/{fd}")
            except OSError:
                continue
            if (st.st_dev, st.st_ino) == key:
                try:
                    comm, _ = proc_comm_ppid(pid)
                except (OSError, ValueError, IndexError):
                    continue
                if comm != "shfs":
                    return pid, comm
    return None


def share_root(path):
    """/mnt/pool/Share/a/b.mkv -> /mnt/pool/Share/ (ongewijzigd als er geen share in zit)."""
    parts = path.split("/")
    return "/".join(parts[:4]) + "/" if len(parts) > 4 else path


procs_seen = {}       # pid -> (comm, dag) van de laatste proc-regel


def proc_line(ts, pid, comm):
    """`proc`-regel als deze PID vandaag nog niet met deze procesnaam gelogd is, anders None.
    Zo weet de frontend welk proces binnen een container (Plex Media Scanner, Plex Transcoder)
    een bestand opende, zonder de who-regel te veranderen."""
    day = day_of(ts)
    if procs_seen.get(pid) == (comm, day):
        return None
    if len(procs_seen) > 50_000:
        procs_seen.clear()
    procs_seen[pid] = (comm, day)
    return f"{ts},proc,{pid},{comm.replace(',', ' ')}"


def write_who(ts, kind, name, pid, op, path, now=None, comm=None):
    if kind == "gebruiker" and name in ("SMB-share", "NFS-share"):
        path = share_root(path)              # één regel per share
    now = time.time() if now is None else now
    # op zit in de sleutel: een lees en een schrijf van hetzelfde pad tellen elk één keer
    k = (path, kind, name, op)
    with write_lock:
        if now - recent.get(k, 0) < DEDUP_S:
            return
        recent[k] = now
        if len(recent) > 50_000:
            for kk in [kk for kk, t in recent.items() if now - t > DEDUP_S]:
                del recent[kk]
    line = f"{ts},who,{kind},{name.replace(',', ' ')},{pid},{op},{path}"
    proc = proc_line(ts, pid, comm) if kind == "container" and comm else None
    append_lines([proc, line] if proc else [line])
    stats["written"] += 1
    if proc:
        broadcast(proc)                      # vóór de who-regel, zodat de live-feed de naam al kent
    broadcast(line)


def broadcast(line):
    """Leg een who-regel op alle open SSE-verbindingen. Volle of dichte verbindingen
    worden overgeslagen; de periodieke CSV-verversing blijft de bron van waarheid."""
    with streams_lock:
        for q in list(streams):
            try:
                q.put_nowait(line)
            except queue.Full:
                pass


def resolve_shfs_later(ts, pid, op, path):
    def run():
        time.sleep(SHFS_DELAY)
        hit = via_user_share(path)
        if hit:
            src = classify(*hit)
            if src:
                write_who(ts, *src, hit[0], op, path, comm=hit[1])
        else:
            write_who(ts, "unraid", "shfs (via /mnt/user)", pid, op, path)
    threading.Thread(target=run, daemon=True).start()


def classify_early(line):
    """Direct bij binnenkomst (in de leesthread) de bron van een nieuwe PID bepalen, zolang het
    proces nog draait. De consumer kan achterlopen; classify() geeft dan de onthouden uitkomst."""
    m = LINE.match(line)
    if m and m.group(3) not in seen and m.group(3) != MY_PID:
        classify(m.group(3), m.group(2))


def handle(line):
    m = LINE.match(line.rstrip("\n"))
    if not m:
        return
    ts, comm, pid, types, path = m.groups()
    if pid == MY_PID or is_own_file(path):
        return                               # eigen schrijfacties niet loggen
    if not path.endswith("/") and "D" not in types and os.path.isdir(path):
        path += "/"
    op = next((label for c, label in OPS if c in types), None)
    if not op:
        return
    if comm == "shfs" and op == "geopend" and not path.endswith("/"):
        resolve_shfs_later(ts, pid, op, path)
        return
    src = classify(pid, comm)
    # Cache Dirs draait find zo kort dat fatrace soms alleen nog "unknown" ziet en de ouders weg zijn.
    # find opent alleen mappen: een map-open door een al gestopt proces zonder bron telt dan niet mee.
    if src and src[0] == "onbekend" and op == "geopend" and path.endswith("/") and ignore_active():
        return
    if src:
        write_who(ts, *src, pid, op, path, comm=comm)


def consumer():
    while True:
        try:
            handle(events.get())
        except Exception as e:
            log("error handling event:", repr(e))


def start_fatrace(mp):
    try:
        p = subprocess.Popen(["fatrace", "-c", "-tt", "-f", "O+WD<"], cwd=mp,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, errors="replace", bufsize=1)
    except OSError as e:
        log("could not start fatrace for", mp, e)
        return
    procs.append(p)

    def pump_out():
        for line in p.stdout:
            try:
                classify_early(line)
            except Exception as e:
                log("error in early classification:", repr(e))
            try:
                events.put_nowait(line)
            except queue.Full:
                stats["dropped"] += 1
        log("fatrace stopped for", mp, "exit code", p.wait())

    def pump_err():
        for line in p.stderr:
            log(f"fatrace {mp}:", line.rstrip())

    threading.Thread(target=pump_out, daemon=True).start()
    threading.Thread(target=pump_err, daemon=True).start()


def watcher():
    threading.Thread(target=consumer, daemon=True).start()
    current, beat = None, 0
    while True:
        try:
            mounts = target_mounts()
            dead = any(p.poll() is not None for p in procs)
            if mounts != current or dead:
                for p in procs:
                    p.terminate()
                procs.clear()
                if mounts:
                    log("Watching:", " ".join(mounts))
                    for mp in mounts:
                        start_fatrace(mp)
                else:
                    log("No HDD mounts found under /mnt; retrying in a minute")
                current = mounts
        except Exception as e:
            log("watcher error:", repr(e))
        beat += 1
        if beat % 10 == 0 and current:
            log(f"active: {len(procs)} fatrace, {stats['written']} lines, "
                f"queue {events.qsize()}, dropped {stats['dropped']}")
        time.sleep(60)


# =====================================================================================
# Docker: levenscyclus (ctr-regels) en mounts per container
# =====================================================================================
CTR_ACTIONS = {"start": "start", "die": "die",
               "health_status: healthy": "healthy", "health_status: unhealthy": "unhealthy"}


def ctr_line(ev, now=None):
    """`ctr`-regel voor een Docker-event (start, die met exitcode, healthy, unhealthy), anders None."""
    if ev.get("Type") != "container":
        return None
    act = CTR_ACTIONS.get(ev.get("Action") or ev.get("status") or "")
    attrs = (ev.get("Actor") or {}).get("Attributes") or {}
    name = (attrs.get("name") or "").replace(",", " ")
    if not act or not name:
        return None
    ts = int(ev.get("time") or (time.time() if now is None else now))
    line = f"{ts},ctr,{name},{act}"
    code = str(attrs.get("exitCode", ""))
    if act == "die" and code.lstrip("-").isdigit():
        line += f",{code}"
    return line


def docker_events_once():
    """Eén verbinding met de events-stroom; keert terug als Docker de verbinding sluit."""
    flt = urllib.parse.quote(json.dumps({"type": ["container"]}))
    c = docker_conn(None)
    c.request("GET", f"/events?filters={flt}")
    r = c.getresponse()
    if r.status != 200:
        raise OSError(f"docker events: HTTP {r.status}")
    while True:
        raw = r.readline()
        if not raw:
            return
        try:
            line = ctr_line(json.loads(raw))
        except ValueError:
            continue
        if line:
            append_lines([line])


def docker_events():
    if not os.path.exists(DOCKER_SOCK):
        log(f"No Docker socket at {DOCKER_SOCK}: container start/stop is not logged")
        return
    while True:
        try:
            docker_events_once()
        except Exception as e:
            log("docker events:", repr(e))
        time.sleep(30)


def read_shares_ini():
    """{share: {sleutel: waarde}} uit Unraid's shares.ini (tmpfs onder /emhttp, wekt niets)."""
    out, cur = {}, None
    try:
        for line in open(os.path.join(EMHTTP, "shares.ini")):
            line = line.strip()
            if line.startswith("["):
                cur = out.setdefault(line.strip('[]"'), {})
            elif "=" in line and cur is not None:
                k, v = line.split("=", 1)
                cur[k.strip()] = v.strip().strip('"')
    except OSError:
        pass
    return out


def share_storage(sh):
    """(opslag, pool) volgens de share-instellingen. Opslag: pool (alleen pool), array (alleen array),
    pool>array (eerst pool, de mover zet het naar de array), array>pool (de mover haalt het naar de
    pool) of unknown. De veldnamen verschillen per Unraid-versie; onbekend is unknown."""
    use = (sh.get("useCache") or sh.get("shareUseCache") or "").lower()
    pool = sh.get("cachePool") or sh.get("shareCachePool") or "cache"
    return {"only": "pool", "no": "array", "yes": "pool>array", "prefer": "array>pool"}.get(use, "unknown"), pool


def locate(src, shares, hdd_bases):
    """Waar een host-pad onder /mnt staat: een share (met opslag), een arrayschijf of een pool,
    en of dat (mogelijk) een HDD is. Alleen instellingen en namen: geen stat op de schijf (regel 1)."""
    p = [x for x in src.split("/") if x]
    if len(p) < 2 or p[0] != "mnt":
        return None
    array_hdd = any(re.fullmatch(r"disk\d+", b) for b in hdd_bases)
    top = p[1]
    if top in ("user", "user0"):
        if len(p) < 3:
            return {"kind": "user", "hdd": array_hdd or bool(hdd_bases)}
        st, pool = share_storage(shares.get(p[2], {}))
        if top == "user0":
            st = "array"
        hdd = {"pool": pool in hdd_bases, "array": array_hdd, "pool>array": array_hdd,
               "array>pool": pool in hdd_bases}.get(st)
        return {"kind": "share", "share": p[2], "storage": st, "pool": pool, "hdd": hdd}
    kind = "disk" if re.fullmatch(r"disk\d+", top) else "pool"
    return {"kind": kind, "name": top, "hdd": top in hdd_bases}


_containers_cache = {"t": 0.0, "body": None}


def containers_info(now=None):
    """Inhoud van /data/containers.json: per container de mounts onder /mnt en waar die staan.
    Hooguit eens per 30 s opnieuw opgevraagd; zonder Docker-socket een lege lijst."""
    now = time.time() if now is None else now
    if _containers_cache["body"] is not None and now - _containers_cache["t"] < 30:
        return _containers_cache["body"]
    shares = read_shares_ini()
    hdd_bases = {b for b in map(mount_base, hdd_devices().values()) if b}
    out = []
    for c in docker_get("/containers/json?all=1") or []:
        name = ((c.get("Names") or [""])[0]).lstrip("/")
        if not name:
            continue
        mounts = []
        for m in c.get("Mounts") or []:
            where = locate(m.get("Source") or "", shares, hdd_bases)
            if where:
                mounts.append({"source": m.get("Source"), "target": m.get("Destination"),
                               "rw": bool(m.get("RW", True)), "where": where})
        out.append({"name": name, "state": c.get("State") or "",
                    "project": (c.get("Labels") or {}).get("com.docker.compose.project") or "",
                    "mounts": mounts})
    body = {"docker": os.path.exists(DOCKER_SOCK), "containers": sorted(out, key=lambda x: x["name"].lower())}
    _containers_cache.update(t=now, body=body)
    return body


# =====================================================================================
# Onderhoud: oude regels opruimen, oude labels herstellen
# =====================================================================================
def epoch_of(line):
    first = line.split(",", 1)[0]
    if first.isdigit():
        return int(first)
    try:
        return datetime.fromisoformat(first).timestamp()
    except ValueError:
        return None


def prune_days(now=None):
    """Dagbestanden ouder dan RETENTION_DAYS verwijderen (hele dagen)."""
    cutoff = day_of((time.time() if now is None else now) - RETENTION_DAYS * 86400)
    old = [d for d in list_days() if d < cutoff]
    with write_lock:
        for d in old:
            try:
                os.remove(day_path(d))
            except OSError:
                pass
    if old:
        log(f"removed {len(old)} day file(s) older than {RETENTION_DAYS} days")


def relabel_existing(now=None):
    """who-regels met 'unraid'/'onbekend' van vandaag en gisteren alsnog aan een container
    koppelen als het proces nog draait."""
    now = time.time() if now is None else now
    for day in {day_of(now - 86400), day_of(now)}:
        path = day_path(day)
        with write_lock:
            try:
                lines = open(path).readlines()
            except OSError:
                continue
            changed = 0
            for i, line in enumerate(lines):
                p = line.rstrip("\n").split(",", 6)
                if len(p) < 7 or p[1] != "who" or p[2] not in ("unraid", "onbekend"):
                    continue
                try:
                    comm, _ = proc_comm_ppid(p[4])
                except (OSError, ValueError, IndexError):
                    continue
                cid = container_id(p[4])
                if not cid or comm != p[3].replace(" (al gestopt)", ""):
                    continue
                p[2], p[3] = "container", container_name(cid).replace(",", " ")
                lines[i] = ",".join(p) + "\n"
                changed += 1
            if changed:
                open(path + ".tmp", "w").writelines(lines)
                os.replace(path + ".tmp", path)
                log(f"{day}: linked {changed} older line(s) to a container")


def housekeeping():
    while True:
        try:
            prune_days()
        except Exception as e:
            log("cleanup failed:", repr(e))
        time.sleep(6 * 3600)


# =====================================================================================
# Migratie van de losse bestanden (spin.csv, disks.csv, who.csv, activity.csv)
# =====================================================================================
def _legacy_records():
    """(epoch, regel) in het nieuwe formaat uit de oude bestanden, de statuswijzigingen
    [(epoch, device, status)] en de schijfnamen."""
    recs, spins, disks, skipped = [], [], [], 0
    for line in _read(DISKS):
        p = line.split(",")
        if len(p) == 2 and p[0] and p[1]:
            disks.append((p[0], p[1]))
    for line in _read(SPIN):                 # 2026-10-01T04:15:07+02:00,/dev/sdd,active
        p = line.split(",")
        ts = epoch_of(line)
        if len(p) == 3 and ts is not None and p[2] in ("active", "standby"):
            ts, dev = int(ts), p[1].replace("/dev/", "")
            recs.append((ts, f"{ts},spin,{dev},{p[2]}"))
            spins.append((ts, dev, p[2]))
        else:
            skipped += 1
    for line in _read(WHO):                  # epoch,soort,naam,pid,actie,pad
        p = line.split(",", 5)
        if len(p) == 6 and p[0].isdigit() and p[5].startswith("/"):
            recs.append((int(p[0]), f"{p[0]},who,{line.split(',', 1)[1]}"))
        else:
            skipped += 1
    for line in _read(ACTIVITY):             # epoch,disk,EV,pad
        p = line.split(",", 3)
        if len(p) == 4 and p[0].isdigit() and p[3].startswith("/"):
            recs.append((int(p[0]), f"{p[0]},activity,{line.split(',', 1)[1]}"))
        else:
            skipped += 1
    return recs, sorted(spins), disks, skipped


def _read(path):
    try:
        return [l.rstrip("\n") for l in open(path) if l.strip()]
    except OSError:
        return []


def _sort_key(line):
    p = line.split(",", 2)
    try:
        return int(p[0]), TYPES.index(p[1]), line
    except (ValueError, IndexError):
        return 0, 0, line


def migrate():
    """Losse bestanden eenmalig omzetten naar dagbestanden. Veilig om te herhalen: bestaande
    dagregels blijven staan en dubbele regels worden samengevoegd. De oude bestanden gaan naar
    DATA_DIR/legacy, zodat er niets verloren gaat."""
    sources = [f for f in (SPIN, DISKS, WHO, ACTIVITY) if os.path.exists(f)]
    if not sources:
        return
    recs, spins, disks, skipped = _legacy_records()
    by_day = {}
    for ts, line in recs:
        by_day.setdefault(day_of(ts), []).append(line)
    with write_lock:
        os.makedirs(DAYS, exist_ok=True)
        for day, lines in by_day.items():
            t0 = day_start(day)
            head = [f"{t0},disk,{d},{n}" for d, n in disks]
            state = {}
            for ts, dev, st in spins:        # status van elke schijf bij het begin van de dag
                if ts >= t0:
                    break
                state[dev] = st
            head += [f"{t0},state,{d},{st}" for d, st in sorted(state.items())]
            path = day_path(day)
            merged = set(_read(path)) | set(head) | set(lines)
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                f.writelines(l + "\n" for l in sorted(merged, key=_sort_key))
            os.replace(tmp, path)
        os.makedirs(LEGACY, exist_ok=True)
        for f in sources:
            dest = os.path.join(LEGACY, os.path.basename(f))
            if os.path.exists(dest):
                dest += f".{int(time.time())}"
            os.replace(f, dest)
    log(f"Migration: {len(recs)} lines from {', '.join(os.path.basename(f) for f in sources)} "
        f"split into {len(by_day)} day files; old files moved to {LEGACY}"
        + (f"; skipped {skipped} unreadable line(s)" if skipped else ""))


# =====================================================================================
# Instellingen
# =====================================================================================
HIDE_MAX = 200          # patronen
HIDE_LEN = 500          # tekens per patroon
SETTINGS_BODY_MAX = 64 * 1024
settings_lock = threading.Lock()


def read_settings():
    """Opgeslagen instellingen; {} als er (nog) niets is of het bestand onleesbaar is.
    Zonder sleutel 'hide' weet de browser dat hij zijn eigen lijst eenmalig mag overzetten."""
    try:
        with open(SETTINGS) as f:
            s = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(s, dict):
        return {}
    try:
        return {"hide": clean_hide(s["hide"])} if "hide" in s else {}
    except ValueError:
        return {}


def clean_hide(value):
    """Lijst met patronen controleren: alleen tekst, zonder stuurtekens, binnen de limieten.
    Lege regels vallen weg; ValueError bij iets wat geen geldige lijst is."""
    if not isinstance(value, list) or len(value) > HIDE_MAX:
        raise ValueError(f"hide must be a list of at most {HIDE_MAX} patterns")
    out = []
    for p in value:
        if not isinstance(p, str):
            raise ValueError("every pattern must be a string")
        p = p.strip()
        if len(p) > HIDE_LEN or any(ord(c) < 32 or ord(c) == 127 for c in p):
            raise ValueError(f"patterns are at most {HIDE_LEN} characters, without control characters")
        if p and p not in out:
            out.append(p)
    return out


def save_settings(new):
    """Bekende sleutels uit `new` controleren en atomair wegschrijven (alleen in DATA_DIR).
    Geeft de opgeslagen instellingen terug; ValueError bij ongeldige invoer."""
    if not isinstance(new, dict):
        raise ValueError("settings must be a JSON object")
    with settings_lock:
        s = read_settings()
        if "hide" in new:
            s["hide"] = clean_hide(new["hide"])
        tmp = SETTINGS + ".tmp"
        with open(tmp, "w") as f:
            json.dump(s, f, indent=1)
        os.replace(tmp, SETTINGS)
    return s


# =====================================================================================
# Web
# =====================================================================================
class Handler(http.server.SimpleHTTPRequestHandler):
    """/ → dashboard, /data/days.json → lijst van dagen, /data/days/YYYY-MM-DD.csv → dagbestand,
    /data/spins.csv → alleen de spin-regels van alle dagen, /data/containers.json → mounts per
    container, /data/settings.json → instellingen (GET lezen, POST opslaan)."""
    def do_GET(self):
        clean = self.path.split("?", 1)[0]
        if clean == "/data/days.json":
            self.send_json(days_index())
            return
        if clean == "/data/settings.json":
            self.send_json(read_settings())
            return
        if clean == "/data/spins.csv":
            self.send_spins()
            return
        if clean == "/data/containers.json":
            self.send_json(containers_info())
            return
        if clean == "/data/stream":
            self.stream_events()
            return
        m = re.fullmatch(r"/data/days/(\d{4}-\d{2}-\d{2}\.csv)", clean)
        if m:
            self.send_day(os.path.join(DAYS, m.group(1)))
            return
        super().do_GET()

    def do_POST(self):
        if self.path.split("?", 1)[0] != "/data/settings.json":
            self.send_error(404)
            return
        # alleen JSON: een gewoon formulier van een andere site kan dan niets opslaan
        # (een cross-site JSON-verzoek vraagt eerst toestemming, die we nooit geven)
        if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
            self.send_error(415)
            return
        try:
            size = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self.send_error(411)
            return
        if not 0 <= size <= SETTINGS_BODY_MAX:
            self.send_error(413)
            return
        try:
            saved = save_settings(json.loads(self.rfile.read(size) or b"null"))
        except ValueError as e:      # ook onleesbare JSON
            self.send_json({"error": str(e)}, 400)
            return
        except OSError as e:
            log("could not save settings:", repr(e))
            self.send_json({"error": "could not save settings"}, 500)
            return
        self.send_json(saved)

    def send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_spins(self):
        body, etag = spins_body()
        gz = "gzip" in self.headers.get("Accept-Encoding", "")
        if gz:
            etag = etag[:-1] + '-gz"'
        if etag in self.headers.get("If-None-Match", ""):
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Vary", "Accept-Encoding")
            self.end_headers()
            return
        if gz:
            body = gzip.compress(body, compresslevel=6, mtime=0)
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        if gz:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("ETag", etag)
        self.send_header("Vary", "Accept-Encoding")
        self.end_headers()
        self.wfile.write(body)

    def send_day(self, path):
        """Dagbestand met gzip (als de browser dat accepteert) en een ETag, zodat een ongewijzigde
        dag een 304 zonder inhoud oplevert."""
        try:
            st = os.stat(path)
        except OSError:
            self.send_error(404)
            return
        gz = "gzip" in self.headers.get("Accept-Encoding", "")
        etag = f'"{st.st_mtime_ns:x}-{st.st_size:x}{"-gz" if gz else ""}"'
        if etag in self.headers.get("If-None-Match", ""):
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Vary", "Accept-Encoding")
            self.end_headers()
            return
        try:
            body = day_body(path, st, gz)
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        if gz:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("ETag", etag)
        self.send_header("Vary", "Accept-Encoding")
        self.send_header("Last-Modified", self.date_time_string(st.st_mtime))
        self.end_headers()
        self.wfile.write(body)

    def stream_events(self):
        """Server-Sent Events: stuur elke nieuwe who-regel direct naar de browser,
        zodat de live-feed stroomt in plaats van elke minuut te wachten op de CSV."""
        q = queue.Queue(maxsize=500)
        with streams_lock:
            streams.append(q)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")   # geen proxy-buffering
            self.end_headers()
            self.wfile.write(b": ok\n\n")
            self.wfile.flush()
            while True:
                try:
                    line = q.get(timeout=30)
                    self.wfile.write(f"data: {line}\n\n".encode())
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")        # houd de verbinding levend
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with streams_lock:
                if q in streams:
                    streams.remove(q)

    def translate_path(self, path):
        clean = path.split("?", 1)[0].split("#", 1)[0]
        if clean.startswith("/data/"):
            m = re.fullmatch(r"/data/days/(\d{4}-\d{2}-\d{2}\.csv)", clean)
            return os.path.join(DAYS, m.group(1)) if m else os.path.join(DAYS, "_")
        return super().translate_path(path)

    def end_headers(self):
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def log_message(self, *args):
        pass


def web():
    srv = http.server.ThreadingHTTPServer(("", PORT), partial(Handler, directory=STATIC))
    log(f"Dashboard on port {PORT}")
    srv.serve_forever()


# =====================================================================================
def fatrace_version():
    """fatrace heeft geen --version; de pakketversie uit dpkg, anders alleen of hij er is."""
    try:
        r = subprocess.run(["dpkg-query", "-W", "-f=${Version}", "fatrace"], capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except OSError:
        pass
    return "found" if shutil.which("fatrace") else "not found"


def main():
    os.makedirs(DAYS, exist_ok=True)
    try:
        migrate()
    except Exception as e:
        log("migration failed, old files left in place:", repr(e))
    log(f"Spin Dashboard starting (data: {DATA}, polling every {POLL_INTERVAL}s, "
        f"keeping {RETENTION_DAYS} days, fatrace: {fatrace_version()})")
    relabel_existing()
    threading.excepthook = lambda a: log("thread error:", repr(a.exc_value))
    for target in (poller, watcher, housekeeping, docker_events):
        threading.Thread(target=target, daemon=True).start()
    web()


if __name__ == "__main__":
    main()
