from django.test import SimpleTestCase


def body_of(response):
    """django.views.static.serve returns a streaming FileResponse."""
    return b"".join(response.streaming_content).decode()


class MockupServingTests(SimpleTestCase):
    """The static design mockups are served from /mockups with an index."""

    def test_index_is_served_at_the_directory_url(self):
        response = self.client.get("/mockups/")

        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response["Content-Type"])
        body = body_of(response)
        self.assertIn("Spectrum", body)
        self.assertIn("BrandMeister", body)

    def test_index_links_to_every_mockup(self):
        body = body_of(self.client.get("/mockups/"))

        for page in ("spectrum.html", "receiver.html", "bm-classic.html", "bm-rail.html"):
            self.assertIn(page, body, f"index should link to {page}")

    def test_each_mockup_and_its_assets_are_reachable(self):
        for page in ("spectrum.html", "receiver.html", "bm-classic.html", "bm-rail.html"):
            with self.subTest(page=page):
                response = self.client.get(f"/mockups/{page}")
                self.assertEqual(response.status_code, 200)
                self.assertIn("BrandMeister logo", body_of(response))

        for asset in ("brandmeister-logo.png", "brandmeister-logo-transparent.png", "preview-spectrum.jpg"):
            with self.subTest(asset=asset):
                self.assertEqual(self.client.get(f"/mockups/assets/{asset}").status_code, 200)

    def test_every_mockup_carries_the_disclaimer(self):
        for page in ("spectrum.html", "receiver.html", "bm-classic.html", "bm-rail.html"):
            with self.subTest(page=page):
                body = body_of(self.client.get(f"/mockups/{page}"))
                self.assertIn("mockup-disclaimer", body)
                self.assertIn("not affiliated with, endorsed by, or supported by BrandMeister", body)

    def test_unknown_mockup_is_not_found(self):
        self.assertEqual(self.client.get("/mockups/does-not-exist.html").status_code, 404)

    def test_path_traversal_is_refused(self):
        for attempt in ("/mockups/../config/settings.py", "/mockups/assets/../../dashboard/models.py"):
            with self.subTest(attempt=attempt):
                response = self.client.get(attempt)
                self.assertNotEqual(response.status_code, 200, "traversal must not serve a file")
