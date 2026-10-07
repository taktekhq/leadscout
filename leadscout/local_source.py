"""Load businesses from a local JSON or CSV file into the common lead schema.

No hard-coded schema: common field names are auto-detected, and --map lets
the caller point any column/key at the schema leadscout uses everywhere
else (name, website, phone, email, address, city, country, category).
"""

from __future__ import annotations

import csv
import json
from typing import Dict, Iterable, List, Optional

SCHEMA_FIELDS = ("name", "website", "phone", "email", "address", "city", "country", "category")

# field -> accepted aliases, checked in order, case-insensitive
_ALIASES = {
    "name": ["name", "business_name", "title", "company"],
    "website": ["website", "url", "site", "web", "homepage"],
    "phone": ["phone", "tel", "telephone", "phone_number", "contact_phone"],
    "email": ["email", "mail", "contact_email"],
    "address": ["address", "addr", "location", "street_address"],
    "city": ["city", "town"],
    "country": ["country"],
    "category": ["category", "sector", "type", "industry"],
}


def _build_mapping(available_keys: Iterable[str], overrides: Optional[Dict[str, str]]) -> Dict[str, str]:
    """Return {schema_field: source_key}, using overrides first then alias guesses."""
    lower_to_actual = {k.lower(): k for k in available_keys}
    mapping: Dict[str, str] = {}
    overrides = overrides or {}
    for field in SCHEMA_FIELDS:
        if field in overrides:
            mapping[field] = overrides[field]
            continue
        for alias in _ALIASES[field]:
            if alias in lower_to_actual:
                mapping[field] = lower_to_actual[alias]
                break
    return mapping


def _extract(record: dict, mapping: Dict[str, str]) -> dict:
    out = {f: "" for f in SCHEMA_FIELDS}
    for field, source_key in mapping.items():
        value = record.get(source_key, "")
        if isinstance(value, (list, tuple)):
            value = value[0] if value else ""
        if isinstance(value, dict):
            value = ""
        out[field] = "" if value is None else str(value)
    out["source"] = "local"
    out["source_id"] = str(record.get("id", record.get("slug", "")))
    out["lat"] = str(record.get("lat", ""))
    out["lon"] = str(record.get("lon", ""))
    return out


def _records_from_json_blob(blob) -> List[dict]:
    """Accept a bare list, or a dict with one list-of-dicts value (e.g. {"businesses": [...]})."""
    if isinstance(blob, list):
        return blob
    if isinstance(blob, dict):
        for value in blob.values():
            if isinstance(value, list) and value and isinstance(value[0], dict):
                return value
    raise ValueError("JSON input must be a list of objects, or a dict containing one")


def load_json(path: str, mapping: Optional[Dict[str, str]] = None,
              contact_from: Optional[str] = None) -> List[dict]:
    """Load leads from a JSON file. contact_from: optional dotted path to a nested
    contact object, e.g. 'contact' for {"contact": {"emails": [...], "phones": [...]}}."""
    with open(path, "r", encoding="utf-8") as f:
        blob = json.load(f)
    records = _records_from_json_blob(blob)
    if not records:
        return []
    keys = set()
    for r in records:
        keys.update(r.keys())
    field_map = _build_mapping(keys, mapping)
    out = []
    for r in records:
        row = _extract(r, field_map)
        if contact_from and isinstance(r.get(contact_from), dict):
            nested = r[contact_from]
            if not row["email"]:
                emails = nested.get("emails") or nested.get("email")
                if isinstance(emails, list) and emails:
                    row["email"] = str(emails[0])
                elif emails:
                    row["email"] = str(emails)
            if not row["phone"]:
                phones = nested.get("phones") or nested.get("phone")
                if isinstance(phones, list) and phones:
                    row["phone"] = str(phones[0])
                elif phones:
                    row["phone"] = str(phones)
        if row["name"]:
            out.append(row)
    return out


def load_csv(path: str, mapping: Optional[Dict[str, str]] = None) -> List[dict]:
    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    if not rows:
        return []
    field_map = _build_mapping(reader.fieldnames or [], mapping)
    out = []
    for r in rows:
        row = _extract(r, field_map)
        if row["name"]:
            out.append(row)
    return out


def load(path: str, fmt: Optional[str] = None, mapping: Optional[Dict[str, str]] = None,
         contact_from: Optional[str] = None) -> List[dict]:
    """Load a local list of businesses, guessing JSON vs CSV from the extension."""
    fmt = fmt or ("json" if path.lower().endswith(".json") else "csv")
    if fmt == "json":
        return load_json(path, mapping=mapping, contact_from=contact_from)
    if fmt == "csv":
        return load_csv(path, mapping=mapping)
    raise ValueError(f"Unknown format {fmt!r}, expected 'json' or 'csv'")
