import os
import unittest
from unittest import mock

with mock.patch.dict(os.environ, {"LLM_BASE_URL": "http://unused.invalid/v1", "LLM_API_KEY": "x", "LLM_MODEL": "fake"}):
    from app import web


class RequestAllowedTest(unittest.TestCase):
    def test_only_local_host_names(self):
        for host in ("localhost:8080", "127.0.0.1:8080", "[::1]:8080", "localhost"):
            self.assertTrue(web.request_allowed("GET", host, None), host)
        # DNSリバインディング（別の名前で手元のアプリに届く）
        for host in ("evil.example:8080", None, ""):
            self.assertFalse(web.request_allowed("GET", host, None), host)

    def test_writes_only_from_same_origin(self):
        self.assertTrue(web.request_allowed("POST", "localhost:8080", "http://localhost:8080"))
        self.assertTrue(web.request_allowed("DELETE", "localhost:8080", None))
        self.assertFalse(web.request_allowed("POST", "localhost:8080", "https://evil.example"))
        self.assertFalse(web.request_allowed("DELETE", "localhost:8080", "http://localhost:9999"))


if __name__ == "__main__":
    unittest.main()
