"""
Vercel Python serverless function — /api/search
------------------------------------------------
GET  /api/search  → returns config (has_server_key, available search_types)
POST /api/search  → searches for places along a driving route, returns KML file

Environment variable:
    GOOGLE_MAPS_API_KEY  (optional) — if set, the frontend does not need to
                                      collect an API key from the user.

Logging:
    Every request is appended as a JSON line to:
      • logs/search_log.jsonl  (project root — local development)
      • /tmp/search_log.jsonl  (fallback — Vercel ephemeral /tmp)
    Each entry is also printed to stdout (visible in the Vercel log dashboard).
"""

from http.server import BaseHTTPRequestHandler
import datetime
import json
import math
import os

# ── Logging setup ──────────────────────────────────────────────────────────────

def _resolve_log_path() -> str | None:
    """Return a writable log-file path, or None if the filesystem is read-only."""
    candidates = [
        # Project root  logs/  (works locally)
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "logs", "search_log.jsonl"),
        # Vercel ephemeral tmp (wiped between cold starts, but visible within a session)
        "/tmp/search_log.jsonl",
    ]
    for path in candidates:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a"):          # test write access
                pass
            return path
        except OSError:
            continue
    return None

_LOG_PATH: str | None = _resolve_log_path()


def _log(entry: dict) -> None:
    """Stamp and persist one log entry."""
    entry["timestamp"] = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    line = json.dumps(entry, ensure_ascii=False)
    print(line, flush=True)          # always hits stdout → Vercel dashboard
    if _LOG_PATH:
        try:
            with open(_LOG_PATH, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass

try:
    import googlemaps
    _GMAPS_AVAILABLE = True
except ImportError:
    _GMAPS_AVAILABLE = False

# ── Search type registry ───────────────────────────────────────────────────────

SEARCH_TYPES = {
    "petrol_pumps":   {"type": "gas_station",                       "label": "Petrol Pumps"},
    "ev_charging":    {"type": "electric_vehicle_charging_station",  "label": "EV Charging Stations"},
    "toilets":        {"keyword": "public toilet washroom restroom", "label": "Toilets & Restrooms"},
    "malls":          {"type": "shopping_mall",                      "label": "Shopping Malls"},
    "restaurants":    {"type": "restaurant",                         "label": "Restaurants"},
    "hotels":         {"type": "lodging",                            "label": "Hotels & Lodging"},
    "hospitals":      {"type": "hospital",                           "label": "Hospitals"},
    "atm":            {"type": "atm",                                "label": "ATMs"},
    "pharmacy":       {"type": "pharmacy",                           "label": "Pharmacies"},
    "cafe":           {"type": "cafe",                               "label": "Cafes & Coffee Shops"},
    "supermarket":    {"type": "supermarket",                        "label": "Supermarkets"},
    "tourist_attraction": {"type": "tourist_attraction",             "label": "Tourist Attractions"},
}

MAX_SAMPLE_POINTS = 50  # cap to stay comfortably within the 60-second timeout

# ── Geometry helpers ───────────────────────────────────────────────────────────

def decode_polyline(encoded: str) -> list:
    points = []
    index = lat = lng = 0
    while index < len(encoded):
        for is_lng in (False, True):
            shift = result = 0
            while True:
                b = ord(encoded[index]) - 63
                index += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            delta = ~(result >> 1) if result & 1 else result >> 1
            if is_lng:
                lng += delta
            else:
                lat += delta
        points.append((lat / 1e5, lng / 1e5))
    return points


def haversine(p1: tuple, p2: tuple) -> float:
    R = 6_371_000
    lat1, lng1 = math.radians(p1[0]), math.radians(p1[1])
    lat2, lng2 = math.radians(p2[0]), math.radians(p2[1])
    dlat, dlng = lat2 - lat1, lng2 - lng1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


def sample_route(pts: list, interval_m: float) -> list:
    if not pts:
        return []
    samples = [pts[0]]
    accumulated = 0.0
    for prev, curr in zip(pts, pts[1:]):
        accumulated += haversine(prev, curr)
        if accumulated >= interval_m:
            samples.append(curr)
            accumulated = 0.0
    if samples[-1] != pts[-1]:
        samples.append(pts[-1])
    return samples


# ── Places helpers ─────────────────────────────────────────────────────────────

def search_places(client, location: tuple, radius_m: int, cfg: dict) -> list:
    """First-page Places Nearby search — no pagination to keep latency low."""
    kwargs = {"location": location, "radius": radius_m}
    if "type" in cfg:
        kwargs["type"] = cfg["type"]
    else:
        kwargs["keyword"] = cfg["keyword"]
    resp = client.places_nearby(**kwargs)
    return resp.get("results", [])


def deduplicate(places: list, min_dist_m: float = 50) -> list:
    unique = []
    for c in places:
        loc = c["geometry"]["location"]
        p = (loc["lat"], loc["lng"])
        if all(
            haversine(p, (s["geometry"]["location"]["lat"],
                          s["geometry"]["location"]["lng"])) > min_dist_m
            for s in unique
        ):
            unique.append(c)
    return unique


def format_place(place: dict) -> dict:
    loc = place["geometry"]["location"]
    return {
        "name":     place.get("name", "Unknown"),
        "place_id": place.get("place_id", ""),
        "address":  place.get("vicinity", ""),
        "lat":      loc["lat"],
        "lng":      loc["lng"],
        "rating":   place.get("rating"),
        "open_now": place.get("opening_hours", {}).get("open_now"),
        "maps_url": f"https://www.google.com/maps/place/?q=place_id:{place.get('place_id', '')}",
    }


# ── KML builder ────────────────────────────────────────────────────────────────

def _esc(text) -> str:
    if not text:
        return ""
    return (str(text)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;"))


def _style_id(place: dict) -> str:
    o = place.get("open_now")
    return "open" if o is True else ("closed" if o is False else "unknown")


def _placemark(p: dict) -> str:
    status = ("Open" if p.get("open_now") is True
              else ("Closed" if p.get("open_now") is False else "Unknown"))
    rating = str(p["rating"]) if p.get("rating") is not None else "N/A"
    desc = (
        f"<b>Address:</b> {_esc(p['address'])}<br/>"
        f"<b>Rating:</b> {rating}<br/>"
        f"<b>Status:</b> {status}<br/>"
        f'<a href="{p["maps_url"]}">Open in Google Maps</a>'
    )
    return (
        f'\t\t\t<Placemark>\n'
        f'\t\t\t\t<name>{_esc(p["name"])}</name>\n'
        f'\t\t\t\t<description><![CDATA[{desc}]]></description>\n'
        f'\t\t\t\t<styleUrl>#{_style_id(p)}</styleUrl>\n'
        f'\t\t\t\t<Point><coordinates>{p["lng"]},{p["lat"]},0</coordinates></Point>\n'
        f'\t\t\t</Placemark>'
    )


def build_kml(places: list, route_info: dict, poly_pts: list, search_label: str) -> str:
    route_name = f"{route_info['start']} → {route_info['end']}"
    doc_desc = (
        f"{len(places)} {search_label} along {route_name} "
        f"({route_info['distance_km']:.1f} km, {route_info['duration']})"
    )
    coord_str = " ".join(f"{lng},{lat},0" for lat, lng in poly_pts)
    placemarks_xml = "\n".join(_placemark(p) for p in places)

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
\t<Document>
\t\t<name>{_esc(f"{search_label}: {route_name}")}</name>
\t\t<description>{_esc(doc_desc)}</description>

\t\t<!-- Marker styles: green=open, red=closed, yellow=unknown -->
\t\t<Style id="open">
\t\t\t<IconStyle>
\t\t\t\t<Icon><href>http://maps.google.com/mapfiles/ms/icons/green-dot.png</href></Icon>
\t\t\t</IconStyle>
\t\t</Style>
\t\t<Style id="closed">
\t\t\t<IconStyle>
\t\t\t\t<Icon><href>http://maps.google.com/mapfiles/ms/icons/red-dot.png</href></Icon>
\t\t\t</IconStyle>
\t\t</Style>
\t\t<Style id="unknown">
\t\t\t<IconStyle>
\t\t\t\t<Icon><href>http://maps.google.com/mapfiles/ms/icons/yellow-dot.png</href></Icon>
\t\t\t</IconStyle>
\t\t</Style>
\t\t<Style id="route">
\t\t\t<LineStyle><color>ffee4400</color><width>4</width></LineStyle>
\t\t\t<PolyStyle><fill>0</fill></PolyStyle>
\t\t</Style>

\t\t<!-- Driving route line -->
\t\t<Folder>
\t\t\t<name>Route</name>
\t\t\t<Placemark>
\t\t\t\t<name>{_esc(route_name)}</name>
\t\t\t\t<description>{_esc(doc_desc)}</description>
\t\t\t\t<styleUrl>#route</styleUrl>
\t\t\t\t<LineString>
\t\t\t\t\t<tessellate>1</tessellate>
\t\t\t\t\t<coordinates>{coord_str}</coordinates>
\t\t\t\t</LineString>
\t\t\t</Placemark>
\t\t</Folder>

\t\t<!-- Place markers -->
\t\t<Folder>
\t\t\t<name>{_esc(search_label)} ({len(places)})</name>
{placemarks_xml}
\t\t</Folder>
\t</Document>
</kml>"""


# ── HTTP handler ───────────────────────────────────────────────────────────────

class handler(BaseHTTPRequestHandler):

    # ── Helpers shared across methods ──────────────────────────────────────────

    def _client_ip(self) -> str:
        """Best-effort real IP (Vercel sets X-Forwarded-For)."""
        fwd = self.headers.get("X-Forwarded-For", "")
        if fwd:
            return fwd.split(",")[0].strip()
        return self.headers.get("X-Real-Ip", "") or (self.client_address[0] if self.client_address else "unknown")

    # ── Route handlers ─────────────────────────────────────────────────────────

    def do_GET(self):
        has_key = bool(os.environ.get("GOOGLE_MAPS_API_KEY", "").strip())
        _log({"event": "config_fetch", "ip": self._client_ip()})
        self._json(200, {
            "has_server_key": has_key,
            "search_types": {k: v["label"] for k, v in SEARCH_TYPES.items()},
        })

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors_headers()
        self.end_headers()

    def do_POST(self):
        ip = self._client_ip()

        if not _GMAPS_AVAILABLE:
            _log({"event": "search_error", "ip": ip, "http_status": 500,
                  "error": "googlemaps package missing"})
            self._json(500, {"error": "Server missing 'googlemaps' package."})
            return

        # Parse body
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
        except Exception:
            _log({"event": "search_error", "ip": ip, "http_status": 400,
                  "error": "invalid JSON body"})
            self._json(400, {"error": "Invalid JSON body."})
            return

        start      = (body.get("start") or "").strip()
        end        = (body.get("end") or "").strip()
        search_key = (body.get("search_type") or "petrol_pumps").strip()
        raw_key    = (body.get("api_key") or "").strip()
        api_key    = raw_key or os.environ.get("GOOGLE_MAPS_API_KEY", "")
        radius     = max(100, min(int(body.get("radius") or 2000), 50000))
        interval   = max(1000, min(int(body.get("interval") or 10000), 100000))

        # Base log entry — populated progressively
        log_entry: dict = {
            "event":              "search",
            "ip":                 ip,
            "start":              start,
            "end":                end,
            "search_type":        search_key,
            "radius_m":           radius,
            "interval_m":         interval,
            "has_custom_api_key": bool(raw_key),   # true = user provided own key
        }

        def abort(status: int, msg: str) -> None:
            log_entry.update({"status": "error", "http_status": status, "error": msg})
            _log(log_entry)
            self._json(status, {"error": msg})

        if not start or not end:
            abort(400, "'start' and 'end' are required.")
            return
        if not api_key:
            abort(400, "A Google Maps API key is required.")
            return
        if search_key not in SEARCH_TYPES:
            abort(400, f"Unknown search_type '{search_key}'.")
            return

        search_cfg = SEARCH_TYPES[search_key]
        client = googlemaps.Client(key=api_key)

        # 1. Get driving route
        try:
            dirs = client.directions(start, end, mode="driving")
        except Exception as e:
            abort(502, f"Directions API error: {e}")
            return
        if not dirs:
            abort(404, f"No route found: '{start}' → '{end}'")
            return

        route    = dirs[0]
        leg      = route["legs"][0]
        dist_km  = leg["distance"]["value"] / 1000
        duration = leg["duration"]["text"]

        # 2. Decode polyline & build sample points
        poly_pts = decode_polyline(route["overview_polyline"]["points"])
        samples  = sample_route(poly_pts, interval)
        if len(samples) > MAX_SAMPLE_POINTS:
            new_interval = (dist_km * 1000) / MAX_SAMPLE_POINTS
            samples = sample_route(poly_pts, new_interval)

        # 3. Search for places at each sample point
        all_places: list = []
        seen_ids: set = set()
        for pt in samples:
            try:
                for p in search_places(client, pt, radius, search_cfg):
                    pid = p.get("place_id", "")
                    if pid and pid not in seen_ids:
                        seen_ids.add(pid)
                        all_places.append(p)
            except Exception:
                pass  # skip this point, keep going

        # 4. Deduplicate & format
        all_places = deduplicate(all_places)
        formatted  = sorted([format_place(p) for p in all_places], key=lambda s: s["lat"])

        # 5. Build KML
        route_info = {"start": start, "end": end, "distance_km": dist_km, "duration": duration}
        kml = build_kml(formatted, route_info, poly_pts, search_cfg["label"])

        # 6. Log success
        log_entry.update({
            "status":            "ok",
            "http_status":       200,
            "result_count":      len(formatted),
            "route_distance_km": round(dist_km, 2),
            "route_duration":    duration,
            "sample_points":     len(samples),
        })
        _log(log_entry)

        # 7. Respond with downloadable KML
        def safe(s: str) -> str:
            return "".join(c if c.isalnum() or c in "-_" else "_" for c in s)

        filename = f"{safe(search_key)}_{safe(start[:15])}_to_{safe(end[:15])}.kml"

        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.google-earth.kml+xml")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("X-Result-Count", str(len(formatted)))
        self.send_header("X-Route-Distance", f"{dist_km:.1f}")
        self.send_header("X-Route-Duration", duration)
        self._cors_headers()
        self.end_headers()
        self.wfile.write(kml.encode("utf-8"))

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header(
            "Access-Control-Expose-Headers",
            "X-Result-Count, X-Route-Distance, X-Route-Duration",
        )

    def _json(self, status: int, data: dict):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self._cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass  # suppress Vercel access logs
