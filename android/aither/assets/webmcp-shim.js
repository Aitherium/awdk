/*
 * The app's WebMCP surface (document.modelContext / navigator.modelContext) for its own
 * WebView, which has neither. MainActivity injects this into aitherium.com pages only
 * (PageTools); it is never served to a browser.
 *
 * It holds the tools a page registers in a registry on this page, and lets the phone's
 * agent list and run them through window.AitherMCP (the app's bridge). The page's own
 * scripts may already have looked for the API and found nothing, so it fires
 * 'modelcontextready' on window once it exists; public/webmcp.js and
 * lib/search-webmcp/registry.ts register then.
 *
 * The subset our pages call: registerTool(tool, {signal}), unregisterTool(name),
 * provideContext({tools}), clearContext(). A tool is {name, description,
 * inputSchema | parameters, execute(args)}.
 */
(function () {
  if (window.__aitherMcp || !window.AitherMCP) return;
  var MAX_RESULT = 20000; // the app clips again, smaller; this keeps the bridge cheap
  var tools = {};

  function add(tool, opts) {
    if (!tool || typeof tool.name !== 'string' || typeof tool.execute !== 'function') {
      throw new TypeError('a tool needs a name and an execute function');
    }
    var entry = {
      name: tool.name,
      description: String(tool.description || ''),
      inputSchema: tool.inputSchema || tool.parameters || { type: 'object', properties: {} },
      execute: tool.execute,
    };
    // a second registration under one name replaces the first (webmcp.js and the page
    // tools both register speak); aborting the first must not remove the second
    tools[entry.name] = entry;
    var signal = opts && opts.signal;
    if (signal && typeof signal.addEventListener === 'function') {
      if (signal.aborted) { remove(entry.name, entry); return; }
      signal.addEventListener('abort', function () { remove(entry.name, entry); }, { once: true });
    }
  }

  function remove(name, entry) {
    if (!entry || tools[name] === entry) delete tools[name];
  }

  function asText(r) {
    if (r == null) return '';
    if (typeof r === 'string') return r;
    if (r && Array.isArray(r.content)) {
      return r.content.map(function (c) { return c && c.type === 'text' ? String(c.text) : ''; })
        .filter(Boolean).join('\n');
    }
    try { return JSON.stringify(r); } catch (e) { return String(r); }
  }

  function reply(id, text) {
    text = String(text);
    if (text.length > MAX_RESULT) text = text.slice(0, MAX_RESULT);
    try { window.AitherMCP.result(String(id), text); } catch (e) { /* the app went away */ }
  }

  var mc = {
    registerTool: function (tool, opts) { add(tool, opts); },
    unregisterTool: function (name) { remove(String(name)); },
    provideContext: function (ctx) {
      tools = {};
      ((ctx && ctx.tools) || []).forEach(function (t) { add(t); });
    },
    clearContext: function () { tools = {}; },
  };

  window.__aitherMcp = {
    /** JSON of [{name, description, inputSchema}] for the tools this page has now. */
    list: function () {
      return JSON.stringify(Object.keys(tools).map(function (n) {
        var t = tools[n];
        return { name: t.name, description: t.description, inputSchema: t.inputSchema };
      }));
    },
    /** Run one tool; the answer goes back through AitherMCP.result(id, text). */
    call: function (id, name, argsJson) {
      var t = Object.prototype.hasOwnProperty.call(tools, name) ? tools[name] : null;
      if (!t) { reply(id, 'Error: this page has no tool named ' + name + '.'); return; }
      var args;
      try { args = argsJson ? JSON.parse(argsJson) : {}; } catch (e) { args = {}; }
      var client = { requestUserInteraction: function (cb) { return Promise.resolve().then(cb); } };
      Promise.resolve().then(function () { return t.execute(args || {}, client); })
        .then(function (r) { reply(id, asText(r)); },
          function (e) { reply(id, 'Error: ' + (e && e.message ? e.message : String(e))); });
    },
  };

  try { Object.defineProperty(document, 'modelContext', { value: mc, configurable: true }); } catch (e) { /* kept */ }
  try { Object.defineProperty(navigator, 'modelContext', { value: mc, configurable: true }); } catch (e) { /* kept */ }
  window.dispatchEvent(new Event('modelcontextready'));
})();
