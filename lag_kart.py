"""Lagar kart.json (SVG-kart over Stord kommune) éin gong.

Kjelder: kommunegrense frå Kartverket (kommuneinfo), kystlinje frå
OpenStreetMap (Overpass, © OpenStreetMap-bidragsytarar, ODbL),
stadnamn frå Kartverket (stedsnavn).
"""
import json
import math
import urllib.parse
import urllib.request
from pathlib import Path

from shapely.geometry import LineString, MultiPolygon, Polygon, box, shape
from shapely.ops import linemerge, polygonize, unary_union

MAPPE = Path(__file__).parent
UA = {"User-Agent": "brannlogg-stord/1.0 (privat app)"}
BREIDD = 1000
STADER = ["Leirvik", "Sagvåg", "Heiane", "Litlabø", "Huglo", "Føyno", "Stord lufthamn"]
NABOAR = {"Fitjar": (5.34, 59.905), "Bømlo": (5.25, 59.765), "Tysnes": (5.55, 59.935)}


def hent(url, data=None):
    req = urllib.request.Request(url, data=data, headers=UA)
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


def poly_frå_osm(elements):
    ut = []
    for e in elements:
        if e["type"] == "way" and "geometry" in e:
            pts = [(p["lon"], p["lat"]) for p in e["geometry"]]
            if len(pts) > 3 and pts[0] == pts[-1]:
                p = Polygon(pts)
                ut.append(p if p.is_valid else p.buffer(0))
        elif e["type"] == "relation":
            linjer = [LineString([(p["lon"], p["lat"]) for p in m["geometry"]])
                      for m in e.get("members", [])
                      if m["type"] == "way" and m.get("role") == "outer" and "geometry" in m]
            ps = list(polygonize(linemerge(linjer))) if linjer else []
            if ps:
                ut.append(unary_union(ps))
    return ut


def main():
    kom = shape(hent("https://ws.geonorge.no/kommuneinfo/v1/kommuner/4614/omrade")["omrade"])
    q = ('[out:json][timeout:90];(way["place"~"island|islet"](59.66,5.15,59.97,5.78);'
         'relation["place"="island"](59.66,5.15,59.97,5.78););out geom;')
    osm = hent("https://overpass-api.de/api/interpreter",
               urllib.parse.urlencode({"data": q}).encode())
    land = unary_union(poly_frå_osm(osm["elements"]))
    land_kom = land.intersection(kom)

    x0, y0, x1, y1 = land_kom.bounds
    x0, x1, y0, y1 = x0 - 0.035, x1 + 0.03, y0 - 0.012, y1 + 0.016
    visning = box(x0, y0, x1, y1)
    kontekst = land.difference(kom).intersection(visning)

    cosf = math.cos(math.radians((y0 + y1) / 2))
    k = BREIDD / ((x1 - x0) * cosf)
    hogd = round((y1 - y0) * k)

    def proj(lon, lat):
        return (lon - x0) * cosf * k, (y1 - lat) * k

    def til_sti(geom, toleranse, min_areal):
        geom = geom.simplify(toleranse, preserve_topology=True)
        polys = [geom] if isinstance(geom, Polygon) else list(getattr(geom, "geoms", []))
        deler = []
        for p in polys:
            if not isinstance(p, Polygon) or p.area < min_areal:
                continue
            for ring in [p.exterior, *p.interiors]:
                pts = [proj(*c) for c in ring.coords]
                deler.append("M" + "L".join(f"{a:.1f},{b:.1f}" for a, b in pts) + "Z")
        return "".join(deler)

    stader = []
    for namn in STADER:
        try:
            r = hent("https://ws.geonorge.no/stedsnavn/v1/navn?" + urllib.parse.urlencode(
                {"sok": namn, "knr": "4614", "treffPerSide": 1, "utkoordsys": 4258}))
            p = r["navn"][0]["representasjonspunkt"]
            x, y = proj(p["øst"], p["nord"])
            stader.append({"namn": namn, "x": round(x, 1), "y": round(y, 1)})
        except Exception as feil:
            print("fann ikkje", namn, feil)
    naboar = []
    for namn, (lon, lat) in NABOAR.items():
        x, y = proj(lon, lat)
        if 0 < x < BREIDD and 0 < y < hogd:
            naboar.append({"namn": namn, "x": round(x, 1), "y": round(y, 1)})

    kart = {
        "w": BREIDD, "h": hogd,
        "proj": {"lon0": x0, "lat1": y1, "cos": cosf, "k": k},
        "kmPx": round(k / 111.32, 3),
        "land": til_sti(land_kom, 0.00035, 2e-7),
        "kontekst": til_sti(kontekst, 0.0007, 4e-6),
        "stader": stader,
        "naboar": naboar,
    }
    (MAPPE / "kart.json").write_text(json.dumps(kart, ensure_ascii=False), encoding="utf-8")
    print(f"kart.json: {BREIDD}x{hogd}, land {len(kart['land'])} teikn, kontekst {len(kart['kontekst'])} teikn")


if __name__ == "__main__":
    main()
