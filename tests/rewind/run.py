#!/usr/bin/env python3
"""End-to-end test of ?rewind=SECONDS: real server, real encoders, decoded audio.

The source plays a chirp whose frequency encodes time, so the position a listener
hears is measured from the decoded audio instead of being trusted from the server.
Needs icecast and ffmpeg. Usage: run.py <icecast.xml>

Run with a small kernel send buffer, or the stalled listener is absorbed by the socket:
docker run --sysctl net.ipv4.tcp_wmem="4096 16384 65536" ...
"""
import json, os, socket, struct, subprocess, sys, time, urllib.request

BASE = "http://127.0.0.1:8000"
RATE = 44100
F0, SLOPE = 200.0, 8.0  # frequency = F0 + SLOPE * t
failures = 0


def check(name, ok, detail=""):
    global failures
    failures += 0 if ok else 1
    print(("PASS " if ok else "FAIL ") + name + (" -- " + str(detail) if detail != "" else ""), flush=True)


def start_source(mount, args, content_type):
    expr = f"sin(2*PI*({F0}*t+{SLOPE / 2}*t*t))"
    return subprocess.Popen(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-re", "-f", "lavfi", "-i",
         f"aevalsrc={expr}:s={RATE}:c=stereo", *args, "-content_type", content_type,
         "-ice_name", "rewind-test", f"icecast://source:rewind-test@127.0.0.1:8000{mount}"])


def grab(path, seconds, limit=None):
    """Bytes a listener receives during `seconds`, and the response headers."""
    s = socket.create_connection(("127.0.0.1", 8000))
    s.sendall(f"GET {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n".encode())
    s.settimeout(0.25)
    data, end = b"", time.time() + seconds
    while time.time() < end and (limit is None or len(data) < limit):
        try:
            chunk = s.recv(65536)
        except socket.timeout:
            continue
        if not chunk:
            break
        data += chunk
    s.close()
    head, _, body = data.partition(b"\r\n\r\n")
    return head.decode("latin1"), body


def heard_time(body):
    """Stream time at the first second of audio, from zero crossings of the chirp."""
    pcm = subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "quiet", "-i", "pipe:0", "-f", "s16le", "-ac", "1", "-ar", str(RATE), "pipe:1"],
        input=body, stdout=subprocess.PIPE).stdout
    samples = struct.unpack(f"<{len(pcm) // 2}h", pcm[: len(pcm) // 2 * 2])
    window = samples[RATE // 4: RATE // 4 + RATE // 2]
    if len(window) < RATE // 2:
        return None
    crossings = sum(1 for a, b in zip(window, window[1:]) if (a < 0) != (b < 0))
    freq = crossings / (2 * 0.5)
    return (freq - F0) / SLOPE + 0.5


def stat(mount, key):
    with urllib.request.urlopen(BASE + "/status-json.xsl") as r:
        src = json.load(r)["icestats"].get("source", [])
    for s in src if isinstance(src, list) else [src]:
        if s.get("listenurl", "").endswith(mount):
            return s.get(key)
    return None


def admin_stat(mount, key):
    pw = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    pw.add_password(None, BASE, "admin", "rewind-test")
    opener = urllib.request.build_opener(urllib.request.HTTPBasicAuthHandler(pw))
    xml = opener.open(BASE + "/admin/stats.xml").read().decode()
    block = xml.split(f'mount="{mount}"')[1].split("</source>")[0]
    tag = f"<{key}>"
    return block.split(tag)[1].split("<")[0] if tag in block else None


def main():
    os.makedirs("/tmp/icecast-test", exist_ok=True)
    server = subprocess.Popen(["icecast", "-c", sys.argv[1]], stderr=subprocess.STDOUT, stdout=open("/tmp/icecast-test/stdout.log", "w"))
    time.sleep(2)
    sources = [
        start_source("/rewind.mp3", ["-c:a", "libmp3lame", "-b:a", "128k", "-f", "mp3"], "audio/mpeg"),
        start_source("/rewind.aac", ["-c:a", "aac", "-b:a", "128k", "-f", "adts"], "audio/aac"),
        start_source("/rewind.flac", ["-c:a", "flac", "-f", "ogg"], "audio/ogg"),
        start_source("/plain.mp3", ["-c:a", "libmp3lame", "-b:a", "128k", "-f", "mp3"], "audio/mpeg"),
    ]
    print("filling the window for 60 s", flush=True)
    time.sleep(60)

    for mount in ("/rewind.mp3", "/rewind.aac", "/rewind.flac"):
        head, live = grab(mount, 4)
        live_t = heard_time(live)
        head_r, past = grab(mount + "?rewind=30", 4)
        past_t = heard_time(past)
        check(f"{mount} header announces the window", "X-Icecast-Rewind-Available:" in head, [l for l in head.splitlines() if "Rewind" in l])
        delta = None if live_t is None or past_t is None else live_t - past_t
        # the second grab starts about 4 s later, which is added to the expected distance
        check(f"{mount} rewind=30 is heard 30 s behind live", delta is not None and 28 <= delta + 4 <= 38, f"live {live_t} past {past_t} delta+4s {None if delta is None else round(delta + 4, 1)}")

    _, body = grab("/rewind.mp3?rewind=30", 2)
    check("backlog is paced, not sent at once", 16000 < len(body) < 130000, f"{len(body)} of ~480000 bytes in 2 s")

    for bad in ("abc", "-5", "0", "99999999", ""):
        head, body = grab(f"/rewind.mp3?rewind={bad}", 2)
        check(f"bad rewind={bad!r} still streams", head.startswith("HTTP/1.1 200") and len(body) > 16000, len(body))
    head, body = grab("/rewind.mp3?rewind=86400", 2)
    check("rewind beyond the window starts at its oldest data", head.startswith("HTTP/1.1 200") and len(body) > 16000, len(body))
    head, body = grab("/plain.mp3?rewind=30", 2)
    check("mount without a window stays live", "Rewind" not in head and head.startswith("HTTP/1.1 200") and len(body) < 90000, len(body))

    before = int(admin_stat("/rewind.mp3", "slow_listeners") or 0)
    head, body = grab("/rewind.mp3?rewind=40", 25)
    after = int(admin_stat("/rewind.mp3", "slow_listeners") or 0)
    check("listener in the past is not dropped as slow", after == before and len(body) > 25 * 16000 * 0.9, f"slow {before}->{after}, {len(body)} bytes")
    avail = admin_stat("/rewind.mp3", "rewind_available")
    check("window is exposed in stats and stays bounded", avail is not None and 30 <= int(avail) <= 60, avail)

    stalled = socket.socket()
    stalled.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    stalled.connect(("127.0.0.1", 8000))
    stalled.sendall(b"GET /rewind.mp3?rewind=45 HTTP/1.1\r\nHost: localhost\r\n\r\n")
    before = int(admin_stat("/rewind.mp3", "slow_listeners") or 0)
    deadline, after = time.time() + 75, before
    while time.time() < deadline and after == before:
        time.sleep(3)
        after = int(admin_stat("/rewind.mp3", "slow_listeners") or 0)
    stalled.close()
    check("stalled listener is dropped, memory stays bounded", after > before, f"slow {before}->{after}")

    resident = int(subprocess.check_output(["ps", "-o", "rss=", "-p", str(server.pid)]).strip())
    print(f"INFO server RSS {resident // 1024} MiB with ASAN", flush=True)

    victim = socket.create_connection(("127.0.0.1", 8000))
    victim.sendall(b"GET /rewind.mp3?rewind=30 HTTP/1.1\r\nHost: localhost\r\n\r\n")
    time.sleep(1)
    sources[0].terminate()
    sources[0].wait()
    time.sleep(3)
    victim.close()
    sources[0] = start_source("/rewind.mp3", ["-c:a", "libmp3lame", "-b:a", "128k", "-f", "mp3"], "audio/mpeg")
    time.sleep(6)
    head, body = grab("/rewind.mp3?rewind=30", 2)
    check("source reconnect: window restarts, listeners still served", head.startswith("HTTP/1.1 200") and len(body) > 8000, len(body))

    check("server survived", server.poll() is None)
    for s in sources:
        s.terminate()
    server.terminate()
    server.wait(timeout=20)
    log = open("/tmp/icecast-test/stdout.log", errors="replace").read()
    check("no memory errors from AddressSanitizer", "AddressSanitizer" not in log, log[:1500] if "AddressSanitizer" in log else "")
    print("failures:", failures, flush=True)
    sys.exit(1 if failures else 0)


main()
