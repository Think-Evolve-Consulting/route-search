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
      • /tmp/search_log.jsonl  (Vercel ephemeral /tmp)
    Each entry is also printed to stdout (visible in the Vercel log dashboard).
"""

from flask import Flask, request, jsonify, make_response
from typing import Optional
import datetime
import json
import math
import os

# ── Logging setup ──────────────────────────────────────────────────────────────

def _resolve_log_path():
    """Return a writable log-file path, or None if the filesystem is read-only."""
    candidates = [
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "logs", "search_log.jsonl"),
        "/tmp/search_log.jsonl",
    ]
    for path in candidates:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a"):
                pass
            return path
        except OSError:
            continue
    return None

_LOG_PATH = _resolve_log_path()


def _log(entry):
    """Stamp and persist one log entry."""
    entry["timestamp"] = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    line = json.dumps(entry, ensure_ascii=False)
    print(line, flush=True)
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
    "petrol_pumps":       {"type": "gas_station",                       "label": "Petrol Pumps"},
    "ev_charging":        {"type": "electric_vehicle_charging_station",  "label": "EV Charging Stations"},
    "toilets":            {"keyword": "public toilet washroom restroom", "label": "Toilets & Restrooms"},
    "malls":              {"type": "shopping_mall",                      "label": "Shopping Malls"},
    "restaurants":        {"type": "restaurant",                         "label": "Restaurants"},
    "hotels":             {"type": "lodging",                            "label": "Hotels & Lodging"},
    "hospitals":          {"type": "hospital",                           "label": "Hospitals"},
    "atm":                {"type": "atm",                                "label": "ATMs"},
    "pharmacy":           {"type": "pharmacy",                           "label": "Pharmacies"},
    "cafe":               {"type": "cafe",                               "label": "Cafes & Coffee Shops"},
    "supermarket":        {"type": "supermarket",                        "label": "Supermarkets"},
    "tourist_attraction": {"type": "tourist_attraction",                 "label": "Tourist Attractions"},
}

MAX_SAMPLE_POINTS = 50


# ── Geometry helpers ───────────────────────────────────────────────────────────

def decode_polyline(encoded):
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


def haversine(p1, p2):
    R = 6_371_000
    lat1, lng1 = math.radians(p1[0]), math.radians(p1[1])
    lat2, lng2 = math.radians(p2[0]), math.radians(p2[1])
    dlat, dlng = lat2 - lat1, lng2 - lng1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


def sample_route(pts, interval_m):
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

def search_places(client, location, radius_m, cfg):
    kwargs = {"location": location, "radius": radius_m}
    if "type" in cfg:
        kwargs["type"] = cfg["type"]
    else:
        kwargs["keyword"] = cfg["keyword"]
    resp = client.places_nearby(**kwargs)
    return resp.get("results", [])


def deduplicate(places, min_dist_m=50):
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


def format_place(place):
    loc = place["geometry"]["location"]
    return {
        "name":     place.get("name", "Unknown"),
        "place_id": place.get("place_id", ""),
        "address":  place.get("vicinity", ""),
        "lat":      loc["lat"],
        "lng":      loc["lng"],
        "rating":   place.get("rating"),
        "open_now": place.get("opening_hours", {}).get("open_now"),
        "maps_url": "https://www.google.com/maps/place/?q=place_id:" + place.get("place_id", ""),
    }


# ── KML builder ────────────────────────────────────────────────────────────────

def _esc(text):
    if not text:
        return ""
    return (str(text)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;"))


def _style_id(place):
    o = place.get("open_now")
    return "open" if o is True else ("closed" if o is False else "unknown")


def _placemark(p):
    status = ("Open" if p.get("open_now") is True
              else ("Closed" if p.get("open_now") is False else "Unknown"))
    rating = str(p["rating"]) if p.get("rating") is not None else "N/A"
    desc = (
        "<b>Address:</b> {addr}<br/>"
        "<b>Rating:</b> {rat}<br/>"
        "<b>Status:</b> {st}<br/>"
        '<a href="{url}">Open in Google Maps</a>'
    ).format(addr=_esc(p["address"]), rat=rating, st=status, url=p["maps_url"])
    return (
        '\t\t\t<Placemark>\n'
        '\t\t\t\t<name>{name}</name>\n'
        '\t\t\t\t<description><![CDATA[{desc}]]></description>\n'
        '\t\t\t\t<styleUrl>#{sid}</styleUrl>\n'
        '\t\t\t\t<Point><coordinates>{lng},{lat},0</coordinates></Point>\n'
        '\t\t\t</Placemark>'
    ).format(name=_esc(p["name"]), desc=desc, sid=_style_id(p),
             lng=p["lng"], lat=p["lat"])


def build_kml(places, route_info, poly_pts, search_label):
    route_name = "{start} to {end}".format(**route_info)
    doc_desc = (
        "{count} {label} along {rn} "
        "({dist:.1f} km, {dur})"
    ).format(count=len(places), label=search_label, rn=route_name,
             dist=route_info["distance_km"], dur=route_info["duration"])
    coord_str = " ".join("{},{},0".format(lng, lat) for lat, lng in poly_pts)
    placemarks_xml = "\n".join(_placemark(p) for p in places)

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<kml xmlns="http://www.opengis.net/kml/2.2">\n'
        '\t<Document>\n'
        '\t\t<name>{doc_name}</name>\n'
        '\t\t<description>{doc_desc}</description>\n'
        '\n'
        '\t\t<Style id="open">\n'
        '\t\t\t<IconStyle>\n'
        '\t\t\t\t<Icon><href>http://maps.google.com/mapfiles/ms/icons/green-dot.png</href></Icon>\n'
        '\t\t\t</IconStyle>\n'
        '\t\t</Style>\n'
        '\t\t<Style id="closed">\n'
        '\t\t\t<IconStyle>\n'
        '\t\t\t\t<Icon><href>http://maps.google.com/mapfiles/ms/icons/red-dot.png</href></Icon>\n'
        '\t\t\t</IconStyle>\n'
        '\t\t</Style>\n'
        '\t\t<Style id="unknown">\n'
        '\t\t\t<IconStyle>\n'
        '\t\t\t\t<Icon><href>http://maps.google.com/mapfiles/ms/icons/yellow-dot.png</href></Icon>\n'
        '\t\t\t</IconStyle>\n'
        '\t\t</Style>\n'
        '\t\t<Style id="route">\n'
        '\t\t\t<LineStyle><color>ffee4400</color><width>4</width></LineStyle>\n'
        '\t\t\t<PolyStyle><fill>0</fill></PolyStyle>\n'
        '\t\t</Style>\n'
        '\n'
        '\t\t<Folder>\n'
        '\t\t\t<name>Route</name>\n'
        '\t\t\t<Placemark>\n'
        '\t\t\t\t<name>{route_name}</name>\n'
        '\t\t\t\t<description>{doc_desc}</description>\n'
        '\t\t\t\t<styleUrl>#route</styleUrl>\n'
        '\t\t\t\t<LineString>\n'
        '\t\t\t\t\t<tessellate>1</tessellate>\n'
        '\t\t\t\t\t<coordinates>{coords}</coordinates>\n'
        '\t\t\t\t</LineString>\n'
        '\t\t\t</Placemark>\n'
        '\t\t</Folder>\n'
        '\n'
        '\t\t<Folder>\n'
        '\t\t\t<name>{folder_name}</name>\n'
        '{placemarks}\n'
        '\t\t</Folder>\n'
        '\t</Document>\n'
        '</kml>'
    ).format(
        doc_name=_esc(search_label + ": " + route_name),
        doc_desc=_esc(doc_desc),
        route_name=_esc(route_name),
        coords=coord_str,
        folder_name=_esc(search_label + " (" + str(len(places)) + ")"),
        placemarks=placemarks_xml,
    )


# ── Flask app ──────────────────────────────────────────────────────────────────

app = Flask(__name__)


@app.after_request
def _cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Expose-Headers"] = (
        "X-Result-Count, X-Route-Distance, X-Route-Duration"
    )
    return response


def _client_ip():
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.headers.get("X-Real-Ip", "") or request.remote_addr or "unknown"


@app.route("/api/search", methods=["GET"])
def get_config():
    ip = _client_ip()
    has_key = bool(os.environ.get("GOOGLE_MAPS_API_KEY", "").strip())
    _log({"event": "config_fetch", "ip": ip})
    return jsonify({
        "has_server_key": has_key,
        "search_types": {k: v["label"] for k, v in SEARCH_TYPES.items()},
    })


@app.route("/api/search", methods=["POST"])
def post_search():
    ip = _client_ip()

    if not _GMAPS_AVAILABLE:
        _log({"event": "search_error", "ip": ip, "http_status": 500,
              "error": "googlemaps package missing"})
        return jsonify({"error": "Server missing 'googlemaps' package."}), 500

    body = request.get_json(silent=True)
    if not body:
        _log({"event": "search_error", "ip": ip, "http_status": 400,
              "error": "invalid JSON body"})
        return jsonify({"error": "Invalid JSON body."}), 400

    start      = (body.get("start") or "").strip()
    end        = (body.get("end") or "").strip()
    search_key = (body.get("search_type") or "petrol_pumps").strip()
    raw_key    = (body.get("api_key") or "").strip()
    api_key    = raw_key or os.environ.get("GOOGLE_MAPS_API_KEY", "")
    radius     = max(100, min(int(body.get("radius") or 2000), 50000))
    interval   = max(1000, min(int(body.get("interval") or 10000), 100000))

    log_entry = {
        "event":              "search",
        "ip":                 ip,
        "start":              start,
        "end":                end,
        "search_type":        search_key,
        "radius_m":           radius,
        "interval_m":         interval,
        "has_custom_api_key": bool(raw_key),
    }

    def abort(status, msg):
        log_entry.update({"status": "error", "http_status": status, "error": msg})
        _log(log_entry)
        return jsonify({"error": msg}), status

    if not start or not end:
        return abort(400, "'start' and 'end' are required.")
    if not api_key:
        return abort(400, "A Google Maps API key is required.")
    if search_key not in SEARCH_TYPES:
        return abort(400, "Unknown search_type '{}'.".format(search_key))

    search_cfg = SEARCH_TYPES[search_key]
    client = googlemaps.Client(key=api_key)

    # 1. Get driving route
    try:
        dirs = client.directions(start, end, mode="driving")
    except Exception as e:
        return abort(502, "Directions API error: {}".format(e))
    if not dirs:
        return abort(404, "No route found: '{}' to '{}'".format(start, end))

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
    all_places = []
    seen_ids = set()
    for pt in samples:
        try:
            for p in search_places(client, pt, radius, search_cfg):
                pid = p.get("place_id", "")
                if pid and pid not in seen_ids:
                    seen_ids.add(pid)
                    all_places.append(p)
        except Exception:
            pass

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

    # 7. Return KML file
    def safe(s):
        return "".join(c if c.isalnum() or c in "-_" else "_" for c in s)

    filename = "{}_{}_{}.kml".format(safe(search_key), safe(start[:15]), safe(end[:15]))

    resp = make_response(kml)
    resp.headers["Content-Type"] = "application/vnd.google-earth.kml+xml"
    resp.headers["Content-Disposition"] = 'attachment; filename="{}"'.format(filename)
    resp.headers["X-Result-Count"] = str(len(formatted))
    resp.headers["X-Route-Distance"] = "{:.1f}".format(dist_km)
    resp.headers["X-Route-Duration"] = duration
    return resp
