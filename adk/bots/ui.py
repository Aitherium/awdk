"""Self-contained interactive Playground UI for AitherGem and AitherGPT bots.

All HTML, CSS, and JS live inside this Python module (zero external CDN
dependencies, zero non-``.py`` package data files) so wheel builds and offline
air-gapped deployments render identically.
"""

from __future__ import annotations

import html
import json
from typing import Any


def _safe_json_for_html(obj: Any) -> str:
    return (
        json.dumps(obj, ensure_ascii=False)
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )


def render_playground_html(bot_info: dict[str, Any]) -> str:
    """Render a self-contained HTML5/CSS/JS Playground UI for *bot_info*."""
    style = str(bot_info.get("style") or "gemini").lower()
    is_gemini = style == "gemini"

    brand_name = "AitherGem" if is_gemini else "AitherGPT"
    accent = "#1a73e8" if is_gemini else "#10a37f"
    accent_soft = "rgba(26, 115, 232, 0.14)" if is_gemini else "rgba(16, 163, 127, 0.14)"

    name = html.escape(str(bot_info.get("name") or bot_info.get("id") or "Aither Bot"))
    bot_id = html.escape(str(bot_info.get("id") or "bot"))
    description = html.escape(str(bot_info.get("description") or ""))
    model = html.escape(str(bot_info.get("model") or ""))
    backend = html.escape(str(bot_info.get("backend") or "auto"))
    version = html.escape(str(bot_info.get("version") or "0.1.0"))
    safe_style = html.escape(style)

    starters = bot_info.get("conversation_starters") or []
    starters_html = "".join(
        f'<button type="button" class="starter-chip" data-prompt="{html.escape(str(s), quote=True)}">'
        f"{html.escape(str(s))}</button>"
        for s in starters
    )

    tools_list = [str(t) for t in (bot_info.get("registered_tools") or [])]
    actions_list = [
        str(a.get("name") or "")
        for a in (bot_info.get("actions") or [])
        if isinstance(a, dict)
    ]
    knowledge_count = int(bot_info.get("knowledge_chunks") or 0)
    knowledge_paths = [
        str(p) for p in ((bot_info.get("knowledge") or {}).get("paths") or [])
    ]

    capabilities = bot_info.get("capabilities") or {}
    grounding = bot_info.get("grounding") or {}
    if is_gemini:
        feature_badges = [
            f"local_knowledge: {'on' if grounding.get('local_knowledge') else 'off'}",
            f"web_grounding: {'on' if grounding.get('google_search_style_web') else 'off'}",
            f"safety_rules: {len(bot_info.get('safety_settings') or [])}",
        ]
    else:
        feature_badges = [
            f"file_search: {'on' if capabilities.get('file_search') else 'off'}",
            f"code_interpreter: {'on' if capabilities.get('code_interpreter') else 'off'}",
            f"web_browsing: {'on' if capabilities.get('web_browsing') else 'off'}",
        ]

    badges_html = "".join(
        f'<span class="badge">{html.escape(b)}</span>' for b in feature_badges
    )

    tools_html = (
        "".join(f"<li><code>{html.escape(t)}</code></li>" for t in tools_list)
        if tools_list
        else '<li class="muted">No tools registered</li>'
    )
    actions_html = (
        "".join(f"<li><code>{html.escape(a)}</code></li>" for a in actions_list)
        if actions_list
        else '<li class="muted">No OpenAPI actions</li>'
    )
    knowledge_html = (
        "".join(f"<li><code>{html.escape(k)}</code></li>" for k in knowledge_paths)
        if knowledge_paths
        else '<li class="muted">No knowledge paths</li>'
    )

    snippets = bot_info.get("snippets") or {}
    default_snippet = (
        snippets.get("gemini") if is_gemini else snippets.get("openai")
    ) or ""
    boot_json = _safe_json_for_html(
        {
            "id": bot_info.get("id"),
            "style": style,
            "model": bot_info.get("model"),
            "snippets": snippets,
        }
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{name} — {brand_name} Playground</title>
<style>
  :root {{
    --bg: #0b0f17;
    --panel: #131924;
    --panel-2: #192130;
    --border: #263144;
    --text: #e6ecf5;
    --muted: #93a1b8;
    --accent: {accent};
    --accent-soft: {accent_soft};
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
    background: var(--bg);
    color: var(--text);
    display: flex;
    flex-direction: column;
    min-height: 100vh;
  }}
  header {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 14px 20px;
    border-bottom: 1px solid var(--border);
    background: var(--panel);
  }}
  .title-wrap {{ display: flex; align-items: center; gap: 12px; }}
  .pill {{
    background: var(--accent-soft);
    color: var(--accent);
    border: 1px solid var(--accent);
    border-radius: 999px;
    padding: 3px 10px;
    font-size: 12px;
    font-weight: 600;
  }}
  .meta {{ font-size: 12px; color: var(--muted); }}
  main {{
    flex: 1;
    display: grid;
    grid-template-columns: 320px 1fr;
    gap: 16px;
    padding: 16px;
    max-width: 1320px;
    width: 100%;
    margin: 0 auto;
  }}
  @media (max-width: 860px) {{
    main {{ grid-template-columns: 1fr; }}
  }}
  aside, .chat-pane {{
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 16px;
  }}
  aside h3 {{ margin: 14px 0 6px; font-size: 13px; text-transform: uppercase; letter-spacing: 0.04em; color: var(--muted); }}
  aside h3:first-child {{ margin-top: 0; }}
  .badge {{
    display: inline-block;
    margin: 2px 4px 2px 0;
    padding: 2px 8px;
    border-radius: 6px;
    background: var(--panel-2);
    border: 1px solid var(--border);
    font-size: 12px;
  }}
  ul {{ margin: 4px 0 8px; padding-left: 18px; font-size: 13px; }}
  .muted {{ color: var(--muted); }}
  pre {{
    background: #080b11;
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 10px;
    font-size: 11px;
    overflow-x: auto;
    color: #c9d6ea;
  }}
  .chat-pane {{
    display: flex;
    flex-direction: column;
    min-height: 540px;
  }}
  .starters {{ display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 12px; }}
  .starter-chip {{
    background: var(--panel-2);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 999px;
    padding: 6px 12px;
    font-size: 12px;
    cursor: pointer;
  }}
  .starter-chip:hover {{ border-color: var(--accent); }}
  .messages {{
    flex: 1;
    overflow-y: auto;
    display: flex;
    flex-direction: column;
    gap: 10px;
    padding: 8px 4px;
  }}
  .msg {{
    max-width: 82%;
    padding: 10px 14px;
    border-radius: 10px;
    line-height: 1.45;
    white-space: pre-wrap;
    font-size: 14px;
  }}
  .msg.user {{ align-self: flex-end; background: var(--accent); color: #fff; }}
  .msg.bot {{ align-self: flex-start; background: var(--panel-2); border: 1px solid var(--border); }}
  form.composer {{
    display: flex;
    gap: 10px;
    margin-top: 12px;
    border-top: 1px solid var(--border);
    padding-top: 12px;
  }}
  input[type="text"] {{
    flex: 1;
    background: var(--panel-2);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 10px 12px;
    font-size: 14px;
  }}
  button.primary {{
    background: var(--accent);
    color: #fff;
    border: none;
    border-radius: 8px;
    padding: 10px 16px;
    font-weight: 600;
    cursor: pointer;
  }}
  button.secondary {{
    background: var(--panel-2);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 4px 10px;
    font-size: 12px;
    cursor: pointer;
  }}
</style>
</head>
<body>
<header>
  <div class="title-wrap">
    <span class="pill">{brand_name}</span>
    <div>
      <strong>{name}</strong>
      <div class="meta"><code>{bot_id}</code> &middot; v{version} &middot; model: <code>{model}</code> &middot; backend: <code>{backend}</code></div>
    </div>
  </div>
  <div class="meta">{description}</div>
</header>
<main>
  <aside>
    <h3>Configuration</h3>
    <div>{badges_html}</div>
    <h3>Knowledge ({knowledge_count} chunks)</h3>
    <ul>{knowledge_html}</ul>
    <h3>Tools</h3>
    <ul>{tools_html}</ul>
    <h3>OpenAPI Actions</h3>
    <ul>{actions_html}</ul>
    <h3>SDK Snippet <button type="button" class="secondary" id="copy-snippet">Copy</button></h3>
    <pre id="sdk-snippet">{html.escape(default_snippet)}</pre>
  </aside>
  <section class="chat-pane">
    <div class="starters">{starters_html}</div>
    <div class="messages" id="messages">
      <div class="msg bot">Ready. Send a message or click a starter above to test {name} ({brand_name} / {safe_style}).</div>
    </div>
    <form class="composer" id="chat-form">
      <input type="text" id="prompt-input" placeholder="Ask {name}..." autocomplete="off" />
      <button type="submit" class="primary">Send</button>
    </form>
  </section>
</main>
<script>
const BOOT = {boot_json};
const messagesEl = document.getElementById("messages");
const inputEl = document.getElementById("prompt-input");
const formEl = document.getElementById("chat-form");
const copyBtn = document.getElementById("copy-snippet");
const history = [];

function appendMessage(role, text) {{
  const el = document.createElement("div");
  el.className = "msg " + (role === "user" ? "user" : "bot");
  el.textContent = text;
  messagesEl.appendChild(el);
  messagesEl.scrollTop = messagesEl.scrollHeight;
  return el;
}}

async function sendPrompt(text) {{
  const trimmed = (text || "").trim();
  if (!trimmed) return;
  appendMessage("user", trimmed);
  history.push({{role: "user", content: trimmed}});
  const botEl = appendMessage("bot", "...");
  let fullText = "";
  try {{
    if (BOOT.style === "gemini") {{
      const contents = history.map(m => ({{
        role: m.role === "assistant" ? "model" : "user",
        parts: [{{text: m.content}}]
      }}));
      const resp = await fetch(`/v1beta/models/${{encodeURIComponent(BOOT.model)}}:streamGenerateContent?alt=sse`, {{
        method: "POST",
        headers: {{"Content-Type": "application/json"}},
        body: JSON.stringify({{contents}})
      }});
      const reader = resp.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      botEl.textContent = "";
      while (true) {{
        const {{done, value}} = await reader.read();
        if (done) break;
        buf += dec.decode(value, {{stream: true}});
        const lines = buf.split("\\n");
        buf = lines.pop() || "";
        for (const line of lines) {{
          if (!line.startsWith("data:")) continue;
          const payload = line.slice(5).trim();
          if (!payload || payload === "[DONE]") continue;
          try {{
            const obj = JSON.parse(payload);
            const parts = (((obj.candidates || [])[0] || {{}}).content || {{}}).parts || [];
            for (const p of parts) {{
              if (p.text) {{
                fullText += p.text;
                botEl.textContent = fullText;
              }}
            }}
          }} catch (_) {{}}
        }}
      }}
    }} else {{
      const resp = await fetch("/v1/chat/completions", {{
        method: "POST",
        headers: {{"Content-Type": "application/json"}},
        body: JSON.stringify({{model: BOOT.model, messages: history, stream: true}})
      }});
      const reader = resp.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      botEl.textContent = "";
      while (true) {{
        const {{done, value}} = await reader.read();
        if (done) break;
        buf += dec.decode(value, {{stream: true}});
        const lines = buf.split("\\n");
        buf = lines.pop() || "";
        for (const line of lines) {{
          if (!line.startsWith("data:")) continue;
          const payload = line.slice(5).trim();
          if (!payload || payload === "[DONE]") continue;
          try {{
            const obj = JSON.parse(payload);
            const delta = (((obj.choices || [])[0] || {{}}).delta || {{}}).content || "";
            if (delta) {{
              fullText += delta;
              botEl.textContent = fullText;
            }}
          }} catch (_) {{}}
        }}
      }}
    }}
    history.push({{role: "assistant", content: fullText || "(empty)"}});
  }} catch (err) {{
    botEl.textContent = "Error: " + String(err);
  }}
}}

formEl.addEventListener("submit", (e) => {{
  e.preventDefault();
  const val = inputEl.value;
  inputEl.value = "";
  sendPrompt(val);
}});

document.querySelectorAll(".starter-chip").forEach((btn) => {{
  btn.addEventListener("click", () => sendPrompt(btn.dataset.prompt || ""));
}});

copyBtn.addEventListener("click", async () => {{
  const text = document.getElementById("sdk-snippet").textContent || "";
  if (navigator.clipboard) {{
    await navigator.clipboard.writeText(text);
    copyBtn.textContent = "Copied";
    setTimeout(() => (copyBtn.textContent = "Copy"), 1500);
  }}
}});
</script>
</body>
</html>
"""
