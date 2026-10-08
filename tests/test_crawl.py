"""Site-wide crawl and the three-way HTTPS split, tested against fixtures."""
import http.server
import threading
import unittest
from unittest import mock

from leadscout import audit
from leadscout.audit import audit_site
from leadscout.httpclient import FetchResult

HEAD = '<head><title>Clinic</title><meta name="viewport" content="width=device-width"><meta name="description" content="d"></head>'


def page(body: str, head: str = HEAD) -> bytes:
    return f"<!doctype html><html>{head}<body>{body}<!-- {'x' * 300} --></body></html>".encode()


class _Handler(http.server.BaseHTTPRequestHandler):
    routes: dict = {}
    hits: list = []

    def do_GET(self):
        type(self).hits.append(self.path)
        body, status = self.routes.get(self.path, (b"Not Found", 404))
        self.send_response(status)
        self.send_header("Content-Type", "application/xml" if self.path.endswith(".xml") else "text/html")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


class Site:
    def __init__(self, routes):
        _Handler.routes, _Handler.hits = routes, []
        self.server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        # fixtures may use {base} for the server's own origin
        for path, (body, status) in list(routes.items()):
            routes[path] = (body.replace(b"{base}", self.base.encode()), status)

    hits = property(lambda self: _Handler.hits)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.server.shutdown()
        self.server.server_close()


def run(routes, **kw):
    routes.setdefault("/robots.txt", (b"", 404))
    with Site(routes) as site:
        r = audit_site(site.base, timeout=5, page_delay=0, **kw)
        return r, site.hits


CONTACT_FORM = '<form action="/send"><input name="name"><input name="email"><textarea name="msg"></textarea></form>'


class CrawlTest(unittest.TestCase):
    def test_form_only_on_contact_page_is_not_reported_missing(self):
        r, _ = run({
            "/": (page('<a href="/contact-us">Contact</a><a href="/blog">Blog</a>'), 200),
            "/contact-us": (page(CONTACT_FORM), 200),
        })
        self.assertEqual(r.pages_checked, ["/", "/contact-us"])
        self.assertTrue(r.has_contact_form)
        self.assertEqual(r.found_on["form"], ["/contact-us"])
        self.assertNotIn("booking", r.missing_sitewide)
        self.assertIn("booking", r.missing_home_only)
        self.assertFalse([p for p in r.problems if "booking" in p])
        self.assertTrue([n for n in r.notes if "home page" in n and "/contact-us" in n])

    def test_maps_only_on_location_page(self):
        r, _ = run({
            "/": (page('<a href="/find-us">Where to find us</a>'), 200),
            "/find-us": (page('<iframe src="https://www.google.com/maps/embed?pb=1"></iframe>'), 200),
        })
        self.assertTrue(r.has_maps_link)
        self.assertEqual(r.found_on["maps"], ["/find-us"])
        self.assertIn("maps", r.missing_home_only)
        self.assertNotIn("maps", r.missing_sitewide)

    def test_missing_everywhere_is_worded_site_wide(self):
        r, _ = run({
            "/": (page('<a href="/contact">Contact</a><a href="/about">About</a>'), 200),
            "/contact": (page("call us"), 200),
            "/about": (page("who we are"), 200),
        })
        for key in ("maps", "whatsapp", "booking", "jsonld"):
            self.assertIn(key, r.missing_sitewide)
        self.assertEqual(r.missing_home_only, [])
        maps = [p for p in r.problems if "Maps" in p][0]
        self.assertIn("any of the 3 pages checked", maps)
        self.assertIn("/contact", maps)

    def test_home_only_wording_when_no_other_pages_exist(self):
        r, _ = run({"/": (page("hello"), 200)})
        self.assertEqual(r.pages_checked, ["/"])
        maps = [p for p in r.problems if "Maps" in p][0]
        self.assertIn("the home page (no contact/booking/location pages were found to check)", maps)

    def test_sitemap_entries_matching_keywords_are_crawled(self):
        sm = (b"<urlset><url><loc>{base}/book-online</loc></url><url><loc>{base}/blog/post-1</loc></url>"
              b"<url><loc>http://elsewhere.test/contact</loc></url></urlset>")
        r, hits = run({
            "/": (page("hello"), 200),
            "/sitemap.xml": (sm, 200),
            "/book-online": (page('<a href="https://calendly.com/clinic">Book</a>'), 200),
            "/blog/post-1": (page("post"), 200),
        })
        self.assertIn("/book-online", r.pages_checked)
        self.assertNotIn("/blog/post-1", hits)
        self.assertEqual(r.found_on["booking"], ["/book-online"])
        self.assertTrue(r.has_sitemap)

    def test_page_cap_and_external_links(self):
        links = "".join(f'<a href="/contact-{i}">c{i}</a>' for i in range(20))
        links += '<a href="https://other.example/contact">ext</a>'
        routes = {"/": (page(links), 200)}
        routes.update({f"/contact-{i}": (page("x"), 200) for i in range(20)})
        r, hits = run(routes)
        self.assertEqual(len(r.pages_checked), 8)
        self.assertEqual(len([h for h in hits if h.startswith("/contact-")]), 7)
        r1, _ = run(dict(routes), max_pages=1)
        self.assertEqual(r1.pages_checked, ["/"])

    def test_robots_disallowed_inner_page_is_not_fetched(self):
        r, hits = run({
            "/robots.txt": (b"User-agent: *\nDisallow: /contact\n", 200),
            "/": (page('<a href="/contact">Contact</a><a href="/about">About</a>'), 200),
            "/contact": (page(CONTACT_FORM), 200),
            "/about": (page("about"), 200),
        })
        self.assertNotIn("/contact", hits)
        self.assertEqual(r.pages_checked, ["/", "/about"])
        self.assertIn("booking", r.missing_sitewide)

    def test_failed_inner_page_is_recorded_not_fatal(self):
        r, _ = run({"/": (page('<a href="/contact">Contact</a>'), 200)})
        self.assertEqual(r.pages_failed, ["/contact"])
        self.assertTrue(r.fetched)

    def test_search_box_is_not_a_contact_form(self):
        r, _ = run({"/": (page('<form role="search"><input type="search" name="q"><input name="x"></form>'
                               '<form><input type="hidden" name="a"><input name="q"><input type="submit"></form>'), 200)})
        self.assertFalse(r.has_contact_form)

    def test_jsonld_on_inner_page_counts(self):
        ld = '<script type="application/ld+json">{"@graph":[{"@type":"Dentist","name":"x"}]}</script>'
        r, _ = run({
            "/": (page('<a href="/contact">Contact</a>'), 200),
            "/contact": (page("c", head=HEAD.replace("</head>", ld + "</head>")), 200),
        })
        self.assertTrue(r.has_jsonld_localbusiness)
        self.assertEqual(r.found_on["jsonld"], ["/contact"])

    def test_csv_fields_flatten_found_on(self):
        r, _ = run({
            "/": (page('<a href="/contact">Contact</a>'), 200),
            "/contact": (page(CONTACT_FORM), 200),
        })
        d = r.as_dict()
        self.assertEqual(d["form_found_on"], "/contact")
        self.assertEqual(d["pages_checked"], "/ /contact")
        self.assertIn("booking", d["missing_home_only"])


def _res(url, status=200, body=b"<html><head><title>t</title></head><body>hello world, long enough to not be a placeholder page. " + b"x" * 300 + b"</body></html>",
         final=None, error=None, headers=None):
    return FetchResult(url=url, final_url=final or url, status=status, headers=headers or {"Content-Type": "text/html"},
                       body=body, elapsed_ms=10, error=error)


def fake_fetch(table):
    """table: url -> FetchResult or callable; unknown URLs fail like a closed port."""
    def f(url, user_agent="", timeout=10.0, **kw):
        if url in table:
            return table[url]
        return FetchResult(url=url, final_url=url, status=0, headers={}, body=b"", elapsed_ms=1, error="connection refused")
    return f


class HttpsTest(unittest.TestCase):
    def audit(self, table, url="https://shop.test/"):
        table.setdefault("https://shop.test/robots.txt", _res("", 404, b""))
        with mock.patch.object(audit, "fetch", fake_fetch(table)):
            return audit_site(url, page_delay=0)

    def test_invalid_certificate_is_no_valid_https(self):
        r = self.audit({
            "https://shop.test/": _res("", 0, b"", error="[SSL: CERTIFICATE_VERIFY_FAILED] certificate has expired"),
            "http://shop.test/": _res("http://shop.test/"),
        })
        self.assertTrue(r.fetched)
        self.assertFalse(r.https)
        self.assertIn("CERTIFICATE_VERIFY_FAILED", r.https_error)
        self.assertTrue([p for p in r.problems if p.startswith("No valid HTTPS")])
        self.assertFalse([p for p in r.problems if "does not redirect" in p])

    def test_https_ok_but_http_does_not_redirect(self):
        r = self.audit({
            "https://shop.test/": _res("https://shop.test/"),
            "http://shop.test/": _res("http://shop.test/"),  # served as-is, no redirect
        })
        self.assertTrue(r.https)
        self.assertIs(r.http_redirects_to_https, False)
        self.assertFalse([p for p in r.problems if p.startswith("No valid HTTPS")])
        self.assertTrue([p for p in r.problems if "does not redirect" in p])

    def test_http_redirects_to_https_is_clean(self):
        r = self.audit({
            "https://shop.test/": _res("https://shop.test/"),
            "http://shop.test/": _res("http://shop.test/", final="https://shop.test/"),
        })
        self.assertTrue(r.https)
        self.assertIs(r.http_redirects_to_https, True)
        self.assertFalse([p for p in r.problems if "HTTPS" in p or "http://" in p])

    def test_http_url_that_redirects_counts_as_valid_https(self):
        r = self.audit({"http://shop.test/": _res("http://shop.test/", final="https://shop.test/")},
                       url="http://shop.test/")
        self.assertTrue(r.https)
        self.assertIs(r.http_redirects_to_https, True)

    def test_http_site_where_https_also_exists_but_is_not_forced(self):
        r = self.audit({
            "http://shop.test/": _res("http://shop.test/"),
            "https://shop.test/": _res("https://shop.test/"),
        }, url="http://shop.test/")
        self.assertTrue(r.https)
        self.assertIs(r.http_redirects_to_https, False)

    def test_sitemap_and_robots_still_pointing_at_http(self):
        r = self.audit({
            "https://shop.test/": _res("https://shop.test/"),
            "http://shop.test/": _res("http://shop.test/", final="https://shop.test/"),
            "https://shop.test/robots.txt": _res("", 200, b"User-agent: *\nSitemap: http://shop.test/sitemap.xml\n"),
            "http://shop.test/sitemap.xml": _res("", 200, b"<urlset><url><loc>http://shop.test/</loc></url></urlset>"),
        })
        self.assertTrue(r.https)
        self.assertTrue(r.sitemap_robots_http)
        self.assertIs(r.http_redirects_to_https, True)
        self.assertTrue([p for p in r.problems if "sitemap.xml/robots.txt still point to http" in p])

    def test_https_sitemap_is_clean(self):
        r = self.audit({
            "https://shop.test/": _res("https://shop.test/"),
            "http://shop.test/": _res("http://shop.test/", final="https://shop.test/"),
            "https://shop.test/sitemap.xml": _res("", 200, b"<urlset><url><loc>https://shop.test/</loc></url></urlset>"),
        })
        self.assertFalse(r.sitemap_robots_http)
        self.assertTrue(r.has_sitemap)

    def test_site_down_entirely_is_not_blamed_on_https(self):
        r = self.audit({"https://shop.test/": _res("", 0, b"", error="timed out")})
        self.assertFalse(r.fetched)
        self.assertIn("did not respond", " ".join(r.problems))


if __name__ == "__main__":
    unittest.main()


class CandidateSelectionTest(unittest.TestCase):
    def test_blog_posts_and_listings_are_not_contact_pages(self):
        anchors = [("/blog/all-about-easter-traditions-in-the-world", "Read more"),
                   ("/property/Sabtiyeh/abc/Prime-Location", "Prime location"),
                   ("/contact", "Contact"), ("/about-us", "About us"), ("/find-us", "Where"),
                   ("/news", "Facts about pomegranates and other long headline text here")]
        got = audit._candidate_pages("http://x.test/", anchors, [])
        self.assertEqual(got, ["http://x.test/contact", "http://x.test/find-us", "http://x.test/about-us"])

    def test_contact_pages_rank_before_about_and_duplicates_collapse(self):
        anchors = [("/about", "About"), ("/contact/", "Contact"), ("//contact", "Contact"), ("/Contact", "x")]
        got = audit._candidate_pages("http://x.test/", anchors, ["http://x.test/book-online"])
        self.assertEqual(got[:2], ["http://x.test/contact/", "http://x.test/Contact"])
        self.assertEqual(got[-1], "http://x.test/about")

    def test_link_text_can_qualify_a_generic_url(self):
        got = audit._candidate_pages("http://x.test/", [("/p?id=7", "Book an appointment")], [])
        self.assertEqual(got, ["http://x.test/p?id=7"])

    def test_redirect_to_page_already_checked_is_not_counted_twice(self):
        r, _ = run({
            "/": (page('<a href="/contact">Contact</a><a href="/contact-us/">Contact again</a>'), 200),
            "/contact": (page("c1"), 200),
            "/contact-us/": (page("c2"), 200),
        })
        self.assertEqual(r.pages_checked, ["/", "/contact", "/contact-us/"])
