import http.server
import threading
import unittest

from leadscout.audit import audit_site

GOOD_HTML = b"""<!doctype html>
<html><head>
<title>Example Dental Clinic</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="A friendly dental clinic in Beirut.">
<script type="application/ld+json">
{"@context": "https://schema.org", "@type": "Dentist", "name": "Example Dental Clinic"}
</script>
</head>
<body>
<a href="https://wa.me/9611234567">WhatsApp us</a>
<a href="https://www.google.com/maps/place/example">Find us</a>
<a href="https://calendly.com/example-dental">Book an appointment</a>
</body></html>
"""

BARE_HTML = b"<html><head><title>Just a title</title></head><body>Hello</body></html>"

PARKED_HTML = b"<html><body>This domain is parked. Buy this domain today!</body></html>"


class _Handler(http.server.BaseHTTPRequestHandler):
    routes = {}

    def do_GET(self):
        body, status = self.routes.get(self.path, (b"Not Found", 404))
        self.send_response(status)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass  # keep test output quiet


def _start_server(routes: dict):
    _Handler.routes = routes
    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    return server, f"http://127.0.0.1:{port}"


class TestAuditSite(unittest.TestCase):
    def test_good_site_has_no_missing_feature_problems(self):
        server, base = _start_server({
            "/": (GOOD_HTML, 200),
            "/robots.txt": (b"", 404),
            "/sitemap.xml": (b"<urlset></urlset>", 200),
        })
        try:
            r = audit_site(base, timeout=5)
        finally:
            server.shutdown()
            server.server_close()

        self.assertTrue(r.fetched)
        self.assertEqual(r.title, "Example Dental Clinic")
        self.assertEqual(r.meta_description, "A friendly dental clinic in Beirut.")
        self.assertTrue(r.mobile_viewport)
        self.assertTrue(r.has_jsonld_localbusiness)
        self.assertTrue(r.has_whatsapp_link)
        self.assertTrue(r.has_maps_link)
        self.assertTrue(r.has_booking_link)
        self.assertTrue(r.has_sitemap)
        self.assertFalse(r.broken_or_parked)
        # the test server is plain HTTP, so only the HTTPS problem should remain
        self.assertEqual(r.problems, ["Not using HTTPS, browsers mark it 'Not secure'."])

    def test_bare_site_reports_every_missing_feature(self):
        server, base = _start_server({
            "/": (BARE_HTML, 200),
            "/robots.txt": (b"", 404),
            "/sitemap.xml": (b"", 404),
        })
        try:
            r = audit_site(base, timeout=5)
        finally:
            server.shutdown()
            server.server_close()

        self.assertTrue(r.fetched)
        self.assertFalse(r.mobile_viewport)
        self.assertEqual(r.meta_description, "")
        self.assertFalse(r.has_jsonld_localbusiness)
        self.assertFalse(r.has_whatsapp_link)
        self.assertFalse(r.has_maps_link)
        self.assertFalse(r.has_booking_link)
        self.assertFalse(r.has_sitemap)
        problems_text = " ".join(r.problems)
        self.assertIn("viewport", problems_text)
        self.assertIn("meta description", problems_text)
        self.assertIn("WhatsApp", problems_text)
        self.assertIn("Maps", problems_text)
        self.assertIn("booking", problems_text)

    def test_parked_site_is_flagged(self):
        server, base = _start_server({
            "/": (PARKED_HTML, 200),
            "/robots.txt": (b"", 404),
        })
        try:
            r = audit_site(base, timeout=5)
        finally:
            server.shutdown()
            server.server_close()
        self.assertTrue(r.broken_or_parked)
        self.assertIn("parked", " ".join(r.problems).lower())

    def test_error_status_is_flagged(self):
        server, base = _start_server({
            "/robots.txt": (b"", 404),
        })  # "/" falls through to 404
        try:
            r = audit_site(base, timeout=5)
        finally:
            server.shutdown()
            server.server_close()
        self.assertTrue(r.broken_or_parked)
        self.assertEqual(r.status, 404)

    def test_robots_disallow_is_honored_without_fetching_page(self):
        robots = b"User-agent: *\nDisallow: /\n"
        server, base = _start_server({
            "/robots.txt": (robots, 200),
            "/": (GOOD_HTML, 200),
        })
        try:
            r = audit_site(base, user_agent="leadscout-test/0.1", timeout=5)
        finally:
            server.shutdown()
            server.server_close()
        self.assertTrue(r.robots_disallowed)
        self.assertFalse(r.fetched)

    def test_no_url_reports_problem_without_crashing(self):
        r = audit_site("")
        self.assertIn("No website on file.", r.problems)


if __name__ == "__main__":
    unittest.main()
