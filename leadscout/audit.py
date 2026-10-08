"""Audit a business website: quick, polite, and in plain English.

Checks: HTTPS, a mobile viewport tag, title/meta description, JSON-LD
LocalBusiness markup, a Google Maps link, a WhatsApp link, a booking link,
whether the site looks broken or parked, page weight and load time, and
whether sitemap.xml exists.

Every presence check (Maps, WhatsApp, booking/contact form, structured data)
is made site-wide: the home page plus a few same-site pages whose link text or
URL looks like contact / booking / appointment / reserve / location / find-us /
about (and sitemap entries that match), at most MAX_PAGES in total. Each
result says where the thing was found, and "missing on the home page only"
is kept apart from "missing site-wide" so findings can be worded honestly.

HTTPS is three separate questions: is there valid HTTPS at all, does http://
redirect to https://, and do sitemap.xml / robots.txt still point to http://.

Politeness: one request per resource, a short pause between pages, a real
User-Agent that names the tool and a contact URL, a timeout on every request,
and robots.txt is honored before any page is fetched (inner pages included).
"""

from __future__ import annotations

import html.parser
import json
import re
import time
import urllib.robotparser
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from urllib.parse import urljoin, urlparse

from . import DEFAULT_USER_AGENT
from .httpclient import fetch

_PARKED_MARKERS = (
    "domain is for sale", "this domain is parked", "buy this domain",
    "domain may be for sale", "godaddy.com/domains", "related searches",
    "this web page is parked", "courtesy page",
)

_WHATSAPP_RE = re.compile(r"(whatsapp|wa\.me/|wa\.link/|api\.whatsapp\.com|web\.whatsapp\.com|whatsapp://|whatsapp-chat|joinchat|click-to-chat)", re.I)
_MAPS_RE = re.compile(r"(google\.[a-z.]+/maps|maps\.google\.|maps\.googleapis\.com|maps\.app\.goo\.gl|goo\.gl/maps|g\.page/)", re.I)
_BOOKING_RE = re.compile(
    r"(calendly\.com|book\.?now|book[- ](an?|your|online|appointment|a )|appointment|reserve|reservation|rendez-vous|r\u00e9server|\u062d\u062c\u0632|booking|fresha\.com|setmore\.com|acuityscheduling\.com|"
    r"squareup\.com/appointments|simplybook\.me|vagaro\.com)", re.I
)


_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
_AT_RE = re.compile(r"@")
_WA_NUM_RE = re.compile(r"(?:wa\.me/|phone=)\+?(\d{7,15})", re.I)
_BAD_EMAIL_SUFFIX = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".css", ".js")
_BAD_EMAIL_DOMAINS = ("website.com", "yourdomain.com", "sentry.io", "example.com", "wixpress.com", "domain.com", "email.com")


def _find_emails(text: str, links: List[str]) -> List[str]:
    found: List[str] = []
    for href in links:
        if href.lower().startswith("mailto:"):
            found += _EMAIL_RE.findall(href[7:].split("?")[0])
    # Anchor on '@' instead of scanning the whole page with a leading
    # [A-Za-z0-9...]+ (quadratic on big base64/minified blobs).
    for m in _AT_RE.finditer(text):
        found += _EMAIL_RE.findall(text[max(0, m.start() - 64):m.end() + 100])
        if len(found) > 20:
            break
    out: List[str] = []
    for e in found:
        e = e.strip(".").lower()
        if e.endswith(_BAD_EMAIL_SUFFIX) or e.split("@")[1] in _BAD_EMAIL_DOMAINS or e in out:
            continue
        out.append(e)
    return out[:3]


class _PageParts(html.parser.HTMLParser):
    """Pull out the bits of HTML we care about without a full DOM."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title_parts: List[str] = []
        self._in_title = False
        self.meta_description = ""
        self.has_viewport = False
        self.ldjson_blobs: List[str] = []
        self._in_ldjson = False
        self.links: List[str] = []
        self.anchors: List[tuple] = []  # (href, link text)
        self._a_href: Optional[str] = None
        self._a_text: List[str] = []
        self.has_form = False
        self._form_fields: List[int] = []  # per open <form>: count of real fields
        self.lang = ""
        self.visible_chars = 0
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        attrs_d = dict(attrs)
        if tag in ("script", "style", "noscript"):
            self._skip += 1
        if tag == "html":
            self.lang = (attrs_d.get("lang") or "").lower()
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            name = (attrs_d.get("name") or "").lower()
            if name == "description":
                self.meta_description = attrs_d.get("content", "")
            if name == "viewport":
                self.has_viewport = True
        elif tag == "script" and (attrs_d.get("type") or "").lower() == "application/ld+json":
            self._in_ldjson = True
        elif tag == "a" and attrs_d.get("href"):
            self.links.append(attrs_d["href"])
            self._a_href, self._a_text = attrs_d["href"], []
        elif tag == "form":
            role = (attrs_d.get("role") or "").lower()
            self._form_fields.append(-100 if role == "search" else 0)
        elif tag in ("input", "textarea", "select") and self._form_fields:
            typ = (attrs_d.get("type") or "text").lower()
            if tag != "input" or typ not in ("hidden", "submit", "button", "search", "image", "reset"):
                self._form_fields[-1] += 1
                if tag == "textarea" or self._form_fields[-1] >= 2:
                    self.has_form = True

    def handle_endtag(self, tag):
        if tag == "a" and self._a_href is not None:
            self.anchors.append((self._a_href, " ".join("".join(self._a_text).split())))
            self._a_href = None
        if tag == "form" and self._form_fields:
            self._form_fields.pop()
        if tag in ("script", "style", "noscript") and self._skip:
            self._skip -= 1
        if tag == "title":
            self._in_title = False
        elif tag == "script":
            self._in_ldjson = False

    def handle_data(self, data):
        if not self._skip:
            self.visible_chars += len(data.strip())
        if self._a_href is not None:
            self._a_text.append(data)
        if self._in_title:
            self.title_parts.append(data)
        if self._in_ldjson:
            self.ldjson_blobs.append(data)


MAX_PAGES = 8
PAGE_DELAY = 0.3  # seconds between page fetches on one site

_SKIP_EXT = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".zip", ".doc", ".docx",
             ".xls", ".xlsx", ".mp4", ".mp3", ".css", ".js", ".xml", ".ico")
_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)

# check key -> (human name, AuditResult attr)
_CHECKS = {
    "maps": "Google Maps link",
    "whatsapp": "WhatsApp link/button",
    "booking": "online booking link or contact/enquiry form",
    "jsonld": "LocalBusiness structured data",
}


@dataclass
class AuditResult:
    url: str
    fetched: bool = False
    https: bool = False                 # valid HTTPS (kept under the old name)
    https_error: str = ""
    http_redirects_to_https: Optional[bool] = None  # None = not checked / unknown
    sitemap_robots_http: bool = False   # sitemap.xml/robots.txt still point to http://
    status: int = 0
    mobile_viewport: bool = False
    title: str = ""
    meta_description: str = ""
    has_jsonld_localbusiness: bool = False   # anywhere on the pages checked
    has_maps_link: bool = False
    has_whatsapp_link: bool = False
    has_booking_link: bool = False
    has_contact_form: bool = False
    broken_or_parked: bool = False
    page_weight_kb: float = 0.0
    load_time_ms: float = 0.0
    has_sitemap: bool = False
    lang: str = ""
    visible_text_chars: int = 0
    emails: str = ""
    whatsapp_number: str = ""
    phones: str = ""
    instagram: str = ""
    robots_disallowed: bool = False
    pages_checked: List[str] = field(default_factory=list)
    pages_failed: List[str] = field(default_factory=list)
    found_on: Dict[str, List[str]] = field(default_factory=dict)  # check -> page paths
    missing_sitewide: List[str] = field(default_factory=list)     # absent on every page checked
    missing_home_only: List[str] = field(default_factory=list)    # absent on home, present elsewhere
    error: str = ""
    problems: List[str] = field(default_factory=list)  # real gaps, worded by scope
    notes: List[str] = field(default_factory=list)     # softer findings ("only on /contact")

    def as_dict(self) -> dict:
        d = dict(self.__dict__)
        d["problems"] = "; ".join(self.problems)
        d["notes"] = "; ".join(self.notes)
        d["pages_checked"] = " ".join(self.pages_checked)
        d["pages_failed"] = " ".join(self.pages_failed)
        d["missing_sitewide"] = ",".join(self.missing_sitewide)
        d["missing_home_only"] = ",".join(self.missing_home_only)
        found = d.pop("found_on")
        for k in ("maps", "whatsapp", "booking", "form", "jsonld"):
            d[f"{k}_found_on"] = " ".join(found.get(k, []))
        d["http_redirects_to_https"] = "" if self.http_redirects_to_https is None else self.http_redirects_to_https
        return d


class _Robots:
    """robots.txt per origin: rules for can_fetch plus any Sitemap: lines."""

    def __init__(self, user_agent: str, timeout: float):
        self.ua, self.timeout = user_agent, timeout
        self._cache: Dict[str, tuple] = {}

    def _load(self, origin: str):
        if origin not in self._cache:
            rp = urllib.robotparser.RobotFileParser()
            res = fetch(origin + "/robots.txt", user_agent=self.ua, timeout=self.timeout)
            lines = res.text().splitlines() if res.status == 200 else []
            if res.status == 200:
                rp.parse(lines)
            sitemaps = [ln.split(":", 1)[1].strip() for ln in lines if ln.lower().startswith("sitemap:")]
            self._cache[origin] = (res.status == 200, rp, sitemaps)
        return self._cache[origin]

    def allows(self, url: str) -> bool:
        p = urlparse(url)
        present, rp, _ = self._load(f"{p.scheme}://{p.netloc}")
        return rp.can_fetch(self.ua, url) if present else True  # no robots.txt = no restriction

    def sitemaps(self, url: str) -> List[str]:
        p = urlparse(url)
        return self._load(f"{p.scheme}://{p.netloc}")[2]


def _robots_allows(base_url: str, user_agent: str, timeout: float) -> bool:
    return _Robots(user_agent, timeout).allows(base_url)


def _looks_parked(text_lower: str) -> bool:
    return any(marker in text_lower for marker in _PARKED_MARKERS)


def _host(url: str) -> str:
    h = urlparse(url).netloc.lower()
    return h[4:] if h.startswith("www.") else h


def _path(url: str) -> str:
    p = urlparse(url)
    return (p.path or "/") + (("?" + p.query) if p.query else "")


_RANK = (
    (re.compile(r"(contact|book|appointment|reserv|rendez-vous|nous[-_ ]joindre|\u062d\u062c\u0632|\u0627\u062a\u0635\u0644)", re.I), 0),
    (re.compile(r"(location|find[-_ ]?us|visit|direction|\u0645\u0648\u0642\u0639)", re.I), 1),
    (re.compile(r"(about|a-propos)", re.I), 2),
)


def _candidate_pages(home_url: str, anchors: List[tuple], sitemap_urls: List[str]) -> List[str]:
    """Same-site URLs worth a look, most useful first (contact/booking, then location, then about).

    Only short, page-like paths count: a blog post called "all-about-easter" or a
    listing called "prime-location-in-dubai" is not the page we are after.
    """
    base_host = _host(home_url)
    h = urlparse(home_url)
    seen = {re.sub(r"/+", "/", h.path or "/").rstrip("/")}
    found: List[tuple] = []  # (rank, depth, order, url)

    def consider(raw: str, text: str):
        raw = (raw or "").strip()
        if not raw or raw.lower().startswith(("mailto:", "tel:", "javascript:", "#", "sms:", "whatsapp:")):
            return
        full = urljoin(home_url, raw).split("#", 1)[0]
        p = urlparse(full)
        if p.scheme not in ("http", "https") or _host(full) != base_host:
            return
        path = re.sub(r"/+", "/", p.path or "/")
        if path.lower().endswith(_SKIP_EXT):
            return
        segs = [x for x in path.split("/") if x]
        page_like = len(segs) <= 3 and len(path) <= 48 and all(x.count("-") <= 3 for x in segs)
        label = text if len(text or "") <= 40 else ""
        hay = path if page_like else ""
        rank = next((r for rx, r in _RANK if rx.search(hay) or rx.search(label)), None)
        if rank is None or (not page_like and rank != 0):
            return  # deep paths (listings, posts) only count when the link text says contact/book
        key = path.rstrip("/")
        if key in seen:
            return
        seen.add(key)
        # fetch over the scheme/host the home page actually answered on
        found.append((rank, len(segs), len(found), f"{h.scheme}://{h.netloc}{path}" + (f"?{p.query}" if p.query else "")))

    for href, text in anchors:
        consider(href, text)
    for u in sitemap_urls:
        consider(u, "")
    return [u for *_, u in sorted(found)]


def _sitemap_locs(urls: List[str], ua: str, timeout: float, max_children: int = 2) -> tuple:
    """(exists, [page urls]) from sitemap.xml and any robots-declared sitemaps."""
    exists, locs, fetched = False, [], 0
    queue = list(urls)
    done = set()
    while queue and fetched < 3:
        u = queue.pop(0)
        if u in done:
            continue
        done.add(u)
        sm = fetch(u, user_agent=ua, timeout=timeout)
        fetched += 1
        if sm.status != 200 or b"<" not in sm.body[:50]:
            continue
        exists = True
        body = sm.text()
        found = _LOC_RE.findall(body)
        if "<sitemapindex" in body.lower():
            # index: look inside the children most likely to hold pages
            kids = [f for f in found if "page" in f.lower()] + [f for f in found if "page" not in f.lower()]
            queue += kids[:max_children]
        else:
            locs += found
    return exists, locs


def _scan_page(text: str, parser: "_PageParts") -> Dict[str, bool]:
    # also scan raw HTML so e.g. onclick-only buttons still count
    def hit(rx):
        return bool(rx.search(text)) or any(rx.search(h) for h in parser.links)
    jsonld = False
    for blob in parser.ldjson_blobs:
        try:
            data = json.loads(blob)
        except Exception:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            c = stack.pop()
            if isinstance(c, dict):
                t = c.get("@type", "")
                types = t if isinstance(t, list) else [t]
                if any("LocalBusiness" in str(x) or str(x) in _LOCAL_BUSINESS_SUBTYPES for x in types):
                    jsonld = True
                g = c.get("@graph")
                if isinstance(g, list):
                    stack += g
    return {
        "maps": hit(_MAPS_RE), "whatsapp": hit(_WHATSAPP_RE), "booking": hit(_BOOKING_RE),
        "form": parser.has_form, "jsonld": jsonld,
    }


def _is_cert_problem(err: str) -> bool:
    e = (err or "").lower()
    return "certificate" in e or "ssl" in e or "tls" in e


def audit_site(url: str, user_agent: str = DEFAULT_USER_AGENT, timeout: float = 10.0,
               check_sitemap: bool = True, max_pages: int = MAX_PAGES,
               page_delay: float = PAGE_DELAY) -> AuditResult:
    """Audit one business website. Never raises; failures show up in .error/.problems."""
    if not url:
        r = AuditResult(url=url, error="no url given")
        r.problems.append("No website on file.")
        return r

    if not urlparse(url).scheme:
        url = "https://" + url

    r = AuditResult(url=url)
    robots = _Robots(user_agent, timeout)

    if not robots.allows(url):
        r.robots_disallowed = True
        r.problems.append("robots.txt disallows automated checks; skipped the audit out of respect.")
        return r

    result = fetch(url, user_agent=user_agent, timeout=timeout)
    start_scheme = urlparse(url).scheme
    if (result.error or result.status == 0) and start_scheme == "https":
        # Is it down, or is it only HTTPS that is broken (bad/expired certificate)?
        https_err = result.error or "no response"
        plain = fetch("http://" + urlparse(url).netloc + (urlparse(url).path or "/"),
                      user_agent=user_agent, timeout=timeout)
        if plain.status and not plain.error:
            result, r.https_error = plain, https_err
    r.load_time_ms = round(result.elapsed_ms, 1)
    r.status = result.status

    if result.error or result.status == 0:
        r.error = result.error or "no response"
        r.broken_or_parked = True
        r.problems.append(f"Site did not respond ({r.error}).")
        return r

    r.fetched = True
    home_url = result.final_url
    final_scheme = urlparse(home_url).scheme
    r.page_weight_kb = round(len(result.body) / 1024, 1)

    # --- HTTPS, as three separate questions --------------------------------
    host_part = urlparse(home_url).netloc
    if final_scheme == "https":
        r.https = True
        if start_scheme == "http":
            r.http_redirects_to_https = True
        else:
            plain = fetch("http://" + host_part + "/", user_agent=user_agent, timeout=timeout)
            if plain.status and not plain.error:
                r.http_redirects_to_https = urlparse(plain.final_url).scheme == "https"
    elif not r.https_error:
        # Landed on http (given as http, or an http redirect target): does https exist at all?
        probe = fetch("https://" + host_part + "/", user_agent=user_agent, timeout=timeout)
        if probe.status and not probe.error:
            r.https = True
            r.http_redirects_to_https = False
        else:
            r.https_error = probe.error or "no response on https"

    if not robots.allows(home_url):
        r.fetched, r.robots_disallowed = False, True
        r.problems.append("robots.txt disallows automated checks; skipped the audit out of respect.")
        return r

    if result.status >= 400:
        r.broken_or_parked = True
        r.problems.append(f"Site returned an error page (HTTP {result.status}).")

    text = result.text()
    text_lower = text.lower()
    if _looks_parked(text_lower) or (result.status < 400 and len(result.body) < 300):
        r.broken_or_parked = True
        if "Site returned an error page" not in "; ".join(r.problems):
            r.problems.append("Site looks parked or placeholder, not a real business page.")

    parser = _PageParts()
    try:
        parser.feed(text)
    except Exception:
        pass

    r.title = "".join(parser.title_parts).strip()
    r.meta_description = parser.meta_description.strip()
    r.mobile_viewport = parser.has_viewport
    r.lang = parser.lang
    r.visible_text_chars = parser.visible_chars
    if "under maintenance" in text_lower or "coming soon" in r.title.lower():
        r.broken_or_parked = True
        r.problems.append("Site shows an under-maintenance / coming-soon page.")

    # --- sitemap / robots (also feeds the crawl) ---------------------------
    sitemap_pages: List[str] = []
    if check_sitemap and not r.broken_or_parked:
        origin = f"{urlparse(home_url).scheme}://{host_part}"
        r.has_sitemap, sitemap_pages = _sitemap_locs(
            [origin + "/sitemap.xml"] + robots.sitemaps(home_url), user_agent, timeout)
        if r.https:
            declared = robots.sitemaps(home_url)
            http_prefix = "http://" + _host(home_url)
            if any(d.lower().startswith(("http://" + _host(home_url), "http://www." + _host(home_url)))
                   for d in declared) or any(
                    u.lower().startswith((http_prefix, "http://www." + _host(home_url)))
                    for u in sitemap_pages[:50]):
                r.sitemap_robots_http = True

    # --- pages: home + a few contact/booking/location-style pages ----------
    pages = [(home_url, text, parser)]
    r.pages_checked.append(_path(home_url))
    if not r.broken_or_parked and max_pages > 1:
        for cand in _candidate_pages(home_url, parser.anchors, sitemap_pages)[: max_pages - 1]:
            if not robots.allows(cand):
                continue
            if page_delay:
                time.sleep(page_delay)
            res = fetch(cand, user_agent=user_agent, timeout=timeout)
            ctype = (res.headers.get("Content-Type") or res.headers.get("content-type") or "").lower()
            if res.error or res.status >= 400 or (ctype and "html" not in ctype):
                r.pages_failed.append(_path(cand))
                continue
            sub = _PageParts()
            try:
                sub.feed(res.text())
            except Exception:
                pass
            fkey = re.sub(r"/+", "/", _path(res.final_url)).rstrip("/") or "/"
            if fkey in {re.sub(r"/+", "/", x).rstrip("/") or "/" for x in r.pages_checked}:
                continue  # a redirect landed on a page we already have
            pages.append((res.final_url, res.text(), sub))
            r.pages_checked.append(_path(res.final_url))

    found: Dict[str, List[str]] = {k: [] for k in ("maps", "whatsapp", "booking", "form", "jsonld")}
    emails, tels = [], []
    for page_url, page_text, sub in pages:
        path = _path(page_url)
        for k, v in _scan_page(page_text, sub).items():
            if v:
                found[k].append(path)
        emails += _find_emails(page_text, sub.links)
        for href in sub.links:
            if href.lower().startswith("tel:"):
                t = re.sub(r"[^\d+]", "", href[4:])
                if t and t not in tels:
                    tels.append(t)
    r.found_on = {k: v for k, v in found.items() if v}
    r.has_jsonld_localbusiness = bool(found["jsonld"])
    r.has_maps_link = bool(found["maps"])
    r.has_whatsapp_link = bool(found["whatsapp"])
    r.has_booking_link = bool(found["booking"])
    r.has_contact_form = bool(found["form"])
    dedup = []
    for e in emails:
        if e not in dedup:
            dedup.append(e)
    r.emails = ", ".join(dedup[:3])
    r.phones = ", ".join(tels[:3])
    for _, page_text, _sub in pages:
        wa = _WA_NUM_RE.search(page_text)
        if wa:
            r.whatsapp_number = wa.group(1)
            break
    for _, page_text, _sub in pages:
        ig = re.search(r"instagram\.com/([A-Za-z0-9_.]+)", page_text)
        if ig and ig.group(1).lower() not in ("p", "explore", "accounts", "reel", "sharer"):
            r.instagram = "https://www.instagram.com/" + ig.group(1)
            break

    # --- findings, worded by scope -----------------------------------------
    n = len(pages)
    where = ("the home page (no contact/booking/location pages were found to check)" if n == 1
             else f"any of the {n} pages checked ({', '.join(r.pages_checked)})")
    home_path = _path(home_url)
    present = {
        "maps": found["maps"], "whatsapp": found["whatsapp"], "jsonld": found["jsonld"],
        "booking": sorted(set(found["booking"]) | set(found["form"]), key=(found["booking"] + found["form"]).index),
    }
    advice = {
        "maps": "makes it harder for customers to find the physical location.",
        "whatsapp": "customers in this market expect one-tap WhatsApp contact.",
        "booking": "customers must call during business hours to book.",
        "jsonld": "Google/AI assistants can't read hours/address/phone directly.",
    }
    for key, label in _CHECKS.items():
        locs = present[key]
        if not locs:
            r.missing_sitewide.append(key)
            r.problems.append(f"No {label} on {where}, {advice[key]}")
        elif home_path not in locs:
            r.missing_home_only.append(key)
            r.notes.append(f"No {label} on the home page, but found on {', '.join(locs)}.")

    if not r.https:
        why = f" ({r.https_error})" if r.https_error else ""
        r.problems.append(f"No valid HTTPS{why}, browsers mark it 'Not secure' or block it.")
    else:
        if r.http_redirects_to_https is False:
            r.problems.append("HTTPS works, but http:// does not redirect to https://, so old links and typed "
                              "addresses land on the 'Not secure' version.")
        if r.sitemap_robots_http:
            r.problems.append("sitemap.xml/robots.txt still point to http:// URLs, search engines are told "
                              "to index the insecure version.")
    if not r.mobile_viewport:
        r.problems.append("No mobile viewport tag, the site will look broken on phones.")
    if not r.title:
        r.problems.append("No <title>, hurts how the site shows up in search and when shared.")
    if not r.meta_description:
        r.problems.append("No meta description on the home page, search engines show a random snippet instead.")
    if not r.has_sitemap and check_sitemap and not r.broken_or_parked:
        r.problems.append("No sitemap.xml, can slow down how fully search engines index the site.")
    if r.load_time_ms > 3000:
        r.problems.append(f"Slow to load ({int(r.load_time_ms)}ms), visitors tend to leave before it finishes.")
    if r.page_weight_kb > 3000:
        r.problems.append(f"Heavy page ({r.page_weight_kb}KB), slow on mobile data.")

    return r


_LOCAL_BUSINESS_SUBTYPES = {
    "Dentist", "MedicalClinic", "HealthAndBeautyBusiness", "BeautySalon", "HairSalon",
    "ExerciseGym", "GymOrFitnessCenter", "RealEstateAgent", "Restaurant", "Spa",
}
