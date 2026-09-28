# adk.reasoning.solve — general reasoning loop design

Status (2026-09-25): the core is vendored and wired. `solve/_vendor/` holds h30 at
`c076671233` (hashes in `_provenance.PINNED_SOURCE`, checked by
`tests/test_reasoning_solve_pinned.py`). `_bridge`, `_control` and `_run` provide
`solve` / `SolveRun` / `SolveLoop`. The 37 h30 core, memory and sandbox tests run as
`tests/test_reasoning_solve_{core,memory,sandbox}.py`. This version differs from the
spec below in these ways:

- There is no `_events.py`. The sink is a callable, and `run_dir` gives the core's own JSONL.
- `SolveResult` carries `calibration` (ledger hits / made), not `brier`.
- `Hypothesis` has `detail` and no `abstained`.
- `Budget.max_llm_calls` defaults to h30's 40, and `max_actions` is unbounded.
- Memory: `memory.TypedMemoryBackend` stores the `Memory` protocol in
  `adk.typed_memory.TypedMemory` (keyed records, role/tier typed), scoped
  `procedural` (skills + PRISM) per domain, `hypotheses` per game, anything else per run.
  `hypotheses.py` persists hypotheses-as-code with their evidence under ns
  `hypotheses`, key `store` (one merged map, not `<domain>/<sha>` keys: the protocol
  cannot list keys); `_run` loads refuted fingerprints before a run and saves after
  it whenever a `memory` is given. Crystal / `FactStoreMemory` is not on this base.
- Slice 3's `plan()` landed as `_mcts.py` (pure: `plan`, `disagree`, `find_disagreement`,
  `ImaginedEnv`) plus `_daydream.PlanningLoop`, a subclass of the vendored loop (so the pins
  hold) that adds `plan()` / `disagree()` / `DREAM` to the sase namespace and daydreams
  offline at the head of exploring/stuck turns. `plan()` is exact breadth-first imagination
  read back through `ObservedTransitionModel.path_to_reward`, and falls back to
  `UnifiedMCTS` past `LoopConfig.plan_states`. Tests: `tests/test_reasoning_solve_plan.py`.
- `Budget.max_turns`, `min_coverage`, warm start, `doctrine`, `seed`, `SOLVE_REPLAY_BREAK`, the
  strategy rename and the CLI are **not built yet**.
- Genesis (slice 4, partial): `solve/runs.py` (named environments, host-clamped budgets,
  concurrency cap, per-process run registry) and `envs/debug.py` (`debug:demo`: make a
  failing test pass) back `lib/orchestration/SolveRuns.py`, the `solve_*` agent tools, and
  `/exploration/run/async` `driver="solve"` plus `/exploration/{run_id}/steer|cancel|events`.
  Runs stay in the starting gunicorn worker (not WorkerJobDispatcher), because steering
  needs the live session. The model is adk's `MicroSchedulerBackend`, not a
  `GatewayModelBackend`. The `_IEnvShim` for `InteractiveEnvironment` is not built.

Owner decisions from 2026-09-25: there
is **no Kaggle submission**. The goal is a GENERAL reasoning loop, and the 25 public
ARC-AGI-3 games are its first **eval**.

## How this design was chosen

Three candidate designs were scored on fit with awdk, on knowledge management and
testability, and on time to a first measured result.

| candidate | fit | knowledge / test | speed | total |
|---|---|---|---|---|
| **A. minimal-API `solve()` over ModelBackend** | 8 | 6.5 | 7 | **21.5** |
| C. surface-first `SolveRun` over vendored h30 | 7 | 7 | 6 | 20 |
| B. memory-first `mnemos` | 6 | 8.5 | 3.5 | 18 |

**Base: A.** It uses one LLM plane (`ModelBackend`), has a small frozen public surface,
keeps its imports hermetic, and reaches an ARC eval in slice 2.

Ideas grafted from the other two:

- **From C (surface-first):**
  - Vendor the h30 core verbatim at a pinned sha, with a provenance header.
  - Add only keyword-only no-op seams.
  - Run the h30 tests as parity tests, plus a file-hash drift test.
  - Enforce `Budget` / `CancelToken` / `Governor` at the sandbox trace hook, at `act()`, and before each LLM call.
  - Use a thread-safe `SessionSink` bridge onto `ReasoningSession`.
  - Add a `SOLVE_SELFTEST_BREAK` arm.
- **From B (memory-first):**
  - Content-address hypotheses (`sha256(source)[:16]`).
  - Imported or warm-started hypotheses are **priors that must re-pass replay** before they go live.
  - Add an abstention/coverage floor, so a hypothesis that predicts `None` everywhere cannot pass vacuously.
  - Stop cross-game poisoning by scoping sharing per domain.
  - Force hermetic defaults (Spirit and fleet sync off).
  - Measure the warm-vs-cold curriculum delta, so the claim that memory helps can be falsified.
  - Add a `SOLVE_REPLAY_BREAK` arm on the replay invariant.

Judge-flagged defects that this synthesis fixes:

- `__all__` is stated **once**, as 12 names.
- The eval gives each game its own run dir, so the headline number is cold.
- All TypedMemory calls (`remember` / `reinforce` / `supersede`, all `async`) stay out of the worker thread. They run on the caller's loop after the episode ends.
- ReasoningSession line references are corrected to `cancel` 142, `steer` 149, `pop_steering` 166, `emit` 177 and `observe` 190.

---

## 1. Package layout

```
awdk/adk/reasoning/solve/                 NEW. Never imported eagerly: reasoning/__init__.py
                                          gains a module __getattr__ that lazy-loads "solve",
                                          so `import adk.reasoning` works without numpy.
  __init__.py      PUBLIC. __all__ is pinned by a test (section 2). The docstring records the h30 sha.
  _types.py        Obs, Action, Environment/Memory Protocols; Strategy, Hypothesis,
                   LoopConfig, Budget, SolveResult. Derived from h30 core/interfaces.py.
  _bridge.py       _SyncModel: a sync chat() over ModelBackend.generate, run with
                   run_coroutine_threadsafe onto the caller's loop, with a per-call timeout.
                   It raises _LLMFailure. This is the ONLY module that touches adk.core.model.
  _control.py      Budget accounting, CancelToken (threading.Event) and Governor.
                   check() raises _Stopped("budget:<which>" | "cancelled").
  _events.py       EventSink protocol; NullSink, JsonlSink(path), SessionSink(session, loop).
                   The worker thread only ever uses call_soon_threadsafe / run_coroutine_threadsafe.
  _vendor/         Vendored h30 core. Each file carries the header
                   `# vendored from <upstream>@<sha>:agent/repl/core/<file>` (upstream named in _provenance.py)
    loop.py        ReasoningLoop, verbatim plus 3 keyword-only seams: sink=None, governor=None,
                   and Sandbox cancel_check=None
    intent.py      IntentClassifier (explore / exploit / test-hypothesis / plan / recover)
    sase.py        parse_reply (SASE)
    prism.py       rotation + scorer: 0.4*affinity + 0.3*win_rate + 0.2*recency;
                   the seed and clock are INJECTED
    learning.py    prediction ledger: Brier per bucket; the -0.7*gap overconfidence
                   adjustment is APPLIED
    memory.py      WorkingMemory (token-budgeted), Episodic, HypothesisStore, SkillLibrary,
                   merge_skill_maps
    sandbox.py     settrace time cap + print cap. It is a GUARD, not a security boundary.
  _adapters.py     Hermetic memory backends: InMemory, FileMemory. Optional, guarded
                   imports: TypedMemoryExporter, FactStoreMemory (awm).
  _run.py          solve(), SolveRun (the async handle), SolveLoop (AgentLoop)
  envs/toy.py      Counter1D and GridWalk5 (deterministic, stdlib only). Used by tests,
                   the CLI smoke test and self-test.
  _mcts.py         (slice 3) Adapts Environment + Episodic to an MCTSEnvironment, using
                   ObservedTransitionModel with verified predict-hypotheses as the
                   TransitionModel, so that UnifiedMCTS.search becomes a REPL tool `plan()`.

awdk/adk/evalharness/arc_agi3/            NEW (slice 2). ARC lives here and NEVER in the core.
  env_arc.py       ArcAgi3Environment(env_dir, game_id, seed) over the offline arc_agi.Arcade.
                   It carries the RESET accounting from local_loop.play_one:331-374 and the
                   ARC hooks: primer / render / describe / tools(loop) / candidates /
                   needs_xy / hud / significant_change / state_key with the HUD mask.
                   The games dir comes from env_dir= or $ADK_ARC_ENV_DIR only; neither
                   set is ArcUnavailableError (exit 2), never a guessed host path.
  perception.py    HudMask (a learned per-level meter/step-counter mask: thin,
                   never-reverting ticks on a border or edge-anchored line, confirmed
                   twice, capped at 10% of the frame), diff_text, board_map, view_text.
                   numpy; imported only when an environment is built.
  llm_policy.py    a minimal model-in-the-loop policy (render -> up to N actions per
                   call) that proves the model path and its token counts end to end.
  rhae.py          verbatim port of the upstream agent's eval/rhae.py (self-test + break arm)
  suite.py         run_suite(games, model, *, seeds=1, env_dir, out, learn_scope=None) -> rows
  fixtures/metadata.json   so rhae self-test arm G cannot silently skip in CI

awdk/adk/commands/solve.py                cmd_solve(args) -> 0|1|2 (pattern: commands/wm.py)
awdk/tests/test_reasoning_solve.py        slice 1 hermetic core
awdk/tests/test_reasoning_solve_parity.py h30 tests re-run + vendored file-hash drift
awdk/tests/test_eval_arc_agi3.py          slice 2 (includes ported test_reset_accounting)
awdk/adk/core/backends/microscheduler.py  MicroSchedulerBackend: ModelBackend + a blocking
                                          chat() over a scheduler's /v1, https verified by
                                          adk._tls.tls_verify(), token counters, model "auto"
                                          = first PREFERRED_MODELS entry /v1/models lists;
                                          5xx / unreachable -> SchedulerUnavailableError
                                          (backend_dead) -> suite exit 2.
awdk/pyproject.toml                       extra `reason = ["numpy>=1.24"]`;
                                          extra `arc` = "arc-agi==0.9.9", "arcengine==0.9.3",
                                          numpy, requests
                                          (pinned: scorer semantics differ across versions)
                                          adk/reasoning/__init__.py lazy-loads `solve` (PEP 562)
```

---

## 2. Public API

`adk.reasoning.solve.__all__` has **exactly 12 names**, and a test pins them:
`solve, SolveRun, SolveLoop, SolveResult, LoopConfig, Budget, Environment, Obs, Action, Strategy, Hypothesis, Memory`.
`InMemory` / `FileMemory` are reachable from `adk.reasoning.solve.memory` and are not in `__all__`.

```python
from __future__ import annotations           # 3.10-safe throughout
from adk.core.model import ModelBackend      # the ONLY LLM contract. No new LLM protocol.
from adk.reasoning_session import ReasoningSession

Action = tuple[int, int, int]                # (action_id, x, y); x = y = -1 when unparameterised

@dataclass
class Obs:
    state: Any                               # domain-chosen; a numpy array for ARC
    level: int = 0
    level_up: bool = False
    died: bool = False
    done: bool = False
    info: dict[str, Any] = field(default_factory=dict)

@runtime_checkable
class Environment(Protocol):                 # structurally identical to h30 interfaces.py:43-50
    def observe(self) -> Obs: ...
    def act(self, action: Action, source: str = "model") -> Obs: ...   # blocking
    def available_actions(self) -> list[int]: ...
    def done(self) -> bool: ...
    # OPTIONAL hooks, probed with getattr and never required (the h30 _hook pattern,
    # getattr(hooks, name)(*args), h30 core/loop.py:187-191). The arities are the
    # loop's CALL SITES; _types.HOOK_ARGS pins them and conformance.check_environment
    # rejects a hook that cannot be called that way (a wrong arity is a TypeError
    # inside the loop, not a doc nit). Called by the loop:
    #   primer() -> str                               loop.py:143
    #   state_key(state) -> str                       loop.py:194 (default: blake2b of bytes)
    #   describe(t) -> str                            loop.py:223 (t: Transition)
    #   auto_action() -> Action | None                loop.py:286 (the "handoff" strategy
    #                                                 and fallbacks run loop.auto -> this)
    #   candidates() -> list[Action]                  loop.py:301
    #   render(obs, last) -> str                      loop.py:463
    #   needs_xy(action_id) -> bool                   loop.py:677
    #   tools(loop) -> dict[str, tuple[Callable, str]]  loop.py:723 -- the loop passes ITSELF
    #   significant_change(before, after) -> bool     loop.py:141 -> learning.py:80
    # Declared in h30 DomainHooks (interfaces.py:53-70) or its ARC adapter, not called
    # by the core:
    #   handoff(n) -> dict                            interfaces.py:64
    #   hud() -> mask | None                          arc_env.py:368

@runtime_checkable
class Memory(Protocol):                      # same shape as h30 MemoryBackend
    def get(self, namespace: str, key: str, default: Any = None) -> Any: ...
    def put(self, namespace: str, key: str, value: Any) -> None: ...
    def update(self, namespace: str, key: str, fn: Callable[[Any], Any]) -> Any: ...

@dataclass(frozen=True)
class Strategy:                              # a PRISM arm
    name: str
    overlay: str                             # persona, heuristics, anti-patterns
    prior: float = 0.5                       # domain-affinity prior

@dataclass(frozen=True)
class Hypothesis:                            # read-only view
    id: str                                  # sha256(source)[:16], stable across runs
    kind: Literal["predict", "goal"]         # predict(state, action) -> next | None ; goal(state) -> bool
    source: str
    support: int                             # transitions predicted correctly
    abstained: int                           # transitions it returned None on
    status: Literal["pending", "active", "refuted"]

@dataclass(frozen=True)
class Budget:
    max_llm_calls: int = 80
    max_actions: int = 400
    max_wall_s: float | None = None
    max_tokens: int | None = None            # sums usage; falls back to est_tokens, labelled "estimated"
    turn_s: float = 20.0                     # sandbox time cap per REPL cell
    llm_timeout_s: float = 120.0             # bounds worst-case cancel latency

@dataclass
class LoopConfig:
    budget: Budget = field(default_factory=Budget)
    max_turns: int = 80
    wm_tokens: int = 6000
    temperature: float = 0.4
    max_tokens: int = 2000
    sase: bool = True
    doctrine: bool = True                    # reasoning_doctrine.with_doctrine(system)
    strategies: tuple[Strategy, ...] | None = None   # None -> the internal default set (publishable names)
    min_coverage: float = 0.5                # a hypothesis must decide at least this share of transitions to go active
    seed: int = 0
    run_dir: str | None = None               # JSONL log + FileMemory root; None = in-memory only

@dataclass
class SolveResult:
    finish_reason: Literal["won", "done", "budget:llm_calls", "budget:actions", "budget:wall",
                           "budget:tokens", "cancelled", "llm_error", "error"]
    won: bool; levels: int; level_actions: list[int]
    actions: int; turns: int; llm_calls: int; tokens: dict[str, int]; wall_s: float
    hypotheses: list[Hypothesis]             # final active + refuted
    brier: float | None
    strategy_trace: list[str]
    log_path: str | None
    @property
    def exit_code(self) -> int: ...          # 0 won; 1 ran-not-won/budget/cancelled;
                                             # 2 llm_error with 0 successful calls, or error before turn 1

async def solve(env: Environment, model: ModelBackend, *, goal: str = "",
                config: LoopConfig | None = None, memory: Memory | None = None,
                session: ReasoningSession | None = None) -> SolveResult:
    """SolveRun(...).start() then join(). The sync core runs in asyncio.to_thread;
    LLM calls are bridged back onto THIS loop."""

class SolveRun:                              # the handle every surface uses
    def __init__(self, env: Environment, model: ModelBackend, *, goal: str = "",
                 config: LoopConfig | None = None, memory: Memory | None = None,
                 session: ReasoningSession | None = None) -> None: ...
    run_id: str
    session: ReasoningSession                # get_session_manager().get_or_create(run_id) when not given
    def start(self) -> asyncio.Task[SolveResult]: ...
    def steer(self, message: str) -> bool: ...            # session.steer (149); drained at the head of each turn
    def cancel(self, reason: str = "cancelled") -> None: ...   # token + session.cancel (142)
    async def join(self, timeout: float | None = None) -> SolveResult: ...
    def events(self) -> AsyncIterator[dict]: ...          # session.observe(backfill=True) (190)
    def snapshot(self) -> dict: ...

class SolveLoop:                             # adk.core.agent.AgentLoop (core/agent.py:79)
    def __init__(self, env: Environment | Callable[[str], Environment], *,
                 config: LoopConfig | None = None, memory: Memory | None = None,
                 model: ModelBackend | None = None) -> None: ...
    async def run(self, agent: "Agent", prompt: str) -> "AgentResult": ...
    # model = self.model or agent.model;
    # AgentResult(output=json(SolveResult), messages=[], tool_calls=[], steps=turns, finish_reason=...)
    # The adk.core.agent import is guarded, as in mcts/loop.py:30-40.
```

**Model choice.** The core never picks a model. The CLI uses
`ReasoningRouter().resolve(tier=ModelTier.REASONING).backend` (tiers.py:266-296), or a
`--backend` preset from `_BACKEND_PRESETS` (cli.py:5534) that is materialised through
`ReasoningRouter._build_backend` into a ModelBackend. The CLI prints the resolved model
and the budget on every run.

**One turn.**

1. Drain steering into WorkingMemory.
2. Classify the intent.
3. The PRISM overlay and the doctrine build the system prompt.
4. The LLM returns a SASE reply (governor check first).
5. The code cell runs in the sandbox (governor check at every trace line). Inside the cell,
   `act()`, `hypothesize()`, `goal()`, `note()`, `skill()` and `plan()` (slice 3) are called.
   Each `act()` is charged to the Governor.
6. Each new transition is appended to Episodic and mirrored into ObservedTransitionModel.
7. The ledger resolves the predicted next obs against the actual obs.
8. `HypothesisStore` rechecks live hypotheses on the new transition (incremental). Newly
   proposed ones get a full replay over ALL history.
9. PRISM progress is MEASURED as (Δ active-hypothesis support + new states + level-ups).
   After `stuck_window` turns with no progress, rotate the strategy.
10. `sink.emit` the turn record.

---

## 3. Memory mapping

| tier | home | lifetime | notes |
|---|---|---|---|
| Working | `_vendor/memory.WorkingMemory` | per turn, token-bounded (`wm_tokens`) | not persisted; nothing in adk is token-bounded, so it ports verbatim |
| Episodic | in-process `Episodic` + `run_dir/<episode>.jsonl` + `ObservedTransitionModel.record` (observed_transition.py:79; save 178) | per episode; the log is kept | the replay ground truth; never summarised inside a run |
| Hypotheses (as code) | in-process `HypothesisStore`; source at `run_dir/hyps/<sha>.py` | live only while `replay_check` passes over ALL of Episodic | admission needs replay pass AND coverage ≥ `min_coverage`; any failure → `refuted`, out of the prompt, never re-admitted under the same sha (`_refuted_fp`) |
| Prediction ledger | `_vendor/learning` | per episode | Brier per bucket in `SolveResult.brier`; the overconfidence adjustment (−0.7·gap, n≥5, gap>0.1) is APPLIED to the next stated confidences |
| PRISM scores | `Memory` ns=`"prism"` | per run dir | wins come from measured progress, never from what the LLM reports |
| Skills | `Memory` ns=`"skills"` via `update()` + `merge_skill_maps` | per run dir | NOT `SkillStore` (global `~/.aither/skills` default, skills.py:55); `--promote-skills` exports explicitly later |
| Verified hyps (per run) | `Memory` ns=`"hypotheses"`, key `<domain>/<sha>` | per run dir | written at episode end |

**Cross-run / cross-agent (opt-in, phase 2, never imported by the core).**

- `FactStoreMemory(AwmFactStore(scope))` (crystal.py:72; awm import guarded as at 77-80) is a `Memory` a caller may pass instead of FileMemory.
- **Warm start** reads the prior verified hypotheses for the **same domain** only. They enter as `pending` priors and must re-pass replay on THIS run's transitions before they become `active`. A contradicted import is refuted, which is the poisoning defence.
- **`TypedMemoryExporter`** runs on the caller's event loop **after** `join()`, not in the worker, because `remember` / `reinforce` / `supersede` are async (typed_memory.py:428/515/528). Mapping: verified → `remember(role=INSIGHT, tier=SESSION)`; re-verified → `reinforce`; refuted → `supersede("REFUTED: …")`.

**Hermetic defaults.** The core never constructs `adk.memory.Memory`, GraphMemory or
Crystal. When the exporter does construct `Memory`, the CLI and the suite first force
`AITHER_SPIRIT_BRIDGE=false` and `AITHER_FLEET_SYNC=false` (memory.py:43-44).

---

## 4. Entry points

Every surface calls `SolveRun(env, model, config=, memory=, session=)`. The surfaces
differ only in how they build `env` and `model`.

### awsh / adk CLI

```
adk solve --env toy|arc [--game ls20] [--env-dir PATH]
          [--tier reasoning | --backend <preset> [--model M]]
          [--max-calls N] [--max-actions N] [--wall-s S] [--max-tokens N]
          [--run-dir D] [--learn-scope S] [--events jsonl|pretty|none] [--steer-stdin] [--json]
```

- **Handler.** `awdk/adk/commands/solve.py:cmd_solve(args) -> int`. The exit code is `SolveResult.exit_code`. A missing `arc` extra or a missing env-dir gives 2. A dead backend is never 0.
- **Wiring in cli.py.**
  - Parser: `_register_commands` (cli.py:12737).
  - Dispatch: `elif args.command == "solve": from adk.commands.solve import cmd_solve; sys.exit(cmd_solve(args))`, shaped like cli.py:15978.
  - Then regenerate `docs/CLI-REFERENCE.md` with `scripts/gen_cli_reference.py`. `--check` is gated in debt-invariants.yml:1464-1476, so do it in the same commit.
- **Runtime behaviour.**
  - Events stream as JSONL.
  - With `--steer-stdin`, each stdin line goes to `run.steer`.
  - Ctrl-C calls `run.cancel("sigint")`, and the final result still prints.
- **Model presets.** The default is `--tier reasoning` from `~/.aither/reasoning.json`. The `genesis` preset (`http://localhost:8001`) is dead and is never the default. A preset for the strongest :8150 pool model is added only if the tier config cannot express it.
- **`aither solve` (TS, awsh).**
  - Lives in the awsh CLI source as `src/solve-command.ts`. It is a pure `parseSolveArgs` / `buildSolveArgv` producing `['-m','adk.cli','solve',…]`, then a spawn with `stdio:'inherit'`, cloned from claude-command.ts:50-240.
  - It is intercepted in main.ts before `resolveBackend` (991), in the same shape as 929-933.
  - If adk is missing it exits 2 with an install line (rc-command.ts:71-80).
  - It does NOT use `/arc`, which ArcPlugin owns (plugins/builtins/arc.py:139).
- **Later.**
  - Long runs are submitted as awrun jobs through daemon `POST /awrun/submit` (daemon.py:1443). There is no new harness transport.
  - Optional MCP rows: `awsh_solve` and `awsh_solve_knowledge` in mcp_stdio.py TOOLS (886).

### Genesis (slice 4; lib is baked and awdk is pip-installed non-editable, so a rebuild with a real cachebust is needed)

- **`lib/core/GatewayModelBackend.py`** (about 40 lines). Its `generate()` calls `get_llm_gateway().chat(messages, tier="reasoning", effort=9, max_tokens=cfg.max_tokens)` (LLMGateway.py:1748), so every call goes through MicroScheduler. It never uses `_call_local`, which caps output at 512 tokens.
- **Stuck-problem tool.** `register_solve_tool(registry)` goes beside `register_reasoning_tool` (AgentRuntime.py:9748) and is registered at chat_engine.py:10631. It is suggested when `is_truncated_exit(reason)` fires (AgentRuntime.py:278-296).
- **Interactive runs.** `/exploration/run/async` (routers/exploration.py:239) gains `driver="solve"`. A ~30-line `_IEnvShim(InteractiveEnvironment) -> Environment` (ABC at InteractiveEnvironmentDriver.py:78-117) adapts the environment, and the run goes through WorkerJobDispatcher so it is never in a gunicorn request worker.
- **Steering routes.** `POST /exploration/{run_id}/steer` and `GET /exploration/{run_id}/events` (SSE). A run_id unknown to the current process returns **404, never 200**. This is Genesis's first real use of `adk.reasoning_session`; the per-worker dicts at routers/chat.py:98-100 are not extended.
- **Sandbox.** It is a guard only. Customer-supplied environments are refused until a persistent isolated REPL (awrepl in a no-network container) exists.

### evalharness

```
adk eval arc --env-dir <path to the public games' environment_files>   (or $ADK_ARC_ENV_DIR)
             [--games all|ls20,...] [--seeds 1] [--tier reasoning | --backend P]
             [--out rows.jsonl] [--baseline eval/baseline.json] [--learn-scope S] [--curriculum]
             [--max-over-seeds] [--self-test]
```

- **Where it lives.** It is added to the `adk eval` subparser (cli.py:14716-14740), with dispatch at 16064-16080. It calls `evalharness/arc_agi3/suite.py`.
- **One run dir per game by default.** The headline is cold, one run per game, which is the competition rule. `--max-over-seeds` is reported separately and labelled.
- **Rows.** Written in local_loop's schema: `game, policy="solve", seed, levels, level_actions[], baseline[], local_rhae, wall_s, actions, llm_calls, llm_s, abandon_reason=finish_reason`. Memory metrics are added: `hyps_active, hyps_refuted, replay_checks, brier, skills_added, warm_start_hits`.
- **Scoring.** Uses the ported `rhae.game_rhae` / `score_rows`. The `arc_agi` scorecard is never used (it measured 1.0 against the official 1.15).
- **`--curriculum`.** Runs the games in sequence under one `learn_scope` and reports RHAE on game k **warm vs cold**. This is the only place the claim that shared memory helps may be made.
- **Exit codes.** 0 if mean RHAE > 0.00219 (the random floor). 1 if it is at or below the floor. 2 if there are zero rows, the engine is missing, or the backend is dead on every game.
- **`--self-test`.** Runs rhae's break arm (it must exit 1) plus one toy episode with a scripted model. CI needs neither the games nor an LLM.

---

## 5. Migration from h30 (the prototype checkout, `$ADK_H30_DIR` in tests; branch `h30-repl-agent`, package `agent/repl/`)

h30 is **read-only**; nothing in the worktree is ever edited.

1. **Freeze.** When the h30 owner calls `core/` stable, record the sha. Copy `agent/repl/core/{interfaces,loop,intent,sase,prism,learning,memory,sandbox}.py` into `solve/_vendor/`. Only relative imports and the provenance header change.
2. **Seams only.** Add three keyword-only arguments, each defaulting to a no-op: `sink`, `governor`, and Sandbox `cancel_check`. They are called at about 5 sites: `_log`, the head of `_turn`, `step`, before each LLM call, and `Sandbox._check`.
   - Three behaviour changes are allowed, each behind a flag that defaults to the h30 behaviour in parity mode: the injected PRISM seed and clock, the applied overconfidence adjustment, and SkillLibrary persistence through `Memory`.
3. **Contracts.**
   - `_types.Environment` has the same shape as h30's, so h30 environments satisfy it unchanged.
   - h30's sync `LLM` protocol is dropped from the public API and lives on only as `_bridge._SyncModel`.
   - The h30 agent keeps working through a ~15-line `ModelBackend` shim in **its own repo** that wraps its `llm_client.LLMClient`. awdk never imports the h30 client.
4. **Domain split.** `arc_env.py`, the RESET accounting, `arc_knowledge.md` and the prompt primers go to `evalharness/arc_agi3/env_arc.py` as optional hook methods. `rule_wm.py` stays in h30 until it earns a place in `_mcts.py`.
5. **Publishability.** `check_adk_publishable.py` ADK001/003/005/007 must pass on `solve/`. The PRISM personas are renamed to publishable strategy names (hypothesis-elimination, systematic-probe, goal-regression, analogy-to-skill, simplify-and-isolate, contrarian), and the strategy text lives in `_strategies.py`, outside `_vendor/`, so the scrub does not break the vendored hash. Internal hostnames and model names are stripped from vendored prompts, and the manifest records each such edit.
6. **Drift.** `test_reasoning_solve_parity.py` does two things: it re-runs the h30 unit tests (copied as fixtures) with `sink=governor=None`, and it hashes `_vendor/*.py` against the manifest. A hand-edit without a re-vendor fails the test. The only way to sync is to re-vendor at a new sha.
7. **Parity eval.** Run the same 3 games with the same seed through wt-h30 and through `adk eval arc`. With a scripted-replay model, `level_actions` must match exactly. With the live model, RHAE must be within noise. Only after that does awdk become the source of truth; the kaggle repo can later vendor the awdk wheel back.
8. **Commits.**
   - `awgit lease acquire` on `awdk/adk/reasoning/solve`, `awdk/adk/reasoning/__init__.py`, `awdk/adk/commands/solve.py`, `awdk/adk/cli.py`, `awdk/docs/CLI-REFERENCE.md`, `awdk/pyproject.toml` and the tests.
   - `awgit blob-commit --base <develop HEAD>` with an explicit path list filtered with `test -f`.
   - `cli.py` is a hot shared file: commit only the solve hunk.

---

## 6. First slice (hermetic: no fleet, no ARC, and no numpy on the core path)

**Build.**

- `solve/{__init__,_types,_bridge,_control,_events,_run,_adapters}.py`
- `_vendor/*` at the pinned sha, with the 3 seams
- `envs/toy.py`
- the lazy `__getattr__` in `reasoning/__init__.py`

`cli.py` / CLI-REFERENCE work waits until slice 2, except that (h) below needs a tiny
`cmd_solve`. If `cmd_solve` is deferred, (h) is checked by calling `cmd_solve` directly
and not through the parser.

**Tests.** `awdk/tests/test_reasoning_solve.py` uses a `ScriptedModel(ModelBackend)`, the
FakeModel pattern from tests/test_predict_loop.py:12-30, extended to return a scripted
sequence of SASE replies and to record every message list it received. Each check below
can fail:

| # | test | assertion |
|---|---|---|
| a | `test_solves_toy` | On Counter1D, turn 1 calls `hypothesize(lambda s,a: s+1 if a[0]==1 else s-1)` and then `act(1)`×5. Expect `won`, `finish_reason=="won"`, `actions==5`, exactly one active Hypothesis with `support>=3`, `exit_code==0`. |
| b | `test_replay_refutes_over_all_history` | A wrong rule `s+1` for every action passes after act(1) and is `refuted` after act(2). Re-proposing it after another act(1) leaves it refuted (checked over ALL history, not only the newest transition). |
| c | `test_abstainer_not_admitted` | A hypothesis that returns `None` everywhere stays `pending` because of `min_coverage`. |
| d | `test_warm_start_is_a_prior` | A FileMemory seeded with one correct and one poisoned hypothesis under the same domain: the correct one goes `active` only after it re-passes replay on the new run; the poisoned one is refuted at its first contradiction. |
| e | `test_events_and_steering` | Events arrive in order start < turn < finish. `run.steer("try action 2")` issued after the first turn event appears in `ScriptedModel.calls[1][-1]["content"]`, and a `steer` event is emitted (this proves the thread-safe bridge). |
| f | `test_budget` | With `Budget(max_actions=5)` and a script that loops `act(1)`, expect `finish_reason=="budget:actions"`, `actions<=5`, `exit_code==1`. |
| g | `test_cancel_and_sandbox_guard` | A cell with `while True: pass` and `turn_s=0.5` costs exactly one turn and the loop continues. With `turn_s=60`, `run.cancel()` after 0.5 s makes `join()` return within 2 s with `"cancelled"`, and the worker thread is gone (`threading.enumerate`). |
| h | `test_dead_backend_is_not_success` | `generate` always raises: expect `finish_reason=="llm_error"`, `won is False`, `exit_code==2` (and `cmd_solve` returns 2 with a monkeypatched router). |
| i | `test_public_api_frozen` | `set(__all__)` equals the 12 names. An AST scan finds no module under `solve/` that imports `adk.llm`, `adk.packs`, `adk.evalharness`, `adk.memory`, anything named `arc`, or `numpy` at top level. |
| j | `test_solveloop_is_agentloop` | `Agent(name="t", model=ScriptedModel(...), loop=SolveLoop(Counter1D()))` gives `AgentResult.finish_reason=="won"`. |
| k | `test_nested_loop` | `solve()` runs both under `asyncio.run` and from inside an already-running loop (pytest-asyncio) without deadlocking. |

**Break arms.**

- `SOLVE_REPLAY_BREAK=1` makes `replay_check` return pass. Then (b) and (d) **must fail**.
- `SOLVE_SELFTEST_BREAK=1` makes `Governor.check` a no-op. Then (f) and (g) **must fail**.

Both arms are exercised by a meta-test that runs pytest in a subprocess and asserts
exit ≠ 0.

**Commands.**

```bash
cd awdk   # from the repository root
python -m pytest tests/test_reasoning_solve.py tests/test_reasoning_solve_parity.py -x --tb=short -p no:cov
ruff check adk/reasoning/solve
py -3.10 -m pytest tests/test_reasoning_solve.py -x      # CI gates on 3.10
python -c "import adk.reasoning"                         # in a venv WITHOUT numpy
```

**Slice 2 (the first real eval).**

- `evalharness/arc_agi3` with the rhae self-test (the break arm must exit 1) and the ported `test_reset_accounting`.
- `cli.py` `solve` + `eval arc` + regenerated CLI-REFERENCE.
- Acceptance: `adk eval arc --games ls20 --runs 1 --tier reasoning` writes a row with non-null `local_rhae` and `llm_calls > 0` (proving the MicroScheduler path is real), and exits 0 or 1, never 2.
- Then all 25 games, one run each, against the 0.00219 floor.

**Slice 3.** `_mcts.py` `plan()` tool; `--curriculum` warm-vs-cold; TypedMemoryExporter; `aither solve` TS alias.

**Slice 4.** Genesis `GatewayModelBackend`, the tool, and the `/exploration` driver plus steer/events routes. Verify after the rebuild with `podman exec aitheros-genesis curl -sk https://localhost:8001/exploration/<id>/events`.

---

## 7. Open risks

1. **Sync core in an async API.** `_bridge` schedules `generate` onto the caller's loop. If that loop is blocked (for example a sync wrapper doing `asyncio.run` inside a running loop), the run deadlocks. Mitigations: `future.result(timeout=llm_timeout_s)` and test (k). An in-flight HTTP call cannot be interrupted, so worst-case cancel latency is `llm_timeout_s`. `cancel()` returns immediately and marks the result while the thread drains.
2. **settrace collisions.** The sandbox clock uses per-thread `sys.settrace`. pytest-cov and debuggers fight it, so tests run with `-p no:cov`. Coverage for `_vendor` comes from the parity run.
3. **The sandbox is a guard, not a boundary.** It is an in-process exec with restricted builtins. That is acceptable for the owner's model on eval games and NOT for customer environments in Genesis. `DockerCellExecutor` keeps no state between cells, so it cannot host the REPL. An isolated persistent REPL (awrepl plus a container) is a separate change and must not widen the API.
4. **Replay cost is O(H·T).** Mitigations: incremental recheck of live hypotheses, with full replay only on admission or import, a sha-keyed verdict cache, and the per-hypothesis `turn_s` cap. Measure steps/sec in slice 1.
5. **The strongest model is not selected by effort today.** ReasoningModelSelector ignores `reasoning_model` (ReasoningModelSelector.py:130-166). The tiers.py defaults are placeholders, and no preset names the strongest :8150 pool model. The CLI prints the resolved model every run. A MicroScheduler 502 can be a context overflow, so keep prompt tokens below the served context.
6. **Publishability versus verbatim vendoring.** Scrubbing prompts breaks a pure hash match. The manifest records the scrubbed hash and the parity tests pin behaviour, so the drift test checks the scrubbed bytes.
7. **Steering is per process.** ReasoningSession state lives in one process. Genesis must pin runs to the owning worker and 404 an unknown run_id. A durable queue comes later.
8. **Token accounting.** Some backends return empty `usage`. The Governor falls back to `est_tokens` and labels the figure as estimated.
9. **Scoring honesty.** rhae.py takes the max over runs, but competition mode is one run per game. The headline is one run per game with a per-game run dir. Max-over-seeds is labelled. Warm-start benefit is claimed only from `--curriculum` deltas.
10. **h30 is still moving.** The copy is pinned to an sha. Re-vendoring is the only way to sync, and the parity and drift tests fail loudly on divergence.
11. **CI friction.** A new CLI verb fails debt-invariants until CLI-REFERENCE is regenerated. awsh TS `npm test` has no CI step, so those tests are local-only until one is added.
12. **Shared worktree.** Every commit is an awgit blob-commit of explicit paths. The `cli.py` hunk goes alone.
