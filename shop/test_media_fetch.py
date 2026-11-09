"""The asset fetch behind POST /api/raw/smart-contract-data/ (shop.media.scp_up_url)."""

import io
import os
import shutil
import socket
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from project.utils import safe_fetch
from project.utils.safe_fetch import FetchFailed, UnsafeUrl, fetch_to_file, is_public_address
from shop import media


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        routes = {
            "/image.png": lambda: self._send(200, b"PNGDATA"),
            "/missing.png": lambda: self._send(404, b"<html>Not Found</html>"),
            "/big.png": lambda: self._send(200, b"x" * 64, length=10**12),
            "/stream.png": self._stream_unsized,
            "/slow.png": self._slow,
            "/to-metadata": lambda: self._redirect("http://169.254.169.254/latest/meta-data/"),
            "/to-file": lambda: self._redirect("file:///etc/passwd"),
            "/to-image": lambda: self._redirect("/image.png"),
            "/loop": lambda: self._redirect("/loop"),
        }
        routes.get(self.path, lambda: self._send(404, b""))()

    def _send(self, code, body, length=None):
        self.send_response(code)
        self.send_header("Content-Length", str(len(body) if length is None else length))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _stream_unsized(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"y" * 4096)
        self.close_connection = True

    def _slow(self):
        time.sleep(1.5)
        self._send(200, b"late")


def _loopback_is_public(address):
    """The test server lives on 127.0.0.1; every other address is judged for real."""
    return address == "127.0.0.1" or is_public_address(address)


class _LocalServerTestCase(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        super().tearDownClass()

    def setUp(self):
        patcher = patch.object(safe_fetch, "is_public_address", _loopback_is_public)
        patcher.start()
        self.addCleanup(patcher.stop)


class PublicAddressTests(SimpleTestCase):
    def test_internal_ranges_are_refused(self):
        for address in (
            "127.0.0.1", "10.1.2.3", "172.16.0.1", "192.168.1.1", "169.254.169.254",
            "100.64.0.1", "0.0.0.0", "224.0.0.1", "255.255.255.255", "::1", "fe80::1",
            "fc00::1", "::ffff:127.0.0.1", "::ffff:169.254.169.254", "64:ff9b::a9fe:a9fe",
            "2002:7f00:1::", "fe80::1%eth0",
        ):
            with self.subTest(address=address):
                self.assertFalse(is_public_address(address))

    def test_public_addresses_pass(self):
        for address in ("8.8.8.8", "52.216.0.1", "2606:4700:4700::1111", "64:ff9b::808:808"):
            with self.subTest(address=address):
                self.assertTrue(is_public_address(address))


class FetchRefusalTests(SimpleTestCase):
    def _refused(self, url):
        with self.assertRaises(UnsafeUrl) as caught:
            fetch_to_file(url, io.BytesIO())
        return caught.exception.reason

    def test_only_http_and_https(self):
        for url in ("file:///etc/passwd", "ftp://example.com/a.png", "gopher://example.com/", "a.png"):
            with self.subTest(url=url):
                self.assertEqual(self._refused(url), "scheme")

    def test_private_ip_literals_are_refused_without_connecting(self):
        with patch.object(socket.socket, "connect") as connect:
            for url in (
                "http://127.0.0.1/a.png", "http://169.254.169.254/latest/meta-data/",
                "http://10.0.0.5:8000/a.png", "http://[::1]/a.png", "https://localhost/a.png",
            ):
                with self.subTest(url=url):
                    self.assertEqual(self._refused(url), "blocked_address")
        connect.assert_not_called()

    def test_a_host_with_any_private_address_is_refused(self):
        answers = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.7", 80)),
        ]
        with patch.object(safe_fetch.socket, "getaddrinfo", return_value=answers):
            self.assertEqual(self._refused("http://rebind.example/a.png"), "blocked_address")

    def test_an_unresolvable_host_fails(self):
        error = socket.gaierror(socket.EAI_NONAME, "not known")
        with patch.object(safe_fetch.socket, "getaddrinfo", side_effect=error):
            with self.assertRaises(FetchFailed) as caught:
                fetch_to_file("http://nowhere.invalid/a.png", io.BytesIO())
        self.assertEqual(caught.exception.reason, "unresolvable")


class FetchTests(_LocalServerTestCase):
    def test_streams_the_body(self):
        out = io.BytesIO()
        self.assertEqual(fetch_to_file(f"{self.base}/image.png", out), 7)
        self.assertEqual(out.getvalue(), b"PNGDATA")

    def test_follows_a_redirect_to_an_allowed_url(self):
        out = io.BytesIO()
        fetch_to_file(f"{self.base}/to-image", out)
        self.assertEqual(out.getvalue(), b"PNGDATA")

    def test_an_error_page_is_not_written(self):
        out = io.BytesIO()
        with self.assertRaises(FetchFailed) as caught:
            fetch_to_file(f"{self.base}/missing.png", out)
        self.assertEqual(caught.exception.reason, "http_status")
        self.assertEqual(out.getvalue(), b"")

    def test_a_redirect_to_the_metadata_address_is_refused(self):
        with self.assertRaises(UnsafeUrl) as caught:
            fetch_to_file(f"{self.base}/to-metadata", io.BytesIO())
        self.assertEqual(caught.exception.reason, "blocked_address")

    def test_a_redirect_to_another_scheme_is_refused(self):
        with self.assertRaises(UnsafeUrl) as caught:
            fetch_to_file(f"{self.base}/to-file", io.BytesIO())
        self.assertEqual(caught.exception.reason, "scheme")

    def test_redirects_are_capped(self):
        with self.assertRaises(UnsafeUrl) as caught:
            fetch_to_file(f"{self.base}/loop", io.BytesIO())
        self.assertEqual(caught.exception.reason, "too_many_redirects")

    def test_a_declared_size_over_the_limit_is_refused_before_reading(self):
        out = io.BytesIO()
        with self.assertRaises(UnsafeUrl) as caught:
            fetch_to_file(f"{self.base}/big.png", out)
        self.assertEqual(caught.exception.reason, "too_large")
        self.assertEqual(out.getvalue(), b"")

    def test_an_undeclared_size_is_cut_off_at_the_limit(self):
        with self.assertRaises(UnsafeUrl) as caught:
            fetch_to_file(f"{self.base}/stream.png", io.BytesIO(), max_bytes=1000)
        self.assertEqual(caught.exception.reason, "too_large")

    def test_a_slow_answer_times_out(self):
        with patch.object(safe_fetch, "READ_TIMEOUT", 0.3):
            with self.assertRaises(FetchFailed) as caught:
                fetch_to_file(f"{self.base}/slow.png", io.BytesIO())
        self.assertEqual(caught.exception.reason, "timeout")

    def test_a_closed_port_is_unreachable(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        with self.assertRaises(FetchFailed) as caught:
            fetch_to_file(f"http://127.0.0.1:{port}/a.png", io.BytesIO())
        self.assertEqual(caught.exception.reason, "unreachable")

    def test_environment_proxies_are_ignored(self):
        with patch.dict(os.environ, {"HTTP_PROXY": "http://10.9.9.9:3128", "NO_PROXY": ""}):
            out = io.BytesIO()
            fetch_to_file(f"{self.base}/image.png", out)
        self.assertEqual(out.getvalue(), b"PNGDATA")


class ScpUpUrlTests(_LocalServerTestCase):
    def test_uploads_the_fetched_file(self):
        with override_settings(RBX_TEMP_PATH=self._temp_dir()), \
                patch.object(media, "scp_up_file") as scp:
            remote = media.scp_up_url(f"{self.base}/image.png")
        local_path, remote_path = scp.call_args.args
        self.assertEqual(remote, remote_path)
        self.assertTrue(remote.endswith("/image.png"))
        with open(local_path, "rb") as f:
            self.assertEqual(f.read(), b"PNGDATA")

    def test_a_failed_fetch_uploads_nothing_and_leaves_no_folder(self):
        temp_dir = self._temp_dir()
        with override_settings(RBX_TEMP_PATH=temp_dir), \
                patch.object(media, "scp_up_file") as scp:
            with self.assertRaises(FetchFailed):
                media.scp_up_url(f"{self.base}/missing.png")
            with self.assertRaises(UnsafeUrl):
                media.scp_up_url("http://169.254.169.254/latest/meta-data/iam")
        scp.assert_not_called()
        self.assertEqual(os.listdir(temp_dir), [])

    def _temp_dir(self):
        path = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, path, ignore_errors=True)
        return path


class SmartContractDataErrorTests(SimpleTestCase):
    def _post(self, location):
        from rest_framework.test import APIRequestFactory

        from api.raw.views import SmartContractDataView

        payload = {"SmartContractAsset": {"Name": "image.png", "Location": location}}
        request = APIRequestFactory().post("/raw/smart-contract-data/", payload, format="json")
        return SmartContractDataView.as_view()(request)

    def test_a_private_url_is_refused_with_json(self):
        with patch.object(media, "scp_up_file") as scp:
            response = self._post("http://169.254.169.254/latest/meta-data/iam")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["reason"], "blocked_address")
        self.assertIn("169.254.169.254", response.data["error"])
        scp.assert_not_called()

    def test_a_url_without_a_file_name_is_refused_with_json(self):
        response = self._post("https://example.com/assets/")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["reason"], "invalid_url")

    def test_a_non_http_url_is_refused_with_json(self):
        response = self._post("file:///etc/passwd")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["reason"], "scheme")

    def test_a_failed_fetch_is_a_json_500(self):
        from rbx import client

        failure = FetchFailed("unreachable", "Could not fetch 'https://example.com/a.png'")
        with patch.object(client, "scp_up_url", side_effect=failure):
            response = self._post("https://example.com/a.png")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(
            response.data,
            {"error": "Could not fetch 'https://example.com/a.png'", "reason": "unreachable"},
        )
