"""Company logos for the dashboard: a deterministic icon fetch, cached on disk, no model usage.

The employer's website comes from the job's own company URL, then from the posting host when that host is not a
job board (Greenhouse, Lever, Ashby, Workday, LinkedIn, …), then from a small built-in alias table of public
company domains. The icon is fetched server-side only, in order: the site's own favicon or `<link rel="icon">`,
then DuckDuckGo's icon endpoint, then Google's favicon service. Limits: public http(s) hosts only, short timeout,
512 KB cap, magic-byte sniffing, SVG skipped. Bytes live under
`<artifact_dir>/public-cache/logos/<employer_key>.<ext>` next to a JSON sidecar. A miss is remembered for seven
days, but a sidecar from an older fetch strategy is ignored so a new source list can retry. The browser only ever
loads `/logos/<key>.png` from this app.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
import socket
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

log = logging.getLogger(__name__)

# Hosts that host job ads for many employers; a URL on one of them says nothing about the employer's own website.
ATS_HOST_SUFFIXES = (
    "greenhouse.io",
    "lever.co",
    "ashbyhq.com",
    "myworkdayjobs.com",
    "workday.com",
    "linkedin.com",
    "indeed.com",
    "glassdoor.com",
    "smartrecruiters.com",
    "jobvite.com",
    "icims.com",
    "bamboohr.com",
    "workable.com",
    "recruitee.com",
    "successfactors.com",
    "taleo.net",
    "oraclecloud.com",
    "breezy.hr",
    "applytojob.com",
    "rippling.com",
    "dover.com",
    "teamtailor.com",
    "pinpointhq.com",
    "eightfold.ai",
    "phenom.com",
    "wellfound.com",
    "ziprecruiter.com",
)
# Board slugs that map to an employer website. Small and explicit by design: anything not listed gives no logo
# rather than a guess. Keys are "<ats>:<slug>".
ATS_SLUG_DOMAINS: dict[str, str] = {
    "greenhouse:faire": "faire.com",
    "greenhouse:stripe": "stripe.com",
    "greenhouse:affirm": "affirm.com",
    "greenhouse:instacart": "instacart.com",
    "greenhouse:lyft": "lyft.com",
    "greenhouse:toast": "toasttab.com",
    "greenhouse:robinhood": "robinhood.com",
    "greenhouse:okta": "okta.com",
    "lever:1password": "1password.com",
    "ashby:1password": "1password.com",
    "workday:cibc": "cibc.com",
    "workday:bmo": "bmo.com",
}
# Public company websites keyed by employer_key(display name). Used when the posting lives on a job board, so the
# host is not the employer's site. Names only — no candidate data.
EMPLOYER_DOMAINS: dict[str, str] = {
    "robinhood": "robinhood.com",
    "electric-mind": "electricmind.com",
    "cibc": "cibc.com",
    "pointclickcare": "pointclickcare.com",
    "autodesk": "autodesk.com",
    "broadridge": "broadridge.com",
    "doordash": "doordash.com",
    "toast": "toasttab.com",
    "interac": "interac.ca",
    "marqeta": "marqeta.com",
    "tata-consultancy-services": "tcs.com",
    "the-mrg-group": "themrggroup.com",
    "citi": "citi.com",
    "citibank": "citi.com",
    "stripe": "stripe.com",
    "faire": "faire.com",
    "1password": "1password.com",
    "affirm": "affirm.com",
    "instacart": "instacart.com",
    "lyft": "lyft.com",
    "okta": "okta.com",
    "bell": "bell.ca",
    "bmo": "bmo.com",
    "fidelity-canada": "fidelity.ca",
    "guidewire": "guidewire.com",
    "haventree-bank": "haventreebank.com",
    "ibm": "ibm.com",
    "robert-half": "roberthalf.com",
    "rogers": "rogers.com",
    "scotiabank": "scotiabank.com",
}
FETCH_STRATEGY = "site+ddg+google"  # sidecars from an older strategy are not treated as a fresh miss
REQUEST_TIMEOUT = 2.0  # per-request budget on the web process; there may be no worker to retry later
_DOMAIN_RE = re.compile(r"^(?!-)[a-z0-9.-]{1,253}(?<!-)$")
# Subdomains that point at a careers site or job board on the employer's own domain; the icon lives on the apex.
STRIP_SUBDOMAINS = ("www", "careers", "career", "jobs", "job", "apply", "boards", "job-boards", "talent", "recruiting", "hiring", "work", "joinus", "join")
URL_FIELDS = ("company_website", "website", "homepage", "posting_url", "apply_url", "url", "job_url", "source_url")
NEGATIVE_TTL = timedelta(days=7)
MAX_BYTES = 512 * 1024
TIMEOUT = 3.0
MAX_REDIRECTS = 3
USER_AGENT = "AmoreHireMePls/1.0 (+private dashboard; favicon fetch)"


def employer_key(employer: str | None) -> str:
    """Filesystem and URL safe key for an employer name: lowercase, a-z0-9 and single dashes."""
    s = re.sub(r"[^a-z0-9]+", "-", (employer or "").lower()).strip("-")
    return s[:80] or "unknown"


def _host_of(url: str) -> str | None:
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    return parts.hostname.lower().rstrip(".")


def is_ats_host(host: str) -> bool:
    return any(host == s or host.endswith("." + s) for s in ATS_HOST_SUFFIXES)


def _ats_slug(url: str) -> str | None:
    host = _host_of(url) or ""
    path = [p for p in urlsplit(url).path.split("/") if p]
    if "greenhouse.io" in host and path:
        slug = path[1] if path[0] in ("embed", "job_app") and len(path) > 1 else path[0]
        return f"greenhouse:{slug.lower()}"
    if host.endswith("lever.co") and path:
        return f"lever:{path[0].lower()}"
    if host.endswith("ashbyhq.com") and path:
        return f"ashby:{path[0].lower()}"
    if host.endswith("myworkdayjobs.com"):
        return f"workday:{host.split('.')[0]}"
    return None


def apex_domain(host: str) -> str:
    labels = host.split(".")
    while len(labels) > 2 and labels[0] in STRIP_SUBDOMAINS:
        labels = labels[1:]
    return ".".join(labels)


def derive_domain(job: dict[str, Any] | None) -> str | None:
    """The employer's own website host for a job record, or None when it cannot be known without guessing."""
    if not job:
        return None
    for field in URL_FIELDS:
        url = job.get(field)
        if not isinstance(url, str) or not url.strip():
            continue
        if "://" not in url and field in ("company_website", "website", "homepage"):
            url = "https://" + url.strip()
        host = _host_of(url)
        if not host:
            continue
        if is_ats_host(host):
            mapped = ATS_SLUG_DOMAINS.get(_ats_slug(url) or "")
            if mapped:
                return mapped
            continue
        if not is_public_hostname(host):
            continue
        if "." not in host:
            continue
        return apex_domain(host)
    return domain_for_employer(job.get("employer"))


def domain_for_employer(name: str | None) -> str | None:
    """Built-in public website for an employer name, or None when the name is not in the alias table."""
    if not name:
        return None
    return EMPLOYER_DOMAINS.get(employer_key(name))


def fetchable_domain(domain: str) -> bool:
    d = (domain or "").lower().strip(".")
    return bool(_DOMAIN_RE.fullmatch(d)) and "." in d and is_public_hostname(d) and not is_ats_host(d)


def is_public_hostname(host: str) -> bool:
    """Literal check only (no DNS): rejects localhost, .local/.internal names and private, loopback, link-local,
    multicast or reserved IP literals."""
    h = host.lower().strip("[]")
    if h in ("localhost",) or h.endswith((".localhost", ".local", ".internal", ".lan", ".home", ".arpa")):
        return False
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return bool(h) and " " not in h
    return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified)


def resolves_public(host: str, resolver: Callable[[str], list[str]] | None = None) -> bool:
    """DNS check at fetch time: every address the name resolves to must be public."""
    if not is_public_hostname(host):
        return False
    try:
        addrs = resolver(host) if resolver else [ai[4][0] for ai in socket.getaddrinfo(host, None)]
    except (socket.gaierror, OSError, ValueError):
        return False
    return bool(addrs) and all(is_public_hostname(a) for a in addrs)


def sniff_image(data: bytes) -> tuple[str, str] | None:
    """(extension, media type) from magic bytes; SVG and anything unknown are rejected."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png", "image/png"
    if data.startswith(b"\x00\x00\x01\x00"):
        return "ico", "image/x-icon"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg", "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif", "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp", "image/webp"
    return None


_ICON_LINK = re.compile(r"<link\b[^>]*>", re.I)
_ATTR = re.compile(r"""([a-zA-Z-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))""")


def icon_links(html: str, base_url: str) -> list[str]:
    """href values of <link rel="icon"/"shortcut icon"/"apple-touch-icon"> in document order, resolved against base."""
    out: list[str] = []
    for tag in _ICON_LINK.findall(html[:200_000]):
        attrs = {m.group(1).lower(): (m.group(2) or m.group(3) or m.group(4) or "") for m in _ATTR.finditer(tag)}
        rel = attrs.get("rel", "").lower().split()
        if "icon" in rel or "apple-touch-icon" in rel:
            href = attrs.get("href", "").strip()
            if href and not href.lower().startswith(("data:", "javascript:")):
                out.append(urljoin(base_url, href))
    return out


@dataclass
class Fetched:
    data: bytes
    ext: str
    media_type: str
    source: str


class LogoFetcher:
    """Fetches one employer's icon from its own website. `resolver` is injectable so tests never touch DNS."""

    def __init__(
        self, timeout: float = TIMEOUT, max_bytes: int = MAX_BYTES, resolver: Callable[[str], list[str]] | None = None, transport: httpx.BaseTransport | None = None
    ) -> None:
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.resolver = resolver
        self.transport = transport

    def _get(self, client: httpx.Client, url: str, accept: str, seen: list[bool]) -> tuple[httpx.Headers, bytes, str] | None:
        """(headers, body, final url) for a 200 response under the size cap; follows a few redirects, each to a
        public host. None for anything else. `seen` records that an HTTP response actually arrived (a timeout does not)."""
        for _ in range(MAX_REDIRECTS + 1):
            host = _host_of(url)
            if not host or not resolves_public(host, self.resolver):
                return None
            with client.stream("GET", url, headers={"Accept": accept, "User-Agent": USER_AGENT}) as r:
                seen.append(True)
                if r.is_redirect and r.headers.get("location"):
                    url = urljoin(url, r.headers["location"])
                    continue
                if r.status_code != 200:
                    return None
                length = r.headers.get("content-length")
                if length and length.isdigit() and int(length) > self.max_bytes:
                    return None
                body = bytearray()
                for chunk in r.iter_bytes():
                    body.extend(chunk)
                    if len(body) > self.max_bytes:
                        return None
                return r.headers, bytes(body), str(r.url)
        return None

    def _image(self, client: httpx.Client, url: str, seen: list[bool], timed: list[bool]) -> Fetched | None:
        try:
            got = self._get(client, url, "image/*,*/*;q=0.5", seen)
        except httpx.TimeoutException:
            timed.append(True)
            return None
        except httpx.HTTPError as exc:
            log.debug("logo probe %s failed: %s", url, type(exc).__name__)
            return None
        if got is None:
            return None
        headers, body, final = got
        ctype = headers.get("content-type", "").split(";")[0].strip().lower()
        if ctype and not ctype.startswith("image/") and ctype != "application/octet-stream":
            return None
        kind = sniff_image(body)
        if not kind:
            return None
        return Fetched(body, kind[0], kind[1], final)

    def _from_site(self, client: httpx.Client, domain: str, seen: list[bool], timed: list[bool]) -> Fetched | None:
        # Some apex hosts fail TLS or refuse connections while `www.` works; try both, apex first.
        hosts = [domain] if domain.startswith("www.") else [domain, f"www.{domain}"]
        for host in hosts:
            found = self._image(client, f"https://{host}/favicon.ico", seen, timed)
            if found:
                return found
            try:
                home = self._get(client, f"https://{host}/", "text/html", seen)
            except httpx.TimeoutException:
                timed.append(True)
                continue
            except httpx.HTTPError as exc:
                log.debug("logo probe %s failed: %s", host, type(exc).__name__)
                continue
            if home is None:
                continue
            for href in icon_links(home[1].decode("utf-8", "replace"), home[2])[:4]:
                found = self._image(client, href, seen, timed)
                if found:
                    return found
        return None

    def fetch(self, domain: str, timeout: float | None = None) -> Fetched | None:
        """Site favicon / rel=icon, then DuckDuckGo, then Google. One slow source does not skip the later ones.
        Raises TimeoutException only when every source timed out before any HTTP response, so a short request
        budget is not stored as a seven-day miss."""
        if not fetchable_domain(domain):
            return None
        seen: list[bool] = []
        timed: list[bool] = []
        fallbacks = (
            f"https://icons.duckduckgo.com/ip3/{domain}.ico",
            f"https://www.google.com/s2/favicons?domain={domain}&sz=128",
        )
        with httpx.Client(timeout=timeout or self.timeout, follow_redirects=False, transport=self.transport) as client:
            found = self._from_site(client, domain, seen, timed)
            if found:
                return found
            for url in fallbacks:
                found = self._image(client, url, seen, timed)
                if found:
                    return found
        if timed and not seen:
            raise httpx.TimeoutException("logo fetch timed out")
        return None


class LogoCache:
    """Disk cache under `<root>/public-cache/logos`; public research data only, never candidate context."""

    def __init__(self, root: Path, fetcher: LogoFetcher | None = None, now: Callable[[], datetime] | None = None) -> None:
        self.dir = root / "public-cache" / "logos"
        self.fetcher = fetcher or LogoFetcher()
        self.now = now or (lambda: datetime.now(UTC))
        self.wanted: dict[str, str] = {}  # employer_key -> domain, filled while pages render
        self._lock = threading.Lock()
        self._inflight: set[str] = set()

    # -- sidecar ------------------------------------------------------------------------------------------------
    def _meta_path(self, key: str) -> Path:
        return self.dir / f"{key}.json"

    def meta(self, key: str) -> dict[str, Any] | None:
        p = self._meta_path(key)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text())
        except (OSError, ValueError):
            return None

    def _write_meta(self, key: str, **fields: Any) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self._meta_path(key).write_text(json.dumps({"fetched_at": self.now().isoformat(), "strategy": FETCH_STRATEGY, **fields}, indent=1))

    # -- reads --------------------------------------------------------------------------------------------------
    def lookup(self, key: str) -> tuple[Path, str] | None:
        """(file path, media type) when a logo is cached and still on disk."""
        m = self.meta(key)
        if not m or m.get("status") != "ok":
            return None
        p = self.dir / f"{key}.{m.get('ext', 'png')}"
        return (p, m.get("media_type", "image/png")) if p.exists() else None

    def known_missing(self, key: str) -> bool:
        m = self.meta(key)
        # An older strategy (site favicon only, or a sidecar written before fallbacks) must not block a retry.
        if not m or m.get("status") != "missing" or m.get("strategy") != FETCH_STRATEGY:
            return False
        try:
            fetched = datetime.fromisoformat(m["fetched_at"])
        except (KeyError, ValueError):
            return False
        return self.now() - fetched < NEGATIVE_TTL

    def resolve_domain(self, key: str, jobs: list[dict[str, Any]] | None = None) -> str | None:
        """Domain for a logo request. The page's in-memory hint wins, then a matching job, then the alias table,
        so the first `/logos/{key}` hit can fetch even when no worker has run and the process just started."""
        hinted = self.wanted.get(key)
        if hinted:
            return hinted
        for job in jobs or ():
            if employer_key(job.get("employer")) == key:
                domain = derive_domain(job)
                if domain:
                    return domain
        return EMPLOYER_DOMAINS.get(key)

    def needs_fetch(self, key: str) -> bool:
        return self.lookup(key) is None and not self.known_missing(key)

    # -- what the templates ask ---------------------------------------------------------------------------------
    def src_for(self, job: dict[str, Any] | None, employer: str | None = None) -> str | None:
        """URL of the app-served logo for this job's employer, or None when initials should be shown instead.
        Registers the employer as wanted so the worker (or the first image request) fetches it."""
        name = employer or (job or {}).get("employer")
        if not name:
            return None
        key = employer_key(name)
        if self.lookup(key):
            return f"/logos/{key}.png"
        if self.known_missing(key):
            return None
        domain = derive_domain(job) or domain_for_employer(name)
        if not domain:
            return None
        self.wanted.setdefault(key, domain)
        return f"/logos/{key}.png"

    # -- writes -------------------------------------------------------------------------------------------------
    def ensure(self, key: str, domain: str, timeout: float | None = None) -> tuple[Path, str] | None:
        """Fetch and store the logo for `key` from `domain` unless it is cached or recently known missing.
        A network error is remembered as missing too, so the same broken site is not retried on every view."""
        hit = self.lookup(key)
        if hit or self.known_missing(key):
            return hit
        with self._lock:
            if key in self._inflight:
                return None
            self._inflight.add(key)
        try:
            found = None
            try:
                found = self.fetcher.fetch(domain, timeout=timeout)
            except httpx.TimeoutException:
                if timeout is not None:
                    return None  # short request-path budget ran out: leave it for the worker's full attempt
                log.info("logo fetch for %s (%s) timed out", key, domain)
            except (httpx.HTTPError, OSError, ValueError) as exc:
                log.info("logo fetch for %s (%s) failed: %s", key, domain, type(exc).__name__)
            if not found:
                self._write_meta(key, status="missing", domain=domain)
                return None
            self.dir.mkdir(parents=True, exist_ok=True)
            path = self.dir / f"{key}.{found.ext}"
            path.write_bytes(found.data)
            self._write_meta(key, status="ok", domain=domain, source=found.source, ext=found.ext, media_type=found.media_type, bytes=len(found.data))
            return path, found.media_type
        finally:
            with self._lock:
                self._inflight.discard(key)

    def refresh(self, jobs: list[dict[str, Any]], limit: int = 5) -> int:
        """Worker step: fetch at most `limit` wanted or derivable employer logos that are not cached yet."""
        todo: dict[str, str] = dict(self.wanted)
        for j in jobs:
            name = j.get("employer")
            if not name:
                continue
            key = employer_key(name)
            if key in todo:
                continue
            domain = derive_domain(j)
            if domain:
                todo[key] = domain
        n = 0
        for key, domain in todo.items():
            if n >= limit:
                break
            if self.needs_fetch(key):
                self.ensure(key, domain)
                n += 1
            self.wanted.pop(key, None)
        return n
