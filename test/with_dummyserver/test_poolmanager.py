import gzip
from test import LONG_TIMEOUT
from unittest import mock

import pytest

from dummyserver.server import HAS_IPV6
from dummyserver.testcase import HTTPDummyServerTestCase, IPv6HTTPDummyServerTestCase
from urllib3 import HTTPHeaderDict, HTTPResponse, request
from urllib3.connectionpool import port_by_scheme
from urllib3.exceptions import MaxRetryError, URLSchemeUnknown
from urllib3.poolmanager import PoolManager
from urllib3.util.retry import Retry

# Retry failed tests
pytestmark = pytest.mark.flaky


class TestPoolManager(HTTPDummyServerTestCase):
    @classmethod
    def setup_class(cls) -> None:
        super().setup_class()
        cls.base_url = f"http://{cls.host}:{cls.port}"
        cls.base_url_alt = f"http://{cls.host_alt}:{cls.port}"

    def test_redirect(self) -> None:
        with PoolManager() as http:
            r = http.request(
                "GET",
                f"{self.base_url}/redirect",
                fields={"target": f"{self.base_url}/"},
                redirect=False,
            )

            assert r.status == 303

            r = http.request(
                "GET",
                f"{self.base_url}/redirect",
                fields={"target": f"{self.base_url}/"},
            )

            assert r.status == 200
            assert r.data == b"Dummy server!"

    def test_redirect_twice(self) -> None:
        with PoolManager() as http:
            r = http.request(
                "GET",
                f"{self.base_url}/redirect",
                fields={"target": f"{self.base_url}/redirect"},
                redirect=False,
            )

            assert r.status == 303

            r = http.request(
                "GET",
                f"{self.base_url}/redirect",
                fields={"target": f"{self.base_url}/redirect?target={self.base_url}/"},
            )

            assert r.status == 200
            assert r.data == b"Dummy server!"

    def test_redirect_to_relative_url(self) -> None:
        with PoolManager() as http:
            r = http.request(
                "GET",
                f"{self.base_url}/redirect",
                fields={"target": "/redirect"},
                redirect=False,
            )

            assert r.status == 303

            r = http.request(
                "GET", f"{self.base_url}/redirect", fields={"target": "/redirect"}
            )

            assert r.status == 200
            assert r.data == b"Dummy server!"

    def test_cross_host_redirect(self) -> None:
        with PoolManager() as http:
            cross_host_location = f"{self.base_url_alt}/echo?a=b"
            with pytest.raises(MaxRetryError):
                http.request(
                    "GET",
                    f"{self.base_url}/redirect",
                    fields={"target": cross_host_location},
                    timeout=LONG_TIMEOUT,
                    retries=0,
                )

            r = http.request(
                "GET",
                f"{self.base_url}/redirect",
                fields={"target": f"{self.base_url_alt}/echo?a=b"},
                timeout=LONG_TIMEOUT,
                retries=1,
            )

            assert isinstance(r, HTTPResponse)
            assert r._pool is not None
            assert r._pool.host == self.host_alt

    def test_too_many_redirects(self) -> None:
        with PoolManager() as http:
            with pytest.raises(MaxRetryError):
                http.request(
                    "GET",
                    f"{self.base_url}/redirect",
                    fields={
                        "target": f"{self.base_url}/redirect?target={self.base_url}/"
                    },
                    retries=1,
                    preload_content=False,
                )

            with pytest.raises(MaxRetryError):
                http.request(
                    "GET",
                    f"{self.base_url}/redirect",
                    fields={
                        "target": f"{self.base_url}/redirect?target={self.base_url}/"
                    },
                    retries=Retry(total=None, redirect=1),
                    preload_content=False,
                )

            # Even with preload_content=False and raise on redirects, we reused the same
            # connection
            assert len(http.pools) == 1
            pool = http.connection_from_host(self.host, self.port)
            assert pool.num_connections == 1

    def test_redirect_cross_host_remove_headers(self) -> None:
        with PoolManager() as http:
            r = http.request(
                "GET",
                f"{self.base_url}/redirect",
                fields={"target": f"{self.base_url_alt}/headers"},
                headers={"Authorization": "foo"},
            )

            assert r.status == 200

            data = r.json()

            assert "Authorization" not in data

            r = http.request(
                "GET",
                f"{self.base_url}/redirect",
                fields={"target": f"{self.base_url_alt}/headers"},
                headers={"authorization": "foo"},
            )

            assert r.status == 200

            data = r.json()

            assert "authorization" not in data
            assert "Authorization" not in data

    def test_redirect_cross_host_no_remove_headers(self) -> None:
        with PoolManager() as http:
            r = http.request(
                "GET",
                f"{self.base_url}/redirect",
                fields={"target": f"{self.base_url_alt}/headers"},
                headers={"Authorization": "foo"},
                retries=Retry(remove_headers_on_redirect=[]),
            )

            assert r.status == 200

            data = r.json()

            assert data["Authorization"] == "foo"

    def test_redirect_cross_host_set_removed_headers(self) -> None:
        with PoolManager() as http:
            r = http.request(
                "GET",
                f"{self.base_url}/redirect",
                fields={"target": f"{self.base_url_alt}/headers"},
                headers={"X-API-Secret": "foo", "Authorization": "bar"},
                retries=Retry(remove_headers_on_redirect=["X-API-Secret"]),
            )

            assert r.status == 200

            data = r.json()

            assert "X-API-Secret" not in data
            assert data["Authorization"] == "bar"

            headers = {"x-api-secret": "foo", "authorization": "bar"}
            r = http.request(
                "GET",
                f"{self.base_url}/redirect",
                fields={"target": f"{self.base_url_alt}/headers"},
                headers=headers,
                retries=Retry(remove_headers_on_redirect=["X-API-Secret"]),
            )

            assert r.status == 200

            data = r.json()

            assert "x-api-secret" not in data
            assert "X-API-Secret" not in data
            assert data["Authorization"] == "bar"

            # Ensure the header argument itself is not modified in-place.
            assert headers == {"x-api-secret": "foo", "authorization": "bar"}

    def test_redirect_without_preload_releases_connection(self) -> None:
        with PoolManager(block=True, maxsize=2) as http:
            r = http.request("GET", f"{self.base_url}/redirect", preload_content=False)
            assert isinstance(r, HTTPResponse)
            assert r._pool is not None
            assert r._pool.num_requests == 2
            assert r._pool.num_connections == 1
            assert len(http.pools) == 1

    def test_unknown_scheme(self) -> None:
        with PoolManager() as http:
            unknown_scheme = "unknown"
            unknown_scheme_url = f"{unknown_scheme}://host"
            with pytest.raises(URLSchemeUnknown) as e:
                r = http.request("GET", unknown_scheme_url)
            assert e.value.scheme == unknown_scheme
            r = http.request(
                "GET",
                f"{self.base_url}/redirect",
                fields={"target": unknown_scheme_url},
                redirect=False,
            )
            assert r.status == 303
            assert r.headers.get("Location") == unknown_scheme_url
            with pytest.raises(URLSchemeUnknown) as e:
                r = http.request(
                    "GET",
                    f"{self.base_url}/redirect",
                    fields={"target": unknown_scheme_url},
                )
            assert e.value.scheme == unknown_scheme

    def test_raise_on_redirect(self) -> None:
        with PoolManager() as http:
            r = http.request(
                "GET",
                f"{self.base_url}/redirect",
                fields={"target": f"{self.base_url}/redirect?target={self.base_url}/"},
                retries=Retry(total=None, redirect=1, raise_on_redirect=False),
            )

            assert r.status == 303

    def test_raise_on_status(self) -> None:
        with PoolManager() as http:
            with pytest.raises(MaxRetryError):
                # the default is to raise
                r = http.request(
                    "GET",
                    f"{self.base_url}/status",
                    fields={"status": "500 Internal Server Error"},
                    retries=Retry(total=1, status_forcelist=range(500, 600)),
                )

            with pytest.raises(MaxRetryError):
                # raise explicitly
                r = http.request(
                    "GET",
                    f"{self.base_url}/status",
                    fields={"status": "500 Internal Server Error"},
                    retries=Retry(
                        total=1, status_forcelist=range(500, 600), raise_on_status=True
                    ),
                )

            # don't raise
            r = http.request(
                "GET",
                f"{self.base_url}/status",
                fields={"status": "500 Internal Server Error"},
                retries=Retry(
                    total=1, status_forcelist=range(500, 600), raise_on_status=False
                ),
            )

            assert r.status == 500

    def test_missing_port(self) -> None:
        # Can a URL that lacks an explicit port like ':80' succeed, or
        # will all such URLs fail with an error?

        with PoolManager() as http:
            # By globally adjusting `port_by_scheme` we pretend for a moment
            # that HTTP's default port is not 80, but is the port at which
            # our test server happens to be listening.
            port_by_scheme["http"] = self.port
            try:
                r = http.request("GET", f"http://{self.host}/", retries=0)
            finally:
                port_by_scheme["http"] = 80

            assert r.status == 200
            assert r.data == b"Dummy server!"

    def test_headers(self) -> None:
        with PoolManager(headers={"Foo": "bar"}) as http:
            r = http.request("GET", f"{self.base_url}/headers")
            returned_headers = r.json()
            assert returned_headers.get("Foo") == "bar"

            r = http.request("POST", f"{self.base_url}/headers")
            returned_headers = r.json()
            assert returned_headers.get("Foo") == "bar"

            r = http.request_encode_url("GET", f"{self.base_url}/headers")
            returned_headers = r.json()
            assert returned_headers.get("Foo") == "bar"

            r = http.request_encode_body("POST", f"{self.base_url}/headers")
            returned_headers = r.json()
            assert returned_headers.get("Foo") == "bar"

            r = http.request_encode_url(
                "GET", f"{self.base_url}/headers", headers={"Baz": "quux"}
            )
            returned_headers = r.json()
            assert returned_headers.get("Foo") is None
            assert returned_headers.get("Baz") == "quux"

            r = http.request_encode_body(
                "GET", f"{self.base_url}/headers", headers={"Baz": "quux"}
            )
            returned_headers = r.json()
            assert returned_headers.get("Foo") is None
            assert returned_headers.get("Baz") == "quux"

    def test_headers_http_header_dict(self) -> None:
        headers = HTTPHeaderDict()
        headers.add("Foo", "bar")
        headers.add("Multi", "1")
        headers.add("Baz", "quux")
        headers.add("Multi", "2")

        with PoolManager(headers=headers) as http:
            r = http.request("GET", f"{self.base_url}/headers")
            returned_headers = r.json()
            assert returned_headers["Foo"] == "bar"
            assert returned_headers["Multi"] == "1, 2"
            assert returned_headers["Baz"] == "quux"

            r = http.request(
                "GET",
                f"{self.base_url}/headers",
                headers={
                    **headers,
                    "Extra": "extra",
                    "Foo": "new",
                },
            )
            returned_headers = r.json()
            assert returned_headers["Foo"] == "new"
            assert returned_headers["Multi"] == "1, 2"
            assert returned_headers["Baz"] == "quux"
            assert returned_headers["Extra"] == "extra"

    def test_body(self) -> None:
        with PoolManager() as http:
            r = http.request("POST", f"{self.base_url}/echo", body=b"test")
            assert r.data == b"test"

    def test_http_with_ssl_keywords(self) -> None:
        with PoolManager(ca_certs="REQUIRED") as http:
            r = http.request("GET", f"http://{self.host}:{self.port}/")
            assert r.status == 200

    def test_http_with_ca_cert_dir(self) -> None:
        with PoolManager(ca_certs="REQUIRED", ca_cert_dir="/nosuchdir") as http:
            r = http.request("GET", f"http://{self.host}:{self.port}/")
            assert r.status == 200

    @pytest.mark.parametrize(
        ["target", "expected_target"],
        [
            ("/echo_uri?q=1#fragment", b"/echo_uri?q=1"),
            ("/echo_uri?#", b"/echo_uri?"),
            ("/echo_uri#?", b"/echo_uri"),
            ("/echo_uri#?#", b"/echo_uri"),
            ("/echo_uri??#", b"/echo_uri??"),
            ("/echo_uri?%3f#", b"/echo_uri?%3F"),
            ("/echo_uri?%3F#", b"/echo_uri?%3F"),
            ("/echo_uri?[]", b"/echo_uri?%5B%5D"),
        ],
    )
    def test_encode_http_target(self, target: str, expected_target: bytes) -> None:
        with PoolManager() as http:
            url = f"http://{self.host}:{self.port}{target}"
            r = http.request("GET", url)
            assert r.data == expected_target

    def test_top_level_request(self) -> None:
        r = request("GET", f"{self.base_url}/")
        assert r.status == 200
        assert r.data == b"Dummy server!"

    def test_top_level_request_without_keyword_args(self) -> None:
        body = ""
        with pytest.raises(TypeError):
            request("GET", f"{self.base_url}/", body)  # type: ignore[misc]

    def test_top_level_request_with_body(self) -> None:
        r = request("POST", f"{self.base_url}/echo", body=b"test")
        assert r.status == 200
        assert r.data == b"test"

    def test_top_level_request_with_preload_content(self) -> None:
        r = request("GET", f"{self.base_url}/echo", preload_content=False)
        assert r.status == 200
        assert r.connection is not None
        r.data
        assert r.connection is None

    def test_top_level_request_with_decode_content(self) -> None:
        r = request(
            "GET",
            f"{self.base_url}/encodingrequest",
            headers={"accept-encoding": "gzip"},
            decode_content=False,
        )
        assert r.status == 200
        assert gzip.decompress(r.data) == b"hello, world!"

        r = request(
            "GET",
            f"{self.base_url}/encodingrequest",
            headers={"accept-encoding": "gzip"},
            decode_content=True,
        )
        assert r.status == 200
        assert r.data == b"hello, world!"

    def test_top_level_request_with_redirect(self) -> None:
        r = request(
            "GET",
            f"{self.base_url}/redirect",
            fields={"target": f"{self.base_url}/"},
            redirect=False,
        )

        assert r.status == 303

        r = request(
            "GET",
            f"{self.base_url}/redirect",
            fields={"target": f"{self.base_url}/"},
            redirect=True,
        )

        assert r.status == 200
        assert r.data == b"Dummy server!"

    def test_top_level_request_with_retries(self) -> None:
        r = request("GET", f"{self.base_url}/redirect", retries=False)
        assert r.status == 303

        r = request("GET", f"{self.base_url}/redirect", retries=3)
        assert r.status == 200

    def test_top_level_request_with_timeout(self) -> None:
        with mock.patch("urllib3.poolmanager.RequestMethods.request") as mockRequest:
            mockRequest.return_value = HTTPResponse(status=200)

            r = request("GET", f"{self.base_url}/redirect", timeout=2.5)

            assert r.status == 200

            mockRequest.assert_called_with(
                "GET",
                f"{self.base_url}/redirect",
                body=None,
                fields=None,
                headers=None,
                preload_content=True,
                decode_content=True,
                redirect=True,
                retries=None,
                timeout=2.5,
                json=None,
            )

    @pytest.mark.parametrize(
        "headers",
        [
            None,
            {"content-Type": "application/json"},
            {"content-Type": "text/plain"},
            {"attribute": "value", "CONTENT-TYPE": "application/json"},
            HTTPHeaderDict(cookie="foo, bar"),
        ],
    )
    def test_request_with_json(self, headers: HTTPHeaderDict) -> None:
        body = {"attribute": "value"}
        r = request(
            method="POST", url=f"{self.base_url}/echo_json", headers=headers, json=body
        )
        assert r.status == 200
        assert r.json() == body
        if headers is not None and "application/json" not in headers.values():
            assert "text/plain" in r.headers["Content-Type"].replace(" ", "").split(",")
        else:
            assert "application/json" in r.headers["Content-Type"].replace(
                " ", ""
            ).split(",")

    def test_top_level_request_with_json_with_httpheaderdict(self) -> None:
        body = {"attribute": "value"}
        header = HTTPHeaderDict(cookie="foo, bar")
        with PoolManager(headers=header) as http:
            r = http.request(method="POST", url=f"{self.base_url}/echo_json", json=body)
            assert r.status == 200
            assert r.json() == body
            assert "application/json" in r.headers["Content-Type"].replace(
                " ", ""
            ).split(",")

    def test_top_level_request_with_body_and_json(self) -> None:
        match = "request got values for both 'body' and 'json' parameters which are mutually exclusive"
        with pytest.raises(TypeError, match=match):
            body = {"attribute": "value"}
            request(method="POST", url=f"{self.base_url}/echo", body="", json=body)


@pytest.mark.skipif(not HAS_IPV6, reason="IPv6 is not supported on this system")
class TestIPv6PoolManager(IPv6HTTPDummyServerTestCase):
    @classmethod
    def setup_class(cls) -> None:
        super().setup_class()
        cls.base_url = f"http://[{cls.host}]:{cls.port}"

    def test_ipv6(self) -> None:
        with PoolManager() as http:
            http.request("GET", self.base_url)
