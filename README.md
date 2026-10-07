# leadscout

Find small-business leads and audit their websites — without an API key, a
scraping framework, or a paid data source.

Two jobs:

1. **Find businesses.** Pull them from OpenStreetMap (via the free Overpass
   API, by category and place) or load them from a local JSON/CSV list you
   already have. Output is a flat CSV: `name,website,phone,email,address,
   city,country,category,source,source_id,lat,lon`. Only contact details the
   business itself published (its OSM tags, its own site, your list) — no
   guessing, no scraping of third-party directories.
2. **Audit a website.** One quick, polite check per site: does it use
   HTTPS, does it have a mobile viewport, a title and meta description,
   `LocalBusiness` structured data, a Google Maps link, a WhatsApp link, a
   booking link; is it broken or parked; how heavy and slow is it; does it
   have a `sitemap.xml`. The result is a plain-English list of concrete
   problems — the kind a small business owner can read without a glossary.

"Polite" means: one request per page, a real `User-Agent` that names the
tool and links back to this repo, a timeout on every request, and
`robots.txt` is checked and honored before the page is even fetched.

## Install

Needs Python 3.9+. Everything is stdlib except `certifi`, pulled in to avoid
`CERTIFICATE_VERIFY_FAILED` errors on Python installs that ship without a
usable system CA bundle (notably python.org builds on macOS).

```bash
git clone https://github.com/taktekhq/leadscout.git
cd leadscout
pip install -e .
```

Or just run it in place: `python -m leadscout.cli ...` from the repo root.

## Find businesses on OpenStreetMap

```bash
leadscout find-overpass \
  --tag amenity=dentist --tag shop=hairdresser \
  --area "Beirut, Lebanon" \
  --out leads.csv
```

- `--tag` is any OSM tag, `key=value` (repeatable). Common ones: `amenity=
  dentist|clinic`, `shop=beauty|hairdresser`, `leisure=fitness_centre`,
  `office=estate_agent`, `craft=solar` / `shop=solar`.
- Location is either `--area "City, Country"` (resolved to a bounding box
  via the free Nominatim API, one request) or `--bbox south,west,north,east`
  if you already know the box.
- Results only include named places; OSM nodes with no `name` tag aren't
  usable leads and are skipped.

This hits the public Overpass API (`overpass-api.de`) and Nominatim once
each. Both are shared community resources — don't loop this in a tight
script; space out repeat runs.

## Load a local list

```bash
leadscout find-local --input businesses.json --out leads.csv
```

Works on a JSON file (a bare list of objects, or a dict with one such list
inside, e.g. `{"businesses": [...]}`) or a CSV. Common field names (`name`/
`business_name`, `website`/`url`, `phone`/`tel`, `email`, `address`,
`city`, `country`, `category`/`sector`) are detected automatically. Point
it at a different schema with `--map field=source_key`, e.g.:

```bash
leadscout find-local --input clients.csv \
  --map name=company_name --map website=homepage \
  --out leads.csv
```

For JSON records that nest contact details (e.g.
`{"contact": {"emails": [...], "phones": [...]}}`), pass
`--contact-from contact` to pull the first email/phone out of there too.

## Audit a website

```bash
leadscout audit --url https://example.com
```

Prints every field plus a `problems` list. Use `--out result.csv` to write
it as a one-row CSV instead. To audit every site in a leads CSV at once:

```bash
leadscout audit-csv --input leads.csv --out audited.csv
```

Adds the audit columns to each row and writes the merged CSV. `--delay`
(default 1 second) is the pause between sites — keep it, these are other
people's servers.

## As a library

```python
from leadscout import overpass, local_source
from leadscout.audit import audit_site

leads = overpass.find(tags=["shop=hairdresser"], area="Dublin, Ireland")
result = audit_site(leads[0]["website"])
print(result.problems)
```

## Tests

```bash
python -m unittest discover -v
```

Tests run offline: `audit` tests serve HTML from an in-process HTTP server,
and `overpass`/`local_source` tests use fixture data, not the live APIs.

## License

MIT, see `LICENSE`.
