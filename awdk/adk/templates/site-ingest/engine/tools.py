"""Curated tool set for the Site Ingest Agent.

These are the ONLY tools the agent may call (default-deny; the brain pack lists
them as a whitelist). Tools analyze websites and produce SiteSpec JSON output.

Each tool is a closure over a SiteIngestSession so it can:
  - reach the workspace directory for intermediate/final artifacts,
  - coordinate discovery across multiple tools (mirrored pages, screenshots, etc.),
  - share extraction state.

`build_ingest_tools(session)` returns a list of plain functions; serve.py
registers them on the AitherAgent via agent._tools.register(fn).
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit

import httpx

logger = logging.getLogger("site_ingest.tools")


@dataclass
class SiteIngestSession:
    """Shared state for one site ingest session: workspace, caches, artifacts."""

    workspace_dir: Path
    session_id: str = "ingest"
    _page_cache: dict[str, str] = field(default_factory=dict)
    _screenshots: dict[str, str] = field(default_factory=dict)
    _brand_tokens: dict[str, Any] = field(default_factory=dict)
    captured_at: str = ""

    def __post_init__(self):
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        if not self.captured_at:
            self.captured_at = time.strftime("%Y-%m-%dT%H:%M:%SZ")


def _strip_html(html: str) -> str:
    """Strip HTML tags and script/style content from HTML text."""
    html = re.sub(
        r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.DOTALL | re.IGNORECASE
    )
    html = re.sub(r"<[^>]+>", " ", html)
    html = re.sub(r"\s+", " ", html)
    return html.strip()


def _as_int(v, default: int) -> int:
    """Coerce an LLM-supplied arg to int."""
    if isinstance(v, bool):
        return default
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    if isinstance(v, (list, tuple, set)):
        return len(v) or default
    if isinstance(v, str):
        m = re.search(r"-?\d+", v)
        return int(m.group()) if m else default
    return default


def _safe_public_url(url: str) -> bool:
    """Allow only public http(s) URLs (block localhost/private/link-local hosts)."""
    try:
        p = urlsplit(url)
    except ValueError:
        return False
    host = (p.hostname or "").lower()
    private = re.compile(
        r"^(localhost|0\.0\.0\.0|127\.|10\.|192\.168\.|169\.254\.|::1|"
        r"172\.(1[6-9]|2\d|3[01])\.)|(\.internal|\.local)$",
        re.IGNORECASE,
    )
    return p.scheme in ("http", "https") and bool(host) and not private.search(host)


def _sitemap_locs(xml_text: str) -> list[str]:
    """Extract <loc> URLs from a sitemap or sitemap index."""
    return re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml_text)


def _kb_rel_path(url: str) -> str:
    """Map a URL to a safe relative .md path mirroring its tree."""
    path = urlsplit(url).path.strip("/") or "index"
    path = re.sub(r"\.(html?|md)$", "", path, flags=re.I)
    segs = [
        re.sub(r"[^A-Za-z0-9._-]+", "-", s).strip("-") or "_"
        for s in path.split("/")
    ]
    return "/".join(segs) + ".md"


def build_ingest_tools(session: SiteIngestSession) -> list[Callable]:
    """Build the site-ingest tool set. Returns a list of functions to register."""

    async def mirror_site(base_url: str, max_pages: int = 25) -> str:
        """Mirror a website into local markdown files indexed by URL path.

        Discovers pages via sitemap.xml and llms.txt (scoped to same host/path),
        downloads each as markdown (native .md first, else HTML->text), and writes
        them under the workspace artifacts directory. Auto-generates INDEX.md.

        Use this to capture the full content inventory offline.

        base_url: root URL to mirror (e.g., "https://example.com")
        max_pages: safety cap on pages to mirror (default 25, max 200)
        """
        base_url = str(base_url).strip()
        max_pages = max(1, min(_as_int(max_pages, 25), 200))
        if not _safe_public_url(base_url):
            return json.dumps({
                "error": "URL blocked (must be a public http/https site)",
                "base_url": base_url,
            })

        parts = urlsplit(base_url)
        origin = f"{parts.scheme}://{parts.netloc}"
        prefix = parts.path.rstrip("/")
        domain = parts.netloc.replace(":", "_")
        out_dir = session.workspace_dir / "mirrors" / domain
        session._page_cache.clear()

        headers = {"User-Agent": "Mozilla/5.0 (compatible; SiteIngestAgent/1.0)"}
        urls: list[str] = []
        try:
            async with httpx.AsyncClient(
                timeout=20.0, follow_redirects=True, max_redirects=5
            ) as client:
                # Discover via sitemap + llms.txt
                for disc in (
                    urljoin(origin, "/sitemap.xml"),
                    urljoin(origin, "/llms.txt"),
                ):
                    try:
                        r = await client.get(disc, headers=headers)
                        if r.status_code != 200 or not r.text:
                            continue
                        if disc.endswith(".xml"):
                            urls += _sitemap_locs(r.text)
                        else:
                            urls += [
                                u[:-3] if u.endswith(".md") else u
                                for u in re.findall(r"https?://[^\s)\]]+", r.text)
                            ]
                    except httpx.HTTPError:
                        continue

                # Scope to same host + path prefix; dedupe; cap
                seen: set[str] = set()
                scoped: list[str] = []
                for u in [base_url, *urls]:
                    pu = urlsplit(u)
                    norm = f"{pu.scheme}://{pu.netloc}{pu.path}"
                    if pu.netloc != parts.netloc or not pu.path.startswith(
                        prefix or "/"
                    ):
                        continue
                    if re.search(
                        r"\.(png|jpe?g|gif|svg|ico|css|js|pdf|zip|woff2?)$",
                        norm,
                        re.I,
                    ):
                        continue
                    if norm in seen or not _safe_public_url(norm):
                        continue
                    seen.add(norm)
                    scoped.append(norm)
                    if len(scoped) >= max_pages:
                        break

                # Download each as markdown (.md-suffix first, else HTML->text)
                pages: list[dict] = []
                for u in scoped:
                    md, title, src = "", u.rsplit("/", 1)[-1], "html"
                    try:
                        if not u.endswith(".md"):
                            rm = await client.get(u + ".md", headers=headers)
                            body = rm.text if rm.status_code == 200 else ""
                            if body and not body.lstrip()[:64].lower().startswith(
                                ("<!doctype", "<html", "<?xml")
                            ):
                                md, src = body.strip(), "markdown"
                        if not md:
                            rp = await client.get(u, headers=headers)
                            if rp.status_code == 200:
                                md = _strip_html(rp.text)
                                tm = re.search(
                                    r"<title[^>]*>([^<]+)</title>",
                                    rp.text,
                                    re.I,
                                )
                                if tm:
                                    title = tm.group(1).strip()
                    except httpx.HTTPError:
                        continue
                    if not md:
                        continue
                    for line in md.splitlines():
                        if line.startswith("# "):
                            title = line[2:].strip()
                            break
                    dest = out_dir / _kb_rel_path(u)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_text(
                        f"---\nurl: {u}\ntitle: {title}\n---\n\n{md}\n",
                        encoding="utf-8",
                    )
                    session._page_cache[u] = md
                    pages.append({
                        "url": u,
                        "title": title,
                        "file": _kb_rel_path(u),
                        "source": src,
                    })
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"error": str(exc), "base_url": base_url})

        if not pages:
            return json.dumps({
                "error": "no pages mirrored",
                "base_url": base_url,
                "discovered": len(scoped),
            })

        # Generate INDEX.md
        idx = [
            f"# Mirror: {domain}",
            "",
            f"Source: {base_url}",
            f"Pages: {len(pages)}",
            "",
            "## Pages",
            "",
        ]
        idx += [f"- [{p['title']}]({p['file']})" for p in pages]
        (out_dir / "INDEX.md").write_text("\n".join(idx) + "\n", encoding="utf-8")

        return json.dumps({
            "success": True,
            "base_url": base_url,
            "pages_discovered": len(scoped),
            "pages_mirrored": len(pages),
            "output_dir": str(out_dir),
            "index_file": str(out_dir / "INDEX.md"),
        })

    async def screenshot_pages(url: str, paths_csv: str = "") -> str:
        """Capture screenshots of key pages using AitherBrowser (if configured).

        If AITHER_BROWSER_URL environment variable is set, POSTs to
        /capture with the page URL. Otherwise, returns a message that
        screenshot capture was skipped.

        url: base URL (e.g., "https://example.com")
        paths_csv: comma-separated paths (e.g., "/,/about,/contact")
        """
        browser_url = os.getenv("AITHER_BROWSER_URL", "").strip()
        if not browser_url:
            return json.dumps({
                "skipped": "no AITHER_BROWSER_URL configured",
                "hint": "Set AITHER_BROWSER_URL env var to enable screenshots",
            })

        base_url = str(url).strip()
        if not _safe_public_url(base_url):
            return json.dumps({"error": "Invalid or blocked URL", "url": base_url})

        paths = [
            p.strip() for p in (paths_csv or "/").split(",") if p.strip()
        ]
        images: list[str] = []
        captured = 0

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                for path in paths:
                    page_url = urljoin(base_url, path)
                    try:
                        resp = await client.post(
                            f"{browser_url}/capture",
                            json={"url": page_url},
                            timeout=15.0,
                        )
                        if resp.status_code == 200:
                            data = resp.json()
                            img_path = data.get("image_path")
                            if img_path:
                                session._screenshots[path] = img_path
                                images.append(img_path)
                                captured += 1
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("screenshot %s failed: %s", path, exc)
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"error": str(exc)})

        return json.dumps({
            "success": captured > 0,
            "base_url": base_url,
            "requested_paths": len(paths),
            "captured": captured,
            "images": images,
        })

    def extract_brand_tokens(workspace_dir: str = "") -> str:
        """Extract brand tokens from mirrored markdown and screenshots.

        Heuristically scans the workspace mirrors/ directory for:
        - Company name (from h1, headings, meta tags if present)
        - Tagline (from meta description, <title>)
        - Accent colors (from prominent color patterns in text, if visible)
        - Typography (estimated from headings)
        - NAP (Name, Address, Phone) via regex over footer/contact pages
        - Social links (LinkedIn, Twitter, Facebook, etc.)

        workspace_dir: path to the workspace (uses session's workspace_dir if empty)
        """
        ws = Path(workspace_dir) if workspace_dir else session.workspace_dir
        mirrors_dir = ws / "mirrors"

        tokens = {
            "company_name": None,
            "tagline": None,
            "tone": "professional",
            "accent_color": None,
            "palette": {"primary": None, "secondary": None, "accent": None},
            "typography": {"headings": None, "body": None},
            "logo_url": None,
            "nap": {"name": None, "address": None, "phone": None},
            "social_links": {},
            "service_areas": [],
        }

        if not mirrors_dir.exists():
            return json.dumps({"tokens": tokens, "hint": "no mirrors directory"})

        # Scan all markdown files for extraction patterns
        all_text = ""
        for md_file in mirrors_dir.rglob("*.md"):
            try:
                content = md_file.read_text(encoding="utf-8", errors="ignore")
                all_text += "\n" + content
            except Exception:  # noqa: BLE001
                pass

        # Extract company name (look for h1 or strong text at start)
        h1_match = re.search(r"^#+\s+([^\n]+)", all_text, re.MULTILINE)
        if h1_match:
            tokens["company_name"] = h1_match.group(1).strip()

        # Extract tagline (look for a subtitle or meta description pattern)
        subtitle_match = re.search(r"##\s+([^\n]+)", all_text, re.MULTILINE)
        if subtitle_match and not tokens["tagline"]:
            tokens["tagline"] = subtitle_match.group(1).strip()

        # Extract NAP (name, address, phone) via regex
        # Phone: common formats like (555) 123-4567, 555-123-4567, +1-555-123-4567
        phone_match = re.search(
            r"[\+]?[(]?\d{3}[)\-]?[-.\s]?\d{3}[-.\s]?\d{4}", all_text
        )
        if phone_match:
            tokens["nap"]["phone"] = phone_match.group(0).strip()

        # Address: look for street address patterns (123 Main St, City, ST 12345)
        addr_match = re.search(
            r"\d+\s+[A-Za-z\s]+,\s+[A-Za-z\s]+,\s+[A-Z]{2}\s+\d{5}", all_text
        )
        if addr_match:
            tokens["nap"]["address"] = addr_match.group(0).strip()

        # Extract social links (LinkedIn, Twitter, Facebook, etc.)
        social_patterns = {
            "linkedin": r"linkedin\.com/(?:company|in)/([^\s\"'\)]+)",
            "twitter": r"twitter\.com/([^\s\"'\)]+)",
            "facebook": r"facebook\.com/([^\s\"'\)]+)",
            "instagram": r"instagram\.com/([^\s\"'\)]+)",
        }
        for network, pattern in social_patterns.items():
            match = re.search(pattern, all_text, re.IGNORECASE)
            if match:
                tokens["social_links"][network] = (
                    f"https://{network}.com/{match.group(1)}"
                    if network == "twitter" or network == "instagram"
                    else f"https://{network}.com/{match.group(1)}"
                )

        session._brand_tokens = tokens
        return json.dumps({"tokens": tokens, "extracted": True})

    def save_sitespec(json_str: str, workspace_dir: str = "") -> str:
        """Validate and save SiteSpec JSON to sitespec.json.

        Validates required fields and writes to the workspace directory.

        json_str: the complete SiteSpec JSON as a string
        workspace_dir: path to the workspace (uses session's workspace_dir if empty)
        """
        ws = Path(workspace_dir) if workspace_dir else session.workspace_dir
        ws.mkdir(parents=True, exist_ok=True)

        try:
            spec = json.loads(json_str)
        except json.JSONDecodeError as e:
            return json.dumps({"saved": False, "error": f"invalid JSON: {e}"})

        # Validate required fields
        required = ["slug", "source_url", "captured_at"]
        missing = [k for k in required if k not in spec or not spec[k]]
        if missing:
            return json.dumps({
                "saved": False,
                "error": f"missing required fields: {', '.join(missing)}",
            })

        # Write to file
        output_path = ws / "sitespec.json"
        try:
            output_path.write_text(json.dumps(spec, indent=2), encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            return json.dumps({
                "saved": False,
                "error": f"write failed: {e}",
            })

        return json.dumps({
            "saved": True,
            "path": str(output_path),
            "slug": spec.get("slug"),
        })

    return [
        mirror_site,
        screenshot_pages,
        extract_brand_tokens,
        save_sitespec,
    ]
