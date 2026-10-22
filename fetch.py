"""Pull listings from Realtor.com (via HomeHarvest) into data/ for index.html.

    python fetch.py                # fetch real listings using config.yaml
    python fetch.py --sample       # write fake listings instead (demos, screenshots)
    python fetch.py --config-only  # just re-apply config.yaml to the page

Then open index.html in a browser.
"""
import argparse
import hashlib
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yaml
from shapely.geometry import Point, shape

ROOT = Path(__file__).parent
DATA = ROOT / "data"
BOUNDARIES = DATA / "boundaries.geojson"
HISTORY = DATA / "history.json"
OUTPUT = DATA / "listings.js"
BOUNDARIES_JS = DATA / "boundaries.js"
CONFIG_JS = DATA / "config.js"

TIGER_URL = "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/Places_CouSub_ConCity_SubMCD/MapServer/{layer}/query"
TIGER_PLACE_LAYERS = (4, 5)  # incorporated places, census designated places


def load_config():
    path = ROOT / "config.yaml"
    if not path.exists():
        path = ROOT / "config.example.yaml"
        print("No config.yaml found; using config.example.yaml. Copy it to config.yaml to customize.")
    return yaml.safe_load(path.read_text())


def get_json(url, params):
    with urllib.request.urlopen(f"{url}?{urllib.parse.urlencode(params)}", timeout=60) as r:
        return json.load(r)


def sql_list(names):
    return ",".join("'" + n.replace("'", "''") + "'" for n in names)


# ---------- boundaries ----------

def fetch_source(src):
    """Return [(name, geometry)] for one boundary source in the config."""
    kind, names = src["type"], src.get("names")
    if kind == "arcgis":
        field = src.get("name_field", "NAME")
        where = f"{field} IN ({sql_list(names)})" if names else "1=1"
        res = get_json(src["url"].rstrip("/") + "/query", {"where": where, "outFields": field, "outSR": 4326, "f": "geojson"})
        return [(f["properties"][field], f["geometry"]) for f in res.get("features", [])]
    if kind == "census_place":
        out = []
        for layer in TIGER_PLACE_LAYERS:
            res = get_json(TIGER_URL.format(layer=layer), {
                "where": f"STATE='{src['state_fips']}' AND BASENAME IN ({sql_list(names)})",
                "outFields": "BASENAME", "outSR": 4326, "f": "geojson"})
            out += [(f["properties"]["BASENAME"], f["geometry"]) for f in res.get("features", [])]
        return out
    if kind == "geojson":
        gj = json.loads((ROOT / src["path"]).read_text())
        field = src.get("name_field", "name")
        return [(f["properties"][field], f["geometry"]) for f in gj["features"]
                if not names or f["properties"][field] in names]
    sys.exit(f"Unknown boundary type: {kind!r}")


def build_boundaries(sources, config_hash):
    features = []
    for src in sources:
        found = fetch_source(src)
        missing = set(src.get("names") or []) - {n for n, _ in found}
        if missing:
            sys.exit(f"{src.get('label', src['type'])}: no boundary found for {sorted(missing)}")
        for name, geom in found:
            features.append({"type": "Feature", "geometry": geom, "properties": {
                "name": name, "source": src.get("label", src["type"]),
                "tax_area": src.get("tax_area"), "tax_area_by_county": src.get("tax_area_by_county")}})
    BOUNDARIES.write_text(json.dumps({"type": "FeatureCollection", "config_hash": config_hash, "features": features}))


def load_shapes(cfg):
    sources = cfg["area"].get("boundaries") or []
    if not sources:
        BOUNDARIES.write_text(json.dumps({"type": "FeatureCollection", "features": []}))
        return []
    config_hash = hashlib.sha1(json.dumps(sources, sort_keys=True).encode()).hexdigest()
    cached = json.loads(BOUNDARIES.read_text()) if BOUNDARIES.exists() else {}
    if cached.get("config_hash") != config_hash:
        print("Downloading boundaries...")
        build_boundaries(sources, config_hash)
    gj = json.loads(BOUNDARIES.read_text())
    return [(f["properties"], shape(f["geometry"])) for f in gj["features"]]


def locate(lat, lon, zip_code, county, shapes, cfg):
    """Return (neighborhood, tax_area), or (None, None) if outside every boundary."""
    if not shapes:
        return f"ZIP {zip_code}", cfg["area"].get("default_tax_area")
    if pd.isna(lat) or pd.isna(lon):
        return None, None
    p = Point(lon, lat)
    for props, geom in shapes:
        if geom.contains(p):
            by_county = props.get("tax_area_by_county") or {}
            return props["name"], props.get("tax_area") or by_county.get(county, by_county.get("default"))
    return None, None


# ---------- listings ----------

def scrape(cfg, listing_type, **kw):
    from homeharvest import scrape_property  # imported here so --sample works without network access
    s = cfg["search"]
    frames = []
    for z in cfg["area"]["zips"]:
        for attempt in range(3):
            try:
                # Pass one listing type at a time: a list silently drops for-sale homes.
                df = scrape_property(location=str(z), listing_type=listing_type, property_type=s["property_types"],
                                     beds_min=s.get("beds_min"), baths_min=s.get("baths_min"), **kw)
                frames.append(df)
                print(f"  {listing_type:8} {z}: {len(df)}")
                break
            except Exception as e:  # network hiccups / rate limits
                print(f"  {listing_type:8} {z}: error {e!r}, retrying")
                time.sleep(5 * (attempt + 1))
        time.sleep(1)
    frames = [f for f in frames if len(f)]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def has(pattern, text):
    return bool(re.search(pattern, text or "", re.I))


def features_from_text(text, garage_spaces):
    t = text or ""
    basement = has(r"\bbasement\b", t) and not has(r"\bno basement\b", t)
    garage = (garage_spaces or 0) > 0 or (has(r"\bgarage\b", t) and not has(r"\bno garage\b", t))
    return {
        "office": has(r"\b(home office|office|study|den|flex (room|space)|bonus room)\b", t),
        "basement": basement,
        "finished_basement": has(r"finished (daylight |terrace |walk[- ]?out )?basement|basement (is )?finished", t)
                             and not has(r"unfinished basement", t),
        "garage": garage,
        "carport": has(r"\bcarport\b", t),
        "fenced_yard": has(r"fenced[\w\s,-]{0,25}\b(yard|backyard|lot)\b|\b(yard|backyard)\b[\w\s,-]{0,15}\bfenced|fully fenced|privacy fence", t),
        "yard_mention": has(r"\b(back ?yard|yard|garden|deck|patio|lawn)\b", t),
        "renovated": has(r"renovat|remodel|updated|new roof|new hvac", t),
        "fixer": has(r"as[- ]is|investor|tlc|fixer|needs work|cash only", t),
    }


def clean(v):
    if v is None:
        return None
    if isinstance(v, float) and pd.isna(v):
        return None
    if isinstance(v, (pd.Timestamp, datetime)):
        return v.isoformat()
    if hasattr(v, "item"):
        return v.item()
    return v


def fetch_listings(cfg, shapes, now):
    s = cfg["search"]
    print("Fetching for-sale (includes pending/contingent)...")
    active = scrape(cfg, "for_sale", price_max=s["price_max"])
    print("Fetching sold comps...")
    sold = scrape(cfg, "sold", price_max=s.get("sold_price_max", s["price_max"]), past_days=s.get("sold_past_days", 180))
    df = pd.concat([d for d in (active, sold) if len(d)], ignore_index=True)
    if df.empty:
        sys.exit("No listings returned; leaving existing data untouched.")
    df = df.drop_duplicates(subset="property_id", keep="first")
    # Homes listed in two MLSes show up twice, often once with a unit number.
    street = df["street"].fillna(df["formatted_address"]).str.lower().str.replace(r"\s+(unit|apt|#)\s*\S+$", "", regex=True)
    df = (df.assign(_k=street + "|" + df["zip_code"].astype(str) + "|" + df["status"].astype(str) + "|"
                    + df["list_price"].astype(str) + "|" + df["beds"].astype(str))
            .sort_values("sqft", ascending=False).drop_duplicates(subset="_k").drop(columns="_k"))

    history = json.loads(HISTORY.read_text()) if HISTORY.exists() else {}
    listings = []
    for r in df.to_dict("records"):
        county = clean(r.get("county"))
        hood, tax_area = locate(r.get("latitude"), r.get("longitude"), r.get("zip_code"), county, shapes, cfg)
        if not hood:
            continue
        pid = str(r["property_id"])
        status = (r.get("status") or "").upper()
        price = clean(r.get("sold_price")) if status == "SOLD" else clean(r.get("list_price"))
        if status != "SOLD" and price and price > s["price_max"]:
            continue
        h = history.setdefault(pid, {"first_seen": now, "prices": []})
        if price and (not h["prices"] or h["prices"][-1]["price"] != price):
            h["prices"].append({"date": now, "price": price, "status": status})
        h["last_seen"] = now
        h["last_status"] = status
        first_price = next((p["price"] for p in h["prices"] if p["status"] != "SOLD"), price)

        full = clean(r.get("full_baths")) or 0
        half = clean(r.get("half_baths")) or 0
        photos = [p.strip() for p in str(r.get("alt_photos") or "").split(",") if p.strip().startswith("http")]
        text = clean(r.get("text")) or ""
        listings.append({
            "id": pid,
            "url": r.get("property_url"),
            "status": status,
            "address": r.get("formatted_address"),
            "neighborhood": hood,
            "county": county,
            "tax_area": tax_area,
            "zip": r.get("zip_code"),
            "price": price,
            "list_price": clean(r.get("list_price")),
            "original_price": first_price,
            "price_drop": (first_price - price) if (first_price and price and status != "SOLD" and price < first_price) else 0,
            "beds": clean(r.get("beds")),
            "full_baths": full,
            "half_baths": half,
            "baths": full + 0.5 * half,
            "sqft": clean(r.get("sqft")),
            "lot_sqft": clean(r.get("lot_sqft")),
            "year_built": clean(r.get("year_built")),
            "style": r.get("style"),
            "stories": clean(r.get("stories")),
            "hoa_fee": clean(r.get("hoa_fee")),
            "garage_spaces": clean(r.get("parking_garage")),
            "price_per_sqft": clean(r.get("price_per_sqft")),
            "estimated_value": clean(r.get("estimated_value")),
            "days_on_mls": clean(r.get("days_on_mls")),
            "list_date": clean(r.get("list_date")),
            "pending_date": clean(r.get("pending_date")),
            "sold_date": clean(r.get("last_sold_date")) if status == "SOLD" else None,
            "lat": clean(r.get("latitude")),
            "lon": clean(r.get("longitude")),
            "photo": clean(r.get("primary_photo")),
            "photos": photos[:40],
            "description": text,
            "agent": clean(r.get("agent_name")),
            "broker": clean(r.get("broker_name")),
            "first_seen": h["first_seen"],
            "features": features_from_text(text, clean(r.get("parking_garage"))),
        })
    HISTORY.write_text(json.dumps(history, indent=1))
    return listings


# ---------- output ----------

def write_config_js(cfg, sample):
    page_cfg = {
        "title": cfg.get("title", "House Hunter"),
        "criteria": cfg.get("criteria", {}),
        "payment": cfg.get("payment", {}),
        "search": cfg.get("search", {}),
        "map": cfg.get("map", {}),
        "boundary_labels": [b.get("label", b["type"]) for b in cfg["area"].get("boundaries") or []],
        "sample": sample,
    }
    CONFIG_JS.write_text("window.HOUSE_CONFIG = " + json.dumps(page_cfg) + ";\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sample", action="store_true", help="write fake listings instead of fetching real ones")
    ap.add_argument("--config-only", action="store_true", help="only re-apply config.yaml to the page")
    args = ap.parse_args()

    cfg = load_config()
    DATA.mkdir(exist_ok=True)
    if args.config_only:
        sample = CONFIG_JS.exists() and '"sample": true' in CONFIG_JS.read_text()
        write_config_js(cfg, sample)
        print(f"Updated {CONFIG_JS.relative_to(ROOT)}")
        return

    shapes = load_shapes(cfg)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if args.sample:
        from sample import make_listings
        listings = make_listings(cfg, shapes, now)
    else:
        listings = fetch_listings(cfg, shapes, now)

    OUTPUT.write_text("window.HOUSE_DATA = " + json.dumps({"updated": now, "listings": listings}) + ";\n")
    BOUNDARIES_JS.write_text("window.HOUSE_BOUNDARIES = " + BOUNDARIES.read_text() + ";\n")
    write_config_js(cfg, args.sample)
    counts = pd.Series([l["status"] for l in listings]).value_counts().to_dict()
    print(f"Wrote {len(listings)} {'sample ' if args.sample else ''}listings: {counts}")


if __name__ == "__main__":
    main()
