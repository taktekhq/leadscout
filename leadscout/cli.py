"""leadscout command-line interface.

    leadscout find-overpass --tag amenity=dentist --tag shop=hairdresser \\
        --area "Beirut, Lebanon" --out leads.csv

    leadscout find-local --input businesses.json --out leads.csv

    leadscout audit --url https://example.com

    leadscout audit-csv --input leads.csv --out audited.csv
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import List, Optional

from . import DEFAULT_USER_AGENT, __version__
from . import local_source, overpass
from .audit import audit_site
from .csvio import read_csv, write_csv

LEAD_FIELDS = ("name", "website", "phone", "email", "address", "city", "country",
               "category", "source", "source_id", "lat", "lon")

AUDIT_FIELDS = (
    "url", "fetched", "https", "status", "mobile_viewport", "title", "meta_description",
    "has_jsonld_localbusiness", "has_maps_link", "has_whatsapp_link", "has_booking_link",
    "broken_or_parked", "page_weight_kb", "load_time_ms", "has_sitemap", "robots_disallowed",
    "lang", "visible_text_chars", "emails", "whatsapp_number", "phones", "instagram", "error", "problems",
)


def _parse_kv_list(pairs: Optional[List[str]]) -> dict:
    out = {}
    for p in pairs or []:
        if "=" not in p:
            raise SystemExit(f"--map expects field=source_key, got {p!r}")
        k, v = p.split("=", 1)
        out[k] = v
    return out


def cmd_find_overpass(args: argparse.Namespace) -> None:
    records = overpass.find(
        tags=args.tag, area=args.area, bbox=args.bbox,
        user_agent=args.user_agent, category=args.category or "",
    )
    write_csv(records, LEAD_FIELDS, args.out)
    print(f"Wrote {len(records)} leads to {args.out}", file=sys.stderr)


def cmd_find_local(args: argparse.Namespace) -> None:
    mapping = _parse_kv_list(args.map)
    records = local_source.load(args.input, fmt=args.format, mapping=mapping,
                                 contact_from=args.contact_from)
    write_csv(records, LEAD_FIELDS, args.out)
    print(f"Wrote {len(records)} leads to {args.out}", file=sys.stderr)


def cmd_audit(args: argparse.Namespace) -> None:
    result = audit_site(args.url, user_agent=args.user_agent, timeout=args.timeout,
                         check_sitemap=not args.no_sitemap)
    d = result.as_dict()
    if args.out:
        write_csv([d], AUDIT_FIELDS, args.out)
    else:
        for field in AUDIT_FIELDS:
            print(f"{field}: {d.get(field, '')}")


def cmd_audit_csv(args: argparse.Namespace) -> None:
    from concurrent.futures import ThreadPoolExecutor

    rows = read_csv(args.input)

    def work(item):
        i, row = item
        url = row.get("website") or row.get("url") or ""
        result = audit_site(url, user_agent=args.user_agent, timeout=args.timeout,
                            check_sitemap=not args.no_sitemap)
        if args.delay:
            time.sleep(args.delay)
        merged = dict(row)
        merged.update(result.as_dict())
        print(f"[{i + 1}/{len(rows)}] {row.get('name', url)}: "
              f"{'OK' if result.fetched else 'FAILED'}", file=sys.stderr)
        return merged

    # Each site is audited by one worker, so a given host never sees parallel
    # requests; --workers only spreads load across different hosts.
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        out_rows = list(ex.map(work, enumerate(rows)))
    fields = list(rows[0].keys()) if rows else []
    for f in AUDIT_FIELDS:
        if f not in fields:
            fields.append(f)
    write_csv(out_rows, fields, args.out)
    print(f"Wrote {len(out_rows)} audited rows to {args.out}", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="leadscout", description=__doc__.split("\n")[0])
    p.add_argument("--version", action="version", version=f"leadscout {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    fo = sub.add_parser("find-overpass", help="find businesses via the OSM Overpass API")
    fo.add_argument("--tag", action="append", required=True,
                     help="OSM tag to match, e.g. amenity=dentist (repeatable)")
    loc = fo.add_mutually_exclusive_group(required=True)
    loc.add_argument("--area", help='place name, e.g. "Beirut, Lebanon" (resolved via Nominatim)')
    loc.add_argument("--bbox", help="south,west,north,east")
    fo.add_argument("--category", default="", help="label to put in the category column")
    fo.add_argument("--out", required=True, help="output CSV path")
    fo.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    fo.set_defaults(func=cmd_find_overpass)

    fl = sub.add_parser("find-local", help="load businesses from a local JSON/CSV file")
    fl.add_argument("--input", required=True)
    fl.add_argument("--format", choices=["json", "csv"], default=None)
    fl.add_argument("--map", action="append",
                     help="override a field mapping, e.g. --map email=contact_email (repeatable)")
    fl.add_argument("--contact-from", default=None,
                     help="JSON only: key of a nested {emails:[],phones:[]}-style contact object")
    fl.add_argument("--out", required=True)
    fl.set_defaults(func=cmd_find_local)

    au = sub.add_parser("audit", help="audit one website")
    au.add_argument("--url", required=True)
    au.add_argument("--out", default=None, help="write result as a 1-row CSV instead of printing")
    au.add_argument("--timeout", type=float, default=10.0)
    au.add_argument("--no-sitemap", action="store_true", help="skip the sitemap.xml check")
    au.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    au.set_defaults(func=cmd_audit)

    ac = sub.add_parser("audit-csv", help="audit every 'website' in a leads CSV")
    ac.add_argument("--input", required=True)
    ac.add_argument("--out", required=True)
    ac.add_argument("--timeout", type=float, default=10.0)
    ac.add_argument("--delay", type=float, default=1.0, help="seconds to sleep between sites")
    ac.add_argument("--workers", type=int, default=1, help="sites audited in parallel (default 1)")
    ac.add_argument("--no-sitemap", action="store_true")
    ac.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    ac.set_defaults(func=cmd_audit_csv)

    return p


def main(argv: Optional[List[str]] = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
