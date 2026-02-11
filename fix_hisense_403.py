#!/usr/bin/env python3
"""
Launch and cast YouTube on DIAL TVs that return HTTP 403 unless YouTube headers
are present.

This script:
1) Discovers DIAL and resolves the correct Application-URL.
2) Probes/launches YouTube with required Origin/Referer headers.
3) Casts a YouTube URL/id using YouTube Lounge API and screenId from DIAL.
"""

from __future__ import annotations

import argparse
import http.client
import json
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


SSDP_ADDR = ("239.255.255.250", 1900)
DIAL_ST = "urn:dial-multiscreen-org:service:dial:1"
MSEARCH_PAYLOAD = (
    "M-SEARCH * HTTP/1.1\r\n"
    "HOST: 239.255.255.250:1900\r\n"
    'MAN: "ssdp:discover"\r\n'
    "MX: 2\r\n"
    f"ST: {DIAL_ST}\r\n"
    "\r\n"
).encode("ascii")

YOUTUBE_ORIGIN = "https://www.youtube.com"
YOUTUBE_REFERER = "https://www.youtube.com/tv"
YOUTUBE_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/96.0.4664.45 Safari/537.36"
)
YOUTUBE_DIAL_HEADERS = {
    "Origin": YOUTUBE_ORIGIN,
    "Referer": YOUTUBE_REFERER,
    "User-Agent": YOUTUBE_USER_AGENT,
}
YOUTUBE_API_BASE = "https://www.youtube.com/api/lounge"


@dataclass
class DialDevice:
    ip: str
    location: str
    usn: str
    server: str
    friendly_name: Optional[str] = None
    application_url: Optional[str] = None


def parse_ssdp_response(data: bytes) -> Dict[str, str]:
    text = data.decode("utf-8", errors="replace")
    headers: Dict[str, str] = {}
    for line in text.split("\r\n")[1:]:
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        headers[k.strip().lower()] = v.strip()
    return headers


def ssdp_discover(timeout: float = 3.0) -> List[Dict[str, str]]:
    seen: set[Tuple[str, str]] = set()
    results: List[Dict[str, str]] = []

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.settimeout(timeout)
        try:
            sock.sendto(MSEARCH_PAYLOAD, SSDP_ADDR)
        except OSError:
            return []
        end = time.time() + timeout
        while time.time() < end:
            try:
                data, _ = sock.recvfrom(65535)
            except socket.timeout:
                break
            headers = parse_ssdp_response(data)
            location = headers.get("location")
            usn = headers.get("usn", "")
            if not location:
                continue
            key = (location, usn)
            if key in seen:
                continue
            seen.add(key)
            results.append(headers)
    finally:
        sock.close()
    return results


def http_request(
    method: str,
    url: str,
    timeout: float = 5.0,
    headers: Optional[Dict[str, str]] = None,
    body: Optional[bytes] = None,
) -> Tuple[Optional[int], Dict[str, str], str]:
    req = urllib.request.Request(url=url, data=body, method=method)
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp_body = resp.read().decode("utf-8", errors="replace")
            return resp.getcode(), dict(resp.headers.items()), resp_body
    except urllib.error.HTTPError as err:
        resp_body = err.read().decode("utf-8", errors="replace")
        return err.code, dict(err.headers.items()), resp_body
    except Exception:
        return None, {}, ""


def safe_get(
    url: str,
    timeout: float = 3.0,
    headers: Optional[Dict[str, str]] = None,
) -> Tuple[Optional[int], Dict[str, str], str]:
    return http_request("GET", url, timeout=timeout, headers=headers, body=None)


def safe_post(
    url: str,
    timeout: float = 5.0,
    headers: Optional[Dict[str, str]] = None,
    body: Optional[bytes] = b"",
) -> Tuple[Optional[int], Dict[str, str], str]:
    req_headers = {"Content-Length": str(len(body or b""))}
    if headers:
        req_headers.update(headers)
    return http_request("POST", url, timeout=timeout, headers=req_headers, body=body)


def extract_xml_text(xml_text: str, tag: str) -> Optional[str]:
    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return None
    for elem in root.iter():
        if elem.tag.endswith(tag) and elem.text:
            return elem.text.strip()
    return None


def normalize_application_url(app_url: str) -> str:
    app_url = app_url.strip()
    if not app_url:
        return app_url
    if not app_url.endswith("/"):
        app_url += "/"
    return app_url


def derive_application_url_from_location(location: str) -> str:
    parsed = urllib.parse.urlparse(location)
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}/apps/"


def build_dial_devices(timeout: float = 3.0) -> List[DialDevice]:
    devices: List[DialDevice] = []
    for hdr in ssdp_discover(timeout=timeout):
        location = hdr.get("location", "")
        usn = hdr.get("usn", "")
        server = hdr.get("server", "")
        parsed = urllib.parse.urlparse(location)
        ip = parsed.hostname or ""
        if not ip:
            continue

        status, headers, body = safe_get(location, timeout=timeout)
        app_url = headers.get("Application-URL") or headers.get("application-url")
        app_url = normalize_application_url(app_url or derive_application_url_from_location(location))
        friendly = extract_xml_text(body, "friendlyName") if status else None
        devices.append(
            DialDevice(
                ip=ip,
                location=location,
                usn=usn,
                server=server,
                friendly_name=friendly,
                application_url=app_url,
            )
        )
    return devices


def probe_airplay_7000(ip: str, timeout: float = 2.0) -> Tuple[Optional[int], str]:
    conn = http.client.HTTPConnection(ip, 7000, timeout=timeout)
    try:
        conn.request("GET", "/")
        resp = conn.getresponse()
        server = resp.getheader("Server", "")
        _ = resp.read()
        return resp.status, server
    except Exception:
        return None, ""
    finally:
        conn.close()


def dial_youtube_status(
    application_url: str,
    timeout: float = 3.0,
    youtube_headers: bool = False,
) -> Tuple[Optional[int], Dict[str, str], str]:
    url = urllib.parse.urljoin(application_url, "YouTube")
    headers = YOUTUBE_DIAL_HEADERS if youtube_headers else None
    return safe_get(url, timeout=timeout, headers=headers)


def dial_launch_youtube(
    application_url: str,
    timeout: float = 5.0,
    youtube_headers: bool = False,
) -> Tuple[Optional[int], Dict[str, str], str]:
    url = urllib.parse.urljoin(application_url, "YouTube")
    headers = YOUTUBE_DIAL_HEADERS if youtube_headers else None
    return safe_post(url, timeout=timeout, headers=headers, body=b"")


def probe_common_application_urls(tv_ip: str, timeout: float = 3.0) -> List[Tuple[str, Optional[int], str]]:
    candidates = [
        f"http://{tv_ip}:3367/apps/",
        f"http://{tv_ip}:8008/apps/",
        f"http://{tv_ip}:8060/apps/",
        f"http://{tv_ip}:80/apps/",
        f"http://{tv_ip}:7000/apps/",
    ]
    probed: List[Tuple[str, Optional[int], str]] = []
    for base in candidates:
        status, headers, _ = dial_youtube_status(base, timeout=timeout, youtube_headers=True)
        server = headers.get("Server", headers.get("server", ""))
        probed.append((base, status, server))
    return probed


def extract_video_id(video: str) -> Optional[str]:
    v = video.strip()
    if not v:
        return None
    if re.fullmatch(r"[a-zA-Z0-9_-]{11}", v):
        return v
    parsed = urllib.parse.urlparse(v)
    if not parsed.scheme:
        return None
    host = parsed.netloc.lower()
    if host.endswith("youtu.be"):
        vid = parsed.path.strip("/")
        return vid if re.fullmatch(r"[a-zA-Z0-9_-]{11}", vid) else None
    if "youtube.com" not in host:
        return None
    qs = urllib.parse.parse_qs(parsed.query)
    if "v" in qs and qs["v"]:
        vid = qs["v"][0]
        return vid if re.fullmatch(r"[a-zA-Z0-9_-]{11}", vid) else None
    path_parts = [p for p in parsed.path.split("/") if p]
    if len(path_parts) >= 2 and path_parts[0] in {"shorts", "embed", "live"}:
        vid = path_parts[1]
        return vid if re.fullmatch(r"[a-zA-Z0-9_-]{11}", vid) else None
    return None


def youtube_api_request(
    method: str,
    url: str,
    timeout: float,
    query: Optional[Dict[str, str]] = None,
    body: Optional[Dict[str, str]] = None,
) -> Tuple[Optional[int], str]:
    if query:
        url = url + "?" + urllib.parse.urlencode(query)
    payload = urllib.parse.urlencode(body).encode("utf-8") if body else None
    headers = {
        "Origin": YOUTUBE_ORIGIN,
        "User-Agent": YOUTUBE_USER_AGENT,
    }
    if payload is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    status, _, resp_body = http_request(method, url, timeout=timeout, headers=headers, body=payload)
    return status, resp_body


def get_lounge_token(screen_id: str, timeout: float) -> Optional[str]:
    status, body = youtube_api_request(
        method="POST",
        url=f"{YOUTUBE_API_BASE}/pairing/get_lounge_token_batch",
        timeout=timeout,
        body={"screen_ids": screen_id},
    )
    if status != 200:
        return None
    try:
        data = json.loads(body)
        return data["screens"][0]["loungeToken"]
    except Exception:
        return None


def get_session_ids(lounge_token: str, timeout: float, client_name: str) -> Tuple[Optional[str], Optional[str]]:
    status, body = youtube_api_request(
        method="POST",
        url=f"{YOUTUBE_API_BASE}/bc/bind",
        timeout=timeout,
        query={
            "CVER": "1",
            "RID": "1",
            "VER": "8",
            "app": "youtube-desktop",
            "device": "REMOTE_CONTROL",
            "id": "remote",
            "loungeIdToken": lounge_token,
            "name": client_name,
        },
    )
    if status != 200:
        return None, None
    start = body.find("[")
    if start < 0:
        return None, None
    try:
        arr = json.loads(body[start:])
    except Exception:
        return None, None
    sid, gsession = None, None
    for item in arr:
        if not isinstance(item, list) or len(item) < 2:
            continue
        pair = item[1]
        if not isinstance(pair, list) or len(pair) < 2:
            continue
        key, value = pair[0], pair[1]
        if key == "c":
            sid = value
        elif key == "S":
            gsession = value
        if sid and gsession:
            return sid, gsession
    return sid, gsession


def lounge_play(video_id: str, lounge_token: str, sid: str, gsession: str, timeout: float) -> bool:
    status, _ = youtube_api_request(
        method="POST",
        url=f"{YOUTUBE_API_BASE}/bc/bind",
        timeout=timeout,
        query={
            "CVER": "1",
            "RID": "2",
            "SID": sid,
            "VER": "8",
            "gsessionid": gsession,
            "loungeIdToken": lounge_token,
        },
        body={
            "count": "1",
            "req0__sc": "setPlaylist",
            "req0_videoId": video_id,
            "req0_currentTime": "0",
            "req0_currentIndex": "0",
            "req0_videoIds": video_id,
        },
    )
    return status == 200


def choose_application_url(devices: List[DialDevice], tv_ip: str, timeout: float) -> Optional[str]:
    target = [d for d in devices if d.ip == tv_ip]
    if target and target[0].application_url:
        return target[0].application_url

    fallback_probes = probe_common_application_urls(tv_ip, timeout=timeout)
    print("\nDirect probe of common DIAL endpoints (with YouTube headers):")
    for base, st, srv in fallback_probes:
        print(f"- {urllib.parse.urljoin(base, 'YouTube')} -> {st} Server={srv or '<unknown>'}")
    for base, st, srv in fallback_probes:
        if st is None:
            continue
        if st == 403 and "AirTunes" in srv:
            continue
        if st in (200, 201, 202, 204, 404, 405, 503):
            return base
    return None


def run(tv_ip: str, launch: bool, timeout: float, video: Optional[str], client_name: str) -> int:
    devices = build_dial_devices(timeout=timeout)

    print(f"TV IP: {tv_ip}")
    print(f"DIAL devices discovered: {len(devices)}")
    for d in devices:
        print(
            json.dumps(
                {
                    "ip": d.ip,
                    "friendly_name": d.friendly_name,
                    "location": d.location,
                    "application_url": d.application_url,
                    "server": d.server,
                },
                ensure_ascii=True,
            )
        )

    app_url = choose_application_url(devices, tv_ip, timeout)
    if not app_url:
        status_7000, server_7000 = probe_airplay_7000(tv_ip, timeout=timeout)
        print("\nNo usable DIAL endpoint found.")
        if status_7000 is not None:
            print(f"Port 7000 probe: HTTP {status_7000}, Server={server_7000 or '<unknown>'}")
            if status_7000 == 403 and "AirTunes" in server_7000:
                print("Diagnosis: 7000 is AirPlay and rejects DIAL YouTube requests.")
        return 2

    plain_probe_status, plain_probe_headers, _ = dial_youtube_status(
        app_url, timeout=timeout, youtube_headers=False
    )
    plain_probe_server = plain_probe_headers.get("Server", plain_probe_headers.get("server", ""))
    print(
        f"\nPlain probe {urllib.parse.urljoin(app_url, 'YouTube')} -> "
        f"{plain_probe_status} Server={plain_probe_server}"
    )

    yt_probe_status, yt_probe_headers, yt_probe_body = dial_youtube_status(
        app_url, timeout=timeout, youtube_headers=True
    )
    yt_probe_server = yt_probe_headers.get("Server", yt_probe_headers.get("server", ""))
    print(
        f"YouTube-header probe {urllib.parse.urljoin(app_url, 'YouTube')} -> "
        f"{yt_probe_status} Server={yt_probe_server}"
    )

    if plain_probe_status == 403 and yt_probe_status in (200, 201, 202, 204):
        print("Diagnosis: this TV requires YouTube Origin/Referer headers for DIAL access.")

    wants_launch = launch or bool(video)
    if wants_launch:
        launch_status, launch_headers, launch_body = dial_launch_youtube(
            app_url, timeout=timeout, youtube_headers=True
        )
        launch_server = launch_headers.get("Server", launch_headers.get("server", ""))
        print(
            f"Launch {urllib.parse.urljoin(app_url, 'YouTube')} -> "
            f"{launch_status} Server={launch_server}"
        )
        if launch_body.strip():
            print(f"Launch body: {launch_body[:240]}")
        if launch_status not in (200, 201, 202, 204):
            print("Launch failed even with YouTube headers.")
            return 5

    if not video:
        if wants_launch:
            print("YouTube launch request accepted.")
        else:
            print("Endpoint looks usable. Re-run with --launch or --video.")
        return 0

    video_id = extract_video_id(video)
    if not video_id:
        print(f"Invalid YouTube URL/id: {video}")
        return 6

    status_after_launch, _, body_after_launch = dial_youtube_status(
        app_url, timeout=timeout, youtube_headers=True
    )
    if status_after_launch != 200:
        print(f"Cannot read YouTube app state after launch (HTTP {status_after_launch}).")
        return 7
    screen_id = extract_xml_text(body_after_launch, "screenId")
    if not screen_id:
        print("No screenId in DIAL YouTube app state; cannot continue with Lounge API.")
        return 8
    print(f"screenId: {screen_id}")

    lounge_token = get_lounge_token(screen_id=screen_id, timeout=max(timeout, 8.0))
    if not lounge_token:
        print("Unable to get Lounge token from YouTube API.")
        return 9
    print("Lounge token acquired.")

    sid, gsession = get_session_ids(
        lounge_token=lounge_token,
        timeout=max(timeout, 8.0),
        client_name=client_name,
    )
    if not sid or not gsession:
        print("Unable to get YouTube Lounge session IDs.")
        return 10
    print("Lounge session established.")

    ok = lounge_play(
        video_id=video_id,
        lounge_token=lounge_token,
        sid=sid,
        gsession=gsession,
        timeout=max(timeout, 8.0),
    )
    if not ok:
        print("Failed to queue/play video through Lounge API.")
        return 11

    print(f"Cast successful: videoId={video_id}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Avoid Hisense YouTube DIAL 403 and cast a YouTube link."
    )
    parser.add_argument(
        "--tv-ip",
        default="192.168.10.191",
        help="Target TV IP address (default: 192.168.10.191)",
    )
    parser.add_argument(
        "--launch",
        action="store_true",
        help="Launch YouTube only (with required headers).",
    )
    parser.add_argument(
        "--video",
        default=None,
        help="YouTube video URL or 11-char id to cast.",
    )
    parser.add_argument(
        "--client-name",
        default="ytcast-mac",
        help="Name shown by YouTube Lounge while connecting (default: ytcast-mac)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=3.0,
        help="Network timeout in seconds (default: 3.0)",
    )
    args = parser.parse_args()
    return run(
        tv_ip=args.tv_ip,
        launch=args.launch,
        timeout=args.timeout,
        video=args.video,
        client_name=args.client_name,
    )


if __name__ == "__main__":
    sys.exit(main())

