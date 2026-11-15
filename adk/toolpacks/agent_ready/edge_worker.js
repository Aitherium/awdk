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
 *   - A2A (JSON-RPC)          POST /a2a answers message/send from the bundled
 *                             llms.txt, so the published agent-card.json
 *                             advertises a transport that really answers
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
  '/.well-known/agent-card.json': 'application/json; charset=utf-8',
};

/** The A2A JSON-RPC endpoint the agent card advertises (transport JSONRPC). */
export const A2A_PATH = '/a2a';
const A2A_MAX_BODY = 64 * 1024;

/** Who may frame the OS homepage: the apex itself and its own subdomains. */
export const FRAME_ANCESTORS = "frame-ancestors 'self' https://*.aitherium.com";

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

// ── A2A: a thin JSON-RPC concierge answered from the bundled llms.txt ────

const A2A_CORS = {
  'access-control-allow-origin': '*',
  'access-control-allow-methods': 'POST, OPTIONS',
  'access-control-allow-headers': 'content-type, a2a-version, authorization',
};

function rpc(body) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store', ...A2A_CORS },
  });
}

const rpcError = (id, code, message) => rpc({ jsonrpc: '2.0', id: id ?? null, error: { code, message } });

const STOP = new Set(['the', 'an', 'and', 'or', 'of', 'to', 'in', 'on', 'for', 'is', 'are', 'what', 'how',
  'do', 'does', 'you', 'can', 'with', 'me', 'my', 'it', 'about', 'tell', 'your', 'this', 'that']);

function terms(text) {
  return [...new Set(String(text).toLowerCase().match(/[a-z0-9][a-z0-9-]+/g) || [])].filter((t) => !STOP.has(t));
}

/**
 * Answer a question from the site's own llms.txt: the best-matching lines,
 * nothing invented. No model call, no subrequest — the concierge points an
 * agent at the right document; MCP and the app do the real work.
 */
export function conciergeAnswer(question, md) {
  const lines = String(md || '').split('\n').map((l) => l.trim()).filter((l) => l && !l.startsWith('```'));
  const title = (lines.find((l) => l.startsWith('# ')) || '# This site').slice(2);
  const summary = (lines.find((l) => l.startsWith('> ')) || '').slice(2);
  const head = summary ? `${title}: ${summary}` : title;
  const want = terms(question);
  const scored = [];
  lines.forEach((l, i) => {
    if (l.startsWith('# ') || l.startsWith('> ')) return;
    const low = l.toLowerCase();
    let score = 0;
    for (const t of want) if (low.includes(t)) score += 1;
    if (score) scored.push({ l, i, score });
  });
  scored.sort((a, b) => b.score - a.score || a.i - b.i);
  const hits = scored.slice(0, 6).sort((a, b) => a.i - b.i).map((h) => (h.l.startsWith('- ') ? h.l : `- ${h.l}`));
  if (!hits.length) {
    return `${head}\n\nNothing in llms.txt matched that. The full index is /llms.txt; `
      + 'the MCP server card is /.well-known/mcp/server-card.json.';
  }
  return `${head}\n\nFrom llms.txt:\n${hits.join('\n')}\n\nFull index: /llms.txt`;
}

function messageText(message) {
  const parts = (message && Array.isArray(message.parts)) ? message.parts : [];
  return parts
    .filter((p) => p && typeof p.text === 'string' && (p.kind === undefined || p.kind === 'text'))
    .map((p) => p.text)
    .join('\n')
    .trim();
}

async function serveA2A(request, url) {
  const method = request.method;
  if (method === 'OPTIONS') return new Response(null, { status: 204, headers: A2A_CORS });
  if (method !== 'POST') {
    return new Response('A2A JSON-RPC: POST only (see /.well-known/agent-card.json)\n', {
      status: 405,
      headers: { allow: 'POST, OPTIONS', 'content-type': 'text/plain; charset=utf-8', ...A2A_CORS },
    });
  }
  const text = await request.text();
  if (text.length > A2A_MAX_BODY) return rpcError(null, -32600, 'request too large');
  let req;
  try { req = JSON.parse(text); } catch { return rpcError(null, -32700, 'parse error'); }
  if (!req || typeof req !== 'object' || Array.isArray(req) || req.jsonrpc !== '2.0' || typeof req.method !== 'string') {
    return rpcError(req && req.id, -32600, 'invalid request');
  }
  const { id } = req;
  switch (req.method) {
    case 'message/send':
    case 'SendMessage': {
      const message = req.params && req.params.message;
      const q = messageText(message);
      if (!q) return rpcError(id, -32602, 'params.message needs at least one text part');
      let md = homepageMarkdown;
      if (!md) {
        try {
          const r = await fetch(new URL('/llms.txt', url).toString());
          if (r.ok) md = await r.text();
        } catch { /* answer with pointers only */ }
      }
      return rpc({
        jsonrpc: '2.0',
        id,
        result: {
          kind: 'message',
          role: 'agent',
          messageId: crypto.randomUUID(),
          contextId: (message && message.contextId) || crypto.randomUUID(),
          parts: [{ kind: 'text', text: conciergeAnswer(q, md) }],
        },
      });
    }
    case 'tasks/get':
    case 'tasks/cancel':
    case 'GetTask':
    case 'CancelTask':
      // Every reply is an immediate Message; no task is ever created.
      return rpcError(id, -32001, 'task not found');
    default:
      return rpcError(id, -32601, `method not found: ${req.method}`);
  }
}

async function handle(request) {
  const url = new URL(request.url);
  const path = url.pathname;
  const method = request.method;
  if (path === A2A_PATH) return serveA2A(request, url);
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
    // The OS at / mints a sign-in code ("Set up this computer", ?app=setup). Pages
    // cannot send headers, so nothing stopped a stranger's page from framing it under
    // a decoy (security review, 2026-10-04). Our own surfaces
    // (one label under the apex) may still frame it.
    if ((response.headers.get('content-type') || '').includes('text/html')) {
      extra['content-security-policy'] = FRAME_ANCESTORS;
    }
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
