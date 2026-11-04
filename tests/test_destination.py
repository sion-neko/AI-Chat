import socket
import unittest
from unittest import mock

from app import destination


def resolves_to(*addresses):
    return mock.patch.object(socket, "getaddrinfo", return_value=[(None, None, None, "", (a, 0)) for a in addresses])


class DestinationTest(unittest.TestCase):
    def setUp(self):
        destination.classify.cache_clear()

    def test_local(self):
        with resolves_to("10.1.2.3"):
            self.assertEqual(destination.classify("https://llm.example.com/v1")["kind"], "local")
        destination.classify.cache_clear()
        with resolves_to("127.0.0.1", "::1"):
            self.assertEqual(destination.classify("http://localhost:8000/v1")["kind"], "local")

    def test_external_if_any_public(self):
        with resolves_to("10.1.2.3", "8.8.8.8"):
            self.assertEqual(destination.classify("https://api.example.com/v1")["kind"], "external")

    def test_unknown(self):
        with mock.patch.object(socket, "getaddrinfo", side_effect=socket.gaierror):
            self.assertEqual(destination.classify("https://nowhere.invalid/v1"), {"kind": "unknown", "host": "nowhere.invalid", "addresses": []})


if __name__ == "__main__":
    unittest.main()
