import unittest

from leadscout import overpass


class TestTagParsing(unittest.TestCase):
    def test_key_value(self):
        t = overpass.Tag.parse("amenity=dentist")
        self.assertEqual(t.key, "amenity")
        self.assertEqual(t.value, "dentist")

    def test_key_only(self):
        t = overpass.Tag.parse("website")
        self.assertEqual(t.key, "website")
        self.assertIsNone(t.value)

    def test_bad_spec(self):
        with self.assertRaises(ValueError):
            overpass.Tag.parse("!!!not a tag!!!")


class TestBBox(unittest.TestCase):
    def test_parse(self):
        bb = overpass.BBox.parse("33.8,35.4,33.9,35.6")
        self.assertEqual(bb.as_overpass(), "33.8,35.4,33.9,35.6")

    def test_parse_wrong_length(self):
        with self.assertRaises(ValueError):
            overpass.BBox.parse("1,2,3")


class TestBuildQuery(unittest.TestCase):
    def test_includes_all_tags_and_bbox(self):
        bb = overpass.BBox(south=33.8, west=35.4, north=33.9, east=35.6)
        tags = [overpass.Tag.parse("amenity=dentist"), overpass.Tag.parse("shop=hairdresser")]
        q = overpass.build_query(tags, bb, timeout_s=30)
        self.assertIn('[timeout:30]', q)
        self.assertIn('"amenity"="dentist"', q)
        self.assertIn('"shop"="hairdresser"', q)
        self.assertIn("33.8,35.4,33.9,35.6", q)
        self.assertIn("out center tags;", q)


class TestElementsToRecords(unittest.TestCase):
    def test_normalizes_fields_and_skips_unnamed(self):
        data = {
            "elements": [
                {
                    "type": "node", "id": 123, "lat": 33.89, "lon": 35.50,
                    "tags": {
                        "name": "Dr. Example Dentist", "amenity": "dentist",
                        "website": "https://example-dentist.test",
                        "phone": "+961 1 111111",
                        "addr:street": "Main St", "addr:city": "Beirut",
                    },
                },
                {"type": "node", "id": 456, "lat": 33.9, "lon": 35.51, "tags": {"amenity": "dentist"}},
                {
                    "type": "way", "id": 789, "center": {"lat": 33.91, "lon": 35.52},
                    "tags": {"name": "Named Way Clinic", "amenity": "clinic"},
                },
            ]
        }
        records = overpass.elements_to_records(data)
        self.assertEqual(len(records), 2)  # unnamed node dropped

        r = records[0]
        self.assertEqual(r["name"], "Dr. Example Dentist")
        self.assertEqual(r["website"], "https://example-dentist.test")
        self.assertEqual(r["phone"], "+961 1 111111")
        self.assertEqual(r["category"], "dentist")
        self.assertEqual(r["source"], "osm")
        self.assertEqual(r["source_id"], "node/123")
        self.assertIn("Main St", r["address"])

        way_record = records[1]
        self.assertEqual(way_record["lat"], 33.91)
        self.assertEqual(way_record["source_id"], "way/789")


if __name__ == "__main__":
    unittest.main()


class WebsiteOnlyTest(unittest.TestCase):
    def test_website_only_adds_filter(self):
        from leadscout.overpass import BBox, Tag, build_query
        q = build_query([Tag.parse("amenity=dentist")], BBox(1, 2, 3, 4), website_only=True)
        self.assertIn('[~"^(website|contact:website|url)$"~"."]', q)
        self.assertNotIn("website", build_query([Tag.parse("amenity=dentist")], BBox(1, 2, 3, 4)))
