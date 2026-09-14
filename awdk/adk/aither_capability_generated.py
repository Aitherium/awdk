"""AitherCapability v1 — GENERATED FILE, DO NOT EDIT BY HAND.

Source of truth: AitherOS/config/capability-manifest.yaml
Regenerate:      python AitherOS/dev/tools/gen_capability_protocol.py

Editing this file is pointless: check_capability_protocol_parity.py (CP004)
re-runs the generator and fails the build if this file differs."""

from dataclasses import dataclass
from typing import List


@dataclass(slots=True, frozen=True)
class Capability:
    """One agent-callable capability on the platform."""
    id: str
    display_name: str
    kind: str
    tools: List[str]
    owning_agent: str
    available: bool
    description: str
    tier: str
    requires_auth: bool
    icon: str
    tags: List[str]
    why_unavailable: str


CAPABILITIES: List[Capability] = [
    Capability(
        id='acp',
        display_name='ACP â€” drive external Agent Client Protocol agents',
        kind='toolpack',
        tools=['acp_*'],
        owning_agent='demiurge',
        available=True,
        description=(
            'Drive external ACP agents (claude-agent-acp, codex-acp, gemini-cli, ...) from the age'
            'nt console, and serve an AitherOS agent to ACP editors. Tools: acp_list_agents, acp_c'
            'onnect, acp_prompt, acp_close. The `adk acp` CLI and `adk backend add acp` cover the '
            'server / model-backend directions.'
            ),
        tier='free',
        requires_auth=False,
        icon='cable',
        tags=['acp', 'agent-client-protocol', 'external-agents', 'editors', 'protocol', 'stdio'],
        why_unavailable='',
    ),
    Capability(
        id='aitherzero',
        display_name='AitherZero â€” Inventory, Config Generation & Automation Authoring',
        kind='toolpack',
        tools=['az_*'],
        owning_agent='prometheus',
        available=True,
        description=(
            'Self-service infrastructure provisioning surface for AitherZero. Lets an aither-adk a'
            'gent inventory the numbered automation-scripts and playbooks, regenerate the config s'
            "chema from any inventory (public OR a customer's private scripts), generate and valid"
            'ate a config.local.psd1, plan a playbook into ordered steps, and scaffold a new autom'
            'ation-script that auto-extends config once scanned. All tools are fail-soft dict-retu'
            'rners.'
            ),
        tier='free',
        requires_auth=False,
        icon='sliders',
        tags=[
                'aitherzero',
                'config',
                'psd1',
                'bootstrap',
                'automation',
                'playbook',
                'provisioning',
                'inventory'
            ],
        why_unavailable='',
    ),
    Capability(
        id='arc-brainpack',
        display_name='ARC Brain â€” Contribute & Earn',
        kind='toolpack',
        tools=['arc_*'],
        owning_agent='viviane',
        available=True,
        description=(
            'Turnkey crowdsourcing pack for the AitherWorldModel: play real ARC-AGI-3 games (with '
            "a random policy, or your OWN via arc_enroll's EnvironmentAdapter) and contribute ever"
            'y (state, action, next_state) transition to the public Contribution Gateway, then tra'
            'ck your standing. Six free arc_* tools front a self-contained gateway client + a vend'
            'ored random-policy ARC player (no external checkout required): arc_register mints/exc'
            'hanges an ACTA wallet and enrolls a contributor token (POST gateway /v1/register, per'
            'sisted locally); arc_contribute plays N actions over the given games and submits tran'
            'sitions; arc_enroll enrolls a real game through env_enroll â€” the ArcGatewayAdapter '
            'lets a BYO agent play with its own policy while the local world model learns the game'
            " and every transition is contributed; arc_status returns this token's server-side acc"
            'ept count; arc_leaderboard shows the top contributors; and arc_solo prints the one-co'
            'mmand own-stack bootstrap (docker-compose.standalone.yml) so you can run the world mo'
            'del + gateway entirely on your own box. A pack UI tab ("ARC Contribute") shows live s'
            'tatus + leaderboard + a how-to.'
            ),
        tier='free',
        requires_auth=False,
        icon='brain',
        tags=[
                'arc-agi',
                'world-model',
                'crowdsource',
                'contribute',
                'acta',
                'leaderboard',
                'free'
            ],
        why_unavailable='',
    ),
    Capability(
        id='box-master',
        display_name='Box Master â€” Local Discovery & Learning',
        kind='toolpack',
        tools=['explore', 'activate', 'system_report', 'fs_map', 'learn'],
        owning_agent='prometheus',
        available=True,
        description=(
            'Agents on a box discover, activate and learn from local resources. explore() queries '
            'the gateway /discover endpoint for compact tool cards across domains. activate() fetc'
            "hes details, registers the tool schema into the agent's runtime registry, and calls i"
            't via MCPBridge. system_report() and fs_map() introspect the box (hardware, filesyste'
            'ms, network). learn() feeds agent outcomes back to the world-model (/observe :8210) a'
            'nd local GraphMemory for next-turn recall.'
            ),
        tier='free',
        requires_auth=False,
        icon='database',
        tags=['discovery', 'learning', 'local-first', 'box-introspection', 'schema-registry'],
        why_unavailable='',
    ),
    Capability(
        id='cloudflare',
        display_name='Cloudflare â€” Docs, DNS, Workers, Routes & Tunnels',
        kind='toolpack',
        tools=['cf_*'],
        owning_agent='prometheus',
        available=True,
        description=(
            'Cloudflare parity for agents. cf_docs_search queries the OFFICIAL Cloudflare document'
            'ation MCP server (docs.mcp.cloudflare.com, public/no-auth) so the agent reads current'
            ' wrangler/Workers docs instead of hallucinating flags from stale memory. Account oper'
            'ations run against the Cloudflare REST API v4 with an API token: cf_whoami (token ide'
            'ntity + a real capability probe that names the exact missing scopes), cf_zones, cf_dn'
            's_list / cf_dns_upsert / cf_dns_delete, cf_workers_list, cf_worker_routes (a hostname'
            ' route can belong to only ONE worker â€” check before deploying), cf_worker_deploy (E'
            'S-module multipart upload + optional route assignment), cf_tunnel_list (cloudflared t'
            'unnels + their ingress), cf_analytics. Pure HTTP â€” no wrangler, no SDK, no browser '
            'OAuth. Every tool fails soft: a missing token or an insufficient scope is a STATUS wi'
            'th a fix, never an exception into the agent loop.'
            ),
        tier='free',
        requires_auth=False,
        icon='cloud',
        tags=[
                'cloudflare',
                'dns',
                'workers',
                'edge',
                'tunnels',
                'wrangler',
                'docs',
                'infrastructure'
            ],
        why_unavailable='',
    ),
    Capability(
        id='deep_research',
        display_name='Deep Research â€” Multi-Engine Web Research & Report Writing',
        kind='toolpack',
        tools=['dr_*'],
        owning_agent='scribe',
        available=True,
        description=(
            'A full deep-research workflow as agent tools: multi-angle web sweeps (dr_deep_researc'
            'h), keyless multi-engine search (dr_web_search), page fetch + readability extraction '
            'with a dedup cache (dr_fetch_url), same-domain mirroring (dr_mirror_website), a citab'
            'le findings knowledge graph (dr_save_finding / dr_recall / dr_knowledge_graph), and c'
            'ited report generation to markdown/PDF/DOCX (dr_generate_*). Wraps the deep-research '
            'engine that already ships as an ADK agent template; a token-savings ledger credits de'
            'dup/cache hits. Web search is keyless (DuckDuckGo + HTML-scrape fallback), so free ti'
            'er.'
            ),
        tier='free',
        requires_auth=False,
        icon='magnifying-glass',
        tags=['research', 'web-search', 'osint', 'report-writing', 'knowledge-graph', 'citations'],
        why_unavailable='',
    ),
    Capability(
        id='graphrag',
        display_name='GraphRAG â€” Embeddings + Knowledge-Graph Setup',
        kind='toolpack',
        tools=['rag_*'],
        owning_agent='viviane',
        available=True,
        description=(
            'Set up EMBEDDINGS + GraphRAG the way the fleet runs it â€” serve nomic-embed-text (76'
            "8-dim, the fleet's canonical vector space) or CodeRankEmbed via vLLM, wire it into a "
            'local knowledge graph, ingest a corpus, and verify BOTH halves: the embedder returns '
            'the right vector dimension, and retrieval actually returns ingested content (not a si'
            'lent-empty 200). All tools are fail-soft dict-returners.'
            ),
        tier='free',
        requires_auth=False,
        icon='share-2',
        tags=[
                'embeddings',
                'graphrag',
                'rag',
                'knowledge-graph',
                'nomic',
                'vector',
                'retrieval',
                'vllm'
            ],
        why_unavailable='',
    ),
    Capability(
        id='image-bootstrap',
        display_name=(
            'Image Bootstrap â€” Hardware Detection, Recipe Matching & Image-Gen Deployment'
            ),
        kind='toolpack',
        tools=['imagegen_*'],
        owning_agent='prometheus',
        available=True,
        description=(
            'Hardware-aware IMAGE GENERATION node bootstrap â€” the node-bootstrap twin for the vi'
            'sual plane. Detects system specs, resolves deployment recipes (ComfyUI, SANA, CPU-onl'
            'y, Apple Metal, vast.ai burst), renders deployment plans, applies deployment (Docker '
            'Compose or native), registers the backend with Genesis, and verifies the endpoint is '
            'not just UP but actually has models loaded. All tools are fail-soft dict-returners.'
            ),
        tier='free',
        requires_auth=False,
        icon='image',
        tags=[
                'image',
                'comfyui',
                'sana',
                'sdxl',
                'bootstrap',
                'deployment',
                'gpu',
                'creative',
                'edge'
            ],
        why_unavailable='',
    ),
    Capability(
        id='llm-serving',
        display_name='LLM Serving â€” vLLM + Fleet Models, Quant-Optimized to the Hardware',
        kind='toolpack',
        tools=['llm_*'],
        owning_agent='apollo',
        available=True,
        description=(
            'Install vLLM and serve the AitherOS fleet models (Nemotron-Orchestrator-8B, gemma4-12'
            'b, qwen-27b, deepseek-r1-14b) with quantization and serve flags OPTIMIZED to the dete'
            'cted GPU architecture â€” NVFP4 on Blackwell, AWQ/W8A16 on Ampere, bitsandbytes on ol'
            "der cards, fp8 KV where supported. Detects hardware, resolves each model's best runna"
            'ble quant, plans the vLLM command, applies it, and verifies with a real chat round-tr'
            'ip. All tools are fail-soft dict-returners.'
            ),
        tier='free',
        requires_auth=False,
        icon='cpu',
        tags=[
                'llm',
                'vllm',
                'serving',
                'quantization',
                'nvfp4',
                'awq',
                'nemotron',
                'gemma',
                'qwen',
                'deepseek'
            ],
        why_unavailable='',
    ),
    Capability(
        id='mesh-ops',
        display_name='Mesh Ops â€” Enroll, Run, Register & Deploy Across the AitherMesh',
        kind='toolpack',
        tools=['mesh_*'],
        owning_agent='prometheus',
        available=True,
        description=(
            'Codifies the AitherMesh node lifecycle as agent-callable tools so an agent (or operat'
            'or) can drive the whole flow without hand-SSHing: list registered mesh nodes, run a c'
            'ommand on a node over vault-backed SSH, enroll a node onto the Headscale control plan'
            "e, register a node's reachable endpoint + SSH target in the fleet registry, and deplo"
            'y an adk agent onto a node pointed at its own inference. All tools are fail-soft dict'
            '-returners; SSH creds resolve server-side from the vault by name (callers never pass '
            'keys).'
            ),
        tier='free',
        requires_auth=False,
        icon='network',
        tags=[
                'mesh',
                'headscale',
                'tailscale',
                'node',
                'ssh',
                'deploy',
                'enroll',
                'registry',
                'overlay'
            ],
        why_unavailable='',
    ),
    Capability(
        id='node-bootstrap',
        display_name='Node Bootstrap â€” Hardware Detection, Recipe Matching & Deployment',
        kind='toolpack',
        tools=['node_*'],
        owning_agent='prometheus',
        available=True,
        description=(
            'Hardware-aware inference node bootstrap. Detects system specs, resolves deployment re'
            'cipes (vLLM, Ollama, llama.cpp, cloud-api), renders deployment plans, applies deploym'
            'ent (Docker Compose or systemd), enrolls with the control plane, registers backends, '
            'and verifies inference endpoints. All tools are fail-soft dict-returners.'
            ),
        tier='free',
        requires_auth=False,
        icon='server',
        tags=[
                'node',
                'bootstrap',
                'inference',
                'deployment',
                'vllm',
                'ollama',
                'hardware',
                'edge'
            ],
        why_unavailable='',
    ),
    Capability(
        id='nvidia_cosmos',
        display_name='NVIDIA Cosmos Video Generation',
        kind='toolpack',
        tools=['cosmos_*'],
        owning_agent='apollo',
        available=False,
        description='Foundation model for video understanding, generation, and prediction.',
        tier='free',
        requires_auth=False,
        icon='',
        tags=['nvidia', 'cosmos'],
        why_unavailable='Not yet integrated into the platform',
    ),
    Capability(
        id='nvidia_cuda',
        display_name='NVIDIA CUDA â€” Official Docs & Code Examples (Nsight AI MCP)',
        kind='toolpack',
        tools=['cuda_*'],
        owning_agent='prometheus',
        available=True,
        description=(
            "Live access to NVIDIA's hosted CUDA MCP Server (Nsight AI): documentation and code ex"
            'amples indexed and curated by NVIDIA engineers. cuda_docs_search is the point of the '
            'pack â€” call it before writing or reviewing CUDA C++/kernels, nvcc or CMake flags, C'
            'UDA Toolkit / cuBLAS / cuDNN / NCCL / Thrust / CUB calls, occupancy and coalescing ad'
            'vice, Nsight Compute/Systems metric names, or any compute-capability claim. cuda_docs'
            '_tools reports what the server actually exposes (the search tool is discovered by sha'
            'pe, never hardcoded, so an upstream rename fails loudly instead of returning "no resu'
            'lts"). cuda_docs_status asks the SERVER whether the lane can answer rather than readi'
            'ng local config. cuda_docs_login runs the OAuth authorization-code + PKCE flow once i'
            'n a browser (free NVIDIA Developer account) and stores a refresh token 0600 under ~/.'
            'aither/, after which headless agents need no human step. Pure HTTP â€” no NGC CLI, no'
            ' vendored SDK. Every tool fails soft: NOT AUTHENTICATED is a status with a fix, never'
            ' an exception into the agent loop.'
            ),
        tier='free',
        requires_auth=False,
        icon='cpu',
        tags=['nvidia', 'cuda', 'gpu', 'kernels', 'nsight', 'docs', 'performance', 'hpc'],
        why_unavailable='',
    ),
    Capability(
        id='nvidia_deepstream',
        display_name='NVIDIA DeepStream',
        kind='toolpack',
        tools=['deepstream_*'],
        owning_agent='apollo',
        available=False,
        description='Intelligent video analytics platform for real-time multi-stream processing.',
        tier='free',
        requires_auth=False,
        icon='',
        tags=['nvidia', 'deepstream'],
        why_unavailable='Not yet integrated into the platform',
    ),
    Capability(
        id='nvidia_dynamo',
        display_name='NVIDIA Dynamo',
        kind='toolpack',
        tools=['dynamo_*'],
        owning_agent='apollo',
        available=False,
        description='Datacenter-scale inference serving platform with dynamic load balancing.',
        tier='free',
        requires_auth=False,
        icon='',
        tags=['nvidia', 'dynamo'],
        why_unavailable='Not yet integrated into the platform',
    ),
    Capability(
        id='nvidia_isaac',
        display_name='NVIDIA Isaac Robotics',
        kind='toolpack',
        tools=['isaac_*'],
        owning_agent='apollo',
        available=False,
        description='Platform for autonomous machines perception, simulation, and deployment.',
        tier='free',
        requires_auth=False,
        icon='',
        tags=['nvidia', 'isaac'],
        why_unavailable='Not yet integrated into the platform',
    ),
    Capability(
        id='nvidia_jetson',
        display_name='NVIDIA JetPack Edge Runtime',
        kind='toolpack',
        tools=['jetson_*'],
        owning_agent='apollo',
        available=False,
        description='Optimized inference runtime for JetPack edge agents (Jetson, Orin).',
        tier='free',
        requires_auth=False,
        icon='',
        tags=['nvidia', 'jetson'],
        why_unavailable='Not yet integrated into the platform',
    ),
    Capability(
        id='nvidia_nemo',
        display_name='NVIDIA NeMo Framework',
        kind='toolpack',
        tools=['nemo_*'],
        owning_agent='apollo',
        available=False,
        description=(
            'Modular framework for training and fine-tuning LLMs, speech, and multimodal models.'
            ),
        tier='free',
        requires_auth=False,
        icon='',
        tags=['nvidia', 'nemo'],
        why_unavailable='Not yet integrated into the platform',
    ),
    Capability(
        id='nvidia_nemotron',
        display_name='NVIDIA Nemotron Orchestrator',
        kind='toolpack',
        tools=['nemotron_*'],
        owning_agent='apollo',
        available=True,
        description='Fine-tuned LLM optimized for multi-agent orchestration and planning.',
        tier='free',
        requires_auth=False,
        icon='',
        tags=['nvidia', 'nemotron'],
        why_unavailable='',
    ),
    Capability(
        id='nvidia_nim',
        display_name='NVIDIA NIM LLM Microservice',
        kind='toolpack',
        tools=['nim_*'],
        owning_agent='apollo',
        available=True,
        description=(
            'Containerized, production-grade LLM inference microservice (OpenAI-compatible).'
            ),
        tier='free',
        requires_auth=False,
        icon='',
        tags=['nvidia', 'nim'],
        why_unavailable='',
    ),
    Capability(
        id='nvidia_openshell',
        display_name='NVIDIA OpenShell',
        kind='toolpack',
        tools=['openshell_*'],
        owning_agent='prometheus',
        available=False,
        description=(
            'Lightweight agentic shell that orchestrates other tools (integrates into AitherShell)'
            '.'
            ),
        tier='free',
        requires_auth=False,
        icon='',
        tags=['nvidia', 'openshell'],
        why_unavailable='Not yet integrated into the platform',
    ),
    Capability(
        id='nvidia_riva',
        display_name='NVIDIA Riva Speech AI',
        kind='toolpack',
        tools=['riva_*'],
        owning_agent='apollo',
        available=False,
        description='Multi-lingual speech recognition, synthesis, and understanding platform.',
        tier='free',
        requires_auth=False,
        icon='',
        tags=['nvidia', 'riva'],
        why_unavailable='Not yet integrated into the platform',
    ),
    Capability(
        id='nvidia_switchyard',
        display_name='NVIDIA NeMo Switchyard',
        kind='toolpack',
        tools=['switchyard_*'],
        owning_agent='apollo',
        available=False,
        description=(
            'Model routing and orchestration across agent workloads and inference backends.'
            ),
        tier='free',
        requires_auth=False,
        icon='',
        tags=['nvidia', 'switchyard'],
        why_unavailable='Not yet integrated into the platform',
    ),
    Capability(
        id='nvidia_tensorrt_llm',
        display_name='TensorRT-LLM Inference Engine',
        kind='toolpack',
        tools=['trt_*'],
        owning_agent='apollo',
        available=False,
        description=(
            "NVIDIA's production LLM inference engine with dynamic batching and quantization."
            ),
        tier='free',
        requires_auth=False,
        icon='',
        tags=['nvidia', 'trt'],
        why_unavailable='Not yet integrated into the platform',
    ),
    Capability(
        id='planb-ledger',
        display_name='Plan B Ledger â€” One Ledger, Two Faces',
        kind='toolpack',
        tools=['planb_*'],
        owning_agent='demiurge',
        available=True,
        description=(
            'Offline-first personal/business continuity ledger. Digital face (Discord bot, CLI, ag'
            'ent tools) and paper face (printable structured checkpoint sheets) stay interchangeab'
            'le: work on paper when tech is down, reconcile marks back when it returns, with confl'
            'ict detection so nothing is double-counted. Brain is bonsai-27b via llama.cpp (local,'
            ' no cloud) with a deterministic fallback. All tools are fail-soft dict-returners; all'
            ' state is local JSON.'
            ),
        tier='free',
        requires_auth=False,
        icon='book-open-check',
        tags=[
                'ledger',
                'budgeting',
                'offline-first',
                'continuity',
                'discord',
                'bonsai',
                'llamacpp',
                'paper'
            ],
        why_unavailable='',
    ),
    Capability(
        id='split-inference',
        display_name='Split Inference â€” Multi-Node llama.cpp RPC Model Sharding',
        kind='toolpack',
        tools=['split_*'],
        owning_agent='apollo',
        available=True,
        description=(
            "Shard ONE model across MULTIPLE machines' GPUs using llama.cpp's RPC backend. Detects"
            ' the local device topology plus reachable RPC backends, resolves a split recipe again'
            'st combined VRAM, builds llama.cpp with -DGGML_RPC=ON, starts rpc-server on the backe'
            'nd hosts, launches the main server with --rpc, and â€” the point of the pack â€” PROV'
            'ES the split is real rather than a silent local-only fallback. All tools are fail-sof'
            't dict-returners.'
            ),
        tier='free',
        requires_auth=False,
        icon='layers',
        tags=['inference', 'llamacpp', 'rpc', 'distributed', 'sharding', 'gpu', 'bonsai', 'mesh'],
        why_unavailable='',
    ),
    Capability(
        id='svc_directory',
        display_name='AitherDirectory Auth',
        kind='toolpack',
        tools=['dir_*'],
        owning_agent='prometheus',
        available=True,
        description=(
            'Session management, RBAC, and entitlement â€” the auth plane for all agents and users'
            '.'
            ),
        tier='free',
        requires_auth=False,
        icon='',
        tags=['aitheros-platform', 'dir', 'service-api'],
        why_unavailable='',
    ),
    Capability(
        id='svc_identity',
        display_name='AitherIdentity Sessions',
        kind='toolpack',
        tools=['ident_*'],
        owning_agent='prometheus',
        available=True,
        description='User identity and session lifecycle â€” create, refresh, revoke sessions.',
        tier='free',
        requires_auth=False,
        icon='',
        tags=['aitheros-platform', 'ident', 'service-api'],
        why_unavailable='',
    ),
    Capability(
        id='svc_microscheduler',
        display_name='MicroScheduler LLM Queue',
        kind='toolpack',
        tools=['sched_*'],
        owning_agent='prometheus',
        available=True,
        description=(
            'LLM request queue, rate limiting, and VRAM coordination â€” critical for orchestrated'
            ' inference.'
            ),
        tier='free',
        requires_auth=False,
        icon='',
        tags=['aitheros-platform', 'sched', 'service-api'],
        why_unavailable='',
    ),
    Capability(
        id='svc_nexus',
        display_name='AitherNexus Vector Search',
        kind='toolpack',
        tools=['nexus_*'],
        owning_agent='prometheus',
        available=True,
        description=(
            'Vector database and hybrid search â€” agent knowledge retrieval, semantic matching, a'
            'nd ranking via Pinecone.'
            ),
        tier='free',
        requires_auth=False,
        icon='',
        tags=['aitheros-platform', 'nexus', 'service-api'],
        why_unavailable='',
    ),
    Capability(
        id='svc_pulse',
        display_name='AitherPulse Events',
        kind='toolpack',
        tools=['pulse_*'],
        owning_agent='prometheus',
        available=True,
        description=(
            'Real-time event streaming, alert management, and circuit breaker â€” the nervous syst'
            'em.'
            ),
        tier='free',
        requires_auth=False,
        icon='',
        tags=['aitheros-platform', 'pulse', 'service-api'],
        why_unavailable='',
    ),
    Capability(
        id='svc_registry',
        display_name='AitherRegistry Service Discovery',
        kind='toolpack',
        tools=['reg_*'],
        owning_agent='prometheus',
        available=True,
        description=(
            'Service registry with health checks â€” where agents learn about available platform s'
            'ervices.'
            ),
        tier='free',
        requires_auth=False,
        icon='',
        tags=['aitheros-platform', 'reg', 'service-api'],
        why_unavailable='',
    ),
    Capability(
        id='svc_relay',
        display_name='AitherRelay Messaging',
        kind='toolpack',
        tools=['relay_*'],
        owning_agent='prometheus',
        available=True,
        description=(
            'Message routing and relay â€” agent-to-agent messaging, notifications, and async disp'
            'atch.'
            ),
        tier='free',
        requires_auth=False,
        icon='',
        tags=['aitheros-platform', 'relay', 'service-api'],
        why_unavailable='',
    ),
    Capability(
        id='svc_watch',
        display_name='AitherWatch Monitoring',
        kind='toolpack',
        tools=['watch_*'],
        owning_agent='prometheus',
        available=True,
        description=(
            'Health monitoring, metrics, and alerting across the fleet â€” observability plane.'
            ),
        tier='free',
        requires_auth=False,
        icon='',
        tags=['aitheros-platform', 'watch', 'service-api'],
        why_unavailable='',
    ),
    Capability(
        id='vault',
        display_name='Vault Lockbox â€” on-demand access to the AitherSecrets vault',
        kind='toolpack',
        tools=['vault_*'],
        owning_agent='athena',
        available=True,
        description=(
            'Browse, reveal, copy, and rotate credentials from the live AitherSecrets vault withou'
            't curl. The vault master key is sealed in the OS keychain (unlocked by your OS login)'
            '; values copy to the clipboard by default so they never land in scrollback. Tools: va'
            'ult_status, vault_list, vault_search, vault_get, vault_rotate â€” the same lockbox th'
            'e `adk vault` CLI drives, now in the console.'
            ),
        tier='free',
        requires_auth=False,
        icon='lock',
        tags=['secrets', 'vault', 'credentials', 'lockbox', 'security'],
        why_unavailable='',
    ),
    Capability(
        id='boot_codes',
        display_name='Boot Codes - mint codes for the aitherium.com boot gate',
        kind='toolpack',
        tools=['mint_boot_code'],
        owning_agent='aither',
        available=True,
        description=(
            'Mint, list and revoke boot codes for the aitherium.com gate (BFF /api/gate/admin). Sa'
            'me contract as the CLI mint_boot_code tool - this is the surface the owner actually o'
            'pens.'
            ),
        tier='free',
        requires_auth=True,
        icon='key-round',
        tags=['gate', 'boot', 'codes', 'mint', 'aitherium'],
        why_unavailable='',
    ),
]


def find_capability(cap_id: str) -> Capability | None:
    """Find a capability by ID."""
    for cap in CAPABILITIES:
        if cap.id == cap_id:
            return cap
    return None


def capabilities_by_agent(agent: str) -> List[Capability]:
    """Find all capabilities owned by an agent."""
    return [c for c in CAPABILITIES if c.owning_agent == agent]
