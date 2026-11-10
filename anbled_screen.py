#!/usr/bin/env python3
"""
Anbled Screen: makes a Linux computer a screen the Anbled app can cast to, with videos full screen.

To the app it is a smart TV: a UPnP / DLNA media renderer, found on the Wi-Fi with an SSDP search and told what to
play over AVTransport (the app's cast/Dlna.kt). What it's told to play goes to VLC (the Flatpak org.videolan.VLC, or
a vlc on the PATH), started once, hidden, and driven through its local web interface: videos open full screen, songs
play through the speakers. Nothing on this computer is shared: it only plays what a phone on the same Wi-Fi sends.

    python3 anbled_screen.py            run in the foreground (Ctrl+C stops it)
    ANBLED_SCREEN_NAME="Living room"    the name phones show (default "Anbled screen (Linux)")

Standard library only. Phones find it on UDP 1900, which Fedora's firewall closes until "ssdp" is allowed.
"""
import base64
import html
import ipaddress
import json
import os
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

NAME = os.environ.get("ANBLED_SCREEN_NAME", "Anbled screen (Linux)")
HTTP_PORT = int(os.environ.get("ANBLED_SCREEN_PORT", "49494"))
VLC_PORT = 49495
DEBUG_LOG = os.environ.get("ANBLED_SCREEN_LOG", "")  # a file: VLC's own messages go there (for finding problems)
SSDP_ADDR, SSDP_PORT = "239.255.255.250", 1900
STATE_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "anbled-screen")

DEVICE = "urn:schemas-upnp-org:device:MediaRenderer:1"
SERVICES = {
    "AVTransport": "urn:schemas-upnp-org:service:AVTransport:1",
    "RenderingControl": "urn:schemas-upnp-org:service:RenderingControl:1",
    "ConnectionManager": "urn:schemas-upnp-org:service:ConnectionManager:1",
}


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


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
def vlc_command():
    if shutil.which("flatpak") and subprocess.run(["flatpak", "info", "org.videolan.VLC"], capture_output=True).returncode == 0:
        return ["flatpak", "run", "org.videolan.VLC"]
    if shutil.which("vlc"):
        return ["vlc"]
    sys.exit("VLC isn't installed: flatpak install flathub org.videolan.VLC")


class Vlc:
    """
    One VLC per song, started with the song's address (so it plays at once, full screen for a video) and driven
    through its web interface on 127.0.0.1 with a password made new each time. A VLC kept running and handed song
    after song through its playlist kept ending up stopped after a song change, so each song gets a fresh one.
    """

    def __init__(self, uri, volume):
        self.password = secrets.token_urlsafe(16)
        auth = base64.b64encode(f":{self.password}".encode()).decode()
        self.headers = {"Authorization": f"Basic {auth}"}
        self.volume = volume
        self.proc = subprocess.Popen(
            vlc_command() + ["--intf", "dummy", "--extraintf", "http", "--http-host", "127.0.0.1", "--http-port", str(VLC_PORT),
                             "--http-password", self.password, "--fullscreen", "--video-on-top", "--no-video-title-show",
                             "--no-qt-privacy-ask", "--play-and-exit", "--network-caching", "1500"]
            + (["-vv"] if DEBUG_LOG else []) + ["--", uri],  # "--": the address is never read as an option
            stdin=subprocess.DEVNULL, stdout=open(DEBUG_LOG, "ab") if DEBUG_LOG else subprocess.DEVNULL,
            stderr=subprocess.STDOUT if DEBUG_LOG else subprocess.DEVNULL, start_new_session=True)
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

    def close(self):
        if self.alive():
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
                self.proc.wait(timeout=3)
            except Exception:
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except Exception:
                    pass


# ---------- what's playing ----------
class Player:
    def __init__(self):
        self.lock = threading.Lock()
        self.vlc = None            # the VLC playing the current song (None: nothing started yet / stopped)
        self.uri = ""
        self.owner = ""            # the phone that said what to play: only it is told the address back
        self.meta = ""
        self.title = ""
        self.starting_until = 0.0  # just asked to play: TRANSITIONING until VLC answers
        self.volume = 100

    def set_uri(self, uri, meta, owner):
        with self.lock:
            self._close()
            self.uri, self.meta, self.owner = uri, meta, owner
            title = re.search(r"<dc:title>(.*?)</dc:title>", html.unescape(meta or ""), re.S)
            self.title = html.unescape(title.group(1)) if title else ""
        log(f"next: {self.title or self.uri}")

    def play(self):
        with self.lock:
            if not self.uri:
                return False
            st = self.vlc.status() if self.vlc else None
            if st and st.get("state") == "paused":
                self.vlc.status(command="pl_forceresume")
            elif not (self.vlc and self.vlc.alive()):
                self.vlc = Vlc(self.uri, self.volume)
                self.starting_until = time.time() + 20
                log(f"playing: {self.title or self.uri}")
            return True

    def pause(self):
        if self.vlc:
            self.vlc.status(command="pl_forcepause")

    def stop(self):
        with self.lock:
            self._close()

    def _close(self):
        if self.vlc:
            self.vlc.close()
            self.vlc = None
        self.starting_until = 0

    def seek(self, seconds):
        if self.vlc:
            self.vlc.status(command="seek", val=str(int(seconds)))

    def set_volume(self, v):
        self.volume = max(0, min(100, v))
        if self.vlc:
            self.vlc.volume = self.volume
            self.vlc.status(command="volume", val=str(round(self.volume * 2.56)))

    def state(self):
        vlc = self.vlc
        st = (vlc.status() if vlc else None) or {}
        vlc_state = st.get("state")
        if vlc_state == "playing":
            self.starting_until = 0
            return "PLAYING", st
        if vlc_state == "paused":
            return "PAUSED_PLAYBACK", st
        if vlc and vlc.alive() and time.time() < self.starting_until:
            return "TRANSITIONING", st  # VLC is still starting or opening the song
        return ("STOPPED" if self.uri else "NO_MEDIA_PRESENT"), st

    def close(self):
        with self.lock:
            self._close()


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
            log(f"refused an address from {sender}")
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
        return {"CurrentVolume": self.player.volume}

    def RenderingControl_SetVolume(self, body):
        self.player.set_volume(int(arg(body, "DesiredVolume") or 100))
        return {}

    def RenderingControl_GetMute(self, body):
        return {"CurrentMute": 1 if self.player.volume == 0 else 0}

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
    server = "Linux UPnP/1.0 AnbledScreen/1"
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
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
        except socket.timeout:
            continue
        except OSError:
            break
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


def main():
    ip = lan_ip()
    vlc_command()  # says so now if VLC isn't installed
    Handler.player = Player()
    Handler.ip = ip
    httpd = ThreadingHTTPServer((ip, HTTP_PORT), Handler)
    httpd.daemon_threads = True
    stop = threading.Event()

    def shutdown(*_):
        stop.set()
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    finder = threading.Thread(target=ssdp, args=(ip, stop), daemon=True)
    finder.start()
    log(f'"{NAME}" is ready on {ip}: cast to it from Anbled (same Wi-Fi). Videos play full screen.')
    try:
        httpd.serve_forever()
    finally:
        stop.set()
        finder.join(timeout=3)
        Handler.player.close()
        log("stopped")


if __name__ == "__main__":
    main()
