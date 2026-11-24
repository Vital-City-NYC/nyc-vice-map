#!/usr/bin/env python3
"""Build merged GeoJSON + CSV of New York City liquor stores, beer-licensed groceries,
legal cannabis dispensaries and licensed tobacco / e-cigarette retailers.

Sources (all public, no auth):
  - NYS Liquor Authority "Current Liquor Authority Active Licenses" (data.ny.gov, 9s3h-dpkz)
  - NYS Office of Cannabis Management "Current OCM Licenses" (data.ny.gov, jskf-tt3q)
  - NYC DCWP "Issued Licenses" (data.cityofnewyork.us, w7w3-xahh)

Geocoding: NYC Department of City Planning GeoSearch (no key, NYC-tuned), with every
result checked against the input house number and ZIP. GeoSearch only knows New York
City, so for an address outside the city it returns the closest-sounding city address;
an unchecked result can put a Long Island store in the Bronx.

City limits: every record must have a city ZIP code or coordinates inside the borough
boundaries (NYC Department of City Planning, data.cityofnewyork.us gthc-hcne). Records
that fail are dropped and listed in methodology.json.

Outputs:
  docs/data/locations.geojson
  docs/data/locations.csv
  docs/data/methodology.json
"""
from __future__ import annotations

import csv
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
OUT = ROOT / "docs" / "data"
RAW.mkdir(parents=True, exist_ok=True)
OUT.mkdir(parents=True, exist_ok=True)

NYC_COUNTIES = {"New York", "Kings", "Queens", "Bronx", "Richmond"}
COUNTY_TO_BOROUGH = {
    "New York": "Manhattan",
    "Kings": "Brooklyn",
    "Queens": "Queens",
    "Bronx": "Bronx",
    "Richmond": "Staten Island",
}

UA = "nyc-vice-map/1.1 (+https://github.com/vitalcity-nyc/nyc-vice-map)"

# A refresh that loses more than this share of any category is treated as a broken
# pull, not a real change, and the build fails rather than publishing the gap.
MAX_DROP = 0.30


def fetch_json(url: str, timeout: int = 120, tries: int = 3):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except Exception:  # noqa: BLE001
            if attempt == tries - 1:
                raise
            time.sleep(2 * (attempt + 1))


def paginate(base: str, page_size: int = 50000):
    """Yield rows from a Socrata endpoint, paginating with $limit/$offset."""
    offset = 0
    while True:
        sep = "&" if "?" in base else "?"
        url = f"{base}{sep}$order=:id&$limit={page_size}&$offset={offset}"
        batch = fetch_json(url)
        if not batch:
            break
        yield from batch
        if len(batch) < page_size:
            break
        offset += page_size


# ------------------------------- ZIP codes and boroughs ----------------------

# ZIP codes that lie wholly inside the city. Border ZIPs shared with Nassau
# (11001, 11040 and others) are deliberately left out; records there are kept only
# if their coordinates fall inside the borough boundaries.
def zip_borough(z: str) -> str:
    z = (z or "").strip()[:5]
    if not z.isdigit():
        return ""
    n = int(z)
    if 10001 <= n <= 10299:
        return "Manhattan"
    if 10300 <= n <= 10399:
        return "Staten Island"
    if 10400 <= n <= 10499:
        return "Bronx"
    if 11200 <= n <= 11299:
        return "Brooklyn"
    if n in (11004, 11005) or 11100 <= n <= 11199 or 11300 <= n <= 11499 or 11600 <= n <= 11699:
        return "Queens"
    return ""


BOROUGHS_URL = "https://data.cityofnewyork.us/resource/gthc-hcne.geojson"
BOROUGHS_CACHE = RAW / "borough_boundaries.geojson"
_boroughs = None


def load_boroughs():
    global _boroughs
    if _boroughs is not None:
        return _boroughs
    if not BOROUGHS_CACHE.exists():
        data = fetch_json(BOROUGHS_URL)
        BOROUGHS_CACHE.write_text(json.dumps(data))
    data = json.loads(BOROUGHS_CACHE.read_text())
    out = []
    for f in data["features"]:
        name = f["properties"]["boroname"]
        polys = f["geometry"]["coordinates"]
        if f["geometry"]["type"] == "Polygon":
            polys = [polys]
        for poly in polys:
            ring = poly[0]
            xs = [p[0] for p in ring]
            ys = [p[1] for p in ring]
            out.append((name, (min(xs), min(ys), max(xs), max(ys)), poly))
    _boroughs = out
    return out


def _in_ring(x, y, ring):
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


# Shoreline-clipped boundaries miss storefronts on piers and the water's edge, so a
# point within ~100 meters of a borough also counts.
NEAR = 0.0012


def point_borough(lat, lon) -> str:
    for name, (x0, y0, x1, y1), poly in load_boroughs():
        if not (x0 - NEAR <= lon <= x1 + NEAR and y0 - NEAR <= lat <= y1 + NEAR):
            continue
        if _in_ring(lon, lat, poly[0]) and not any(_in_ring(lon, lat, h) for h in poly[1:]):
            return name
    for name, (x0, y0, x1, y1), poly in load_boroughs():
        if not (x0 - NEAR <= lon <= x1 + NEAR and y0 - NEAR <= lat <= y1 + NEAR):
            continue
        for dx, dy in ((NEAR, 0), (-NEAR, 0), (0, NEAR), (0, -NEAR)):
            if _in_ring(lon + dx, lat + dy, poly[0]):
                return name
    return ""


# ------------------------------- SLA liquor stores ---------------------------

SLA_LIQUOR_TYPES = {"Liquor Store", "Wine Store"}
SLA_BEER_TYPES = {"Grocery Store", "Drug Store"}


def _sla_row(r, category):
    geo = r.get("georeference") or {}
    coords = geo.get("coordinates") if geo.get("type") == "Point" else None
    lon, lat = (coords[0], coords[1]) if coords else (None, None)
    street = (r.get("actualaddressofpremises") or "").strip()
    addr_parts = [street, r.get("city", ""), "NY", r.get("zipcode", "")]
    name = (r.get("legalname") or "").strip()
    return {
        "id": f"sla:{r.get('licensepermitid','')}",
        "category": category,
        "subcategory": r.get("description", ""),
        "name": name.title() if name.isupper() or name.islower() else name,
        "address": ", ".join(p for p in addr_parts if p),
        "street": street,
        "city": r.get("city", ""),
        "borough": COUNTY_TO_BOROUGH.get(r.get("premisescounty", ""), ""),
        "zip": (r.get("zipcode") or "")[:5],
        "license_id": r.get("licensepermitid", ""),
        "license_status": "Active",
        "expiration_date": (r.get("expirationdate") or "")[:10],
        "lat": lat,
        "lon": lon,
        "geocode": "SLA georeference" if lat else "",
        "source": "NYS Liquor Authority",
        "source_url": "https://data.ny.gov/d/9s3h-dpkz",
    }


def pull_sla(types, category, label):
    print(f"[{label}] pulling SLA active licenses...", file=sys.stderr)
    type_list = ",".join(f"'{t}'" for t in sorted(types))
    where = (
        f"description in ({type_list}) AND "
        "premisescounty in ('New York','Kings','Queens','Bronx','Richmond')"
    )
    base = (
        "https://data.ny.gov/resource/9s3h-dpkz.json?"
        f"$where={urllib.parse.quote(where)}"
    )
    rows = list(paginate(base))
    print(f"[{label}] {len(rows)} rows", file=sys.stderr)
    return [_sla_row(r, category) for r in rows]


# ------------------------------- OCM cannabis --------------------------------

CANNABIS_RETAIL_TYPES = {
    "Adult-Use Retail Dispensary License",
    "Adult-Use Conditional Retail Dispensary License",
    "Conditional Adult-Use Retail Dispensary License",
    "Adult-Use Microbusiness License",
    "Adult-Use Registered Organization Dispensary License",
}


def pull_cannabis():
    print("[cannabis] pulling OCM licenses...", file=sys.stderr)
    base = "https://data.ny.gov/resource/jskf-tt3q.json"
    rows = list(paginate(base))
    print(f"[cannabis] {len(rows)} total OCM rows", file=sys.stderr)
    out = []
    for r in rows:
        if r.get("license_type") not in CANNABIS_RETAIL_TYPES:
            continue
        if r.get("license_status") != "Active":
            continue
        if r.get("county") not in NYC_COUNTIES:
            continue
        # microbusinesses must have retail activity flagged
        if r.get("license_type") == "Adult-Use Microbusiness License":
            if r.get("retail_activities_sales_with") != "1" and r.get("retail_activities_sales_no") != "1":
                continue
        addr = (r.get("address_line_1") or "").strip()
        addr2 = (r.get("address_line_2") or "").strip()
        city = r.get("city", "")
        zip_ = (r.get("zip_code") or "")[:5]
        full_addr = ", ".join(p for p in [addr, addr2, city, "NY", zip_] if p)
        op = r.get("operational_status", "")
        out.append({
            "id": f"ocm:{r.get('license_number') or r.get('application_number','')}",
            "category": "cannabis",
            "subcategory": r.get("license_type", "").replace(" License", ""),
            "name": (r.get("dba") or r.get("entity_name") or "").strip(),
            "address": full_addr,
            "street": addr,
            "city": city,
            "borough": COUNTY_TO_BOROUGH.get(r.get("county", ""), ""),
            "zip": zip_,
            "license_id": r.get("license_number", ""),
            "license_status": r.get("license_status", ""),
            "operational_status": op,
            "open": op == "Active",
            "opened": (r.get("retail_date_opened_to_public") or "")[:10],
            "expiration_date": (r.get("expiration_date") or "")[:10],
            "lat": None,
            "lon": None,
            "geocode": "",
            "source": "NYS Office of Cannabis Management",
            "source_url": "https://data.ny.gov/d/jskf-tt3q",
        })
    print(f"[cannabis] {len(out)} active NYC retail licenses "
          f"({sum(r['open'] for r in out)} open to the public)", file=sys.stderr)
    return out


# ------------------------------- DCWP tobacco --------------------------------

def _to_float(x):
    try:
        return float(x) if x not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _norm(s):
    return re.sub(r"\s+", " ", (s or "").strip().upper())


def pull_tobacco():
    """Pull DCWP tobacco + e-cig licenses, then merge licenses held by one business
    at one address.

    A single storefront can hold both a Tobacco Retail Dealer and an Electronic
    Cigarette Dealer license, and without merging it would appear twice. The merge
    key is the business plus its full address. An earlier version merged on tax lot
    (BBL) alone, which folded unrelated shops in the same building, mall or terminal
    into one dot.
    """
    print("[tobacco] pulling DCWP licenses...", file=sys.stderr)
    where = (
        "license_status in ('Active','Ready for Renewal') AND "
        "business_category in ('Tobacco Retail Dealer','Electronic Cigarette Dealer')"
    )
    base = (
        "https://data.cityofnewyork.us/resource/w7w3-xahh.json?"
        f"$where={urllib.parse.quote(where)}"
    )
    rows = list(paginate(base))
    print(f"[tobacco] {len(rows)} raw license rows", file=sys.stderr)

    def addr_key(r):
        business = _norm(r.get("business_unique_id")) or _norm(r.get("business_name"))
        return (
            business,
            _norm(r.get("address_building")),
            _norm(r.get("address_street_name")),
            _norm(r.get("address_street_name_2")),
            _norm(r.get("apt_suite")),
            (r.get("address_zip") or "")[:5],
        )

    grouped = defaultdict(list)
    for r in rows:
        grouped[addr_key(r)].append(r)

    out = []
    for group in grouped.values():
        cats = sorted({r.get("business_category", "") for r in group})
        subcategory = "Tobacco + e-cigarette" if len(cats) > 1 else cats[0]

        primary = next(
            (r for r in group if _to_float(r.get("latitude")) and _to_float(r.get("longitude"))),
            group[0],
        )
        lat = _to_float(primary.get("latitude"))
        lon = _to_float(primary.get("longitude"))
        building = (primary.get("address_building") or "").strip()
        street = (primary.get("address_street_name") or "").strip()
        street2 = (primary.get("address_street_name_2") or "").strip()
        if primary.get("address_type", "").startswith("Cross Street") and street2:
            addr_line = f"{street} & {street2}"
        else:
            addr_line = f"{building} {street}".strip()
        suite = (primary.get("apt_suite") or "").strip()
        if suite:
            addr_line = f"{addr_line}, {suite}"
        city = primary.get("address_city", "") or ""
        zip_ = (primary.get("address_zip") or "")[:5]

        license_ids = "; ".join(sorted(r.get("license_nbr", "") for r in group if r.get("license_nbr")))
        statuses = sorted({r.get("license_status", "") for r in group if r.get("license_status")})
        expirations = sorted((r.get("lic_expir_dd") or "")[:10] for r in group if r.get("lic_expir_dd"))

        out.append({
            "id": f"dcwp:{primary.get('license_nbr','')}",
            "category": "tobacco",
            "subcategory": subcategory,
            "name": (primary.get("dba_trade_name") or primary.get("business_name") or "").strip(),
            "address": ", ".join(p for p in [addr_line, city, "NY", zip_] if p),
            "street": f"{building} {street}".strip(),
            "city": city,
            "borough": (primary.get("address_borough") or "").title(),
            "zip": zip_,
            "license_id": license_ids,
            "license_count": len(group),
            "license_status": ", ".join(statuses),
            "expiration_date": expirations[0] if expirations else "",
            "lat": lat,
            "lon": lon,
            "geocode": "DCWP coordinates" if lat else "",
            "source": "NYC Department of Consumer and Worker Protection",
            "source_url": "https://data.cityofnewyork.us/d/w7w3-xahh",
        })
    print(f"[tobacco] {len(out)} storefronts after merging co-held licenses", file=sys.stderr)
    return out


# ------------------------------- Geocoding -----------------------------------

GEOCACHE_PATH = ROOT / "data" / "geocache.json"
GEOCACHE_VERSION = 2


def load_geocache():
    if GEOCACHE_PATH.exists():
        data = json.loads(GEOCACHE_PATH.read_text())
        if isinstance(data, dict) and data.get("_version") == GEOCACHE_VERSION:
            return data
    return {"_version": GEOCACHE_VERSION}


def save_geocache(cache):
    GEOCACHE_PATH.write_text(json.dumps(cache, indent=0, sort_keys=True))


def _digits(s):
    return re.sub(r"\D", "", s or "")


def house_numbers(street, borough):
    """The house numbers a GeoSearch hit may carry for this street address.

    "98-02" or "98 02" in Queens is one hyphenated number; "130-4" means "130-04".
    Elsewhere "204-206" is a range, so either end counts."""
    s = (street or "").upper().strip()
    m = re.match(r"^(\d+)\s*-\s*(\d+)[A-Z]?\b", s) or re.match(r"^(\d{1,3}) (\d{1,3})[A-Z]? \D", s)
    if m:
        a, b = m.group(1), m.group(2)
        if borough == "Queens":
            return {a + b.zfill(2)}
        return {a, b, a + b}
    m = re.match(r"^(\d+)[A-Z]?\b", s)
    return {m.group(1)} if m else set()


def query_variants(street, borough):
    """Queens addresses are hyphenated ("98-02 Jamaica Ave"); the state rolls often
    drop the hyphen ("98 02 JAMAICA AVE", "9802 JAMAICA AVE")."""
    street = re.sub(r"\s+", " ", (street or "").strip())
    variants = [street]
    if borough == "Queens" or not borough:
        m = re.match(r"^(\d{1,3}) (\d{1,3}[A-Z]?) (\D.*)$", street)
        if m:
            variants.insert(0, f"{m.group(1)}-{m.group(2)} {m.group(3)}")
        m = re.match(r"^(\d{2,3})(\d{2}[A-Z]?) (\D.*)$", street)
        if m:
            variants.append(f"{m.group(1)}-{m.group(2)} {m.group(3)}")
    return list(dict.fromkeys(variants))


def geosearch(text, cache):
    key = text.strip().upper()
    if key in cache:
        return cache[key]
    url = "https://geosearch.planninglabs.nyc/v2/search?size=1&text=" + urllib.parse.quote(text)
    try:
        data = fetch_json(url, timeout=30)
        feats = data.get("features") or []
        if feats:
            p = feats[0]["properties"]
            lon, lat = feats[0]["geometry"]["coordinates"]
            cache[key] = {
                "lat": round(lat, 6), "lon": round(lon, 6),
                "housenumber": p.get("housenumber", ""),
                "postalcode": p.get("postalcode", ""),
                "borough": p.get("borough", ""),
                "label": p.get("label", ""),
            }
        else:
            cache[key] = None
    except Exception as e:  # noqa: BLE001
        print(f"  geocode error for {text!r}: {e}", file=sys.stderr)
        return None  # don't cache transient failures
    time.sleep(0.03)
    return cache[key]


def geocode_row(r, cache):
    """Return (lat, lon) only when GeoSearch matched the same house number in the
    same ZIP code. Anything looser is rejected: GeoSearch always answers with its
    nearest guess, and a guess can be miles off."""
    want_nums = house_numbers(r["street"], r["borough"])
    want_zip = r["zip"]
    if not want_nums:
        return None
    for street in query_variants(r["street"], r["borough"]):
        hit = geosearch(f"{street}, {r['city']}, NY {want_zip}", cache)
        if not hit:
            continue
        if _digits(hit["housenumber"]) in want_nums and (not want_zip or hit["postalcode"] == want_zip):
            return hit["lat"], hit["lon"]
    return None


def geocode_rows(rows):
    cache = load_geocache()

    # Coordinates outside the region (DCWP publishes some as 0, 0) count as missing.
    for r in rows:
        if r["lat"] is not None and not (40.4 < r["lat"] < 41.0 and -74.3 < r["lon"] < -73.6):
            r["lat"] = r["lon"] = None
            r["geocode"] = ""

    # 1. Records with no coordinates (all OCM cannabis licenses).
    needed = [r for r in rows if r["lat"] is None or r["lon"] is None]

    # 2. State Liquor Authority records whose georeference is shared with a
    # different address. These are mostly Queens addresses the state geocoder
    # could not parse, piled onto one point per street.
    by_point = defaultdict(set)
    for r in rows:
        if r["source"].startswith("NYS Liquor") and r["lat"] is not None:
            by_point[(round(r["lat"], 5), round(r["lon"], 5))].add(_norm(r["street"]))
    stacked = [
        r for r in rows
        if r["source"].startswith("NYS Liquor") and r["lat"] is not None
        and len(by_point[(round(r["lat"], 5), round(r["lon"], 5))]) > 1
    ]
    print(f"[geocode] {len(needed)} records without coordinates; "
          f"{len(stacked)} liquor-authority records stacked on a shared point", file=sys.stderr)

    hits = fixed = 0
    for i, r in enumerate(needed + stacked, 1):
        res = geocode_row(r, cache)
        if res:
            if r["lat"] is None:
                hits += 1
            else:
                fixed += 1
            r["lat"], r["lon"] = res
            r["geocode"] = "GeoSearch (house number and ZIP verified)"
        if i % 100 == 0:
            save_geocache(cache)
    save_geocache(cache)
    print(f"[geocode] placed {hits}/{len(needed)} records; re-placed {fixed}/{len(stacked)} stacked records",
          file=sys.stderr)
    return {"needed": len(needed), "placed": hits, "stacked": len(stacked), "replaced": fixed}


# ------------------------------- City limits ---------------------------------

def apply_city_limits(rows):
    """Keep a record if its ZIP is wholly inside the city or its point is inside a
    borough. The borough comes from the same test: the liquor authority's county
    field is wrong for a few dozen records (a Brooklyn deli filed under Manhattan),
    while the ZIP code and the point nearly always agree."""
    kept, dropped = [], []
    for r in rows:
        zb = zip_borough(r["zip"])
        pb = point_borough(r["lat"], r["lon"]) if r["lat"] is not None and not zb else ""
        if zb or pb:
            label = r["borough"]
            if zb and label and zb != label and r["lat"] is not None and point_borough(r["lat"], r["lon"]) == label:
                pass  # a mistyped ZIP; the label and the point agree
            else:
                r["borough"] = zb or pb
            kept.append(r)
        else:
            dropped.append(r)
    return kept, dropped


# ------------------------------- Output --------------------------------------

CSV_FIELDS = [
    "id", "category", "subcategory", "name", "address", "borough", "zip",
    "license_id", "license_count", "license_status", "operational_status", "opened",
    "expiration_date", "lat", "lon", "geocode", "source", "source_url",
]

GEOJSON_FIELDS = [
    "id", "category", "subcategory", "name", "address", "borough", "zip",
    "license_id", "license_count", "license_status", "operational_status", "open",
    "opened", "expiration_date",
]


def sanity_check(counts):
    prev_path = OUT / "methodology.json"
    if not prev_path.exists():
        return
    prev = json.loads(prev_path.read_text()).get("counts", {})
    problems = []
    for k, v in counts.items():
        p = prev.get(k)
        if v == 0:
            problems.append(f"{k}: 0 records")
        elif p and v < p * (1 - MAX_DROP):
            problems.append(f"{k}: {v} records, down from {p}")
    if problems:
        print("[sanity] refusing to publish; a source looks broken:\n  " + "\n  ".join(problems), file=sys.stderr)
        sys.exit(1)


def write_outputs(rows, counts, extra):
    mapped = [r for r in rows if r.get("lat") is not None and r.get("lon") is not None]
    geo = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [round(r["lon"], 6), round(r["lat"], 6)]},
                "properties": {k: r[k] for k in GEOJSON_FIELDS if r.get(k) not in (None, "")},
            }
            for r in mapped
        ],
    }
    (OUT / "locations.geojson").write_text(json.dumps(geo, separators=(",", ":")))
    print(f"[write] {len(geo['features'])} features -> locations.geojson", file=sys.stderr)

    with (OUT / "locations.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    print("[write] locations.csv", file=sys.stderr)

    methodology = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "counts": counts,
        "mapped_features": len(geo["features"]),
        **extra,
        "sources": [
            {
                "category": "Liquor stores",
                "name": "Current Liquor Authority Active Licenses",
                "agency": "New York State Liquor Authority",
                "endpoint": "https://data.ny.gov/d/9s3h-dpkz",
                "filter": "description IN ('Liquor Store','Wine Store') AND county in 5 NYC boroughs",
                "geocoding": "Built-in georeference field; records stacked on a shared point re-geocoded with GeoSearch",
            },
            {
                "category": "Beer (groceries / delis / drug stores)",
                "name": "Current Liquor Authority Active Licenses",
                "agency": "New York State Liquor Authority",
                "endpoint": "https://data.ny.gov/d/9s3h-dpkz",
                "filter": "description IN ('Grocery Store','Drug Store') AND county in 5 NYC boroughs",
                "geocoding": "Built-in georeference field; records stacked on a shared point re-geocoded with GeoSearch",
            },
            {
                "category": "Legal cannabis dispensaries",
                "name": "Current OCM Licenses",
                "agency": "New York State Office of Cannabis Management",
                "endpoint": "https://data.ny.gov/d/jskf-tt3q",
                "filter": "license_type in adult-use retail/microbusiness/RO dispensary types AND license_status='Active' AND county in 5 NYC boroughs; operational_status='Active' marks a dispensary open to the public",
                "geocoding": "NYC DCP GeoSearch, accepted only when house number and ZIP match",
            },
            {
                "category": "Tobacco / e-cigarette retailers",
                "name": "Issued Licenses",
                "agency": "NYC Department of Consumer and Worker Protection",
                "endpoint": "https://data.cityofnewyork.us/d/w7w3-xahh",
                "filter": "business_category IN ('Tobacco Retail Dealer','Electronic Cigarette Dealer') AND license_status in ('Active','Ready for Renewal'); licenses held by one business at one address merged",
                "geocoding": "Built-in latitude/longitude fields",
            },
        ],
    }
    (OUT / "methodology.json").write_text(json.dumps(methodology, indent=2))
    print("[write] methodology.json", file=sys.stderr)


def main():
    liquor = pull_sla(SLA_LIQUOR_TYPES, "liquor", "liquor")
    beer = pull_sla(SLA_BEER_TYPES, "beer", "beer")
    cannabis = pull_cannabis()
    tobacco = pull_tobacco()

    all_rows = liquor + beer + cannabis + tobacco
    geo_stats = geocode_rows(all_rows)
    rows, dropped = apply_city_limits(all_rows)
    for r in dropped:
        print(f"  [outside city] {r['category']}: {r['name']} | {r['address']}", file=sys.stderr)

    by_cat = Counter(r["category"] for r in rows)
    counts = {
        "liquor": by_cat["liquor"],
        "beer": by_cat["beer"],
        "cannabis": by_cat["cannabis"],
        "cannabis_open": sum(1 for r in rows if r["category"] == "cannabis" and r.get("open")),
        "tobacco": by_cat["tobacco"],
        "total": len(rows),
    }
    sanity_check(counts)
    unmapped = [r for r in rows if r["lat"] is None]
    extra = {
        "geocoding": geo_stats,
        "unmapped": [
            {"category": r["category"], "name": r["name"], "address": r["address"]} for r in unmapped
        ],
        "excluded_outside_city": [
            {"category": r["category"], "name": r["name"], "address": r["address"]} for r in dropped
        ],
    }
    write_outputs(rows, counts, extra)
    print(json.dumps(counts, indent=2))


if __name__ == "__main__":
    main()
