"""Company logos: domain derivation, host safety, fetcher limits, disk cache, the image route and the toggle.

No network: the fetcher runs against an httpx MockTransport and a fake resolver; the cache tests use a fixed clock.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from amorehiremepls.core.config import Settings
from amorehiremepls.core.runtime import Runtime
from amorehiremepls.core.worker import Worker
from amorehiremepls.jobs.logos import (
    FETCH_STRATEGY,
    REQUEST_TIMEOUT,
    LogoCache,
    LogoFetcher,
    apex_domain,
    derive_domain,
    employer_key,
    icon_links,
    is_public_hostname,
    resolves_public,
    sniff_image,
)
from amorehiremepls.web.ui_settings import UISettings, UISettingsStore
from amorehiremepls.web.web import create_app
from tests.conftest import TEST_MONGO_URI
from tests.test_ui_settings import png_bytes

PUBLIC = lambda host: ["93.184.216.34"]  # noqa: E731 - test resolver: every name is public
ICO = b"\x00\x00\x01\x00" + b"\x01\x00\x10\x10" + b"\x00" * 60


# -- pure helpers -------------------------------------------------------------------------------------------


def test_employer_key_is_url_and_filesystem_safe():
    assert employer_key("Acme Corp.") == "acme-corp"
    assert employer_key("  Übër/Inc  ") == "b-r-inc"
    assert employer_key("") == "unknown"
    assert len(employer_key("x" * 500)) == 80


@pytest.mark.parametrize(
    ("job", "expected"),
    [
        ({"employer": "Acme", "posting_url": "https://careers.acme.example/jobs/123"}, "acme.example"),
        ({"employer": "Acme", "posting_url": "https://www.acme.example/careers/123"}, "acme.example"),
        ({"employer": "Acme", "company_website": "acme.example"}, "acme.example"),
        ({"employer": "Acme", "posting_url": "https://boards.greenhouse.io/unknownco/jobs/1"}, None),  # board slug not mapped -> no guess
        ({"employer": "Faire", "posting_url": "https://boards.greenhouse.io/faire/jobs/1"}, "faire.com"),  # explicit mapping
        ({"employer": "Faire", "posting_url": "https://job-boards.greenhouse.io/embed/faire/jobs/1"}, "faire.com"),
        ({"employer": "X", "posting_url": "https://jobs.lever.co/unknownco/abc"}, None),
        ({"employer": "X", "posting_url": "https://jobs.ashbyhq.com/unknownco/abc"}, None),
        ({"employer": "X", "posting_url": "https://unknownco.wd5.myworkdayjobs.com/en-US/Careers/job/1"}, None),
        ({"employer": "X", "posting_url": "https://www.linkedin.com/jobs/view/123"}, None),
        ({"employer": "X", "posting_url": "https://ca.indeed.com/viewjob?jk=1"}, None),
        # a board link is skipped and the next URL is used
        ({"employer": "X", "posting_url": "https://www.linkedin.com/jobs/view/123", "apply_url": "https://apply.beta.example/r/1"}, "beta.example"),
        ({"employer": "X", "posting_url": "http://localhost:8000/job"}, None),
        ({"employer": "X", "posting_url": "http://10.0.0.5/job"}, None),
        ({"employer": "X", "posting_url": "http://intranet/job"}, None),  # no dot: not a public site
        ({"employer": "X", "posting_url": "ftp://files.example/job"}, None),
        ({"employer": "X", "posting_url": "not a url"}, None),
        ({"employer": "X"}, None),
        (None, None),
        # a job-board link plus a known employer name uses the alias table, not a guess from the board host
        ({"employer": "Robinhood", "posting_url": "https://www.linkedin.com/jobs/view/123"}, "robinhood.com"),
        ({"employer": "CIBC", "posting_url": "https://cibc.wd3.myworkdayjobs.com/en-US/campus/job/1"}, "cibc.com"),
        ({"employer": "Toast", "posting_url": "https://www.linkedin.com/jobs/view/9"}, "toasttab.com"),
        ({"employer": "PointClickCare", "posting_url": "https://jobs.lever.co/pointclickcare/abc"}, "pointclickcare.com"),
        ({"employer": "Nope Inc", "posting_url": "https://www.linkedin.com/jobs/view/1"}, None),
    ],
)
def test_derive_domain(job, expected):
    assert derive_domain(job) == expected


SCREENSHOT_DOMAINS = {
    "Robinhood": "robinhood.com",
    "Electric Mind": "electricmind.com",
    "CIBC": "cibc.com",
    "PointClickCare": "pointclickcare.com",
    "Autodesk": "autodesk.com",
    "Broadridge": "broadridge.com",
    "DoorDash": "doordash.com",
    "Toast": "toasttab.com",
    "Interac": "interac.ca",
    "Marqeta": "marqeta.com",
    "Tata Consultancy Services": "tcs.com",
    "The MRG Group": "themrggroup.com",
    "Citi": "citi.com",
    "Stripe": "stripe.com",
    "Faire": "faire.com",
}


def test_screenshot_employers_resolve_when_the_posting_is_only_a_job_board():
    for name, domain in SCREENSHOT_DOMAINS.items():
        job = {"employer": name, "posting_url": "https://www.linkedin.com/jobs/view/1"}
        assert derive_domain(job) == domain, name


def test_apex_strips_only_known_careers_prefixes():
    assert apex_domain("careers.www.acme.example") == "acme.example"
    assert apex_domain("eng.acme.example") == "eng.acme.example"
    assert apex_domain("acme.co.uk") == "acme.co.uk"


PRIVATE_HOSTS = ["localhost", "LOCALHOST", "foo.localhost", "nas.local", "db.internal", "127.0.0.1", "10.1.2.3", "172.16.0.9", "192.168.2.49"]
PRIVATE_HOSTS += ["169.254.169.254", "0.0.0.0", "::1", "fe80::1", "fd00::5", "224.0.0.1"]


@pytest.mark.parametrize("host", PRIVATE_HOSTS)
def test_private_and_special_hosts_are_rejected(host):
    assert not is_public_hostname(host)
    assert not resolves_public(host, PUBLIC)


def test_public_hosts_pass_and_dns_to_private_is_rejected():
    assert is_public_hostname("acme.example")
    assert is_public_hostname("93.184.216.34")
    assert resolves_public("acme.example", PUBLIC)
    assert not resolves_public("acme.example", lambda h: ["10.0.0.1"])  # DNS rebinding / internal name
    assert not resolves_public("acme.example", lambda h: ["93.184.216.34", "127.0.0.1"])  # every address must be public
    assert not resolves_public("acme.example", lambda h: [])

    def boom(h):
        raise OSError("no dns")

    assert not resolves_public("acme.example", boom)


def test_sniff_accepts_raster_icons_only():
    assert sniff_image(png_bytes()) == ("png", "image/png")
    assert sniff_image(ICO) == ("ico", "image/x-icon")
    assert sniff_image(b"\xff\xd8\xff\xe0rest") == ("jpg", "image/jpeg")
    assert sniff_image(b"GIF89a....") == ("gif", "image/gif")
    assert sniff_image(b"<svg xmlns='http://www.w3.org/2000/svg'></svg>") is None
    assert sniff_image(b"<html>") is None
    assert sniff_image(b"") is None


def test_icon_links_parse_rel_variants_and_resolve_relative_hrefs():
    html = """<html><head>
    <link rel="stylesheet" href="/a.css">
    <LINK REL="Shortcut Icon" HREF="/img/fav.ico">
    <link rel='icon' type='image/png' href='icons/32.png'>
    <link rel="apple-touch-icon" href="https://cdn.acme.example/touch.png">
    <link rel="icon" href="data:image/png;base64,AAAA">
    </head></html>"""
    assert icon_links(html, "https://acme.example/") == ["https://acme.example/img/fav.ico", "https://acme.example/icons/32.png", "https://cdn.acme.example/touch.png"]


# -- fetcher against a mock transport ------------------------------------------------------------------------


def _fetcher(routes: dict[str, httpx.Response], max_bytes: int = 512 * 1024) -> LogoFetcher:
    def handler(request: httpx.Request) -> httpx.Response:
        return routes.get(str(request.url), httpx.Response(404))

    return LogoFetcher(timeout=1.0, max_bytes=max_bytes, resolver=PUBLIC, transport=httpx.MockTransport(handler))


def test_fetcher_prefers_favicon_then_link_rel_icon():
    f = _fetcher({"https://acme.example/favicon.ico": httpx.Response(200, content=ICO, headers={"content-type": "image/x-icon"})})
    got = f.fetch("acme.example")
    assert got and got.ext == "ico" and got.media_type == "image/x-icon" and got.source == "https://acme.example/favicon.ico"

    f = _fetcher(
        {
            "https://acme.example/": httpx.Response(200, text='<html><head><link rel="icon" href="/static/logo.png"></head></html>', headers={"content-type": "text/html"}),
            "https://acme.example/static/logo.png": httpx.Response(200, content=png_bytes(), headers={"content-type": "image/png"}),
        }
    )
    got = f.fetch("acme.example")
    assert got and got.ext == "png" and got.source == "https://acme.example/static/logo.png"


def test_fetcher_rejects_wrong_type_svg_oversize_and_private_redirects():
    html = httpx.Response(200, text="<html></html>", headers={"content-type": "text/html"})
    # text served as favicon.ico, no <link rel=icon>: nothing
    assert _fetcher({"https://acme.example/favicon.ico": html, "https://acme.example/": html}).fetch("acme.example") is None
    # content-type says image but the bytes are svg: nothing
    svg = httpx.Response(200, content=b"<svg xmlns='http://www.w3.org/2000/svg'/>", headers={"content-type": "image/svg+xml"})
    assert _fetcher({"https://acme.example/favicon.ico": svg, "https://acme.example/": html}).fetch("acme.example") is None
    # over the cap (by header and by body)
    big = httpx.Response(200, content=png_bytes() + b"\x00" * 2000, headers={"content-type": "image/png"})
    assert _fetcher({"https://acme.example/favicon.ico": big, "https://acme.example/": html}, max_bytes=1000).fetch("acme.example") is None
    # redirect to a private host is not followed
    hop = httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data"})
    assert _fetcher({"https://acme.example/favicon.ico": hop, "https://acme.example/": html}).fetch("acme.example") is None
    # a redirect to another public host is followed, within the hop limit
    routes = {
        "https://acme.example/favicon.ico": httpx.Response(301, headers={"location": "https://cdn.acme.example/fav.ico"}),
        "https://cdn.acme.example/fav.ico": httpx.Response(200, content=ICO, headers={"content-type": "image/vnd.microsoft.icon"}),
    }
    got = _fetcher(routes).fetch("acme.example")
    assert got and got.source == "https://cdn.acme.example/fav.ico"
    loop = {"https://acme.example/favicon.ico": httpx.Response(302, headers={"location": "https://acme.example/favicon.ico"}), "https://acme.example/": html}
    assert _fetcher(loop).fetch("acme.example") is None
    # never fetches a private domain at all
    assert _fetcher({}).fetch("192.168.2.49") is None


def test_fetcher_falls_back_to_www_when_the_apex_host_fails():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "acme.example":
            raise httpx.ConnectError("certificate verify failed")
        if str(request.url) == "https://www.acme.example/favicon.ico":
            return httpx.Response(200, content=ICO, headers={"content-type": "image/x-icon"})
        return httpx.Response(404)

    got = LogoFetcher(timeout=1.0, resolver=PUBLIC, transport=httpx.MockTransport(handler)).fetch("acme.example")
    assert got and got.source == "https://www.acme.example/favicon.ico"

    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    with pytest.raises(httpx.TimeoutException):  # every source timed out: the short budget is not stored as a miss
        LogoFetcher(timeout=1.0, resolver=PUBLIC, transport=httpx.MockTransport(slow)).fetch("acme.example")


def test_fetcher_falls_back_to_duckduckgo_then_google():
    html = httpx.Response(200, text="<html></html>", headers={"content-type": "text/html"})

    def ddg_only(request: httpx.Request) -> httpx.Response:
        if request.url.host == "icons.duckduckgo.com":
            return httpx.Response(200, content=ICO, headers={"content-type": "image/x-icon"})
        return html

    got = LogoFetcher(timeout=1.0, resolver=PUBLIC, transport=httpx.MockTransport(ddg_only)).fetch("acme.example")
    assert got and got.ext == "ico" and got.source.startswith("https://icons.duckduckgo.com/ip3/acme.example.ico")

    def google_after_svg(request: httpx.Request) -> httpx.Response:
        if request.url.host == "icons.duckduckgo.com":
            return httpx.Response(200, content=b"<svg xmlns='http://www.w3.org/2000/svg'/>", headers={"content-type": "image/svg+xml"})
        if request.url.host == "www.google.com":
            return httpx.Response(200, content=png_bytes(), headers={"content-type": "image/png"})
        return html

    got = LogoFetcher(timeout=1.0, resolver=PUBLIC, transport=httpx.MockTransport(google_after_svg)).fetch("acme.example")
    assert got and got.ext == "png" and "google.com/s2/favicons" in got.source and "domain=acme.example" in got.source

    def private_hop(request: httpx.Request) -> httpx.Response:
        if request.url.host == "icons.duckduckgo.com":
            return httpx.Response(302, headers={"location": "http://169.254.169.254/icon"})
        if request.url.host == "www.google.com":
            return httpx.Response(200, content=png_bytes(), headers={"content-type": "image/png"})
        return html

    got = LogoFetcher(timeout=1.0, resolver=PUBLIC, transport=httpx.MockTransport(private_hop)).fetch("acme.example")
    assert got and "google.com/s2/favicons" in got.source


# -- disk cache ---------------------------------------------------------------------------------------------


class FakeFetcher:
    def __init__(self, result=None, exc=None):
        self.result, self.exc, self.calls = result, exc, []

    def fetch(self, domain, timeout=None):
        self.calls.append((domain, timeout))
        if self.exc:
            raise self.exc
        return self.result


def _cache(tmp_path: Path, fetcher, start: datetime | None = None) -> tuple[LogoCache, list[datetime]]:
    clock = [start or datetime(2026, 11, 5, 12, tzinfo=UTC)]
    return LogoCache(tmp_path / "artifacts", fetcher=fetcher, now=lambda: clock[0]), clock


def test_cache_stores_bytes_and_sidecar_and_hits_without_refetch(tmp_path):
    from amorehiremepls.jobs.logos import Fetched

    fetcher = FakeFetcher(Fetched(png_bytes(), "png", "image/png", "https://acme.example/favicon.ico"))
    cache, _ = _cache(tmp_path, fetcher)
    job = {"employer": "Acme Corp", "posting_url": "https://careers.acme.example/j/1"}
    assert cache.src_for(job) == "/logos/acme-corp.png"  # registered as wanted, nothing fetched yet
    assert cache.wanted == {"acme-corp": "acme.example"} and fetcher.calls == []
    path, media = cache.ensure("acme-corp", "acme.example")
    assert path == tmp_path / "artifacts" / "public-cache" / "logos" / "acme-corp.png" and media == "image/png"
    assert path.read_bytes() == png_bytes()
    meta = json.loads((path.parent / "acme-corp.json").read_text())
    assert meta["status"] == "ok" and meta["source"] == "https://acme.example/favicon.ico" and meta["fetched_at"].startswith("2026-11-05T12:00") and meta["ext"] == "png"
    # second ensure and later lookups are cache hits
    assert cache.ensure("acme-corp", "acme.example") == (path, "image/png")
    assert cache.lookup("acme-corp") == (path, "image/png")
    assert len(fetcher.calls) == 1
    assert cache.src_for({"employer": "Acme Corp"}) == "/logos/acme-corp.png"  # cached: no URL needed any more


def test_negative_cache_expires_after_seven_days(tmp_path):
    fetcher = FakeFetcher(None)
    cache, clock = _cache(tmp_path, fetcher)
    assert cache.ensure("nope-inc", "nope.example") is None
    assert json.loads((cache.dir / "nope-inc.json").read_text())["status"] == "missing"
    assert cache.known_missing("nope-inc") and not cache.needs_fetch("nope-inc")
    assert cache.src_for({"employer": "Nope Inc", "posting_url": "https://nope.example/x"}) is None  # template shows initials, no image request
    assert cache.ensure("nope-inc", "nope.example") is None and len(fetcher.calls) == 1
    clock[0] += timedelta(days=7, seconds=1)
    assert cache.needs_fetch("nope-inc")
    cache.ensure("nope-inc", "nope.example")
    assert len(fetcher.calls) == 2


def test_old_negative_cache_without_the_current_strategy_is_retried(tmp_path):
    from amorehiremepls.jobs.logos import Fetched

    fetcher = FakeFetcher(Fetched(png_bytes(), "png", "image/png", "https://robinhood.com/favicon.ico"))
    cache, _ = _cache(tmp_path, fetcher)
    cache.dir.mkdir(parents=True, exist_ok=True)
    (cache.dir / "robinhood.json").write_text(json.dumps({"fetched_at": "2026-11-05T12:00:00+00:00", "status": "missing", "domain": "robinhood.com"}))
    assert not cache.known_missing("robinhood") and cache.needs_fetch("robinhood")
    path, _media = cache.ensure("robinhood", "robinhood.com")
    assert path is not None and path.exists()
    meta = json.loads((cache.dir / "robinhood.json").read_text())
    assert meta["status"] == "ok" and meta["strategy"] == FETCH_STRATEGY
    assert len(fetcher.calls) == 1


def test_network_errors_are_negative_cached_but_short_budget_timeouts_are_not(tmp_path):
    cache, _ = _cache(tmp_path, FakeFetcher(exc=httpx.ConnectError("down")))
    assert cache.ensure("down-co", "down.example") is None and cache.known_missing("down-co")
    cache, _ = _cache(tmp_path / "b", FakeFetcher(exc=httpx.ReadTimeout("slow")))
    assert cache.ensure("slow-co", "slow.example", timeout=1.0) is None
    assert not cache.known_missing("slow-co") and cache.needs_fetch("slow-co")  # the worker's full attempt may still succeed
    assert cache.ensure("slow-co", "slow.example") is None and cache.known_missing("slow-co")  # full attempt timing out counts


def test_refresh_fetches_wanted_and_derivable_jobs_within_limit(tmp_path):
    from amorehiremepls.jobs.logos import Fetched

    fetcher = FakeFetcher(Fetched(ICO, "ico", "image/x-icon", "x"))
    cache, _ = _cache(tmp_path, fetcher)
    cache.src_for({"employer": "Wanted Co", "posting_url": "https://wanted.example/j"})
    jobs = [
        {"employer": "Alpha", "posting_url": "https://alpha.example/j"},
        {"employer": "Board Only", "posting_url": "https://jobs.lever.co/unknownco/1"},
        {"employer": "Beta", "posting_url": "https://beta.example/j"},
        {"employer": "Gamma", "posting_url": "https://gamma.example/j"},
    ]
    assert cache.refresh(jobs, limit=2) == 2
    assert [c[0] for c in fetcher.calls] == ["wanted.example", "alpha.example"]
    assert cache.wanted == {}
    assert cache.refresh(jobs, limit=10) == 2  # beta and gamma; alpha and wanted are cached, board-only has no domain
    assert cache.refresh(jobs, limit=10) == 0


# -- web route, toggle and templates (Mongo) -----------------------------------------------------------------


@pytest.fixture
def client(store, tmp_path):
    s = Settings(
        mongodb_uri=TEST_MONGO_URI,
        mongodb_database=store.db.name,
        artifact_dir=tmp_path / "a",
        private_dir=tmp_path / "p",
        log_dir=tmp_path / "l",
        app_password="correct-horse-battery",
        app_secret="x" * 40,
        tracker_clone_dir=tmp_path / "clone",
        _env_file=None,
    )
    rt = Runtime(s, store=store)
    from amorehiremepls.jobs.logos import Fetched

    rt.logos.fetcher = FakeFetcher(Fetched(png_bytes(), "png", "image/png", "https://acme.example/favicon.ico"))
    return TestClient(create_app(rt), follow_redirects=False)


def _login(client) -> str:
    assert client.post("/login", data={"password": "correct-horse-battery"}).status_code == 303
    page = client.get("/")
    return re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)


@pytest.mark.mongo
def test_toggle_persists_and_pages_switch_between_initials_and_logo(client):
    rt = client.app.state.rt
    job, _ = rt.jobs.upsert(employer="Acme Corp", title="Backend Engineer", posting_url="https://careers.acme.example/jobs/1")
    board, _ = rt.jobs.upsert(employer="Board Only", title="Engineer", posting_url="https://jobs.lever.co/unknownco/1")
    assert TestClient(client.app, follow_redirects=False).get("/logos/acme-corp.png").status_code == 303  # login required
    csrf = _login(client)
    linkedin, _ = rt.jobs.upsert(employer="Robinhood", title="Backend Engineer", posting_url="https://www.linkedin.com/jobs/view/123")
    assert UISettings().company_logos is True

    # on by default: a derivable employer and an aliased LinkedIn employer render an app-served <img>;
    # a board slug we cannot map still renders initials only. The image address is never a third-party URL.
    page = client.get("/jobs").text
    assert re.search(r'<span class="avatar style-soft has-logo"><img class="logo" src="/logos/acme-corp\.png" alt="AC"[^>]*><span class="initials">AC</span></span>', page)
    assert 'src="/logos/robinhood.png"' in page
    assert 'src="/logos/board-only.png"' not in page and "BO</span>" in page
    assert "http" not in re.search(r'src="(/logos/[^"]+)"', page).group(1)
    assert 'name="company_logos" value="on" checked' in client.get("/settings").text
    for path in (f"/jobs/{job['_id']}", "/"):
        assert 'src="/logos/acme-corp.png"' in client.get(path).text, path

    # first image request fetches once (short budget) in this process, no worker; later requests are cache hits
    img = client.get("/logos/acme-corp.png")
    assert img.status_code == 200 and img.headers["content-type"] == "image/png" and img.content == png_bytes()
    assert img.headers["x-content-type-options"] == "nosniff" and "max-age=604800" in img.headers["cache-control"]
    assert rt.logos.fetcher.calls == [("acme.example", REQUEST_TIMEOUT)]
    assert client.get("/logos/acme-corp.png").status_code == 200 and len(rt.logos.fetcher.calls) == 1
    assert (rt.settings.artifact_dir / "public-cache" / "logos" / "acme-corp.png").exists()

    # the in-memory hint is gone (as after a restart) and Robinhood is only a LinkedIn posting: the route still
    # resolves the alias and fetches, without a worker
    rt.logos.wanted.clear()
    img = client.get("/logos/robinhood.png")
    assert img.status_code == 200 and img.content == png_bytes()
    assert ("robinhood.com", REQUEST_TIMEOUT) in rt.logos.fetcher.calls

    # a career-site employer that is not in the alias table is resolved from the stored job on the image request
    rt.jobs.upsert(employer="Beta Co", title="Engineer", posting_url="https://careers.beta.example/j/1")
    rt.logos.wanted.clear()
    assert client.get("/logos/beta-co.png").status_code == 200
    assert ("beta.example", REQUEST_TIMEOUT) in rt.logos.fetcher.calls

    # unknown or unsafe keys: 404, never a fetch
    assert client.get("/logos/board-only.png").status_code == 404
    assert client.get("/logos/..%2Fsecret.png").status_code == 404
    assert client.get("/logos/Nope_Inc.png").status_code == 404
    assert len(rt.logos.fetcher.calls) == 3

    # a negative-cached employer renders initials without an <img>
    rt.logos.fetcher.result = None
    rt.jobs.upsert(employer="Nope Inc", title="Engineer", posting_url="https://nope.example/j/1")
    client.get("/jobs")
    assert client.get("/logos/nope-inc.png").status_code == 404
    assert 'src="/logos/nope-inc.png"' not in client.get("/jobs").text and "NI</span>" in client.get("/jobs").text

    # turning it off restores initials everywhere and stops serving
    r = client.post("/settings/appearance", data={"csrf": csrf, "theme": "system", "accent": "blue", "tint": "accent"})
    assert r.status_code == 303 and UISettingsStore(rt.store).load().company_logos is False
    assert 'img class="logo"' not in client.get("/jobs").text
    assert client.get("/logos/acme-corp.png").status_code == 404  # the route is closed while the option is off
    assert len(rt.logos.fetcher.calls) == 4 and board["_id"] and linkedin["_id"]  # acme, robinhood, beta, nope; nothing while off


@pytest.mark.mongo
def test_worker_refresh_step_only_runs_when_enabled(client):
    rt = client.app.state.rt
    rt.jobs.upsert(employer="Acme Corp", title="Backend Engineer", posting_url="https://careers.acme.example/jobs/1")
    w = Worker(rt, poll_seconds=1)
    assert w.refresh_logos() == 1 and rt.logos.fetcher.calls == [("acme.example", None)]  # on by default
    ui = UISettingsStore(rt.store).load()
    ui.company_logos = False
    UISettingsStore(rt.store).save(ui)
    rt.jobs.upsert(employer="Beta Co", title="Engineer", posting_url="https://beta.example/j")
    assert w.refresh_logos() == 0 and len(rt.logos.fetcher.calls) == 1
    ui.company_logos = True
    UISettingsStore(rt.store).save(ui)
    assert w.refresh_logos() == 1 and ("beta.example", None) in rt.logos.fetcher.calls
    assert w.refresh_logos() == 0
    assert "logos" in w.tick()
