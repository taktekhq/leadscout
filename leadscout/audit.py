"""Audit a business website: quick, polite, and in plain English.

Checks: HTTPS, a mobile viewport tag, title/meta description, JSON-LD
LocalBusiness markup, a Google Maps link, a WhatsApp link, a booking link,
whether the site looks broken or parked, page weight and load time, and
whether sitemap.xml exists.

Politeness: one request per resource (home page, robots.txt, sitemap.xml),
a real User-Agent that names the tool and a contact URL, a timeout on every
request, and robots.txt is honored before the home page is even fetched.
"""

from __future__ import annotations

import html.parser
import json
import re
import urllib.robotparser
from dataclasses import dataclass, field
from typing import List, Optional
from urllib.parse import urljoin, urlparse

from . import DEFAULT_USER_AGENT
from .httpclient import fetch

_PARKED_MARKERS = (
    "domain is for sale", "this domain is parked", "buy this domain",
    "domain may be for sale", "godaddy.com/domains", "related searches",
    "this web page is parked", "courtesy page",
)

_WHATSAPP_RE = re.compile(r"(wa\.me/|api\.whatsapp\.com)", re.I)
_MAPS_RE = re.compile(r"(google\.[a-z.]+/maps|maps\.app\.goo\.gl|goo\.gl/maps)", re.I)
_BOOKING_RE = re.compile(
    r"(calendly\.com|book\.?now|booking\.|fresha\.com|setmore\.com|acuityscheduling\.com|"
    r"squareup\.com/appointments|simplybook\.me|vagaro\.com)", re.I
)


_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
_WA_NUM_RE = re.compile(r"(?:wa\.me/|phone=)\+?(\d{7,15})", re.I)
_BAD_EMAIL_SUFFIX = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".css", ".js")
_BAD_EMAIL_DOMAINS = ("sentry.io", "example.com", "wixpress.com", "domain.com", "email.com")


def _find_emails(text: str, links: List[str]) -> List[str]:
    found: List[str] = []
    for href in links:
        if href.lower().startswith("mailto:"):
            found += _EMAIL_RE.findall(href[7:].split("?")[0])
    found += _EMAIL_RE.findall(text)
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
        self.lang = ""

    def handle_starttag(self, tag, attrs):
        attrs_d = dict(attrs)
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

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag == "script":
            self._in_ldjson = False

    def handle_data(self, data):
        if self._in_title:
            self.title_parts.append(data)
        if self._in_ldjson:
            self.ldjson_blobs.append(data)


@dataclass
class AuditResult:
    url: str
    fetched: bool = False
    https: bool = False
    status: int = 0
    mobile_viewport: bool = False
    title: str = ""
    meta_description: str = ""
    has_jsonld_localbusiness: bool = False
    has_maps_link: bool = False
    has_whatsapp_link: bool = False
    has_booking_link: bool = False
    broken_or_parked: bool = False
    page_weight_kb: float = 0.0
    load_time_ms: float = 0.0
    has_sitemap: bool = False
    lang: str = ""
    emails: str = ""
    whatsapp_number: str = ""
    phones: str = ""
    instagram: str = ""
    robots_disallowed: bool = False
    error: str = ""
    problems: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        d = dict(self.__dict__)
        d["problems"] = "; ".join(self.problems)
        return d


def _robots_allows(base_url: str, user_agent: str, timeout: float) -> bool:
    parsed = urlparse(base_url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    rp = urllib.robotparser.RobotFileParser()
    result = fetch(robots_url, user_agent=user_agent, timeout=timeout)
    if result.status == 200:
        rp.parse(result.text().splitlines())
    else:
        return True  # no robots.txt (or unreachable) means no restriction
    return rp.can_fetch(user_agent, base_url)


def _looks_parked(text_lower: str) -> bool:
    return any(marker in text_lower for marker in _PARKED_MARKERS)


def audit_site(url: str, user_agent: str = DEFAULT_USER_AGENT, timeout: float = 10.0,
               check_sitemap: bool = True) -> AuditResult:
    """Audit one business website. Never raises; failures show up in .error/.problems."""
    if not url:
        r = AuditResult(url=url, error="no url given")
        r.problems.append("No website on file.")
        return r

    if not urlparse(url).scheme:
        url = "https://" + url

    r = AuditResult(url=url)

    if not _robots_allows(url, user_agent, timeout):
        r.robots_disallowed = True
        r.problems.append("robots.txt disallows automated checks; skipped the audit out of respect.")
        return r

    result = fetch(url, user_agent=user_agent, timeout=timeout)
    r.load_time_ms = round(result.elapsed_ms, 1)
    r.status = result.status

    if result.error or result.status == 0:
        r.error = result.error or "no response"
        r.broken_or_parked = True
        r.problems.append(f"Site did not respond ({r.error}).")
        return r

    r.fetched = True
    r.https = urlparse(result.final_url).scheme == "https"
    r.page_weight_kb = round(len(result.body) / 1024, 1)

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

    for blob in parser.ldjson_blobs:
        try:
            data = json.loads(blob)
        except Exception:
            continue
        candidates = data if isinstance(data, list) else [data]
        for c in candidates:
            if isinstance(c, dict):
                t = c.get("@type", "")
                types = t if isinstance(t, list) else [t]
                if any("LocalBusiness" in str(x) or str(x) in _LOCAL_BUSINESS_SUBTYPES for x in types):
                    r.has_jsonld_localbusiness = True

    r.lang = parser.lang
    r.emails = ", ".join(_find_emails(text, parser.links))
    wa = _WA_NUM_RE.search(text)
    r.whatsapp_number = wa.group(1) if wa else ""
    tels = []
    for href in parser.links:
        if href.lower().startswith("tel:"):
            t = re.sub(r"[^\d+]", "", href[4:])
            if t and t not in tels:
                tels.append(t)
    r.phones = ", ".join(tels[:3])
    ig = re.search(r"instagram\.com/([A-Za-z0-9_.]+)", text)
    if ig and ig.group(1).lower() not in ("p", "explore", "accounts", "reel", "sharer"):
        r.instagram = "https://www.instagram.com/" + ig.group(1)

    all_text_for_links = text  # also scan raw HTML so e.g. onclick-only buttons still count
    r.has_whatsapp_link = bool(_WHATSAPP_RE.search(all_text_for_links)) or any(
        _WHATSAPP_RE.search(href) for href in parser.links
    )
    r.has_maps_link = bool(_MAPS_RE.search(all_text_for_links)) or any(
        _MAPS_RE.search(href) for href in parser.links
    )
    r.has_booking_link = bool(_BOOKING_RE.search(all_text_for_links)) or any(
        _BOOKING_RE.search(href) for href in parser.links
    )

    if check_sitemap and not r.broken_or_parked:
        sitemap_url = urljoin(result.final_url, "/sitemap.xml")
        sm = fetch(sitemap_url, user_agent=user_agent, timeout=timeout)
        r.has_sitemap = sm.status == 200 and b"<" in sm.body[:50]

    if not r.https:
        r.problems.append("Not using HTTPS, browsers mark it 'Not secure'.")
    if not r.mobile_viewport:
        r.problems.append("No mobile viewport tag, the site will look broken on phones.")
    if not r.title:
        r.problems.append("No <title>, hurts how the site shows up in search and when shared.")
    if not r.meta_description:
        r.problems.append("No meta description, search engines show a random snippet instead.")
    if not r.has_jsonld_localbusiness:
        r.problems.append("No LocalBusiness structured data, so Google/AI assistants can't read hours/address/phone directly.")
    if not r.has_maps_link:
        r.problems.append("No Google Maps link, makes it harder for customers to find the physical location.")
    if not r.has_whatsapp_link:
        r.problems.append("No WhatsApp link/button, customers in this market expect one-tap WhatsApp contact.")
    if not r.has_booking_link:
        r.problems.append("No online booking link, customers must call during business hours to book.")
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
