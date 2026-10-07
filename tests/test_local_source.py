import os
import unittest

from leadscout import local_source

HERE = os.path.dirname(__file__)
EXAMPLES = os.path.join(HERE, "..", "examples")


class TestLocalSourceJSON(unittest.TestCase):
    def test_autodetect_and_contact_from(self):
        path = os.path.join(EXAMPLES, "sample_businesses.json")
        rows = local_source.load_json(path, contact_from="contact")
        self.assertEqual(len(rows), 2)

        dental = rows[0]
        self.assertEqual(dental["name"], "Example Dental Clinic")
        self.assertEqual(dental["website"], "https://example-dental.test")
        self.assertEqual(dental["city"], "Beirut")
        self.assertEqual(dental["email"], "info@example-dental.test")
        self.assertEqual(dental["phone"], "+961 1 234567")

        salon = rows[1]
        self.assertEqual(salon["phone"], "+961 70 112233")
        self.assertEqual(salon["email"], "")  # empty list in source -> blank, not crash

    def test_bare_list_json(self):
        import json
        import tempfile

        data = [{"name": "Bare List Co", "website": "https://bare.test"}]
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(data, f)
            path = f.name
        try:
            rows = local_source.load(path)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["name"], "Bare List Co")
        finally:
            os.unlink(path)


class TestLocalSourceCSV(unittest.TestCase):
    def test_custom_mapping(self):
        path = os.path.join(EXAMPLES, "sample_businesses.csv")
        rows = local_source.load_csv(path, mapping={
            "name": "company_name", "website": "homepage",
        })
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["name"], "Example Gym")
        self.assertEqual(rows[0]["website"], "https://example-gym.test")
        # auto-detected aliases still fire for fields not overridden
        self.assertEqual(rows[0]["phone"], "+961 3 456789")
        self.assertEqual(rows[1]["email"], "info@acmerealestate.test")

    def test_rows_without_name_are_skipped(self):
        import csv
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["name", "website"])
            writer.writeheader()
            writer.writerow({"name": "", "website": "https://nameless.test"})
            writer.writerow({"name": "Has A Name", "website": "https://named.test"})
            path = f.name
        try:
            rows = local_source.load_csv(path)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["name"], "Has A Name")
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
