---
name: nvidia-cuda-mcp
description: >-
  Give your agent current CUDA knowledge. CUDA is the worst possible subject to answer from a language model's memory. The API surface is large, version-skewed, and full of names that are *almost* right: cudaMallocAsync vs cudaMalloc, __grid_constant__ (12.x only), Nsight Compute metric names that changed between releases, occupancy rules that differ per compute capability.
---

# nvidia-cuda-mcp — give your agent current CUDA knowledge

CUDA is the worst possible subject to answer from a language model's memory. The API
surface is large, version-skewed, and full of names that are *almost* right:
`cudaMallocAsync` vs `cudaMalloc`, `__grid_constant__` (12.x only), Nsight Compute metric
names that changed between releases, occupancy rules that differ per compute capability.

An answer that is 90% right in CUDA does not compile — or, worse, it compiles and is
silently slow. Nothing fails. Nobody notices.

NVIDIA now hosts an MCP server over a corpus of CUDA documentation and code examples
curated by their own engineers. This skill is how to wire it into a coding agent, and the
doctrine for using it so it actually changes the answers.

## The server

```
https://api.copilot.nsight.ngc.nvidia.com/mcp/cuda-docs
```

It is **not keyless**. Every unauthenticated call returns `401` with a
`www-authenticate: Bearer ... resource_metadata=...` header pointing at the OAuth
protected-resource document. Its authorization server advertises:

```
grant_types_supported:        ["authorization_code", "refresh_token"]
code_challenge_methods_supported: ["S256"]
registration_endpoint:        /register     (open dynamic client registration)
```

Two consequences worth knowing before you design around it:

- **There is no device-code grant.** If your platform mints headless bearers with RFC 8628
  (as many do), that machinery does not apply here. One human browser approval with a free
  NVIDIA Developer account is unavoidable.
- **Dynamic client registration is open**, and a loopback redirect (`http://127.0.0.1:PORT/…`)
  is accepted — so a headless agent *can* run the flow itself and hand a human the URL. After
  the first approval the refresh token carries it indefinitely.

## Claude Code

```bash
claude mcp add --scope user --transport http nvidia-cuda-docs \
  https://api.copilot.nsight.ngc.nvidia.com/mcp/cuda-docs
```

Then run `/mcp`, pick `nvidia-cuda-docs`, and authenticate. Claude Code drives the OAuth
flow itself (registration + PKCE) and reuses the token afterwards. Until you do,
`claude mcp list` reports the server as `! Needs authentication` — which is a *status*,
not a failure, and it is the single most common reason someone concludes "the server is
broken".

Note the asymmetry if you also run a LOCAL MCP gateway: a local gateway is better wired as
a **stdio bridge** that retries forever, because a direct HTTP entry can be permanently
stripped from a session that launched before the local service was up. A remote server
like NVIDIA's has no such window — it is either reachable or it 401s, and both are states
a client retries normally — so plain `http` is correct here.

## awdk agents

The `nvidia_cuda` tool pack ships with the adk and exposes:

| tool | use |
|---|---|
| `cuda_docs_search(query)` | the question. Natural language; be specific with symbol names. |
| `cuda_docs_status()` | can this lane answer? Asks the **server**, not local config. |
| `cuda_docs_tools()` | what the server really exposes right now. |
| `cuda_docs_login()` | one-time browser approval; stores a refresh token `0600`. |
| `cuda_docs_logout()` | forget the credential. |

Enable it by naming `nvidia_cuda` in an agent's tool list, or:

```bash
export NVIDIA_CUDA_MCP_TOKEN=...      # if you already hold a bearer
```

On a headless box, `cuda_docs_login(open_browser=False)` returns the sign-in URL instead
of trying to open one — hand it to the operator rather than declaring the lane dead.

## Doctrine: retrieve, then reason

**Search the corpus BEFORE writing, reviewing, or asserting anything about:**

- CUDA C++ source, kernels, launch configuration, streams, graphs, synchronization
- `nvcc` / CMake / architecture flags, PTX/SASS, separable compilation
- CUDA Toolkit, cuBLAS, cuDNN, cuFFT, NCCL, Thrust, CUB, CUTLASS signatures
- occupancy, memory coalescing, shared-memory bank conflicts, register pressure
- Nsight Compute / Nsight Systems metric or section names
- compute capability, architecture features, minimum toolkit versions

Then **cite the URLs**. A CUDA claim with a source is checkable; one without is a guess
wearing the same clothes.

Order matters. Retrieving *after* you have already drafted an answer produces confirmation
bias with citations attached — you will search for the thing you already wrote and find
something close enough.

## Three failure modes that look like success

**1. Silent fallback to memory.** If the tool reports NOT AUTHENTICATED and the agent
quietly answers anyway, the output is indistinguishable from a sourced answer until it
fails on the user's machine. Make the agent *say* the lane is down. An unsourced answer
that looks sourced is worse than no answer.

**2. A hardcoded upstream tool name.** The server's search tool can be renamed at any
time. If your client hardcodes the name, a rename degrades into "no documentation
matched" — which reads exactly like an honest miss. Discover the tool from `tools/list`
and pick it by shape; then a rename fails *loudly*, with the available names in the error.

**3. Reading an empty result as an absence.** An empty result means your wording did not
match the corpus, not that the API does not exist. Re-query with the exact symbol
(`cudaGraphInstantiateWithFlags`), not a paraphrase. Never report "CUDA has no such
function" on the strength of one empty search.

## Transport, if you are writing your own client

MCP JSON-RPC over HTTP, SSE-framed responses:

```
POST /mcp/cuda-docs   initialize                     -> Mcp-Session-Id response header
POST /mcp/cuda-docs   notifications/initialized      (echo the session id from here on)
POST /mcp/cuda-docs   tools/list                     -> the real tool names + schemas
POST /mcp/cuda-docs   tools/call {name, arguments}   -> result.content[] text blocks
```

Send `Accept: application/json, text/event-stream`; the body may come back as `data: {...}`
frames rather than bare JSON. Read the argument name out of the tool's `inputSchema`
instead of assuming `query`, and retry once through a token refresh on a `401` — your
locally computed expiry is arithmetic, but the server is the authority.
