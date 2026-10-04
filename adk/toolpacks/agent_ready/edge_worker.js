/**
 * apex-agent-edge — the agent-readiness layer in front of aitherium.com.
 *
 * aitherium.com is a static export on GitHub Pages. Pages can serve files but
 * cannot set response headers or negotiate content, and four of the agent
 * discovery standards need exactly that:
 *
 *   - RFC 8288 / RFC 9727 §3  Link headers on the homepage
 *   - RFC 9727                /.well-known/api-catalog as application/linkset+json
 *                             (Pages serves an extensionless file as octet-stream)
 *   - Markdown for Agents     Accept: text/markdown -> a markdown body
 *   - RFC 8414 / OIDC         the authorization-server metadata lives on
 *                             idp.aitherium.com; the apex answers with that
 *                             document instead of a Pages 404
 *
 * Everything else — every page, asset and file — is fetch(request): Pages
 * answers exactly as it did before this Worker existed. ANY exception in here
 * falls back to that same passthrough, so the worst a bug can do is drop the
 * extra headers, never take the site down.
 *
 * The discovery documents themselves are static files in the site (one
 * source, published with it); this Worker only fixes how they are served.
 *
 * Reusing this for another site: change IDP_ISSUER and HOMEPAGE_LINKS, and
 * drop AUTH_METADATA entries for an issuer you do not run.
 */

export const IDP_ISSUER = 'https://idp.aitherium.com/identity';

// The issuer's own metadata documents. The apex is not an issuer; it answers
// with the real issuer's document and says where the canonical copy lives.
const AUTH_METADATA = {
  '/.well-known/openid-configuration': `${IDP_ISSUER}/.well-known/openid-configuration`,
  '/.well-known/oauth-authorization-server': `${IDP_ISSUER}/.well-known/oauth-authorization-server`,
  // Clients verifying our tokens ask the apex for the signing keys (31 requests in
  // one day of edge analytics, all 404). They are the issuer's keys, served as-is.
  '/.well-known/jwks.json': `${IDP_ISSUER}/.well-known/jwks.json`,
};

// The live auth.md is the identity service's; public/auth.md is the fallback
// Pages serves when the IdP is slow or down.
const AUTH_MD_UPSTREAM = `${IDP_ISSUER}/auth.md`;

// Extensionless or mistyped files whose Content-Type Pages cannot get right.
export const CONTENT_TYPES = {
  '/.well-known/api-catalog': 'application/linkset+json; charset=utf-8',
  '/.well-known/oauth-protected-resource': 'application/json; charset=utf-8',
  '/auth.md': 'text/markdown; charset=utf-8',
  '/llms.txt': 'text/markdown; charset=utf-8',
  '/AGENTS.md': 'text/markdown; charset=utf-8',
  '/SKILLS.md': 'text/markdown; charset=utf-8',
};

// RFC 8288 Link header for the homepage. Registered relation types only.
export const HOMEPAGE_LINKS = [
  '</.well-known/api-catalog>; rel="api-catalog"; type="application/linkset+json"',
  '</openapi.json>; rel="service-desc"; type="application/vnd.oai.openapi+json"',
  '</llms.txt>; rel="service-doc"; type="text/markdown"',
  '</.well-known/mcp/server-card.json>; rel="describedby"; type="application/json"',
  '</.well-known/agent-skills/index.json>; rel="describedby"; type="application/json"',
  '</.well-known/oauth-protected-resource>; rel="describedby"; type="application/json"',
  '</auth.md>; rel="describedby"; type="text/markdown"',
].join(', ');

const UPSTREAM_TIMEOUT_MS = 4000;

// The homepage's agent-facing markdown, bundled at deploy time (main.js) so
// Accept: text/markdown on / needs no subrequest. Measured 2026-10-04: a caller
// that is itself a Worker (isitagentready.com) got HTML for / while plain
// clients got markdown; nested same-zone subrequests are what differ.
let homepageMarkdown = null;

/** Set the markdown served for / (e.g. your llms.txt, imported as text). */
export function setHomepageMarkdown(md) {
  homepageMarkdown = typeof md === 'string' && md.trim() ? md : null;
}

/** True when the client asked for markdown at least as strongly as for HTML. */
export function wantsMarkdown(accept) {
  if (!accept) return false;
  let md = -1;
  let html = -1;
  for (const part of accept.toLowerCase().split(',')) {
    const [type, ...params] = part.trim().split(';');
    let q = 1;
    for (const p of params) {
      const [k, v] = p.trim().split('=');
      if (k === 'q') q = Number.parseFloat(v) || 0;
    }
    const t = type.trim();
    if (t === 'text/markdown' || t === 'text/x-markdown') md = Math.max(md, q);
    if (t === 'text/html' || t === 'application/xhtml+xml') html = Math.max(html, q);
  }
  return md > 0 && md >= html;
}

const ENTITIES = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: ' ', '#39': "'" };

function decodeEntities(s) {
  return s
    .replace(/&#x([0-9a-f]+);/gi, (_, h) => String.fromCodePoint(Number.parseInt(h, 16)))
    .replace(/&#(\d+);/g, (_, d) => String.fromCodePoint(Number.parseInt(d, 10)))
    .replace(/&([a-z0-9#]+);/gi, (m, n) => (n.toLowerCase() in ENTITIES ? ENTITIES[n.toLowerCase()] : m));
}

/**
 * A deliberately small HTML -> markdown conversion: headings, paragraphs,
 * lists, links, emphasis, code and line breaks. Scripts, styles, SVG and the
 * document head are dropped. It is for agents reading content, not for
 * round-tripping layout.
 */
export function htmlToMarkdown(html, baseUrl) {
  const titleMatch = /<title[^>]*>([\s\S]*?)<\/title>/i.exec(html);
  const title = titleMatch ? decodeEntities(titleMatch[1].trim()) : '';
  const bodyMatch = /<body[^>]*>([\s\S]*)<\/body>/i.exec(html);
  let s = bodyMatch ? bodyMatch[1] : html;

  s = s.replace(/<(script|style|noscript|svg|template|iframe|head)\b[\s\S]*?<\/\1>/gi, '');
  s = s.replace(/<!--[\s\S]*?-->/g, '');
  s = s.replace(/<pre[^>]*>([\s\S]*?)<\/pre>/gi, (_, c) =>
    `\n\n\`\`\`\n${decodeEntities(c.replace(/<[^>]+>/g, '')).trim()}\n\`\`\`\n\n`);
  s = s.replace(/<h([1-6])[^>]*>([\s\S]*?)<\/h\1>/gi, (_, n, c) =>
    `\n\n${'#'.repeat(Number(n))} ${c.replace(/<[^>]+>/g, '').replace(/\s+/g, ' ').trim()}\n\n`);
  s = s.replace(/<a\b[^>]*href=["']([^"']+)["'][^>]*>([\s\S]*?)<\/a>/gi, (_, href, c) => {
    const text = c.replace(/<[^>]+>/g, '').replace(/\s+/g, ' ').trim();
    if (!text) return '';
    let abs = href;
    try { abs = new URL(decodeEntities(href), baseUrl).toString(); } catch { /* keep as written */ }
    return `[${text}](${abs})`;
  });
  s = s.replace(/<(strong|b)\b[^>]*>([\s\S]*?)<\/\1>/gi, '**$2**');
  s = s.replace(/<(em|i)\b[^>]*>([\s\S]*?)<\/\1>/gi, '*$2*');
  s = s.replace(/<code\b[^>]*>([\s\S]*?)<\/code>/gi, '`$1`');
  s = s.replace(/<li\b[^>]*>/gi, '\n- ');
  s = s.replace(/<br\s*\/?>/gi, '\n');
  s = s.replace(/<\/(p|div|section|article|header|footer|main|nav|ul|ol|table|tr|blockquote)>/gi, '\n\n');
  s = s.replace(/<[^>]+>/g, '');
  s = decodeEntities(s);
  s = s.split('\n').map((l) => l.replace(/[ \t]+/g, ' ').trim()).join('\n');
  s = s.replace(/\n{3,}/g, '\n\n').trim();

  const heading = title && !s.startsWith('# ') ? `# ${title}\n\n` : '';
  return `${heading}${s}\n`;
}

function markdownResponse(body, status, source) {
  return new Response(body, {
    status,
    headers: {
      'content-type': 'text/markdown; charset=utf-8',
      'vary': 'Accept',
      // private: a shared cache keyed on the URL alone (Cloudflare's subrequest
      // cache ignores Vary: Accept) would hand this to an HTML client.
      'cache-control': 'private, max-age=600',
      'x-markdown-tokens': String(Math.ceil(body.length / 4)),
      'x-markdown-source': source,
      'access-control-allow-origin': '*',
    },
  });
}

async function fetchWithTimeout(url, init = {}) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), UPSTREAM_TIMEOUT_MS);
  try {
    return await fetch(url, { ...init, signal: ctrl.signal, cf: { cacheTtl: 3600, cacheEverything: true } });
  } finally {
    clearTimeout(timer);
  }
}

function withHeaders(response, extra) {
  const out = new Response(response.body, response);
  for (const [k, v] of Object.entries(extra)) out.headers.set(k, v);
  return out;
}

async function serveMarkdown(request, url) {
  // The homepage is an interactive desktop with almost no static text; its
  // agent-facing summary IS llms.txt (which is markdown by its own spec).
  if ((url.pathname === '/' || url.pathname === '/index.html') && homepageMarkdown) {
    return markdownResponse(homepageMarkdown, 200, 'bundled');
  }
  if (url.pathname === '/' || url.pathname === '/index.html') {
    // Measured 2026-10-04: a caller that is itself a Worker (the
    // isitagentready.com scanner) got HTML here while a plain client got
    // markdown -- this inner subrequest is the one call such a caller never
    // reaches. Any failure on it now falls through to converting the page.
    try {
      const r = await fetch(new URL('/llms.txt', url).toString());
      if (r.ok) return markdownResponse(await r.text(), 200, '/llms.txt');
    } catch { /* convert the page itself below */ }
  }
  const origin = await fetch(request);
  const type = origin.headers.get('content-type') || '';
  if (!type.includes('text/html')) return origin; // already non-HTML: leave it alone
  const html = await origin.text();
  return markdownResponse(htmlToMarkdown(html, url.toString()), origin.status, 'html');
}

async function handle(request) {
  const url = new URL(request.url);
  const path = url.pathname;
  const method = request.method;
  if (method !== 'GET' && method !== 'HEAD') return fetch(request);

  if (path in AUTH_METADATA) {
    const upstream = AUTH_METADATA[path];
    const r = await fetchWithTimeout(upstream);
    if (!r.ok) return fetch(request);
    return withHeaders(r, {
      'content-type': 'application/json; charset=utf-8',
      'access-control-allow-origin': '*',
      'cache-control': 'public, max-age=3600',
      'link': `<${upstream}>; rel="canonical"`,
    });
  }

  if (path === '/auth.md') {
    try {
      const r = await fetchWithTimeout(AUTH_MD_UPSTREAM);
      if (r.ok) {
        return withHeaders(r, {
          'content-type': CONTENT_TYPES['/auth.md'],
          'access-control-allow-origin': '*',
          'cache-control': 'public, max-age=3600',
          'link': `<${AUTH_MD_UPSTREAM}>; rel="canonical"`,
        });
      }
    } catch { /* IdP slow or down: the static copy below answers */ }
  }

  if (wantsMarkdown(request.headers.get('accept')) && !(path in CONTENT_TYPES) && path !== '/auth.md') {
    return serveMarkdown(request, url);
  }

  const response = await fetch(request);
  const extra = {};
  if (path in CONTENT_TYPES && response.ok) {
    extra['content-type'] = CONTENT_TYPES[path];
    extra['access-control-allow-origin'] = '*';
  }
  if (path === '/' || path === '/index.html') {
    extra['link'] = HOMEPAGE_LINKS;
  }
  if ((response.headers.get('content-type') || '').includes('text/html')) {
    extra['vary'] = 'Accept, Accept-Encoding';
    // Measured 2026-10-04: the isitagentready.com scanner (a Worker) got HTML
    // for Accept: text/markdown on / only -- the URL its other probes had just
    // fetched as HTML. Pages sends max-age=600 and a URL-keyed shared cache
    // ignores Vary, so the HTML was replayed. Browsers keep their 10 minutes.
    extra['cache-control'] = 'private, max-age=600';
  }
  return Object.keys(extra).length ? withHeaders(response, extra) : response;
}

export default {
  async fetch(request, _env, _ctx) {
    try {
      return await handle(request);
    } catch (err) {
      console.error('apex-agent-edge: passthrough after error', err && err.message);
      return fetch(request);
    }
  },
};
