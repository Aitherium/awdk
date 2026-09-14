/* Aither pack-UI bridge SDK.
 *
 * Loaded by a pack's page inside the admin console's sandboxed iframe
 * (<script src="/packs/_sdk.js">). The iframe has an opaque origin and NO
 * bearer token — every privileged call is a postMessage RPC to the console
 * parent, which validates the source window, checks the tool against the
 * pack's manifest, and relays through the bearer-gated
 * POST /admin/packs/{pack}/tools/{tool}/invoke.
 *
 * API (window.aither):
 *   aither.invokeTool(name, args?, opts?) -> Promise<result>
 *       opts.timeoutMs — client-side wait (default 130000; server default 120s).
 *   aither.getPackInfo() -> Promise<{id, name, tools, settings, ...}>
 *   aither.notify(message, level?)  — toast in the console ("info"|"ok"|"warn"|"error")
 */
(function (global) {
  "use strict";
  var nextId = 1;
  var pending = Object.create(null);

  function rpc(type, payload, timeoutMs) {
    return new Promise(function (resolve, reject) {
      var id = "r" + (nextId++);
      var timer = setTimeout(function () {
        delete pending[id];
        reject(new Error("aither bridge timeout (" + type + ")"));
      }, timeoutMs || 130000);
      pending[id] = { resolve: resolve, reject: reject, timer: timer };
      // targetOrigin "*": this frame is sandboxed with an opaque origin, so it
      // cannot name the parent's origin; no secret ever travels in this message.
      global.parent.postMessage(Object.assign({ aither: true, type: type, id: id }, payload), "*");
    });
  }

  global.addEventListener("message", function (ev) {
    var d = ev.data;
    if (!d || d.aither !== true || d.type !== "response" || !d.id) return;
    // Only the embedding console can reach this frame via postMessage; still,
    // ignore anything that is not the direct parent.
    if (ev.source !== global.parent) return;
    var req = pending[d.id];
    if (!req) return;
    delete pending[d.id];
    clearTimeout(req.timer);
    if (d.error) req.reject(new Error(String(d.error)));
    else req.resolve(d.result);
  });

  global.aither = {
    invokeTool: function (name, args, opts) {
      opts = opts || {};
      return rpc("invoke-tool",
        { tool: String(name), args: args || {}, timeout: opts.timeoutSec },
        opts.timeoutMs);
    },
    getPackInfo: function () { return rpc("get-pack-info", {}, 15000); },
    notify: function (message, level) {
      global.parent.postMessage(
        { aither: true, type: "notify", message: String(message), level: level || "info" }, "*");
    }
  };
})(window);
