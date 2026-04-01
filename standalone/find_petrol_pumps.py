"""
find_petrol_pumps.py
--------------------
Finds all petrol pumps (fuel stations) along a driving route using the Google Maps API.

Requirements:
    pip install googlemaps requests

Usage:
    python find_petrol_pumps.py --start "New Delhi" --end "Agra" --api-key YOUR_API_KEY

    # Optional flags:
    # --radius       Search radius around each sample point in meters (default: 2000)
    # --interval     Distance between sample points in meters (default: 5000)
    # --output       Output file path for JSON results (default: petrol_pumps.json)
    # --no-dedup     Disable deduplication of nearby stations
"""

import argparse
import json
import math
import time
import sys
from typing import Any

try:
    import googlemaps
except ImportError:
    sys.exit("❌  Missing dependency. Run: pip install googlemaps")


# ── Geometry helpers ───────────────────────────────────────────────────────────

def decode_polyline(encoded: str) -> list[tuple[float, float]]:
    """Decode a Google Maps encoded polyline into (lat, lng) tuples."""
    points: list[tuple[float, float]] = []
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


def haversine(p1: tuple[float, float], p2: tuple[float, float]) -> float:
    """Great-circle distance in metres between two (lat, lng) points."""
    R = 6_371_000
    lat1, lng1 = math.radians(p1[0]), math.radians(p1[1])
    lat2, lng2 = math.radians(p2[0]), math.radians(p2[1])
    dlat, dlng = lat2 - lat1, lng2 - lng1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


def sample_route(
    polyline_points: list[tuple[float, float]],
    interval_m: float,
) -> list[tuple[float, float]]:
    """
    Walk the decoded polyline and return sample points spaced ~interval_m apart,
    always including the first and last point.
    """
    if not polyline_points:
        return []
    samples = [polyline_points[0]]
    accumulated = 0.0
    for prev, curr in zip(polyline_points, polyline_points[1:]):
        seg_dist = haversine(prev, curr)
        accumulated += seg_dist
        if accumulated >= interval_m:
            samples.append(curr)
            accumulated = 0.0
    if samples[-1] != polyline_points[-1]:
        samples.append(polyline_points[-1])
    return samples


# ── API helpers ────────────────────────────────────────────────────────────────

def get_route(client: googlemaps.Client, origin: str, destination: str) -> dict[str, Any]:
    """Return the first route leg from the Directions API."""
    result = client.directions(origin, destination, mode="driving")
    if not result:
        sys.exit(f"❌  No route found between '{origin}' and '{destination}'.")
    return result[0]


def search_fuel_stations(
    client: googlemaps.Client,
    location: tuple[float, float],
    radius_m: int,
) -> list[dict[str, Any]]:
    """
    Search for fuel stations near a location using Places Nearby Search.
    Returns a list of place dicts.
    """
    places: list[dict] = []
    response = client.places_nearby(
        location=location,
        radius=radius_m,
        type="gas_station",
    )
    places.extend(response.get("results", []))

    # Follow pagination tokens (max 2 extra pages = 60 results per point)
    for _ in range(2):
        token = response.get("next_page_token")
        if not token:
            break
        time.sleep(2)          # Google requires a short delay before using the token
        response = client.places_nearby(page_token=token)
        places.extend(response.get("results", []))

    return places


def deduplicate(stations: list[dict], min_dist_m: float = 50) -> list[dict]:
    """
    Remove duplicate stations that are closer than min_dist_m to each other
    (keeps the first occurrence).
    """
    unique: list[dict] = []
    for candidate in stations:
        loc = candidate["geometry"]["location"]
        p = (loc["lat"], loc["lng"])
        if all(haversine(p, (s["geometry"]["location"]["lat"],
                             s["geometry"]["location"]["lng"])) > min_dist_m
               for s in unique):
            unique.append(candidate)
    return unique


def format_station(place: dict) -> dict[str, Any]:
    """Extract the fields we care about from a Places result."""
    loc = place["geometry"]["location"]
    return {
        "name":        place.get("name", "Unknown"),
        "place_id":    place.get("place_id", ""),
        "address":     place.get("vicinity", ""),
        "lat":         loc["lat"],
        "lng":         loc["lng"],
        "rating":      place.get("rating", None),
        "open_now":    place.get("opening_hours", {}).get("open_now", None),
        "maps_url":    f"https://www.google.com/maps/place/?q=place_id:{place.get('place_id','')}",
    }


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Find petrol pumps along a driving route via Google Maps API."
    )
    parser.add_argument("--start",   required=True, help="Origin address or place name")
    parser.add_argument("--end",     required=True, help="Destination address or place name")
    parser.add_argument("--api-key", required=True, help="Google Maps API key")
    parser.add_argument("--radius",   type=int, default=2000,
                        help="Search radius around each sample point in metres (default: 2000)")
    parser.add_argument("--interval", type=int, default=5000,
                        help="Distance between sample points in metres (default: 5000)")
    parser.add_argument("--output",  default="petrol_pumps.json",
                        help="Output JSON file path (default: petrol_pumps.json)")
    parser.add_argument("--no-dedup", action="store_true",
                        help="Disable deduplication of nearby stations")
    args = parser.parse_args()

    client = googlemaps.Client(key=args.api_key)

    # ── 1. Get route ──────────────────────────────────────────────────────────
    print(f"\n🗺️  Getting route: {args.start!r} → {args.end!r} …")
    route = get_route(client, args.start, args.end)
    leg = route["legs"][0]
    total_dist_km = leg["distance"]["value"] / 1000
    total_time    = leg["duration"]["text"]

    print(f"   Distance : {total_dist_km:.1f} km")
    print(f"   Duration : {total_time}")

    # ── 2. Decode polyline & sample ───────────────────────────────────────────
    encoded = route["overview_polyline"]["points"]
    polyline_pts = decode_polyline(encoded)
    sample_pts   = sample_route(polyline_pts, args.interval)

    print(f"\n📍  Sampled {len(sample_pts)} points along the route "
          f"(every ~{args.interval/1000:.1f} km, radius {args.radius} m)")

    # ── 3. Search for fuel stations at each sample point ──────────────────────
    all_places: list[dict] = []
    seen_ids: set[str] = set()

    for i, point in enumerate(sample_pts, 1):
        print(f"   Searching point {i}/{len(sample_pts)} ({point[0]:.4f}, {point[1]:.4f}) …", end="\r")
        results = search_fuel_stations(client, point, args.radius)
        for place in results:
            pid = place.get("place_id", "")
            if pid not in seen_ids:
                seen_ids.add(pid)
                all_places.append(place)
        time.sleep(0.1)   # be polite to the API

    print(f"\n\n⛽  Found {len(all_places)} raw fuel-station results.")

    # ── 4. Deduplicate ────────────────────────────────────────────────────────
    if not args.no_dedup:
        all_places = deduplicate(all_places)
        print(f"✅  After deduplication: {len(all_places)} unique stations.")

    # ── 5. Format & sort by lat (rough route order) ───────────────────────────
    formatted = [format_station(p) for p in all_places]
    formatted.sort(key=lambda s: s["lat"])

    # ── 6. Print summary ──────────────────────────────────────────────────────
    print(f"\n{'─'*60}")
    print(f"  {'#':<4} {'Name':<35} {'Rating':<7} Open?")
    print(f"{'─'*60}")
    for i, s in enumerate(formatted, 1):
        rating  = f"{s['rating']:.1f}★" if s['rating'] else "  N/A"
        open_st = ("Yes" if s['open_now'] else "No") if s['open_now'] is not None else "?"
        print(f"  {i:<4} {s['name'][:34]:<35} {rating:<7} {open_st}")
    print(f"{'─'*60}\n")

    # ── 7. Save JSON ──────────────────────────────────────────────────────────
    output = {
        "route": {
            "start":       args.start,
            "end":         args.end,
            "distance_km": round(total_dist_km, 2),
            "duration":    total_time,
        },
        "settings": {
            "search_radius_m":   args.radius,
            "sample_interval_m": args.interval,
        },
        "total_stations": len(formatted),
        "stations":       formatted,
    }

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"💾  Results saved to: {args.output}")
    print(f"🔗  Open a station on Google Maps: {formatted[0]['maps_url']}\n" if formatted else "")


if __name__ == "__main__":
    main()
