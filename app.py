#!/usr/bin/env python3
"""BitChord HTTP addon for Korean FM radio.

The addon discovers station names/IDs from the public radio.bsod.kr directory,
then periodically refreshes short-lived stream URLs. HLS manifests and segments
are proxied without being stored so the player can keep requesting fresh audio.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import requests

DIRECTORY_URL = "https://radio.bsod.kr/"
RESOLVER_URL = "https://radio.bsod.kr/api/url"
JIBS_STREAM_HOST = "123.140.197.22"
JIBS_CDN_HOST = "media.jibs.co.kr"
PORT = int(os.environ.get("PORT", "8080"))
PUBLIC_BASE_URL = (
    os.environ.get("PUBLIC_BASE_URL") or os.environ.get("RENDER_EXTERNAL_URL", "")
).rstrip("/")
PROXY_SECRET = os.environ.get("PROXY_SECRET", secrets.token_urlsafe(32)).encode("utf-8")
URL_CACHE_TTL = 18
URL_STALE_TTL = 180
USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36"
)
UPSTREAM_HEADERS = {
    "User-Agent": USER_AGENT,
    "Referer": DIRECTORY_URL,
    "Origin": "https://radio.bsod.kr",
    "Accept": "application/json, text/plain, */*",
}
REGIONS = {
    "seoul": "수도권",
    "busan": "부산·울산·경남",
    "daegu": "대구·경북",
    "gwangju": "광주·전남",
    "jeonbuk": "전북",
    "daejeon": "대전·세종·충남",
    "chungbuk": "충북",
    "gangwon": "강원",
    "jeju": "제주",
}


@dataclass(frozen=True)
class Station:
    id: str
    name: str
    stn: str
    ch: str = ""
    city: str = ""
    region: str = ""
    aliases: tuple[str, ...] = ()

    @property
    def params(self) -> dict[str, str]:
        result = {"stn": self.stn}
        if self.ch:
            result["ch"] = self.ch
        if self.city:
            result["city"] = self.city
        return result


class ChannelParser(HTMLParser):
    """Extract the site's visible channel names and changeSource arguments."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.region_id = "seoul"
        self.current: Optional[dict] = None
        self.items: list[dict] = []
        self.region_divs = set(REGIONS)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        a = dict(attrs)
        if tag == "div" and a.get("id") in self.region_divs:
            self.region_id = str(a["id"])
        if tag == "a" and "channel" in (a.get("class") or "").split():
            href = a.get("href") or ""
            if "changeSource({" not in href:
                return
            m = re.search(r"changeSource\(\{(.*?)\}\)", href)
            if not m:
                return
            args = dict(re.findall(r"([A-Za-z]+)\s*:\s*'([^']*)'", m.group(1)))
            if not args.get("stn"):
                return
            self.current = {
                "name": "",
                "stn": args["stn"],
                "ch": args.get("ch", ""),
                "city": args.get("city", ""),
                "region": self.region_id,
            }

    def handle_data(self, data: str) -> None:
        if self.current is not None:
            self.current["name"] += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self.current is not None:
            self.current["name"] = " ".join(self.current["name"].split())
            if self.current["name"]:
                self.items.append(self.current)
            self.current = None


def station_id(stn: str, ch: str, city: str) -> str:
    raw = "\x1f".join((stn, ch, city)).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def aliases_for(stn: str, ch: str, city: str, name: str) -> tuple[str, ...]:
    aliases: list[str] = []
    if stn == "kbs" and ch == "1fm" and not city:
        aliases += ["KBS Classic FM", "클래식 FM", "KBS 클래식"]
    if stn == "kbs" and ch == "2fm" and not city:
        aliases += ["KBS Cool FM", "쿨 FM", "KBS 쿨"]
    if stn == "mbc" and ch == "sfm":
        aliases += ["MBC Standard FM", "MBC 표준"]
    if stn == "mbc" and ch == "fm4u":
        aliases += ["MBC FM4U", "MBC 포유"]
    if stn == "sbs" and ch == "powerfm":
        aliases += ["SBS Power FM", "SBS 파워"]
    if stn == "sbs" and ch == "lovefm":
        aliases += ["SBS Love FM", "SBS 러브"]
    if stn == "cbs" and ch == "mfm":
        aliases += ["CBS Music FM", "CBS 음악"]
    if stn == "cbs" and ch == "sfm":
        aliases += ["CBS Standard FM", "CBS 표준"]
    if stn == "ebs" and ch == "fm":
        aliases += ["EBS Radio"]
    aliases.append(name)
    return tuple(dict.fromkeys(aliases))


class Catalog:
    REFRESH_INTERVAL = 6 * 60 * 60
    RETRY_INTERVAL = 5 * 60

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stations = self._load_seed()
        self._by_id = {station.id: station for station in self._stations}
        self._last_refresh = 0.0
        self._last_attempt = 0.0
        self._refreshing = False
        # Serve the bundled list immediately; the online directory can be slow or unavailable.
        threading.Thread(target=self.refresh, daemon=True).start()

    def _load_seed(self) -> list[Station]:
        try:
            rows = json.loads(
                Path(__file__).with_name("stations_seed.json").read_text(encoding="utf-8")
            )
            stations: list[Station] = []
            for item in rows:
                stn = str(item["stn"])
                ch = str(item.get("ch", ""))
                city = str(item.get("city", ""))
                name = str(item["name"])
                stations.append(Station(
                    id=str(item.get("id") or station_id(stn, ch, city)),
                    name=name,
                    stn=stn,
                    ch=ch,
                    city=city,
                    region=str(item.get("region", "")),
                    aliases=tuple(item.get("aliases") or aliases_for(stn, ch, city, name)),
                ))
            print(f"Loaded {len(stations)} fallback Korean FM stations", flush=True)
            return stations
        except FileNotFoundError:
            print("Station seed not found; waiting for directory refresh", flush=True)
        except Exception as exc:
            print(f"Station seed load failed: {type(exc).__name__}", flush=True)
        return []

    def refresh(self) -> None:
        with self._lock:
            if self._refreshing:
                return
            self._refreshing = True
            self._last_attempt = time.time()
        try:
            with requests.get(DIRECTORY_URL, headers=UPSTREAM_HEADERS, timeout=15) as response:
                response.raise_for_status()
                parser = ChannelParser()
                parser.feed(response.text)
            stations: list[Station] = []
            seen: set[tuple[str, str, str]] = set()
            for item in parser.items:
                key = (item["stn"], item["ch"], item["city"])
                if key in seen:
                    continue
                seen.add(key)
                stations.append(Station(
                    id=station_id(*key),
                    name=item["name"],
                    stn=item["stn"],
                    ch=item["ch"],
                    city=item["city"],
                    region=REGIONS.get(item["region"], item["region"]),
                    aliases=aliases_for(*key, item["name"]),
                ))
            if not stations:
                raise RuntimeError("No stations found in directory page")
            # Put the national channels first when generic searches return many hits.
            preferred = [
                ("kbs", "1fm", ""), ("kbs", "2fm", ""), ("kbs", "1radio", ""),
                ("mbc", "fm4u", ""), ("mbc", "sfm", ""),
                ("sbs", "powerfm", ""), ("sbs", "lovefm", ""),
                ("ebs", "fm", ""), ("cbs", "mfm", ""), ("cbs", "sfm", ""),
            ]
            rank = {key: i for i, key in enumerate(preferred)}
            stations.sort(key=lambda s: (rank.get((s.stn, s.ch, s.city), len(rank)),))
            with self._lock:
                self._stations = stations
                self._by_id = {s.id: s for s in stations}
                self._last_refresh = time.time()
            print(f"Loaded {len(stations)} Korean FM stations", flush=True)
        except Exception as exc:
            print(f"Station directory refresh failed: {type(exc).__name__}: {exc}", flush=True)
        finally:
            with self._lock:
                self._refreshing = False

    def stations(self) -> list[Station]:
        now = time.time()
        with self._lock:
            should_refresh = (
                now - self._last_refresh > self.REFRESH_INTERVAL
                and now - self._last_attempt > self.RETRY_INTERVAL
                and not self._refreshing
            )
            stations = list(self._stations)
        if should_refresh:
            threading.Thread(target=self.refresh, daemon=True).start()
        return stations

    def get(self, sid: str) -> Optional[Station]:
        with self._lock:
            return self._by_id.get(sid)

    def search(self, query: str) -> list[Station]:
        stations = self.stations()
        q = normalize(query)
        if not q:
            return []
        featured_queries = {"라디오", "한국라디오", "fm", "koreanradio", "radio"}
        if q in featured_queries:
            return stations[:30]
        results: list[tuple[int, int, Station]] = []
        for index, station in enumerate(stations):
            names = [normalize(station.name), *(normalize(a) for a in station.aliases)]
            # Combine name/alias scores, with exact and prefix matches ahead of contains.
            score = 0
            for name in names:
                if q == name:
                    score = max(score, 0)
                elif name.startswith(q):
                    score = max(score, 1)
                elif q in name:
                    score = max(score, 2)
                elif all(token in name for token in q.split()):
                    score = max(score, 3)
            if score or any(q in name for name in names):
                results.append((score, index, station))
        results.sort(key=lambda x: (x[0], x[1]))
        return [s for _, _, s in results[:50]]


def normalize(value: str) -> str:
    return re.sub(r"[^0-9a-z가-힣]+", "", value.casefold())


catalog = Catalog()

_url_cache_lock = threading.Lock()
_url_cache: dict[str, tuple[float, float, str]] = {}


def upstream_url_allowed(url: str, station: Station) -> bool:
    parsed = urlparse(url)
    is_jibs = (
        station.name == "JIBS 뉴파워FM"
        and station.stn == "sbs"
        and station.ch == "powerfm"
        and station.city == "jeju"
    )
    if parsed.scheme == "https" and parsed.hostname:
        if is_jibs:
            return parsed.hostname in {JIBS_STREAM_HOST, JIBS_CDN_HOST}
        return True
    return (
        is_jibs
        and parsed.scheme == "http"
        and parsed.hostname == JIBS_STREAM_HOST
    )


def resolved_stream_url(station: Station) -> str:
    """Resolve a short-lived upstream URL, refreshing it before it can expire."""
    now = time.time()
    with _url_cache_lock:
        cached = _url_cache.get(station.id)
        if cached and now < cached[0]:
            return cached[2]
    try:
        response = requests.get(
            RESOLVER_URL, params=station.params, headers=UPSTREAM_HEADERS, timeout=15,
        )
        response.raise_for_status()
        url = response.json().get("url")
        if not isinstance(url, str) or not upstream_url_allowed(url, station):
            raise RuntimeError("Upstream returned an unsupported stream URL")
    except Exception:
        with _url_cache_lock:
            stale = _url_cache.get(station.id)
            if stale and now < stale[1]:
                return stale[2]
        raise
    with _url_cache_lock:
        _url_cache[station.id] = (now + URL_CACHE_TTL, now + URL_STALE_TTL, url)
    return url


def inspect_stream(station: Station) -> tuple[str, str, str]:
    """Return (URL, transport, codec) so non-HLS stations remain playable too."""
    url = resolved_stream_url(station)
    headers = dict(UPSTREAM_HEADERS)
    headers["Accept-Encoding"] = "identity"
    headers["Range"] = "bytes=0-63"
    response = requests.get(url, headers=headers, stream=True, timeout=(12, 20))
    try:
        response.raise_for_status()
        if not upstream_url_allowed(response.url, station):
            raise RuntimeError("Stream redirected to a disallowed host")
        content_type = response.headers.get("Content-Type", "").lower()
        prefix = response.raw.read(64).lstrip()
        if "mpegurl" in content_type or prefix.startswith(b"#EXTM3U"):
            return url, "hls", "aac"
        path = urlparse(url).path.lower()
        codec = "mp3" if "mpeg" in content_type or path.endswith(".mp3") else "aac"
        if "ogg" in content_type or path.endswith(".ogg"):
            codec = "ogg"
        return url, "direct", codec
    finally:
        response.close()


def fetch_hls_segments(url: str, station: Station) -> tuple[float, list[tuple[int, str, float]]]:
    """Follow HLS master playlists and return the current media-segment window."""
    for _ in range(5):
        if not upstream_url_allowed(url, station):
            raise RuntimeError("Disallowed HLS playlist URL")
        with requests.get(
            url, headers={**UPSTREAM_HEADERS, "Accept-Encoding": "identity"}, timeout=(12, 20),
        ) as response:
            response.raise_for_status()
            playlist_url = response.url
            if not upstream_url_allowed(playlist_url, station):
                raise RuntimeError("HLS playlist redirected to a disallowed host")
            lines = [line.strip() for line in response.text.splitlines() if line.strip()]
        variant = None
        for index, line in enumerate(lines):
            if line.startswith("#EXT-X-STREAM-INF:"):
                variant = next((x for x in lines[index + 1:] if not x.startswith("#")), None)
                break
        if variant:
            url = urljoin(playlist_url, variant)
            if not upstream_url_allowed(url, station):
                raise RuntimeError("HLS variant points to a disallowed host")
            continue

        media_sequence = 0
        target_duration = 6.0
        next_duration = target_duration
        segments: list[tuple[int, str, float]] = []
        for line in lines:
            if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
                try:
                    media_sequence = int(line.split(":", 1)[1])
                except ValueError:
                    pass
            elif line.startswith("#EXT-X-TARGETDURATION:"):
                try:
                    target_duration = float(line.split(":", 1)[1])
                except ValueError:
                    pass
            elif line.startswith("#EXTINF:"):
                try:
                    next_duration = float(line.split(":", 1)[1].split(",", 1)[0])
                except ValueError:
                    next_duration = target_duration
            elif not line.startswith("#"):
                sequence = media_sequence + len(segments)
                segment_url = urljoin(playlist_url, line)
                if not upstream_url_allowed(segment_url, station):
                    raise RuntimeError("HLS segment points to a disallowed host")
                segments.append((sequence, segment_url, next_duration))
                next_duration = target_duration
        return target_duration, segments
    raise RuntimeError("Too many nested HLS playlists")


def inspect_hls_container(url: str, station: Station) -> Optional[str]:
    """Detect a simple audio segment container that can be concatenated live."""
    _, segments = fetch_hls_segments(url, station)
    if not segments:
        return None
    segment_url = segments[0][1]
    path = urlparse(segment_url).path.lower()
    if path.endswith(".ts"):
        return "ts"
    if path.endswith(".aac"):
        return "aac"
    if path.endswith(".mp3"):
        return "mp3"
    response = requests.get(
        segment_url,
        headers={**UPSTREAM_HEADERS, "Accept-Encoding": "identity", "Range": "bytes=0-511"},
        stream=True,
        timeout=(12, 20),
    )
    try:
        response.raise_for_status()
        if not upstream_url_allowed(response.url, station):
            raise RuntimeError("HLS segment redirected to a disallowed host")
        content_type = response.headers.get("Content-Type", "").lower()
        prefix = response.raw.read(512)
        if "mp2t" in content_type or path.endswith(".ts") or (
            len(prefix) >= 376 and prefix[0] == prefix[188] == 0x47
        ):
            return "ts"
        if "audio/aac" in content_type or path.endswith(".aac") or (
            len(prefix) >= 2 and prefix[0] == 0xFF and (prefix[1] & 0xF6) == 0xF0
        ):
            return "aac"
        if "audio/mpeg" in content_type or path.endswith(".mp3"):
            return "mp3"
        return None
    finally:
        response.close()


class Handler(BaseHTTPRequestHandler):
    server_version = "KoreanFMBitChordAddon/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        # Avoid logging temporary signed stream URLs or query strings.
        path = self.path.split("?", 1)[0]
        print(f"{self.client_address[0]} {self.command} {path}", flush=True)

    def send_json(self, status: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def public_base(self) -> str:
        if PUBLIC_BASE_URL:
            return PUBLIC_BASE_URL
        proto = self.headers.get("X-Forwarded-Proto", "http").split(",")[0].strip()
        host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or f"127.0.0.1:{PORT}"
        return f"{proto}://{host}"

    def proxy_url(self, sid: str, upstream_url: str) -> str:
        token = base64.urlsafe_b64encode(upstream_url.encode("utf-8")).decode("ascii").rstrip("=")
        message = f"{sid}\n{token}".encode("utf-8")
        signature = hmac.new(PROXY_SECRET, message, hashlib.sha256).hexdigest()
        return f"{self.public_base()}/proxy/{sid}?u={token}&s={signature}"

    def send_manifest(self, sid: str, body: bytes, source_url: str) -> None:
        text = body.decode("utf-8-sig", errors="replace")
        output: list[str] = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                output.append(line)
            elif stripped.startswith("#"):
                def rewrite_uri(match: re.Match[str]) -> str:
                    target = urljoin(source_url, match.group(1))
                    return f'URI="{self.proxy_url(sid, target)}"'
                output.append(re.sub(r'URI="([^"]+)"', rewrite_uri, line))
            else:
                output.append(self.proxy_url(sid, urljoin(source_url, stripped)))
        payload = ("\n".join(output) + "\n").encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.apple.mpegurl")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def proxy_upstream(self, sid: str, upstream_url: str) -> None:
        station = catalog.get(sid)
        if station is None or not upstream_url_allowed(upstream_url, station):
            self.send_json(400, {"error": "Invalid stream request"})
            return
        headers = {
            "User-Agent": USER_AGENT,
            "Referer": DIRECTORY_URL,
            "Accept": "*/*",
            "Accept-Encoding": "identity",
        }
        if self.headers.get("Range"):
            headers["Range"] = self.headers["Range"]
        try:
            response = requests.get(
                upstream_url, headers=headers, stream=True, timeout=(12, 45), allow_redirects=True,
            )
            response.raise_for_status()
            if not upstream_url_allowed(response.url, station):
                raise RuntimeError("Proxy stream redirected to a disallowed host")
            content_type = response.headers.get("Content-Type", "")
            looks_like_manifest = (
                "mpegurl" in content_type.lower()
                or urlparse(response.url).path.lower().endswith(".m3u8")
            )
            if looks_like_manifest:
                body = response.content
                if body.lstrip().startswith(b"#EXTM3U"):
                    self.send_manifest(sid, body, response.url)
                    return
            self.send_response(response.status_code)
            for header in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges", "Last-Modified", "ETag"):
                value = response.headers.get(header)
                if value:
                    self.send_header(header, value)
            self.send_header("Cache-Control", "no-store")
            if not response.headers.get("Content-Length"):
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if chunk:
                    self.wfile.write(chunk)
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            print(f"Stream proxy failed for {sid}: {type(exc).__name__}", flush=True)
            if not self.wfile.closed:
                try:
                    self.send_json(502, {"error": "Upstream stream unavailable"})
                except Exception:
                    pass
        finally:
            if "response" in locals():
                response.close()

    def handle_proxy(self, sid: str, query: str) -> None:
        params = parse_qs(query)
        token = params.get("u", [""])[0]
        signature = params.get("s", [""])[0]
        expected = hmac.new(PROXY_SECRET, f"{sid}\n{token}".encode("utf-8"), hashlib.sha256).hexdigest()
        if not token or not hmac.compare_digest(signature, expected):
            self.send_json(403, {"error": "Invalid proxy signature"})
            return
        try:
            encoded = token + "=" * ((4 - len(token) % 4) % 4)
            upstream_url = base64.urlsafe_b64decode(encoded.encode("ascii")).decode("utf-8")
        except Exception:
            self.send_json(400, {"error": "Invalid proxy target"})
            return
        self.proxy_upstream(sid, upstream_url)

    def continuous_segments(self, sid: str, container: str) -> None:
        """Join a rolling HLS segment window into an open-ended progressive response."""
        station = catalog.get(sid)
        mime_types = {"ts": "video/mp2t", "aac": "audio/aac", "mp3": "audio/mpeg"}
        content_type = mime_types.get(container)
        if station is None or content_type is None:
            self.send_json(404, {"error": "Continuous stream not found"})
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()

        last_sequence: Optional[int] = None

        def write_chunk(data: bytes) -> None:
            self.wfile.write(f"{len(data):X}\r\n".encode("ascii"))
            self.wfile.write(data)
            self.wfile.write(b"\r\n")
            self.wfile.flush()

        while True:
            try:
                master_url = resolved_stream_url(station)
                target_duration, segments = fetch_hls_segments(master_url, station)
                if not segments:
                    time.sleep(max(1.0, min(5.0, target_duration / 2)))
                    continue
                newest = max(item[0] for item in segments)
                if last_sequence is not None and newest < last_sequence:
                    last_sequence = None
                pending = segments if last_sequence is None else [item for item in segments if item[0] > last_sequence]
                if last_sequence is None and len(pending) > 2:
                    pending = pending[-2:]
                if not pending:
                    time.sleep(max(1.0, min(5.0, target_duration / 2)))
                    continue

                for sequence, segment_url, _duration in pending:
                    response = None
                    try:
                        response = requests.get(
                            segment_url,
                            headers={
                                "User-Agent": USER_AGENT,
                                "Referer": DIRECTORY_URL,
                                "Accept": "*/*",
                                "Accept-Encoding": "identity",
                            },
                            stream=True,
                            timeout=(12, 45),
                        )
                        response.raise_for_status()
                        if not upstream_url_allowed(response.url, station):
                            raise RuntimeError("HLS segment redirected to a disallowed host")
                        for chunk in response.iter_content(chunk_size=64 * 1024):
                            if chunk:
                                write_chunk(chunk)
                        last_sequence = sequence
                    except requests.HTTPError as exc:
                        status = getattr(exc.response, "status_code", "unknown")
                        print(f"Live segment skipped for {sid}: HTTP {status}", flush=True)
                        last_sequence = sequence
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        return
                    finally:
                        if response is not None:
                            response.close()
            except (BrokenPipeError, ConnectionResetError, OSError):
                return
            except Exception as exc:
                print(f"Continuous stream retry for {sid}: {type(exc).__name__}", flush=True)
                time.sleep(2)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        if path == "/healthz":
            self.send_json(200, {"ok": True, "stations": len(catalog.stations())})
            return
        if path == "/manifest.json":
            self.send_json(200, {
                "id": "cue.korean-fm-radio",
                "name": "한국 FM 라디오",
                "version": "1.0.0",
                "resources": ["search", "stream"],
                "allowDownloads": False,
                "settings": [],
            })
            return
        if path.startswith("/live/") and path.endswith("/master.m3u8"):
            sid = path.split("/")[2]
            station = catalog.get(sid)
            if station is None:
                self.send_json(404, {"error": "Station not found"})
                return
            try:
                self.proxy_upstream(sid, resolved_stream_url(station))
            except Exception as exc:
                print(f"Live manifest failed for {station.name}: {type(exc).__name__}", flush=True)
                self.send_json(502, {"error": "Could not resolve this station right now"})
            return
        if path.startswith("/proxy/"):
            self.handle_proxy(path.split("/")[2], parsed.query)
            return
        if path.startswith("/direct/"):
            sid = path.rsplit("/", 1)[-1]
            station = catalog.get(sid)
            if station is None:
                self.send_json(404, {"error": "Station not found"})
                return
            try:
                self.proxy_upstream(sid, resolved_stream_url(station))
            except Exception as exc:
                print(f"Direct stream failed for {station.name}: {type(exc).__name__}", flush=True)
                self.send_json(502, {"error": "Could not resolve this station right now"})
            return
        if path.startswith("/continuous/"):
            resource = path.rsplit("/", 1)[-1]
            if "." not in resource:
                self.send_json(404, {"error": "Not found"})
                return
            sid, container = resource.rsplit(".", 1)
            self.continuous_segments(sid, container)
            return
        if path == "/search":
            query = parse_qs(parsed.query).get("q", [""])[0]
            matches = catalog.search(query)
            self.send_json(200, {"tracks": [
                {
                    "id": s.id,
                    "title": s.name,
                    "artist": s.region or "대한민국 실시간 라디오",
                    "album": "한국 FM 라이브",
                    "format": "aac",
                    "audioQuality": "Live AAC",
                }
                for s in matches
            ]})
            return
        if path.startswith("/stream/"):
            sid = path.rsplit("/", 1)[-1]
            station = catalog.get(sid)
            if station is None:
                self.send_json(404, {"error": "Station not found"})
                return
            try:
                upstream_url, transport, codec = inspect_stream(station)
                if transport == "hls":
                    try:
                        container = inspect_hls_container(upstream_url, station)
                    except Exception as exc:
                        print(f"HLS segment probe failed for {station.name}: {type(exc).__name__}", flush=True)
                        container = None
                    if container:
                        # Many broadcaster feeds expose only a 20–30 second
                        # moving window. Join compatible AAC/MP3/TS segments
                        # so BitChord sees a continuous live response instead.
                        live_url = f"{self.public_base()}/continuous/{sid}.{container}"
                        transport = "direct"
                    else:
                        live_url = f"{self.public_base()}/live/{sid}/master.m3u8"
                else:
                    live_url = f"{self.public_base()}/direct/{sid}"
                self.send_json(200, {
                    "url": live_url,
                    "format": codec,
                    "codec": codec,
                    **({"manifest": "hls"} if transport == "hls" else {}),
                    "quality": f"Live {codec.upper()}",
                })
            except Exception as exc:
                print(f"Resolver failed for {station.name}: {type(exc).__name__}", flush=True)
                self.send_json(502, {"error": "Could not resolve this station right now"})
            return
        self.send_json(404, {"error": "Not found"})


def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Korean FM BitChord addon listening on 0.0.0.0:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
