import tempfile
import threading
import unittest
from functools import partial
from http.client import HTTPConnection
from pathlib import Path

from src import web


class StaticSiteHandlerTest(unittest.TestCase):
    def test_container_keeps_web_module_and_site_under_the_same_root(self):
        dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
        self.assertIn("COPY src/web.py /app/src/web.py", dockerfile)
        self.assertIn("COPY site /app/site", dockerfile)
        self.assertIn("python /app/src/web.py", dockerfile)

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        Path(self.temp_dir.name, "index.html").write_text("<h1>AI-Signal</h1>", encoding="utf-8")
        handler = partial(web.StaticSiteHandler, directory=self.temp_dir.name)
        self.server = web.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.temp_dir.cleanup()

    def request(self, path):
        connection = HTTPConnection("127.0.0.1", self.server.server_port)
        connection.request("GET", path)
        response = connection.getresponse()
        body = response.read()
        connection.close()
        return response.status, body

    def test_health_checks_do_not_require_application_secrets(self):
        for path in ("/healthz", "/readyz"):
            status, body = self.request(path)
            self.assertEqual(status, 200)
            self.assertEqual(body, b'{"status":"ok"}\n')

    def test_serves_the_published_site(self):
        status, body = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn(b"AI-Signal", body)


if __name__ == "__main__":
    unittest.main()
