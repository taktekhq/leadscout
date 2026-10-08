"""Find businesses from OpenStreetMap via the Overpass API.

No API key needed. Be polite: one query per run, a real User-Agent, and a
server-side timeout. See https://wiki.openstreetmap.org/wiki/Overpass_API
and https://operations.osmfoundation.org/policies/nominatim/ for the usage
policies this module follows.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from . import DEFAULT_USER_AGENT
from .httpclient import fetch

# Override with LEADSCOUT_OVERPASS to use a mirror (e.g. https://overpass.kumi.systems/api/interpreter).
OVERPASS_ENDPOINT = os.environ.get("LEADSCOUT_OVERPASS", "https://overpass-api.de/api/interpreter")
NOMINATIM_ENDPOINT = "https://nominatim.openstreetmap.org/search"

_TAG_RE = re.compile(r"^([a-zA-Z0-9_:]+)(=(.*))?$")


@dataclass
class Tag:
    key: str
    value: Optional[str] = None  # None means "key present, any value"

    @classmethod
    def parse(cls, spec: str) -> "Tag":
        m = _TAG_RE.match(spec.strip())
        if not m:
            raise ValueError(f"Bad tag spec {spec!r}, expected key=value or key")
        key, _, value = m.groups()
        return cls(key=key, value=value if value else None)


@dataclass
class BBox:
    south: float
    west: float
    north: float
    east: float

    def as_overpass(self) -> str:
        return f"{self.south},{self.west},{self.north},{self.east}"

    @classmethod
    def parse(cls, spec: str) -> "BBox":
        parts = [p.strip() for p in spec.split(",")]
        if len(parts) != 4:
            raise ValueError("bbox must be 'south,west,north,east'")
        south, west, north, east = (float(p) for p in parts)
        return cls(south=south, west=west, north=north, east=east)


def resolve_area_bbox(area: str, user_agent: str = DEFAULT_USER_AGENT,
                       timeout: float = 10.0) -> BBox:
    """Look up a place name's bounding box via Nominatim (1 request)."""
    from urllib.parse import quote

    url = f"{NOMINATIM_ENDPOINT}?q={quote(area)}&format=json&limit=1"
    result = fetch(url, user_agent=user_agent, timeout=timeout)
    if not result.ok:
        raise RuntimeError(f"Nominatim lookup for {area!r} failed: {result.error or result.status}")
    data = json.loads(result.text())
    if not data:
        raise RuntimeError(f"Nominatim found no place named {area!r}")
    bb = data[0]["boundingbox"]  # [south, north, west, east] as strings
    south, north, west, east = (float(x) for x in bb)
    return BBox(south=south, west=west, north=north, east=east)


def build_query(tags: Sequence[Tag], bbox: BBox, timeout_s: int = 60, website_only: bool = False) -> str:
    """Build an Overpass QL query matching any of the given tags inside bbox."""
    bb = bbox.as_overpass()
    clauses = []
    for t in tags:
        selector = f'"{t.key}"="{t.value}"' if t.value is not None else f'"{t.key}"'
        extra = '[~"^(website|contact:website|url)$"~"."]' if website_only else ""
        for kind in ("node", "way", "relation"):
            clauses.append(f'  {kind}[{selector}]{extra}({bb});')
    body = "\n".join(clauses)
    return f"[out:json][timeout:{timeout_s}];\n(\n{body}\n);\nout center tags;"


def run_query(query: str, user_agent: str = DEFAULT_USER_AGENT,
              endpoint: str = OVERPASS_ENDPOINT, timeout: float = 90.0) -> dict:
    data = ("data=" + _urlencode(query)).encode("ascii")
    result = fetch(endpoint, user_agent=user_agent, timeout=timeout, method="POST", data=data,
                    extra_headers={"Content-Type": "application/x-www-form-urlencoded"})
    if not result.ok:
        raise RuntimeError(f"Overpass query failed: {result.error or result.status}: {result.text()[:300]}")
    return json.loads(result.text())


def _urlencode(s: str) -> str:
    from urllib.parse import quote
    return quote(s, safe="")


# Fields OSM commonly uses for contact details, in priority order.
_NAME_TAGS = ("name", "name:en")
_WEBSITE_TAGS = ("website", "contact:website", "url")
_PHONE_TAGS = ("phone", "contact:phone")
_EMAIL_TAGS = ("email", "contact:email")


def _first(tags: dict, keys: Sequence[str]) -> str:
    for k in keys:
        if tags.get(k):
            return tags[k]
    return ""


def _address(tags: dict) -> str:
    parts = [
        tags.get("addr:housenumber", ""),
        tags.get("addr:street", ""),
        tags.get("addr:city", "") or tags.get("addr:suburb", ""),
    ]
    joined = " ".join(p for p in parts if p).strip()
    return joined or tags.get("addr:full", "")


def elements_to_records(data: dict, category: str = "") -> List[dict]:
    """Normalize Overpass elements into the common lead schema."""
    out = []
    for el in data.get("elements", []):
        tags = el.get("tags", {}) or {}
        name = _first(tags, _NAME_TAGS)
        if not name:
            continue  # unnamed nodes are not usable leads
        lat = el.get("lat") or (el.get("center") or {}).get("lat")
        lon = el.get("lon") or (el.get("center") or {}).get("lon")
        out.append({
            "name": name,
            "website": _first(tags, _WEBSITE_TAGS),
            "phone": _first(tags, _PHONE_TAGS),
            "email": _first(tags, _EMAIL_TAGS),
            "address": _address(tags),
            "city": tags.get("addr:city", ""),
            "country": tags.get("addr:country", ""),
            "category": category or tags.get("amenity") or tags.get("shop") or tags.get("office")
            or tags.get("craft") or tags.get("leisure") or "",
            "source": "osm",
            "source_id": f"{el.get('type', 'node')}/{el.get('id', '')}",
            "lat": lat or "",
            "lon": lon or "",
        })
    return out


def find(tags: Sequence[str], area: Optional[str] = None, bbox: Optional[str] = None,
         user_agent: str = DEFAULT_USER_AGENT, category: str = "",
         website_only: bool = False) -> List[dict]:
    """High-level entry point: tags as 'key=value' strings, an area name or a bbox."""
    if bool(area) == bool(bbox):
        raise ValueError("pass exactly one of area or bbox")
    parsed_tags = [Tag.parse(t) for t in tags]
    box = resolve_area_bbox(area, user_agent=user_agent) if area else BBox.parse(bbox)
    query = build_query(parsed_tags, box, website_only=website_only)
    data = run_query(query, user_agent=user_agent)
    return elements_to_records(data, category=category)
