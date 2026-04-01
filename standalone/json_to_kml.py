import json
import argparse
import os

def escape_xml(text):
    if not text:
        return ''
    return str(text).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')

def style_id(station):
    open_now = station.get('open_now')
    if open_now is True:
        return 'open'
    elif open_now is False:
        return 'closed'
    return 'unknown'

def to_placemark(s):
    open_now = s.get('open_now')
    status = 'Open' if open_now is True else ('Closed' if open_now is False else 'Unknown')
    rating = s['rating'] if s.get('rating') is not None else 'N/A'
    desc = (
        f"<b>Address:</b> {escape_xml(s.get('address', ''))}<br/>"
        f"<b>Rating:</b> {rating}<br/>"
        f"<b>Status:</b> {status}<br/>"
        f'<a href="{s.get("maps_url", "")}">Open in Google Maps</a>'
    )
    return f"""\t\t<Placemark>
\t\t\t<name>{escape_xml(s['name'])}</name>
\t\t\t<description><![CDATA[{desc}]]></description>
\t\t\t<styleUrl>#{style_id(s)}</styleUrl>
\t\t\t<Point>
\t\t\t\t<coordinates>{s['lng']},{s['lat']},0</coordinates>
\t\t\t</Point>
\t\t</Placemark>"""

parser = argparse.ArgumentParser(description='Convert petrol pump JSON file(s) to KML.')
parser.add_argument('inputs', nargs='+', metavar='input.json', help='One or more JSON input files')
parser.add_argument('-o', '--output', default='petrol_pumps.kml', help='Output KML file (default: petrol_pumps.kml)')
args = parser.parse_args()

# Load and merge all input files, deduplicate by place_id
seen = set()
stations = []
for path in args.inputs:
    with open(path, encoding='utf-8') as f:
        data = json.load(f)
    for s in data['stations']:
        if s['place_id'] not in seen:
            seen.add(s['place_id'])
            stations.append(s)

placemarks = '\n'.join(to_placemark(s) for s in stations)

kml = f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
\t<Document>
\t\t<name>Petrol Pumps along Route</name>
\t\t<description>Rohan Mithila A-Y → Harihareshwar Temple ({len(stations)} stations)</description>

\t\t<Style id="open">
\t\t\t<IconStyle>
\t\t\t\t<color>ff00bb00</color>
\t\t\t\t<scale>1.1</scale>
\t\t\t</IconStyle>
\t\t</Style>
\t\t<Style id="closed">
\t\t\t<IconStyle>
\t\t\t\t<color>ff0000cc</color>
\t\t\t\t<scale>1.1</scale>
\t\t\t</IconStyle>
\t\t</Style>
\t\t<Style id="unknown">
\t\t\t<IconStyle>
\t\t\t\t<color>ff00aaff</color>
\t\t\t\t<scale>1.1</scale>
\t\t\t</IconStyle>
\t\t</Style>

{placemarks}
\t</Document>
</kml>"""

with open(args.output, 'w', encoding='utf-8') as f:
    f.write(kml)

print(f"Done: {len(stations)} stations written to {args.output}")
open_count = sum(1 for s in stations if s.get('open_now') is True)
closed_count = sum(1 for s in stations if s.get('open_now') is False)
unknown_count = sum(1 for s in stations if s.get('open_now') is None)
print(f"  Green (open):   {open_count}")
print(f"  Red (closed):   {closed_count}")
print(f"  Orange (unknown): {unknown_count}")
