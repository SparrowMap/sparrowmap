"""Place search for the destination box, proxied so no visitor talks to a
third party. Two providers, merged, biased toward where the driver is.

    geocode.search("walmart", near=(41.5, -83.6))     # closest Walmarts first
    geocode.search("350 Fifth Avenue New York")        # exact address

🚨 WHY TWO PROVIDERS. He asked for this to be "as robust as Apple Maps" - type
"walmart" and get the closest ones, type "walmart ohio" and find those. No
single free geocoder does both well:

  * Photon (OSM, komoot) is built for this - point-of-interest search with a
    proximity bias, so "walmart" near a coordinate returns the nearest stores,
    ranked by distance. It is weak on an exact street address.
  * Nominatim (OSM) is the opposite: strong on a full street address, weak and
    slow at "the nearest X".

So both are asked and the results merged, deduped by position. Whichever
understood the query contributes; the driver never sees which.

🚨 PROXIED, AND THAT IS A PRIVACY PROPERTY. The provider sees THIS SERVER's
address and the search term, never the visitor's IP - the same reason tiles and
road lookups are proxied. The proximity point is rounded to ~1 km before it
leaves here, so "near the driver" does not become "the driver's exact spot".

⚠️ RATE. Nominatim's policy is one request a second, and a good neighbour keeps
to it for Photon too. `_space()` enforces a minimum gap PER PROVIDER, so no
burst of searches can hammer either - which is what actually protects the
upstream, far better than the tiny hourly cap that used to (and that only
managed to lock out the search box for the whole site instead).
"""
from __future__ import annotations

import json
import math
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

UA = "SparrowMap/1.0 (https://sparrowmap.com)"

#: Minimum seconds between calls to one provider. Nominatim's published limit
#: is 1/s; this stays just over it and applies the same courtesy to Photon.
_MIN_GAP_S = 1.1
_LAST: dict = {}
_LOCK = threading.Lock()


def _space(provider: str) -> None:
    """Block until at least _MIN_GAP_S has passed since this provider was last
    called, so concurrent searches queue rather than burst. Bounded: nobody
    waits more than the gap."""
    with _LOCK:
        wait = _MIN_GAP_S - (time.time() - _LAST.get(provider, 0.0))
        if wait > 0:
            time.sleep(min(wait, _MIN_GAP_S))
        _LAST[provider] = time.time()


def _get(url: str, timeout: float = 10.0):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def _km(a, b) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = (math.sin((la2 - la1) / 2) ** 2
         + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2)
    return 2 * 6371 * math.asin(math.sqrt(h))


def _photon(term: str, near, limit: int) -> list:
    p = {"q": term, "limit": str(limit), "lang": "en"}
    if near:
        p["lat"], p["lon"] = f"{near[0]:.4f}", f"{near[1]:.4f}"
    _space("photon")
    d = _get("https://photon.komoot.io/api/?" + urllib.parse.urlencode(p))
    out = []
    for f in d.get("features", []):
        g = (f.get("geometry") or {}).get("coordinates") or []
        pr = f.get("properties") or {}
        if len(g) < 2:
            continue
        head = pr.get("name") or " ".join(
            x for x in (pr.get("housenumber"), pr.get("street")) if x)
        tail = ", ".join(x for x in (pr.get("city") or pr.get("county"),
                                     pr.get("state"), pr.get("countrycode"))
                         if x and x != "US")
        label = ", ".join(x for x in (head, tail) if x) or pr.get("street") or ""
        if not label:
            continue
        out.append({"name": label[:140], "lat": float(g[1]), "lon": float(g[0]),
                    "kind": str(pr.get("osm_value") or pr.get("type") or "")})
    return out


def _nominatim(term: str, near, limit: int) -> list:
    p = {"format": "json", "limit": str(limit), "q": term, "addressdetails": "0"}
    if near:
        # A viewbox around the point as a BIAS (bounded=0), so an address still
        # resolves anywhere but nearby matches float up.
        dlat, dlon = 0.7, 0.9
        p["viewbox"] = (f"{near[1] - dlon},{near[0] + dlat},"
                        f"{near[1] + dlon},{near[0] - dlat}")
    _space("nominatim")
    raw = _get("https://nominatim.openstreetmap.org/search?"
               + urllib.parse.urlencode(p))
    out = []
    for x in raw if isinstance(raw, list) else []:
        if not (x.get("lat") and x.get("lon")):
            continue
        out.append({"name": str(x.get("display_name") or "")[:140],
                    "lat": float(x["lat"]), "lon": float(x["lon"]),
                    "kind": str(x.get("type") or "")})
    return out


def search(term: str, near=None, limit: int = 6) -> list:
    """Merged, deduped, proximity-sorted place results, or [] on total failure.

    Both providers are tried; if one raises (network, bad JSON) the other still
    answers, so search degrades rather than dying. Nothing here logs the term.
    """
    term = (term or "").strip()
    if len(term) < 3:
        return []
    if near:
        try:
            near = (round(float(near[0]), 2), round(float(near[1]), 2))
        except (TypeError, ValueError):
            near = None

    # A query that starts with a house number is an ADDRESS, and Nominatim
    # parses those far better than Photon (which fuzzy-matched "350 Fifth Ave"
    # to "650 Fifth Ave"). Everything else is a place or a name, where Photon's
    # proximity search wins. With a proximity point the final sort is by
    # distance anyway, so this only decides ties and the no-location order.
    address_like = term[:1].isdigit()
    providers = (_nominatim, _photon) if address_like else (_photon, _nominatim)
    results, errs = [], 0
    for fn in providers:
        try:
            results += fn(term, near, limit + 2)
        except (urllib.error.URLError, OSError, ValueError, TypeError) as exc:
            errs += 1
            print(f"[geocode] {fn.__name__} failed: {exc}")
    if not results:
        # No results AND both providers erred is an OUTAGE - the caller must
        # know, so it does not tell a driver the address does not exist. No
        # results with no error is a genuine "nothing found": return empty.
        if errs >= 2:
            raise RuntimeError("all geocoders failed")
        return []

    # Dedupe by ~11 m position, keep the first (richer) label for a spot.
    seen, merged = set(), []
    for r in results:
        k = (round(r["lat"], 4), round(r["lon"], 4))
        if k in seen:
            continue
        seen.add(k)
        merged.append(r)

    if near:
        merged.sort(key=lambda r: _km(near, (r["lat"], r["lon"])))
    return merged[:limit]
