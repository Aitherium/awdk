/* aither-constellation — the ecosystem, live, on every surface.
 *
 * Drop into any Aitherium GitHub Pages site:
 *
 *   <div id="aither-constellation" data-self="awgit"></div>
 *   <script src="aither-constellation.js"></script>
 *
 * Each repo publishes `aither-manifest.json` beside its page. GitHub Pages
 * serves those with `Access-Control-Allow-Origin: *`, so any surface can read
 * every sibling's — the network is browsable from any node in it.
 *
 * THE HONESTY RULE, and it is the whole point: a repo whose manifest is absent
 * or unreadable renders as UNKNOWN — visibly, with the reason — never as zero
 * and never omitted. Absent beats stale, and a star quietly dropped from the
 * map is the same lie as a stale number, just harder to notice. Nothing here
 * is hand-written; if a number is on screen, some repo published it.
 *
 * Self-contained on purpose: no CDN, no framework, no build step. A widget
 * that needs a toolchain does not get adopted across eight repositories.
 */
(function () {
  "use strict";

  var HOST = "https://aitherium.github.io/";
  var REPOS = [
    { id: "awdk", label: "awdk", blurb: "Build AI agent fleets — 3 lines, any backend,..." },
    { id: "awskills", label: "awskills", blurb: "Portable agent skills — self-contained..." },
    { id: "awpack", label: "awpack", blurb: "First-party agent packs — the ones we build,..." },
    { id: "awm", label: "awm", blurb: "A portable, scoped agent memory" },
    { id: "awdesk", label: "awdesk", blurb: "Aither World Desk -- the desktop body of..." },
    { id: "awnode", label: "awnode", blurb: "A lightweight local gateway — bridges your..." },
    { id: "awrun", label: "awrun", blurb: "A priority-aware queue and dispatcher for..." },
    { id: "awgraph", label: "awgraph", blurb: "A semantic code graph for agents — AST +..." },
    { id: "awgit", label: "awgit", blurb: "Semantic version control on top of git —..." },
    { id: "awdelphi", label: "awdelphi", blurb: "Anonymous multi-round expert panels — a..." },
    { id: "awtoll", label: "awtoll", blurb: "What every tool call costs you in context,..." },
    { id: "awseal", label: "awseal", blurb: "Sign an artifact so a stranger can verify it" },
    { id: "awshare", label: "awshare", blurb: "Publish an artifact and fetch it back verified" },
    { id: "awdit", label: "awdit", blurb: "An append-only audit trail whose gaps are..." },
    { id: "awbac", label: "awbac", blurb: "Role-based access control that fails closed..." },
    { id: "awiam", label: "awiam", blurb: "Who is this caller? A directory and session..." },
    { id: "awtunnel", label: "awtunnel", blurb: "Reach a service that has no public address" },
    { id: "awnest", label: "awnest", blurb: "Prove there is a human before you let them..." },
    { id: "awnboard", label: "awnboard", blurb: "A front gate you can put in front of..." },
    { id: "awnix", label: "awnix", blurb: "A Linux you can hand to an agent — immutable..." },
    { id: "awrecover", label: "awrecover", blurb: "Labelled snapshots with an all-or-nothing..." },
    { id: "awstorage", label: "awstorage", blurb: "Every drive on every node, indexed,..." },
    { id: "awrelay", label: "awrelay", blurb: "Portable agent messaging — findings, alerts,..." },
    { id: "awask", label: "awask", blurb: "Your agent asks you a question — and acts on..." },
    { id: "awmail", label: "awmail", blurb: "Give an agent an email address — send, and..." },
    { id: "awnet", label: "awnet", blurb: "The agentic web — agents host a mesh, and..." },
    { id: "awfind", label: "awfind", blurb: "A portable search client — query, results,..." },
    { id: "awbrowse", label: "awbrowse", blurb: "A portable browser client — navigate,..." },
    { id: "awknowledge", label: "awknowledge", blurb: "How to run a coding agent so the result..." },
    { id: "gawbbonet", label: "gawbbonet", blurb: "GobboNet campaigns with a real agent brain —..." },
    { id: "aitherkvcache", label: "aitherkvcache", blurb: "Near-optimal KV cache quantization for LLM..." },
    { id: "awrtifact", label: "awrtifact", blurb: "Deliberately chunk artifacts into GitHub..." },
    { id: "AitherZero", label: "AitherZero", blurb: "PowerShell 7+ automation framework —..." },
    { id: "AitherConnect", label: "AitherConnect", blurb: "Browser extension — federated AI search, page..." },
    { id: "awreason", label: "awreason", blurb: "A portable reasoning client — sessions,..." },
    { id: "awrecurse", label: "awrecurse", blurb: "Answer a question over a context far larger..." },
    { id: "awprism", label: "awprism", blurb: "Turn a failure into ranked hypotheses — and..." },
    { id: "awrepl", label: "awrepl", blurb: "A REPL an agent can actually use — state that..." },
    { id: "awresearch", label: "awresearch", blurb: "Ask a research question, get a cited report..." },
    { id: "awfocus", label: "awfocus", blurb: "See, search and steer every Claude session..." },
    { id: "awgym", label: "awgym", blurb: "An ARC training gym — a game a world model..." },
    { id: "awpredict", label: "awpredict", blurb: "Predict what your environment does next, and..." },
    { id: "awsh", label: "awsh", blurb: "Your terminal answers you -- type a question..." },
    { id: "awkno", label: "awkno", blurb: "The man page for the Aither World — every..." },
    { id: "awembed", label: "awembed", blurb: "Train an embedding model that knows your..." },
    { id: "awtax", label: "awtax", blurb: "Turn any tax PDF -- returns, W-2, 1099,..." },
    { id: "awsettings", label: "awsettings", blurb: "Your agent's permissions and config,..." }
  ];

  var CSS = [
    "#aither-constellation{--ac-line:rgba(140,150,190,.22);position:relative;margin:2rem 0;",
    "  border:1px solid var(--line,var(--ac-line));border-radius:14px;overflow:hidden;",
    "  background:var(--panel,#12121a)}",
    "#aither-constellation canvas{display:block;width:100%;height:clamp(260px,38vh,380px);cursor:default}",
    "#aither-constellation .ac-head{position:absolute;top:.85rem;left:1rem;right:1rem;",
    "  display:flex;justify-content:space-between;gap:1rem;pointer-events:none;",
    "  font:600 .82rem/1.3 var(--sans,system-ui);color:var(--ink,#e8e8ef)}",
    "#aither-constellation .ac-head small{display:block;font-weight:400;font-size:.74rem;",
    "  color:var(--dim,#9a9aad)}",
    "#aither-constellation .ac-cards{display:grid;gap:0;grid-template-columns:repeat(auto-fit,minmax(11rem,1fr));",
    "  border-top:1px solid var(--line,var(--ac-line))}",
    "#aither-constellation .ac-card{padding:.7rem .9rem;border-right:1px solid var(--line,var(--ac-line));",
    "  font:400 .78rem/1.45 var(--sans,system-ui);color:var(--dim,#9a9aad);text-decoration:none;display:block}",
    "#aither-constellation .ac-card:last-child{border-right:none}",
    "#aither-constellation .ac-card:hover{background:rgba(140,160,255,.07)}",
    "#aither-constellation .ac-card b{display:block;color:var(--ink,#e8e8ef);font:600 .84rem/1.3 var(--mono,ui-monospace,monospace)}",
    "#aither-constellation .ac-card .ac-m{font-family:var(--mono,ui-monospace,monospace);font-size:.72rem}",
    "#aither-constellation .ac-card .ac-unknown{color:var(--warn,#e0a458);font-style:italic}",
    "#aither-constellation .ac-self{background:rgba(140,160,255,.09)}"
  ].join("\n");

  function el(tag, cls, txt) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (txt != null) n.textContent = txt;
    return n;
  }

  function fmt(v, unit) {
    if (typeof v === "number") {
      var s = v >= 1000 ? v.toLocaleString() : String(v);
      return unit ? s + unit : s;
    }
    return String(v);
  }

  /* Pull the two or three metrics worth showing on a card. A manifest may
     publish many; a card that lists all of them is a table, not a card. */
  function topMetrics(manifest, n) {
    var m = (manifest && manifest.metrics) || {};
    var keys = Object.keys(m).slice(0, n || 2);
    return keys.map(function (k) {
      return { label: k, text: fmt(m[k].value, m[k].unit || "") };
    });
  }

  function boot(mount) {
    var self = mount.getAttribute("data-self") || "";
    var style = el("style"); style.textContent = CSS;
    document.head.appendChild(style);

    var canvas = el("canvas");
    var head = el("div", "ac-head");
    var left = el("div"); left.innerHTML =
      "The Aitherium ecosystem<small>live, from each repository's own published manifest</small>";
    var right = el("div"); right.style.textAlign = "right";
    right.innerHTML = '<small id="ac-status">reading manifests…</small>';
    head.appendChild(left); head.appendChild(right);
    var cards = el("div", "ac-cards");
    mount.appendChild(canvas); mount.appendChild(head); mount.appendChild(cards);

    var stars = REPOS.map(function (r, i) {
      return {
        repo: r, i: i, manifest: null, state: "loading", reason: "",
        x: 0, y: 0, phase: i * 1.7, self: r.id === self
      };
    });

    /* --- fetch every sibling manifest; absence is a RESULT, not an error --- */
    stars.forEach(function (s) {
      fetch(HOST + s.repo.id + "/aither-manifest.json", { cache: "no-store" })
        .then(function (r) {
          if (!r.ok) throw new Error("HTTP " + r.status);
          return r.json();
        })
        .then(function (j) { s.manifest = j; s.state = "live"; render(); })
        .catch(function (e) {
          s.state = "unknown";
          s.reason = e.message === "Failed to fetch" ? "unreachable" : e.message;
          render();
        });
    });

    function render() {
      var live = stars.filter(function (s) { return s.state === "live"; }).length;
      var pending = stars.filter(function (s) { return s.state === "loading"; }).length;
      var st = document.getElementById("ac-status");
      if (st) {
        st.textContent = pending
          ? "reading manifests…"
          : live + " of " + stars.length + " publishing live metrics";
      }
      cards.textContent = "";
      stars.forEach(function (s) {
        var a = el("a", "ac-card" + (s.self ? " ac-self" : ""));
        a.href = HOST + s.repo.id + "/";
        a.appendChild(el("b", null, s.repo.label + (s.self ? "  ← you are here" : "")));
        a.appendChild(el("div", null, s.repo.blurb));
        if (s.state === "live") {
          topMetrics(s.manifest, 2).forEach(function (m) {
            a.appendChild(el("div", "ac-m", m.text + "  " + m.label));
          });
        } else if (s.state === "unknown") {
          // Never a zero. A repo that publishes nothing says so.
          a.appendChild(el("div", "ac-m ac-unknown", "no manifest (" + s.reason + ")"));
        } else {
          a.appendChild(el("div", "ac-m", "…"));
        }
        cards.appendChild(a);
      });
    }
    render();

    /* --- the star map --- */
    var ctx = canvas.getContext("2d"), t = 0, raf = null;
    var dust = [];
    for (var d = 0; d < 70; d++) {
      dust.push({ x: Math.random(), y: Math.random(), r: Math.random() * 1.1 + .2,
                  s: Math.random() * .3 + .05 });
    }

    function dims() {
      var r = canvas.getBoundingClientRect(), dp = window.devicePixelRatio || 1;
      canvas.width = r.width * dp; canvas.height = r.height * dp;
      ctx.setTransform(dp, 0, 0, dp, 0, 0);
      return { w: r.width, h: r.height };
    }

    function frame() {
      var D = dims(); t += 0.006;
      ctx.clearRect(0, 0, D.w, D.h);

      for (var i = 0; i < dust.length; i++) {
        var p = dust[i];
        var alpha = .18 + .17 * Math.sin(t * 2 + i);
        ctx.beginPath();
        ctx.arc(p.x * D.w, ((p.y + t * p.s * .12) % 1) * D.h, p.r, 0, 6.284);
        ctx.fillStyle = "rgba(150,165,220," + alpha.toFixed(3) + ")";
        ctx.fill();
      }

      var cx = D.w / 2, cy = D.h / 2 + 8;
      var R = Math.min(D.w, D.h) * 0.32;
      stars.forEach(function (s, i) {
        var a = (i / stars.length) * Math.PI * 2 + t * 0.18;
        s.x = cx + Math.cos(a) * R * (D.w > 520 ? 1.45 : 1);
        s.y = cy + Math.sin(a) * R;
      });

      ctx.strokeStyle = "rgba(140,164,255,.16)"; ctx.lineWidth = 1;
      for (var m = 0; m < stars.length; m++) {
        for (var n = m + 1; n < stars.length; n++) {
          ctx.beginPath();
          ctx.moveTo(stars[m].x, stars[m].y);
          ctx.lineTo(stars[n].x, stars[n].y);
          ctx.stroke();
        }
      }

      stars.forEach(function (s) {
        var pulse = 1 + 0.16 * Math.sin(t * 2.2 + s.phase);
        var live = s.state === "live";
        var base = live ? (s.self ? 8 : 6) : 4;
        var r = base * pulse;
        var col = live ? (s.self ? "255,255,255" : "150,175,255") : "224,164,88";
        ctx.beginPath(); ctx.arc(s.x, s.y, r * 3.4, 0, 6.284);
        ctx.fillStyle = "rgba(" + col + ",.10)"; ctx.fill();
        ctx.beginPath(); ctx.arc(s.x, s.y, r, 0, 6.284);
        ctx.fillStyle = "rgba(" + col + ",.95)"; ctx.fill();
        ctx.fillStyle = live ? "rgba(220,226,255,.85)" : "rgba(224,164,88,.85)";
        ctx.font = (s.self ? "600 " : "400 ") + "11px ui-monospace,monospace";
        ctx.textAlign = "center";
        ctx.fillText(s.repo.label, s.x, s.y + r + 14);
      });

      raf = window.requestAnimationFrame(frame);
    }

    /* A page that burns CPU in a background tab is a page people close. */
    document.addEventListener("visibilitychange", function () {
      if (document.hidden && raf) { window.cancelAnimationFrame(raf); raf = null; }
      else if (!document.hidden && !raf) { frame(); }
    });

    canvas.addEventListener("click", function (ev) {
      var b = canvas.getBoundingClientRect();
      var mx = ev.clientX - b.left, my = ev.clientY - b.top;
      for (var i = 0; i < stars.length; i++) {
        var s = stars[i], dx = mx - s.x, dy = my - s.y;
        if (dx * dx + dy * dy < 400) { window.location.href = HOST + s.repo.id + "/"; return; }
      }
    });
    canvas.addEventListener("mousemove", function (ev) {
      var b = canvas.getBoundingClientRect();
      var mx = ev.clientX - b.left, my = ev.clientY - b.top, hit = false;
      for (var i = 0; i < stars.length; i++) {
        var s = stars[i], dx = mx - s.x, dy = my - s.y;
        if (dx * dx + dy * dy < 400) { hit = true; break; }
      }
      canvas.style.cursor = hit ? "pointer" : "default";
    });

    frame();
  }

  function init() {
    var mount = document.getElementById("aither-constellation");
    if (mount) boot(mount);
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else { init(); }
})();
