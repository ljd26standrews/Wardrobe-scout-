"""One politeness budget per host for the whole app: a scan, an import job and photos fetched while you
browse share it (audit finding O2)."""

from __future__ import annotations

import threading
import time

import httpx


def _recording():
    stamps: list[tuple[str, float]] = []

    def handler(r: httpx.Request) -> httpx.Response:
        stamps.append((r.url.host, time.monotonic()))
        return httpx.Response(503 if r.url.path == "/busy" else 200, text="ok")

    return stamps, httpx.MockTransport(handler)


def test_two_clients_together_respect_one_host_delay():
    from app.ingestion.http import HostRegistry, PoliteClient

    stamps, transport = _recording()
    shared = HostRegistry()
    scan = PoliteClient(delay=0.2, respect_robots=False, transport=transport, hosts=shared)
    job = PoliteClient(delay=0.2, respect_robots=False, transport=transport, hosts=shared)  # e.g. an import
    threads = [threading.Thread(target=c.get, args=(f"https://a.example/{i}",)) for i, c in enumerate([scan, job] * 2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    times = sorted(t for _, t in stamps)
    assert min(b - a for a, b in zip(times, times[1:], strict=False)) >= 0.18


def test_a_slow_address_lookup_does_not_shorten_the_delay(monkeypatch):
    """The delay is booked after the address check. Booked before it, a slow first DNS answer (cached for the
    second request) let the second request reach the host early: 0.166 s apart for a 0.2 s delay on CI."""
    import socket

    from app.ingestion import http

    real = socket.getaddrinfo

    def slow(host, *a, **kw):
        if host == "slow-dns.example":
            time.sleep(0.12)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
        return real(host, *a, **kw)

    monkeypatch.setattr(socket, "getaddrinfo", slow)
    http._resolve_cache.pop("slow-dns.example", None)
    stamps, transport = _recording()
    client = http.PoliteClient(delay=0.2, respect_robots=False, transport=transport, hosts=http.HostRegistry())
    client.get("https://slow-dns.example/1")
    client.get("https://slow-dns.example/2")
    first, second = (t for _, t in stamps)
    assert second - first >= 0.195


def test_a_back_off_seen_by_one_client_slows_the_other():
    from app.ingestion.http import FetchError, HostRegistry, PoliteClient

    stamps, transport = _recording()
    shared = HostRegistry()
    scan = PoliteClient(delay=0.0, respect_robots=False, transport=transport, hosts=shared)
    viewer = PoliteClient(delay=0.0, respect_robots=False, transport=transport, hosts=shared)
    try:
        scan.get("https://a.example/busy", retries=0)  # the host answers 503: back off
    except FetchError:
        pass
    viewer.get("https://a.example/photo.jpg", binary=True)
    assert stamps[1][1] - stamps[0][1] >= 0.45
    t0 = time.monotonic()
    viewer.get("https://b.example/other")  # another host is not held up
    assert time.monotonic() - t0 < 0.2


def test_real_network_clients_share_the_process_budget():
    from app.ingestion.http import SHARED_HOSTS, PoliteClient

    with PoliteClient() as a, PoliteClient(delay=0.5) as b:  # a scan, and the on-demand photo route
        assert a.host_state("photo.yupoo.com") is b.host_state("photo.yupoo.com")
        assert a.host_state("x.example") is SHARED_HOSTS.get("x.example", 0)
    _, transport = _recording()
    with PoliteClient(transport=transport) as private:  # tests and benchmarks stay isolated
        assert private.host_state("photo.yupoo.com") is not SHARED_HOSTS.get("photo.yupoo.com", 0)
