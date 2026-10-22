"""Fake listings for demos and screenshots (python fetch.py --sample).

Homes are placed at random points inside the configured boundaries, with
made-up prices, features, descriptions and placeholder SVG photos.
"""
import random
import sys
import urllib.parse
from datetime import datetime, timedelta

from shapely.geometry import Point

from fetch import features_from_text

STREETS = ["Oak", "Maple", "Hillcrest", "Ridgeview", "Magnolia", "Pine", "Elm", "Cedar", "Willow", "Laurel",
           "Peachtree Ln", "Dogwood", "Briar", "Linden", "Holly", "Juniper", "Sycamore", "Chestnut"]
SUFFIX = ["St", "Ave", "Dr", "Way", "Pl", "Ct", "Cir"]
COLORS = ["#7a9e7e", "#c9a66b", "#8f7aa8", "#6b8fb3", "#b36b6b", "#a8a07a", "#6ba8a0", "#9e7a5f"]

DESCRIPTION_BITS = {
    "office": "A bright home office off the living room makes working from home easy.",
    "basement": "The full basement offers storage and room to grow.",
    "finished_basement": "A finished basement adds a media room and guest space.",
    "garage": "An attached two-car garage keeps the cars out of the weather.",
    "fenced_yard": "Out back, a level fenced yard is ready for dogs and gardens.",
    "deck": "Enjoy evenings on the rear deck overlooking the backyard.",
    "renovated": "Recently renovated kitchen with new appliances and a new roof in 2024.",
    "fixer": "Sold as-is; a great opportunity for an investor or buyer with vision.",
    "porch": "A rocking-chair front porch faces a quiet, tree-lined street.",
}


def photo(color, label, seed):
    """A small placeholder 'photo' as an SVG data URI."""
    r = random.Random(seed)
    roof = r.choice(["#4a3f35", "#3d4a52", "#5a4632"])
    svg = f"""<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 400 300'>
<rect width='400' height='300' fill='#cfe3ef'/><rect y='210' width='400' height='90' fill='#9cc28a'/>
<polygon points='90,130 200,60 310,130' fill='{roof}'/><rect x='110' y='130' width='180' height='100' fill='{color}'/>
<rect x='185' y='170' width='30' height='60' fill='#5b4636'/><rect x='130' y='150' width='35' height='30' fill='#eef6fb'/>
<rect x='235' y='150' width='35' height='30' fill='#eef6fb'/>
<text x='200' y='285' font-family='sans-serif' font-size='18' text-anchor='middle' fill='#2d3b2d'>{label}</text></svg>"""
    return "data:image/svg+xml," + urllib.parse.quote(svg)


def random_point(geom, rng):
    minx, miny, maxx, maxy = geom.bounds
    for _ in range(1000):
        p = Point(rng.uniform(minx, maxx), rng.uniform(miny, maxy))
        if geom.contains(p):
            return p
    return geom.representative_point()


def make_listings(cfg, shapes, now, n=90, seed=7):
    if not shapes:
        sys.exit("--sample needs at least one boundary in config.yaml (area.boundaries).")
    rng = random.Random(seed)
    search = cfg["search"]
    now_dt = datetime.fromisoformat(now)
    listings = []
    for i in range(n):
        props, geom = rng.choice(shapes)
        pt = random_point(geom, rng)
        by_county = props.get("tax_area_by_county") or {}
        tax_area = props.get("tax_area") or by_county.get("default")
        status = rng.choices(["FOR_SALE", "PENDING", "SOLD"], [0.5, 0.1, 0.4])[0]
        style = rng.choices(["SINGLE_FAMILY", "TOWNHOMES", "CONDOS"], [0.6, 0.3, 0.1])[0]
        beds = rng.choice([2, 2, 3, 3, 3, 4, 4, 5])
        full, half = rng.choice([(1, 1), (2, 0), (2, 1), (3, 0), (3, 1)])
        sqft = int(rng.gauss(650 + beds * 380, 250))
        cap = search.get("sold_price_max", search["price_max"]) if status == "SOLD" else search["price_max"]
        price = int(min(cap, max(180_000, sqft * rng.uniform(190, 330))) // 500 * 500)
        lot = rng.choice([436, 1307, 1742, 4356, 5663, 7405, 8712, 11761]) if style != "SINGLE_FAMILY" or rng.random() < 0.15 \
            else rng.choice([5227, 6534, 7841, 9583, 12632, 16553])
        traits = [k for k, p in [("office", 0.4), ("basement", 0.3), ("garage", 0.45), ("fenced_yard", 0.35), ("deck", 0.5),
                                 ("renovated", 0.4), ("fixer", 0.08), ("porch", 0.5)] if rng.random() < p]
        if "basement" in traits and rng.random() < 0.5:
            traits.append("finished_basement")
        text = f"Welcome to this {beds}-bedroom {'townhouse' if style != 'SINGLE_FAMILY' else 'home'} in {props['name']}. " + \
               " ".join(DESCRIPTION_BITS[t] for t in traits)
        sold = now_dt - timedelta(days=rng.randint(1, search.get("sold_past_days", 180)))
        listed = sold - timedelta(days=rng.randint(10, 60)) if status == "SOLD" else now_dt - timedelta(days=rng.randint(0, 120))
        list_price = price if status != "SOLD" else int(price * rng.uniform(0.97, 1.05) // 500 * 500)
        dropped = status == "FOR_SALE" and rng.random() < 0.2
        color = COLORS[i % len(COLORS)]
        photos = [photo(color, f"Sample home {i + 1} · {label}", i * 10 + k)
                  for k, label in enumerate(["Front", "Living room", "Kitchen", "Backyard"])]
        listings.append({
            "id": f"sample-{i + 1}",
            "url": "https://example.com/",
            "status": status,
            "address": f"{rng.randint(100, 1999)} {rng.choice(STREETS)} {rng.choice(SUFFIX)}, Sample City",
            "neighborhood": props["name"],
            "county": None,
            "tax_area": tax_area,
            "zip": "00000",
            "price": price,
            "list_price": list_price,
            "original_price": price + 15_000 if dropped else price,
            "price_drop": 15_000 if dropped else 0,
            "beds": beds,
            "full_baths": full,
            "half_baths": half,
            "baths": full + 0.5 * half,
            "sqft": sqft,
            "lot_sqft": lot,
            "year_built": rng.randint(1920, 2022),
            "style": style,
            "stories": rng.choice([1, 2, 2, 3]),
            "hoa_fee": rng.choice([100, 150, 225]) if style != "SINGLE_FAMILY" else None,
            "garage_spaces": rng.choice([1, 2]) if "garage" in traits else None,
            "price_per_sqft": round(price / sqft),
            "estimated_value": int(price * rng.uniform(0.95, 1.05)),
            "days_on_mls": (now_dt - listed).days,
            "list_date": listed.isoformat(),
            "pending_date": None,
            "sold_date": sold.isoformat() if status == "SOLD" else None,
            "lat": pt.y,
            "lon": pt.x,
            "photo": photos[0],
            "photos": photos,
            "description": text,
            "agent": "Sample Agent",
            "broker": "Sample Realty",
            "first_seen": now,
            "features": features_from_text(text, 2 if "garage" in traits else None),
        })
    return listings
