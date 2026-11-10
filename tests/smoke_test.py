"""
Checks Anbled Screen the way the Anbled app uses it, without a real VLC (a stand-in records what it's asked):
refused addresses, playing, pausing, the window closed mid-song, and discovery. Runs on Linux and Windows.

    python -I tests/smoke_test.py
"""
import html
import os
import re
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="anbled-screen-test-")
for var in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "APPDATA", "LOCALAPPDATA"):
    os.environ[var] = TMP
sys.path.insert(0, os.path.dirname(HERE))
import anbled_screen as a  # noqa: E402

failures = []


def check(ok, what):
    print(("ok   " if ok else "FAIL ") + what, flush=True)
    if not ok:
        failures.append(what)
        if os.environ.get("GITHUB_ACTIONS"):
            print(f"::error::{what}", flush=True)


class FakeVlc:
    """Stands in for VLC: plays whatever it's given, 100 s long, one second per status call."""
    made = []

    def __init__(self, playlist, volume, fullscreen):
        self.playlists = [playlist]
        self.state, self.time, self.up = "playing", 0, True
        FakeVlc.made.append(self)

    def alive(self):
        return self.up

    def status(self, **command):
        if not self.up:
            return None
        c = command.get("command")
        if c == "pl_forcepause":
            self.state = "paused"
        elif c == "pl_forceresume":
            self.state = "playing"
        elif c == "pl_stop":
            self.state, self.time = "stopped", 0
        elif c == "seek":
            self.time = int(command["val"])
        elif self.state == "playing":
            self.time += 1
        return {"state": self.state, "time": self.time, "length": 100, "fullscreen": False, "volume": 256}

    def open(self, playlist):
        self.playlists.append(playlist)
        self.state, self.time = "playing", 0

    def close(self):
        self.up = False


a.Vlc = FakeVlc
a.fetch_cover = lambda url, base: None
a.Handler.player = a.Player()
a.Handler.ip = "127.0.0.1"
server = a.Server(("127.0.0.1", 0), a.Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
PORT = server.server_address[1]


def soap(action, inner=""):
    service = "RenderingControl" if "Volume" in action else "AVTransport"
    body = (f'<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
            f'<u:{action} xmlns:u="{a.SERVICES[service]}"><InstanceID>0</InstanceID>{inner}</u:{action}></s:Body></s:Envelope>')
    r = urllib.request.Request(f"http://127.0.0.1:{PORT}/{service}/control", body.encode(),
                               {"SOAPACTION": f'"{a.SERVICES[service]}#{action}"', "Content-Type": "text/xml"})
    try:
        with urllib.request.urlopen(r, timeout=10) as x:
            return x.status, x.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, ""


def value(xml, name):
    m = re.search(f"<{name}>(.*?)</{name}>", xml, re.S)
    return m.group(1) if m else None


def transport():
    return value(soap("GetTransportInfo")[1], "CurrentTransportState")


def cast(uri, title="Test song"):
    didl = (f'<DIDL-Lite><item><dc:title>{title}</dc:title><upnp:artist>Tester</upnp:artist>'
            f'<upnp:class>object.item.audioItem.musicTrack</upnp:class></item></DIDL-Lite>')
    soap("Stop")
    code, _ = soap("SetAVTransportURI", f"<CurrentURI>{html.escape(uri)}</CurrentURI>"
                                        f"<CurrentURIMetaData>{html.escape(didl)}</CurrentURIMetaData>")
    return code


# --- what may be played ---
for bad in ("--lua-config=x", "file:///etc/passwd", "file:///C:/Windows/win.ini", "http://10.9.9.9/song", "ftp://127.0.0.1/x"):
    check(cast(bad) == 500, f"refuses {bad}")
check(not FakeVlc.made, "nothing was played from a refused address")

# --- playing ---
song = f"http://127.0.0.1:{PORT + 1}/secret/song.m4a"
check(cast(song) == 200, "takes the casting phone's own address")
check(soap("Play")[0] == 200, "Play is accepted")
check(transport() in ("PLAYING", "TRANSITIONING"), "plays")
playlist = open(FakeVlc.made[-1].playlists[-1], encoding="utf-8").read()
check(html.escape(song) in playlist and "Test song" in playlist and "Tester" in playlist, "the window gets the address, title and artist")
check(value(soap("GetMediaInfo")[1], "CurrentURI") == html.escape(song), "the phone that cast it is told its address back")

# --- pause and resume ---
soap("Pause")
check(transport() == "PAUSED_PLAYBACK", "pauses")
soap("Play")
check(transport() == "PLAYING", "resumes")

# --- the window closed mid-song counts as a pause, Play opens it again where it was ---
vlc = FakeVlc.made[-1]
vlc.time = 40
a.Handler.player.refresh()
vlc.up = False
check(transport() == "PAUSED_PLAYBACK", "closing the window mid-song is a pause (the phone doesn't skip ahead)")
soap("Play")
check(len(FakeVlc.made) == 2 and transport() == "PLAYING", "Play opens the window again")
reopened = open(FakeVlc.made[-1].playlists[-1], encoding="utf-8").read()
check("start-time=4" in reopened, "...where it was")

# --- the next song plays in the same window ---
cast(f"http://127.0.0.1:{PORT + 1}/secret/next.m4a", "Next song")
soap("Play")
check(len(FakeVlc.made) == 2 and len(FakeVlc.made[-1].playlists) == 2, "the next song opens in the same window")

# --- requests from far away and oversized ones ---
check(not a.local("8.8.8.8") and a.local("192.168.1.5") and a.local("10.1.2.3"), "only the local network is listened to")
r = urllib.request.Request(f"http://127.0.0.1:{PORT}/AVTransport/control", b"x", {"Content-Length": str(10 ** 7)})
try:
    urllib.request.urlopen(r, timeout=5)
    big = 200
except urllib.error.HTTPError as e:
    big = e.code
except OSError:
    big = 413
check(big == 413, "an oversized request is refused")

# --- discovery: answers an SSDP search ---
try:
    ip = a.lan_ip()
except OSError:
    ip = None
if ip:
    stop = threading.Event()
    threading.Thread(target=a.ssdp, args=(ip, stop), daemon=True).start()
    time.sleep(1.5)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(3)
    s.sendto(b'M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: "ssdp:discover"\r\nMX: 1\r\n'
             b"ST: urn:schemas-upnp-org:device:MediaRenderer:1\r\n\r\n", (ip, 1900))
    try:
        reply = s.recv(4096).decode()
    except OSError:
        reply = ""
    check("LOCATION: http://" in reply and "/description.xml" in reply, f"answers a search for TVs on {ip}")
    stop.set()
else:
    print("skip discovery (no network)")

# --- Windows: start at sign-in ---
if a.WINDOWS:
    import winreg
    a.windows_command("--autostart")
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, a.RUN_KEY) as k:
        check("anbled_screen.py" in winreg.QueryValueEx(k, "Anbled Screen")[0], "start at sign-in is set")
    a.windows_command("--no-autostart")
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, a.RUN_KEY) as k:
        try:
            winreg.QueryValueEx(k, "Anbled Screen")
            gone = False
        except OSError:
            gone = True
    check(gone, "start at sign-in is removed")

server.shutdown()
print(f"\n{'All checks passed' if not failures else f'{len(failures)} check(s) failed'}")
sys.exit(1 if failures else 0)
