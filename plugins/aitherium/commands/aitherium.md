---
description: Get started with Aitherium - what it is, what this machine can run, and how to set it up
argument-hint: "[status | setup | fit]"
---

The user ran `/aitherium $ARGUMENTS`.

Use the `aitherium` MCP server that this plugin installs. Its tools are named
`mcp__plugin_aitherium_aitherium__<tool>`.

1. Call `about_aitherium`. It works without an account. Tell the user in two or
   three sentences what Aitherium is and whether they are signed in.
2. Pick the branch from the argument (no argument means `status`):
   - `status`: if signed in, call `account_status` and report the plan and
     workspace. If not signed in, say which tools work now
     (`about_aitherium`, `check_local_model_fit`, `search_docs`) and that
     running `/mcp` and choosing `aitherium` signs them in or creates a free
     account.
   - `fit`: ask for (or measure, with the user's permission) installed RAM and
     GPU memory, call `check_local_model_fit`, and report the recommended model,
     its download size and why the larger ones do not fit.
   - `setup`: follow the `aitherium:setup` skill.
3. End with the one next step that fits what you found. Do not list every tool.

If the MCP server is not connected, say so and point the user to `/mcp`; the
connector URL is `https://mcp.aitherium.com/connector`.
