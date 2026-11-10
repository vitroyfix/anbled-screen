"""
A real cast to the installed Anbled Screen, with real VLC: a phone's part is played from this computer (a song
served over http, then Stop / SetAVTransportURI / Play / Pause like the Anbled app). Used on the Windows test
machine after windows/install.ps1; works on Linux too with the receiver running.

    python -I tests/cast_test.py [path to the installed anbled_screen.py]
"""
import html
import http.server
import math
import os
import re
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import wave

INSTALLED = sys.argv[1] if len(sys.argv) > 1 else None
failures = []


def check(ok, what):
    print(("ok   " if ok else "FAIL ") + what, flush=True)
    if not ok:
        failures.append(what)


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.connect(("1.1.1.1", 80))
    ip = s.getsockname()[0]
    s.close()
    return ip


IP = lan_ip()
CONTROL = f"http://{IP}:49494/AVTransport/control"


def soap(action, inner=""):
    body = (f'<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
            f'<u:{action} xmlns:u="urn:schemas-upnp-org:service:AVTransport:1"><InstanceID>0</InstanceID>{inner}'
            f"</u:{action}></s:Body></s:Envelope>")
    r = urllib.request.Request(CONTROL, body.encode(), {
        "SOAPACTION": f'"urn:schemas-upnp-org:service:AVTransport:1#{action}"', "Content-Type": "text/xml"})
    try:
        with urllib.request.urlopen(r, timeout=10) as x:
            return x.read().decode()
    except Exception as e:
        return f"error {e}"


def value(xml, name):
    m = re.search(f"<{name}>(.*?)</{name}>", xml, re.S)
    return m.group(1) if m else None


def state():
    return value(soap("GetTransportInfo"), "CurrentTransportState")


def seconds():
    t = value(soap("GetPositionInfo"), "RelTime") or "0:00:00"
    h, m, s = t.split(":")
    return int(h) * 3600 + int(m) * 60 + int(float(s))


def wait_for(states, limit):
    end = time.time() + limit
    while time.time() < end:
        if state() in states:
            return True
        time.sleep(0.5)
    return False


def manage(command):
    if INSTALLED:
        return subprocess.run([sys.executable, INSTALLED, command], capture_output=True, text=True, timeout=60).stdout.strip()
    return ""


# A 60 s tone, served like the phone serves a song.
media = tempfile.mkdtemp(prefix="anbled-cast-")
with wave.open(os.path.join(media, "song.wav"), "wb") as w:
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(22050)
    w.writeframes(b"".join(struct.pack("<h", int(2000 * math.sin(i * 440 * 2 * math.pi / 22050))) for i in range(22050 * 60)))
handler = lambda *a, **k: http.server.SimpleHTTPRequestHandler(*a, directory=media, **k)
http.server.SimpleHTTPRequestHandler.log_message = lambda *a: None
files = http.server.ThreadingHTTPServer((IP, 0), handler)
threading.Thread(target=files.serve_forever, daemon=True).start()
song = f"http://{IP}:{files.server_address[1]}/song.wav"

if INSTALLED:
    print(manage("--start"))
    check(manage("--status").startswith("on"), "--start turns it on")

# Discovery
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.settimeout(3)
s.sendto(b'M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: "ssdp:discover"\r\nMX: 1\r\n'
         b"ST: urn:schemas-upnp-org:device:MediaRenderer:1\r\n\r\n", (IP, 1900))
try:
    reply = s.recv(4096).decode()
except OSError:
    reply = ""
check(f"LOCATION: http://{IP}:49494/description.xml" in reply, "is found by a search for TVs")

# Casting
didl = ('<DIDL-Lite><item><dc:title>Cast test</dc:title><upnp:artist>Anbled</upnp:artist>'
        "<upnp:class>object.item.audioItem.musicTrack</upnp:class></item></DIDL-Lite>")
soap("Stop")
soap("SetAVTransportURI", f"<CurrentURI>{html.escape(song)}</CurrentURI><CurrentURIMetaData>{html.escape(didl)}</CurrentURIMetaData>")
soap("Play", "<Speed>1</Speed>")
check(wait_for({"PLAYING"}, 45), "VLC plays the cast song")
a = seconds()
time.sleep(4)
b = seconds()
check(b > a, f"it moves forward ({a}s -> {b}s)")
soap("Pause")
check(wait_for({"PAUSED_PLAYBACK"}, 10), "pauses")
soap("Play", "<Speed>1</Speed>")
check(wait_for({"PLAYING"}, 10), "plays again")
soap("Seek", "<Unit>REL_TIME</Unit><Target>0:00:30</Target>")
time.sleep(2)
check(seconds() >= 29, "seeks")
soap("Stop")
check(wait_for({"STOPPED"}, 10), "stops")

if INSTALLED:
    print(manage("--stop"))
    check(manage("--status") == "off", "--stop turns it off")

files.shutdown()
if failures:
    log = os.path.join(os.environ.get("APPDATA", ""), "Anbled Screen", "anbled-screen.log")
    if os.path.exists(log):
        text = open(log, encoding="utf-8", errors="replace").read()[-3000:]
        print(text)
        if os.environ.get("GITHUB_ACTIONS"):
            print("::error title=Receiver log::" + text.replace("%", "%25").replace("\r", "").replace("\n", "%0A"))
    if os.environ.get("GITHUB_ACTIONS"):
        print("::error::" + "; ".join(failures))
print(f"\n{'All checks passed' if not failures else f'{len(failures)} check(s) failed'}")
sys.exit(1 if failures else 0)
