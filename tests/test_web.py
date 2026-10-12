import gzip
import json
import os
import threading
import urllib.error
import urllib.request
from functools import partial

import pytest


@pytest.fixture
def server(sd, tmp_path):
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<html>ok</html>")
    (tmp_path / "secret.txt").write_text("nee")
    os.makedirs(sd.DAYS)
    open(sd.day_path("2026-10-09"), "w").write("1791496800,spin,sdd,active\n")
    open(sd.day_path("2026-10-08"), "w").write("x\n")
    open(os.path.join(sd.DAYS, "notes.txt"), "w").write("nee")
    srv = sd.http.server.ThreadingHTTPServer(("127.0.0.1", 0), partial(sd.Handler, directory=str(static)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def get(url):
    try:
        r = urllib.request.urlopen(url, timeout=5)
        return r.status, r.read().decode(), r.headers
    except urllib.error.HTTPError as e:
        return e.code, "", e.headers


def test_serves_dashboard_day_list_and_day_files(server):
    assert get(server + "/")[:2] == (200, "<html>ok</html>")
    status, body, headers = get(server + "/data/days.json")
    # bytes: totale grootte van de dagbestanden, voor de voortgangsbalk ("x\n" + 27 tekens)
    assert status == 200 and json.loads(body) == {"days": ["2026-10-08", "2026-10-09"], "bytes": 2 + 27,
                                                   "sizes": [2, 27]}
    assert headers["Cache-Control"] == "no-cache"
    status, body, headers = get(server + "/data/days/2026-10-09.csv")
    assert (status, body) == (200, "1791496800,spin,sdd,active\n")
    assert headers["Cache-Control"] == "no-cache"


def test_missing_day_is_404(server):
    assert get(server + "/data/days/2026-01-01.csv")[0] == 404


def test_only_day_files_from_data_dir(server):
    assert get(server + "/data/../secret.txt")[0] == 404
    assert get(server + "/data/%2e%2e/secret.txt")[0] == 404
    assert get(server + "/data/days/notes.txt")[0] == 404
    assert get(server + "/data/days/../../secret.txt")[0] == 404
    assert get(server + "/data/spin.csv")[0] == 404


def test_broadcast_fills_stream_queues(sd):
    import queue
    q1, q2 = queue.Queue(), queue.Queue()
    sd.streams.extend([q1, q2])
    try:
        sd.broadcast("1791496800,who,container,plex,1,geopend,/mnt/trunk/a.mkv")
    finally:
        sd.streams.clear()
    assert q1.get_nowait() == "1791496800,who,container,plex,1,geopend,/mnt/trunk/a.mkv"
    assert q2.get_nowait() == "1791496800,who,container,plex,1,geopend,/mnt/trunk/a.mkv"


def test_broadcast_skips_full_queue(sd):
    import queue
    full = queue.Queue(maxsize=1)
    full.put("x")
    sd.streams.append(full)
    try:
        sd.broadcast("1791496800,who,container,plex,1,geopend,/mnt/trunk/a.mkv")   # mag niet blokkeren
    finally:
        sd.streams.clear()
    assert full.get_nowait() == "x"     # alleen het oude item; de broadcast is geskipt


def test_stream_endpoint_delivers_broadcast(sd, server):
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", int(server.rsplit(":", 1)[1]), timeout=5)
    conn.request("GET", "/data/stream")
    resp = conn.getresponse()
    assert resp.status == 200
    assert resp.headers["Content-Type"] == "text/event-stream"
    line = resp.readline()              # ": ok" begroeting
    assert line.startswith(b":")
    assert len(sd.streams) == 1
    sd.broadcast("1791496800,who,container,plex,1,geopend,/mnt/trunk/a.mkv")
    body = resp.readline() + resp.readline()
    assert b"data: 1791496800,who,container,plex,1,geopend,/mnt/trunk/a.mkv" in body
    conn.close()


def fetch(url, **headers):
    req = urllib.request.Request(url, headers=headers)
    try:
        r = urllib.request.urlopen(req, timeout=5)
        return r.status, r.read(), r.headers
    except urllib.error.HTTPError as e:
        return e.code, b"", e.headers


def test_day_files_are_gzipped_when_accepted(server):
    status, body, headers = fetch(server + "/data/days/2026-10-09.csv", **{"Accept-Encoding": "gzip, deflate"})
    assert status == 200 and headers["Content-Encoding"] == "gzip"
    assert gzip.decompress(body) == b"1791496800,spin,sdd,active\n"
    assert headers["Vary"] == "Accept-Encoding" and headers["Content-Type"].startswith("text/csv")
    status, body, headers = fetch(server + "/data/days/2026-10-09.csv")          # zonder gzip
    assert (status, body, headers["Content-Encoding"]) == (200, b"1791496800,spin,sdd,active\n", None)


def test_unchanged_day_gives_304(server, sd):
    _, _, h = fetch(server + "/data/days/2026-10-09.csv", **{"Accept-Encoding": "gzip"})
    status, body, _ = fetch(server + "/data/days/2026-10-09.csv",
                            **{"Accept-Encoding": "gzip", "If-None-Match": h["ETag"]})
    assert (status, body) == (304, b"")
    with open(sd.day_path("2026-10-09"), "a") as f:                            # dag groeit: weer 200
        f.write("1791496900,spin,sdd,standby\n")
    status, body, _ = fetch(server + "/data/days/2026-10-09.csv",
                            **{"Accept-Encoding": "gzip", "If-None-Match": h["ETag"]})
    assert status == 200 and gzip.decompress(body).count(b"\n") == 2
