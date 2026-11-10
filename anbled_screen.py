#!/usr/bin/env python3
"""
Anbled Screen: a cast receiver. Makes a Linux computer a screen the Anbled app can cast songs and videos to.

To the app it is a smart TV: a UPnP / DLNA media renderer, found on the Wi-Fi with an SSDP search and told what to
play over AVTransport (the app's cast/Dlna.kt). What it's told to play goes to VLC (the Flatpak org.videolan.VLC, or
a vlc on the PATH) in a normal player window, with the song's picture, title and artist: it can be resized, put full
screen, paused, seeked or closed from the computer as well as from the phone. Nothing on this computer is shared: it
only plays what a phone on the same Wi-Fi sends.

    python3 anbled_screen.py            run in the foreground (Ctrl+C stops it)
    ANBLED_SCREEN_NAME="Living room"    the name phones show (default "Anbled screen (Linux)")

Runs on Linux and Windows. On Windows, the Start menu shortcuts use:
    pythonw anbled_screen.py --start | --stop | --status | --autostart | --no-autostart

Standard library only. Phones find it on UDP 1900 (SSDP) and control it on TCP 49494: a firewall must let both in.
"""
import base64
import html
import ipaddress
import json
import os
import platform
import pathlib
import re
import secrets
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

WINDOWS = os.name == "nt"
VERSION = "1.1.1"
NAME = os.environ.get("ANBLED_SCREEN_NAME", "Anbled screen (Windows)" if WINDOWS else "Anbled screen (Linux)")
HTTP_PORT = int(os.environ.get("ANBLED_SCREEN_PORT", "49494"))
VLC_PORT = 49495
DEBUG_LOG = os.environ.get("ANBLED_SCREEN_LOG", "")  # a file: VLC's own messages go there (for finding problems)
SSDP_ADDR, SSDP_PORT = "239.255.255.250", 1900
if WINDOWS:
    CACHE_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "Anbled Screen", "cache")
else:
    CACHE_DIR = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "anbled-screen")
# Where song pictures may come from (Anbled's covers are on Google's image servers).
COVER_HOSTS = ("googleusercontent.com", "ggpht.com", "ytimg.com", "anbled.com")
if WINDOWS:
    STATE_DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "Anbled Screen")
else:
    STATE_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "anbled-screen")
PID_FILE = os.path.join(STATE_DIR, "running.pid")
LOG_FILE = os.path.join(STATE_DIR, "anbled-screen.log")  # (Windows, started without a console: where log() writes)

DEVICE = "urn:schemas-upnp-org:device:MediaRenderer:1"
SERVICES = {
    "AVTransport": "urn:schemas-upnp-org:service:AVTransport:1",
    "RenderingControl": "urn:schemas-upnp-org:service:RenderingControl:1",
    "ConnectionManager": "urn:schemas-upnp-org:service:ConnectionManager:1",
}


def log(msg):
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    if sys.stdout is not None:
        print(line, flush=True)
        return
    try:  # no console (pythonw on Windows): a small log file instead
        os.makedirs(STATE_DIR, exist_ok=True)
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 1_000_000:
            os.replace(LOG_FILE, LOG_FILE + ".old")
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d ") + line + "\n")
    except OSError:
        pass


def lan_ip():
    """The address this computer has on the Wi-Fi (the one its default route leaves from)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("1.1.1.1", 80))
        return s.getsockname()[0]
    finally:
        s.close()


def device_uuid():
    """The same id every time, so phones recognise this screen across restarts."""
    os.makedirs(STATE_DIR, exist_ok=True)
    path = os.path.join(STATE_DIR, "uuid")
    try:
        with open(path) as f:
            value = f.read().strip()
            if value:
                return value
    except OSError:
        pass
    value = str(uuid.uuid4())
    with open(path, "w") as f:
        f.write(value)
    return value


UDN = "uuid:" + device_uuid()


# ---------- VLC ----------
def find_vlc():
    """How to start VLC here, or None. ANBLED_SCREEN_VLC can name the program."""
    named = os.environ.get("ANBLED_SCREEN_VLC")
    if named and os.path.isfile(named):
        return [named]
    if WINDOWS:
        import winreg
        for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for key in (r"SOFTWARE\VideoLAN\VLC", r"SOFTWARE\WOW6432Node\VideoLAN\VLC"):
                try:
                    with winreg.OpenKey(root, key) as k:
                        for name in ("", "InstallDir"):
                            try:
                                value = winreg.QueryValueEx(k, name)[0]
                            except OSError:
                                continue
                            exe = value if value.lower().endswith(".exe") else os.path.join(value, "vlc.exe")
                            if os.path.isfile(exe):
                                return [exe]
                except OSError:
                    pass
        for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"), os.environ.get("LOCALAPPDATA")):
            exe = os.path.join(base or "", "VideoLAN", "VLC", "vlc.exe")
            if base and os.path.isfile(exe):
                return [exe]
    elif shutil.which("flatpak") and subprocess.run(["flatpak", "info", "org.videolan.VLC"], capture_output=True).returncode == 0:
        return ["flatpak", "run", "org.videolan.VLC"]
    found = shutil.which("vlc")
    return [found] if found else None


def vlc_command():
    command = find_vlc()
    if command:
        return command
    log("VLC isn't installed")
    sys.exit("VLC isn't installed: " + ("winget install VideoLAN.VLC" if WINDOWS else "flatpak install flathub org.videolan.VLC"))


class Vlc:
    """
    VLC with its own window and controls: resize it, close it, go full screen, pause or seek from the computer as
    well as from the phone. It stays open from song to song (each one is handed over through its local web interface
    on 127.0.0.1, with a password made new each time it starts).
    """

    def __init__(self, playlist, volume, fullscreen):
        self.password = secrets.token_urlsafe(16)
        auth = base64.b64encode(f":{self.password}".encode()).decode()
        self.headers = {"Authorization": f"Basic {auth}"}
        self.volume = volume
        self.proc = subprocess.Popen(
            vlc_command() + ["--intf", "qt", "--extraintf", "http", "--http-host", "127.0.0.1", "--http-port", str(VLC_PORT),
                             "--http-password", self.password, "--no-one-instance", "--no-qt-privacy-ask",
                             "--qt-continue", "0", "--no-qt-recentplay", "--no-qt-video-autoresize", "--qt-auto-raise", "3",
                             "--no-loop", "--no-repeat", "--no-random", "--no-play-and-exit", "--no-play-and-stop",
                             "--fullscreen" if fullscreen else "--no-fullscreen", "--network-caching", "1500"]
            + (["-vv"] if DEBUG_LOG else []) + ["--", playlist],  # a file this program wrote, never a network address
            stdin=subprocess.DEVNULL, stdout=open(DEBUG_LOG, "ab") if DEBUG_LOG else subprocess.DEVNULL,
            stderr=subprocess.STDOUT if DEBUG_LOG else subprocess.DEVNULL,
            **({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if WINDOWS else {"start_new_session": True}))
        self.volume_set = False

    def alive(self):
        return self.proc.poll() is None

    def status(self, **command):
        if not self.alive():
            return None
        query = urllib.parse.urlencode(command, quote_via=urllib.parse.quote)
        url = f"http://127.0.0.1:{VLC_PORT}/requests/status.json" + (f"?{query}" if query else "")
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=self.headers), timeout=3) as r:
                st = json.loads(r.read().decode("utf-8", "replace"))
        except Exception:
            return None
        if not self.volume_set and not command and st.get("state") == "playing":
            self.volume_set = True
            self.status(command="volume", val=str(round(self.volume * 2.56)))
        return st

    def open(self, playlist):
        """The next song, in the same window."""
        self.status(command="pl_empty")
        self.status(command="in_play", input=pathlib.Path(playlist).as_uri())

    def close(self):
        if self.alive() and WINDOWS:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except Exception:
                pass
        elif self.alive():
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
                self.proc.wait(timeout=3)
            except Exception:
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except Exception:
                    pass


# ---------- the song's picture ----------
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_fetch = urllib.request.build_opener(_NoRedirect)


def fetch_cover(url, dest_base):
    """The song's picture, saved next to its playlist: only https from the places Anbled's pictures come from, an
    image, at most 5 MB, no redirects. None when there isn't one."""
    try:
        u = urllib.parse.urlsplit(url)
    except ValueError:
        return None
    host = (u.hostname or "").lower()
    if u.scheme != "https" or not any(host == h or host.endswith("." + h) for h in COVER_HOSTS):
        return None
    try:
        with _fetch.open(urllib.request.Request(url, headers={"User-Agent": "AnbledScreen/1"}), timeout=4) as r:
            kind = r.headers.get_content_type()
            if not kind.startswith("image/"):
                return None
            data = r.read(5_000_001)
    except Exception:
        return None
    if len(data) > 5_000_000:
        return None
    path = dest_base + {"image/png": ".png", "image/webp": ".webp"}.get(kind, ".jpg")
    with open(path, "wb") as f:
        f.write(data)
    return path


def write_playlist(path, uri, title, artist, cover, start):
    """A one-song playlist: how VLC is given the song's title, artist and picture along with its address."""
    esc = html.escape
    image = f"<image>{esc(pathlib.Path(cover).as_uri())}</image>" if cover else ""
    option = f"<vlc:option>start-time={start:.1f}</vlc:option>" if start else ""
    with open(path, "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<playlist version="1" xmlns="http://xspf.org/ns/0/" '
                'xmlns:vlc="http://www.videolan.org/vlc/playlist/ns/0/"><title>Anbled</title><trackList><track>'
                f"<location>{esc(uri)}</location><title>{esc(title or 'Anbled')}</title><creator>{esc(artist)}</creator>"
                f'{image}<extension application="http://www.videolan.org/vlc/playlist/0"><vlc:id>0</vlc:id>{option}'
                "</extension></track></trackList></playlist>\n")


# ---------- what's playing ----------
class Player:
    """
    What the phone cast, and what the window on this computer is doing with it. Closing the window, or pressing Stop
    on the computer, in the middle of a song counts as a pause (the phone would otherwise take it for the end of the
    song and send the next one); Play on the phone opens it again where it was.
    """

    def __init__(self):
        self.lock = threading.RLock()
        self.vlc = None            # the window (None: not opened yet, or closed)
        self.uri = ""
        self.owner = ""            # the phone that said what to play: only it is told the address back
        self.meta = ""
        self.title = self.artist = ""
        self.video = False
        self.cover = None          # the song's picture: (event set once fetched, [path or None])
        self.loaded = ""           # the address the window has open
        self.starting_until = 0.0  # just asked to play: TRANSITIONING until VLC answers
        self.volume = 100
        self.last = {}             # VLC's latest answer (state, time, length, fullscreen, volume)
        self.resume_at = None      # paused by closing the window / Stop on the computer: where to carry on from
        self.phone_stopped = False
        self.fullscreen = False    # the last video's full screen choice, kept for the next video
        self.count = 0
        os.makedirs(CACHE_DIR, exist_ok=True)
        threading.Thread(target=self._watch, daemon=True).start()

    # -- following the window --
    def _watch(self):
        while True:
            time.sleep(0.5)
            try:
                self.refresh()
            except Exception as e:
                log(f"watching the window: {e}")

    def refresh(self):
        """Asks VLC what it's doing, and notices the window closed or stopped from this computer."""
        with self.lock:
            vlc = self.vlc
            if vlc is None:
                return
            st = vlc.status() if vlc.alive() else None
            before = self.last
            mid_song = (before.get("state") in ("playing", "paused") and (before.get("length") or 0) > 0
                        and (before.get("time") or 0) < before["length"] - 3)
            if not vlc.alive():
                self.vlc = None
                self.loaded = ""
                if mid_song and not self.phone_stopped:
                    self.resume_at = before.get("time") or 0
                    log("window closed: paused")
                self.last = {}
                return
            if st is None:
                return
            if st.get("state") == "stopped" and mid_song and not self.phone_stopped and self.loaded == self.uri:
                self.resume_at = before.get("time") or 0
                log("stopped on this computer: paused")
            if self.video and st.get("state") == "playing":
                self.fullscreen = bool(st.get("fullscreen"))
            self.last = st

    # -- what the phone asks --
    def set_uri(self, uri, meta, owner):
        text = html.unescape(meta or "")

        def field(name):
            m = re.search(rf"<{name}(?:\s[^>]*)?>(.*?)</{name}>", text, re.S)
            return html.unescape(m.group(1)).strip() if m else ""

        with self.lock:
            self.uri, self.meta, self.owner = uri, meta, owner
            self.title, self.artist = field("dc:title"), field("upnp:artist")
            kind = field("upnp:class")
            self.video = "videoItem" in kind if kind else urllib.parse.urlsplit(uri).path.endswith(".mp4")
            self.resume_at = None
            self.count += 1
            base = os.path.join(CACHE_DIR, f"now-{self.count}")
            for old in os.listdir(CACHE_DIR):  # earlier songs' playlists and pictures
                if not old.startswith(f"now-{self.count}-"):
                    try:
                        os.remove(os.path.join(CACHE_DIR, old))
                    except OSError:
                        pass
            done, box = threading.Event(), [None]
            self.cover = (done, box)
            art = field("upnp:albumArtURI")

            def get():
                try:
                    box[0] = fetch_cover(art, base + "-cover") if art else None
                finally:
                    done.set()
            threading.Thread(target=get, daemon=True).start()
        log(f"next: {self.title or self.uri}")

    def play(self):
        cover = self.cover
        if cover:
            cover[0].wait(2)  # the picture, if it comes quickly; the song doesn't wait longer for it
        with self.lock:
            if not self.uri:
                return False
            self.phone_stopped = False
            alive = self.vlc is not None and self.vlc.alive()
            state = self.last.get("state")
            if alive and self.loaded == self.uri and self.resume_at is None:
                if state == "paused":
                    self.vlc.status(command="pl_forceresume")
                    return True
                if state == "playing":
                    return True
            start = self.resume_at or 0
            path = os.path.join(CACHE_DIR, f"now-{self.count}-{int(time.time() * 1000)}.xspf")
            write_playlist(path, self.uri, self.title, self.artist, cover[1][0] if cover else None, start)
            if alive:
                self.vlc.open(path)
            else:
                self.vlc = Vlc(path, self.volume, self.video and self.fullscreen)
            self.loaded, self.resume_at, self.last = self.uri, None, {}
            self.starting_until = time.time() + 20
            log(f"playing: {self.title or self.uri}" + (f" from {hms(start)}" if start else ""))
            return True

    def pause(self):
        with self.lock:
            if self.vlc and self.loaded == self.uri:
                self.vlc.status(command="pl_forcepause")

    def stop(self):
        with self.lock:
            self.phone_stopped = True
            self.resume_at = None
            self.starting_until = 0
            if self.vlc:
                self.vlc.status(command="pl_stop")  # the window stays open

    def seek(self, seconds):
        with self.lock:
            if self.vlc and self.loaded == self.uri and self.resume_at is None:
                self.vlc.status(command="seek", val=str(int(seconds)))
            elif self.resume_at is not None:
                self.resume_at = max(0, seconds)

    def set_volume(self, v):
        self.volume = max(0, min(100, v))
        if self.vlc:
            self.vlc.volume = self.volume
            self.vlc.status(command="volume", val=str(round(self.volume * 2.56)))

    def current_volume(self):
        v = self.last.get("volume")
        return max(0, min(100, round(v / 2.56))) if isinstance(v, (int, float)) else self.volume

    def state(self):
        self.refresh()
        with self.lock:
            st = dict(self.last)
            if self.resume_at is not None:
                return "PAUSED_PLAYBACK", {"time": self.resume_at, "length": st.get("length") or 0}
            alive = self.vlc is not None and self.vlc.alive() and self.loaded == self.uri
            vlc_state = st.get("state") if alive else None
            if vlc_state == "playing":
                self.starting_until = 0
                return "PLAYING", st
            if vlc_state == "paused":
                return "PAUSED_PLAYBACK", st
            if alive and time.time() < self.starting_until:
                return "TRANSITIONING", st  # VLC is still starting or opening the song
            return ("STOPPED" if self.uri else "NO_MEDIA_PRESENT"), st

    def close(self):
        with self.lock:
            if self.vlc:
                self.vlc.close()
                self.vlc = None


def hms(seconds):
    s = max(0, int(seconds or 0))
    return f"{s // 3600}:{(s // 60) % 60:02d}:{s % 60:02d}"


def seconds_of(text):
    parts = (text or "").split(":")
    if len(parts) != 3:
        return 0
    try:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    except ValueError:
        return 0


# ---------- UPnP descriptions ----------
def description(ip):
    services = "".join(
        f"<service><serviceType>{t}</serviceType><serviceId>urn:upnp-org:serviceId:{n}</serviceId>"
        f"<SCPDURL>/{n}.xml</SCPDURL><controlURL>/{n}/control</controlURL><eventSubURL>/{n}/event</eventSubURL></service>"
        for n, t in SERVICES.items())
    return (f'<?xml version="1.0" encoding="utf-8"?><root xmlns="urn:schemas-upnp-org:device-1-0">'
            f"<specVersion><major>1</major><minor>0</minor></specVersion><URLBase>http://{ip}:{HTTP_PORT}/</URLBase>"
            f"<device><deviceType>{DEVICE}</deviceType><friendlyName>{html.escape(NAME)}</friendlyName>"
            f"<manufacturer>Anbled</manufacturer><modelName>Anbled Screen</modelName><modelNumber>1</modelNumber>"
            f"<UDN>{UDN}</UDN><serviceList>{services}</serviceList></device></root>")


ACTIONS = {
    "AVTransport": ["SetAVTransportURI", "Play", "Pause", "Stop", "Seek", "GetTransportInfo", "GetPositionInfo",
                    "GetMediaInfo", "GetTransportSettings", "GetDeviceCapabilities", "GetCurrentTransportActions"],
    "RenderingControl": ["GetVolume", "SetVolume", "GetMute", "SetMute"],
    "ConnectionManager": ["GetProtocolInfo", "GetCurrentConnectionIDs", "GetCurrentConnectionInfo"],
}


def scpd(service):
    actions = "".join(f"<action><name>{a}</name></action>" for a in ACTIONS[service])
    return (f'<?xml version="1.0" encoding="utf-8"?><scpd xmlns="urn:schemas-upnp-org:service-1-0">'
            f"<specVersion><major>1</major><minor>0</minor></specVersion><actionList>{actions}</actionList>"
            f"<serviceStateTable/></scpd>")


def soap_reply(service, action, values):
    body = "".join(f"<{k}>{html.escape(str(v))}</{k}>" for k, v in values.items())
    return (f'<?xml version="1.0" encoding="utf-8"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            f's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
            f'<u:{action}Response xmlns:u="{SERVICES[service]}">{body}</u:{action}Response></s:Body></s:Envelope>')


def soap_fault(code, text):
    return (f'<?xml version="1.0" encoding="utf-8"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            f's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body><s:Fault><faultcode>s:Client</faultcode>'
            f'<faultstring>UPnPError</faultstring><detail><UPnPError xmlns="urn:schemas-upnp-org:control-1-0">'
            f"<errorCode>{code}</errorCode><errorDescription>{html.escape(text)}</errorDescription></UPnPError></detail>"
            f"</s:Fault></s:Body></s:Envelope>")


def local(addr):
    """Whether an address is on this home network (or this computer): nothing from further away is listened to."""
    try:
        a = ipaddress.ip_address(addr)
    except ValueError:
        return False
    if a.version == 6 and a.ipv4_mapped:
        a = a.ipv4_mapped
    return a.is_private or a.is_link_local or a.is_loopback


def playable(uri, sender):
    """Only what the phone asking serves itself, over http(s): never a file here, another computer, or (with a
    leading "-") something VLC would take for one of its own options."""
    try:
        u = urllib.parse.urlsplit(uri)
    except ValueError:
        return False
    if u.scheme not in ("http", "https") or not u.hostname or any(c in uri for c in "\r\n\0 "):
        return False
    try:
        return ipaddress.ip_address(u.hostname) == ipaddress.ip_address(sender)
    except ValueError:
        return False


def arg(body, name):
    m = re.search(rf"<(?:\w+:)?{name}(?:\s[^>]*)?>(.*?)</(?:\w+:)?{name}>", body, re.S)
    return html.unescape(m.group(1).strip()) if m else ""


class Server(ThreadingHTTPServer):
    # On Windows, "reuse" would let a second copy take the same port: there it's exclusive instead.
    allow_reuse_address = not WINDOWS

    def server_bind(self):
        if WINDOWS and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


# ---------- the web side: descriptions and control ----------
class Handler(BaseHTTPRequestHandler):
    server_version = "AnbledScreen/1"
    protocol_version = "HTTP/1.1"
    player = None
    ip = ""

    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="text/xml; charset=\"utf-8\"", extra=None):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/description.xml":
            return self.send(200, description(self.ip))
        name = path.strip("/").removesuffix(".xml")
        if name in SERVICES:
            return self.send(200, scpd(name))
        self.send(404, "")

    def do_SUBSCRIBE(self):
        # Events aren't sent (the app asks for the state itself); subscriptions are accepted so controllers carry on.
        self.send(200, "", extra={"SID": f"uuid:{uuid.uuid4()}", "TIMEOUT": "Second-1800"})

    def do_UNSUBSCRIBE(self):
        self.send(200, "")

    def do_POST(self):
        if not local(self.client_address[0]):
            return self.send(403, "")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self.send(400, "")
        if length < 0 or length > 256 * 1024:  # a phone's command is a few KB
            return self.send(413, "")
        body = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        service = self.path.strip("/").split("/")[0]
        action = (self.headers.get("SOAPACTION") or "").strip('"').rsplit("#", 1)[-1]
        if service not in SERVICES or action not in ACTIONS[service]:
            return self.send(500, soap_fault(401, "Invalid Action"))
        try:
            values = getattr(self, f"{service}_{action}")(body)
        except Exception as e:  # never let one odd request take the screen down
            log(f"{action} failed: {e}")
            return self.send(500, soap_fault(501, "Action Failed"))
        if values is None:
            return self.send(500, soap_fault(701, "Transition not available"))
        self.send(200, soap_reply(service, action, values))

    # AVTransport
    def AVTransport_SetAVTransportURI(self, body):
        uri, sender = arg(body, "CurrentURI"), self.client_address[0]
        if not playable(uri, sender):
            try:
                pointed = urllib.parse.urlsplit(uri).hostname or uri[:40]
            except ValueError:
                pointed = uri[:40]
            log(f"refused an address from {sender}: it pointed to {pointed}, not to the phone itself"
                " (a hotspot, Wi-Fi Direct or VPN on the phone can cause this)")
            return None
        self.player.set_uri(uri, arg(body, "CurrentURIMetaData"), sender)
        return {}

    def AVTransport_Play(self, body):
        return {} if self.player.play() else None

    def AVTransport_Pause(self, body):
        self.player.pause()
        return {}

    def AVTransport_Stop(self, body):
        self.player.stop()
        return {}

    def AVTransport_Seek(self, body):
        if arg(body, "Unit") in ("REL_TIME", "ABS_TIME"):
            self.player.seek(seconds_of(arg(body, "Target")))
        return {}

    def AVTransport_GetTransportInfo(self, body):
        state, _ = self.player.state()
        return {"CurrentTransportState": state, "CurrentTransportStatus": "OK", "CurrentSpeed": "1"}

    def AVTransport_GetPositionInfo(self, body):
        _, st = self.player.state()
        mine = self.client_address[0] == self.player.owner
        return {"Track": 1, "TrackDuration": hms(st.get("length")), "TrackMetaData": self.player.meta if mine else "",
                "TrackURI": self.player.uri if mine else "", "RelTime": hms(st.get("time")), "AbsTime": hms(st.get("time")),
                "RelCount": 2147483647, "AbsCount": 2147483647}

    def AVTransport_GetMediaInfo(self, body):
        _, st = self.player.state()
        mine = self.client_address[0] == self.player.owner  # the cast link is the phone's secret
        return {"NrTracks": 1 if self.player.uri else 0, "MediaDuration": hms(st.get("length")),
                "CurrentURI": self.player.uri if mine else "", "CurrentURIMetaData": self.player.meta if mine else "", "NextURI": "",
                "NextURIMetaData": "", "PlayMedium": "NETWORK", "RecordMedium": "NOT_IMPLEMENTED",
                "WriteStatus": "NOT_IMPLEMENTED"}

    def AVTransport_GetTransportSettings(self, body):
        return {"PlayMode": "NORMAL", "RecQualityMode": "NOT_IMPLEMENTED"}

    def AVTransport_GetDeviceCapabilities(self, body):
        return {"PlayMedia": "NETWORK", "RecMedia": "NOT_IMPLEMENTED", "RecQualityModes": "NOT_IMPLEMENTED"}

    def AVTransport_GetCurrentTransportActions(self, body):
        return {"Actions": "Play,Pause,Stop,Seek"}

    # RenderingControl
    def RenderingControl_GetVolume(self, body):
        return {"CurrentVolume": self.player.current_volume()}

    def RenderingControl_SetVolume(self, body):
        self.player.set_volume(int(arg(body, "DesiredVolume") or 100))
        return {}

    def RenderingControl_GetMute(self, body):
        return {"CurrentMute": 1 if self.player.current_volume() == 0 else 0}

    def RenderingControl_SetMute(self, body):
        if arg(body, "DesiredMute") in ("1", "true"):
            if self.player.vlc:
                self.player.vlc.status(command="volume", val="0")
        else:
            self.player.set_volume(self.player.volume or 100)
        return {}

    # ConnectionManager
    def ConnectionManager_GetProtocolInfo(self, body):
        return {"Source": "", "Sink": "http-get:*:audio/mp4:*,http-get:*:audio/mpeg:*,http-get:*:audio/webm:*,"
                                      "http-get:*:video/mp4:*,http-get:*:video/webm:*,http-get:*:*:*"}

    def ConnectionManager_GetCurrentConnectionIDs(self, body):
        return {"ConnectionIDs": "0"}

    def ConnectionManager_GetCurrentConnectionInfo(self, body):
        return {"RcsID": 0, "AVTransportID": 0, "ProtocolInfo": "", "PeerConnectionManager": "", "PeerConnectionID": -1,
                "Direction": "Input", "Status": "OK"}


# ---------- being found on the Wi-Fi (SSDP) ----------
def targets():
    """Everything this screen answers to: the device, its services, itself."""
    return ["upnp:rootdevice", UDN, DEVICE, *SERVICES.values()]


def usn(target):
    return UDN if target == UDN else f"{UDN}::{target}"


def ssdp(ip, stop):
    location = f"http://{ip}:{HTTP_PORT}/description.xml"
    server = f"{platform.system() or 'Linux'} UPnP/1.0 AnbledScreen/1"
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    if WINDOWS and hasattr(socket, "SIO_UDP_CONNRESET"):
        # Windows otherwise fails the next receive after an answer to a phone that's gone, ending discovery.
        sock.ioctl(socket.SIO_UDP_CONNRESET, False)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    sock.bind(("", SSDP_PORT))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, struct.pack("4s4s", socket.inet_aton(SSDP_ADDR), socket.inet_aton(ip)))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.settimeout(1.0)

    def notify(kind):
        for t in targets():
            msg = (f"NOTIFY * HTTP/1.1\r\nHOST: {SSDP_ADDR}:{SSDP_PORT}\r\nNT: {t}\r\nNTS: ssdp:{kind}\r\nUSN: {usn(t)}\r\n"
                   + (f"CACHE-CONTROL: max-age=1800\r\nLOCATION: {location}\r\nSERVER: {server}\r\n" if kind == "alive" else "")
                   + "\r\n")
            try:
                sock.sendto(msg.encode(), (SSDP_ADDR, SSDP_PORT))
            except OSError:
                pass

    notify("alive")
    next_alive = time.time() + 60
    while not stop.is_set():
        if time.time() >= next_alive:
            notify("alive")
            next_alive = time.time() + 60
        try:
            data, addr = sock.recvfrom(4096)
        except (socket.timeout, ConnectionResetError):
            continue
        except OSError:
            if stop.is_set():
                break
            time.sleep(1)  # the network changed for a moment: keep answering
            continue
        text = data.decode("utf-8", "replace")
        if not text.startswith("M-SEARCH"):
            continue
        st = re.search(r"(?im)^ST:\s*(.+?)\s*$", text)
        st = st.group(1) if st else ""
        if st == "ssdp:all":
            answers = targets()
        elif st in targets() or st.startswith("urn:schemas-upnp-org:device:MediaRenderer:"):
            answers = [st]
        else:
            continue
        for t in answers:
            reply = (f"HTTP/1.1 200 OK\r\nCACHE-CONTROL: max-age=1800\r\nEXT:\r\nLOCATION: {location}\r\nSERVER: {server}\r\n"
                     f"ST: {t}\r\nUSN: {usn(t)}\r\nDATE: {time.strftime('%a, %d %b %Y %H:%M:%S GMT', time.gmtime())}\r\n\r\n")
            try:
                sock.sendto(reply.encode(), addr)
            except OSError:
                pass
    notify("byebye")
    sock.close()


# ---------- Windows: on, off and start at sign-in (Linux uses the anbled-screen script and systemd) ----------
def _running_pid():
    """The process id of an Anbled Screen already serving here, or None."""
    try:
        with open(PID_FILE) as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return None
    try:
        with urllib.request.urlopen(f"http://{lan_ip()}:{HTTP_PORT}/description.xml", timeout=2) as r:
            if UDN.encode() in r.read():
                return pid
    except Exception:
        pass
    return None


def _message(text):
    """Printed in a terminal; a small window when started from a Start menu shortcut (no terminal)."""
    if sys.stdout is not None:
        print(text)
        return
    if WINDOWS:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, "Anbled Screen", 0x40)


def _pythonw():
    exe = sys.executable
    quiet = os.path.join(os.path.dirname(exe), "pythonw.exe")
    return quiet if os.path.isfile(quiet) else exe


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def windows_command(command):
    script = os.path.abspath(__file__)
    if command == "--start":
        if _running_pid():
            return _message("Anbled Screen is already on.\n\nIn Anbled, tap Cast and choose this computer.")
        if not find_vlc():
            return _message("VLC is needed to play what's cast.\n\nInstall it from videolan.org, or run:\nwinget install VideoLAN.VLC")
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        subprocess.Popen([_pythonw(), script], creationflags=flags, close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(20):
            time.sleep(0.5)
            if _running_pid():
                return _message(f"Anbled Screen is on ({lan_ip()}).\n\nIn Anbled, tap Cast and choose this computer. "
                                "The phone must be on the same Wi-Fi.")
        return _message(f"Anbled Screen couldn't start.\n\nSee the log: {LOG_FILE}")
    if command == "--stop":
        pid = _running_pid()
        if pid:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        return _message("Anbled Screen is off." if pid else "Anbled Screen wasn't on.")
    if command == "--status":
        print(f"on ({lan_ip()}:{HTTP_PORT})" if _running_pid() else "off")
        return
    if command in ("--autostart", "--no-autostart"):
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            if command == "--autostart":
                winreg.SetValueEx(k, "Anbled Screen", 0, winreg.REG_SZ, f'"{_pythonw()}" "{script}"')
            else:
                try:
                    winreg.DeleteValue(k, "Anbled Screen")
                except OSError:
                    pass
        print("Anbled Screen " + ("starts" if command == "--autostart" else "won't start") + " when you sign in.")
        return
    sys.exit(f"Unknown command: {command} (use --start, --stop, --status, --autostart or --no-autostart)")


def main():
    if sys.argv[1:] == ["--version"]:
        return print(VERSION)
    if len(sys.argv) > 1:
        if not WINDOWS:
            sys.exit("On Linux, use the anbled-screen command (on, off, status, logs, autostart).")
        return windows_command(sys.argv[1])
    serve()


def serve():
    ip = lan_ip()
    vlc_command()  # says so now if VLC isn't installed
    Handler.player = Player()
    Handler.ip = ip
    try:
        httpd = Server((ip, HTTP_PORT), Handler)
    except OSError as e:
        log(f"port {HTTP_PORT} is taken (is Anbled Screen already on?): {e}")
        sys.exit(1)
    httpd.daemon_threads = True
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))
    stop = threading.Event()

    def shutdown(*_):
        stop.set()
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    finder = threading.Thread(target=ssdp, args=(ip, stop), daemon=True)
    finder.start()
    log(f'"{NAME}" is ready on {ip}: cast to it from Anbled (same Wi-Fi). Songs and videos open in a player window.')
    try:
        httpd.serve_forever()
    finally:
        stop.set()
        finder.join(timeout=3)
        Handler.player.close()
        try:
            os.remove(PID_FILE)
        except OSError:
            pass
        log("stopped")


if __name__ == "__main__":
    main()
