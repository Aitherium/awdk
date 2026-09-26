# Context dictates what behaviour is permissible

Status: design and first slice (ARC solver). Owner principle, 2026-09-26: *context
dictates what behaviour is permissible.* This document defines that principle once and
shows how each layer adopts it: the ARC reasoning loop, awdk's agent loop, and Genesis
agent tools.

The first slice is on branch `feat/adk-reasoning-context-permission`, cut from
`feat/adk-reasoning-env-arc-eval`:

- `awdk/adk/reasoning/solve/context.py` holds the record, `permits()` and the invariants.
- `awdk/adk/reasoning/solve/_context_gate.py` wires them into the loop.
- `awdk/tests/test_reasoning_solve_context.py` tests them.

Line numbers below refer to that branch where the file is new there. Everywhere else they
refer to `origin/develop` on 2026-09-26. `adk/...` paths are in this package. `lib/...`,
`services/...` and `apps/...` paths are in the platform tree.

## 1. The problem, as the code shows it today

Permission in the stack today is either static (a role or an allowlist) or implicit (a
flag that was true once and stays true). Neither follows what the agent knows *now*.

- **ARC solver.** `plan()` simulates every hypothesis the store calls `verified`
  (`_vendor/loop.py:878`, `_plan`; `PlanningLoop.plan` in `_daydream.py:135`).
  "Verified" means the rule replayed over ALL history. Nothing checks whether it holds
  on the *current level*. On the next level, a rule verified on the last one is still
  treated as a simulator, and a goal carried from the last level is still pursued.
  `_rule_first_on_verified` (`loop.py:753`) switches strategy the moment anything
  verifies, anywhere. When a rule is refuted (`loop.py:651`, `hyps.recheck`), the rule
  itself is removed. But nothing records that the *permission it granted* is gone. The
  prototype `wt-h31/agent/repl/core/` has no level-scoped verification, no audit of
  carried beliefs, and no gate on plan(). The only per-level state there is the action
  counter `level_start_action` (`loop.py:180/306`).
- **awdk.** Tool calls pass through two layers. `LoopGuard.check`
  (`adk/agent.py:2165`) is a repetition breaker. `LoopPolicy` (`adk/agent.py:2288-2290`,
  `adk/loop_policy.py:42`) only nudges. Neither consults what the agent has established.
  `ActionGate` (`adk/safety.py:249`) maps an action to a fixed `PermissionTier`
  (`safety.py:200`), and `CapabilityContext` (`adk/core/capability.py:48`) is a flat set
  of grants. Both are static for the whole run.
- **Genesis.** A caller's scope is derived from the authenticated request:
  `build_caller_context` (`lib/core/AitherTenant.py:1878`) produces
  `CallerContext` (`:1634`) with `can_*` flags. RBAC answers `has_permission`
  (`lib/security/AitherRBAC.py:282`). Agent-vs-principal permissions are intersected by
  `effective_check` (`lib/security/agent_principal.py:125`). Tool allowlists live in
  `agent_registry.get_allowed_tools` (`lib/agent_platform/agent_registry.py:901`) and
  are enforced at `tool_manifest.py:323`. These are sound for WHO may act. None of them
  records owner authorisations for a CLASS of action. Those authorisations arrive as
  ad-hoc prompt text ("the owner said it's fine"), which is exactly the reframing channel
  that must not grant anything.

## 2. The Context record

A context is what one agent currently knows, with the provenance of each piece.
Source: `context.py:189`, class `Context`.

```text
Context
  scope:   str                  # e.g. "ls20/level:3", "session:abc/task:42"
  intent:  str                  # what the agent is trying to do (the intent classifier's label)
  domain:  {str: Any}           # domain state: level, transitions on this level, prior program...
  facts:   {key: Fact}
  log:     [ {op, key, how, refs, scope} ]    # every widen / infer / narrow / rescope

Fact                            # context.py:62
  key, value
  provenance: verified | owner | inferred
  scope:      the scope it was established in
  evidence:   (Evidence(kind, ref, detail), ...)
  status:     held | carried | contradicted
  since:      domain clock (e.g. transition index) of the last status change
  bears_permission = status == held and provenance in (verified, owner)
```

Provenance and the evidence each one needs:

| provenance | how it is established | evidence kinds accepted |
|---|---|---|
| `verified` | the harness measured it | `replay`, `test`, `observation`, `audit` |
| `owner` | an owner authorisation on record | an `OwnerRecord` the host's `OwnerAuthority` verifies (HMAC; `context.py:90`) |
| `inferred` | the model or a heuristic said so | anything; it is **kept for the record and never bears permission** |

Status:

- `held` bears permission in the current scope.
- `carried` was held in an earlier scope and must re-earn evidence here.
- `contradicted` was broken by evidence. It can only be widened again with evidence
  NEWER than the contradiction (`since`).

## 3. The permission function

There is ONE function. Source: `context.py:287`.

```python
permits(ctx: Context, req: Request, *, invariants: Invariants | None,
        policy: Mapping[kind, (ctx, req) -> Decision | None] | None,
        default: bool = True) -> Decision

Request(kind, name="", args={}, evidence=(), owner_record=None)
    kind: act | plan | goal | strategy | model_route | episode | report | tool | destructive
Decision(allowed, reason, layer: invariant | context | default, rule)
```

It evaluates in a fixed order:

1. `invariants.check(req)`. A refusal here is final. The check reads ONLY `req` and the
   frozen invariants, never `ctx`.
2. `policy[req.kind](ctx, req)`. This is the domain's rule set; `None` means no opinion.
3. `default`. This is allow for the ARC slice, where acting is how evidence is gathered.
   It is deny for tool kinds a domain has not classified.

Each layer supplies its own **policy table**. The function, the record and the invariant
layer are shared.

## 4. Widening and narrowing

| operation | trigger | evidence required | code |
|---|---|---|---|
| widen (verified) | the harness measured the fact in the current scope | at least one `replay`/`test`/`observation`/`audit` item | `Context.widen`, `context.py:203` |
| widen (owner) | an owner record the host can verify | a signed `OwnerRecord` whose `action_class` matches; not expired | same |
| infer | the model says something | none; stored as `inferred`, never bears permission, never overwrites a verified or owner fact | `Context.infer`, `:228` |
| narrow | a contradiction: a refutation, a failed test, an audit failure | the contradicting evidence item, with the clock at which it happened | `Context.narrow`, `:239` |
| rescope | the scope changes (new level, new task, new repo) | none; every held fact becomes `carried` | `Context.rescope`, `:250` |
| re-widen after narrow | new evidence | evidence dated after `since`; stale evidence raises `PermissionError` | `widen` |

Narrowing is automatic. The gate derives it from the domain's own contradiction signal
(ARC: `HypothesisStore.refuted`; awdk: a failing `run_tests` after an edit; Genesis: a
revoked card or an expired token). The model is never asked whether it was wrong.

## 5. The invariant layer

`Invariants` is defined at `context.py:148`. It is a frozen dataclass that the HOST
builds from its own configuration:

| invariant | refuses |
|---|---|
| `local_only` + `local_models` | any `model_route` not in the local set. Checked before every model call AND against the model that actually served the reply. |
| `reserved_seeds` + `purpose` | an `episode` whose game id or seed is reserved for evaluation, unless the host's purpose is `eval` |
| no fabrication | a `report` whose evidence is missing or not measured |
| `destructive` + `owner_verifier` | a destructive request without an owner record for exactly that action class |
| security/auth boundaries | not in this class. Enforced by `CallerContext`/RBAC at the service boundary (section 7), which this layer defers to and never widens |

### How it stays outside the model's reach

1. **It never reads context.** `Invariants.check` sees the request and its own frozen
   fields. No fact of any provenance changes its answer. That includes an owner fact
   established inside the run. The owner changes an invariant by changing the host's
   configuration, never by saying so to the agent.
2. **The model has no write path to it.** The model reaches context only through `note()`
   and its replies, and both are recorded via `infer` as `inferred`. `widen` refuses
   `inferred` and refuses `owner` without a record the host can verify. The HMAC key is
   held by `OwnerAuthority`, and only the bound `verify` is handed down.
3. **It is enforced at the effect, not at the prompt.** The route check wraps `llm.chat`
   (`_context_gate.py:426`). It raises `InvariantViolation` with `fatal = True`, which the
   core already honours as "never measure the wrong model" (`loop.py:569`). The check
   runs on the route requested AND on the model that served the reply. A router that
   obeyed a reframing is caught on the reply.
4. **Honest limit.** The vendored sandbox is "a GUARD, not a security boundary"
   (`_vendor/sandbox.py:4`). Model code that walks `__self__`/`__closure__` could reach
   the loop object. So the in-process check is the first line, and the authoritative
   enforcement of each invariant belongs at a process boundary the model cannot touch:
   - local-only belongs in MicroScheduler's route policy. Today it has only a global
     `cloud_mode` (`services/orchestration/AitherMicroScheduler.py:271-278`,
     `:8683`) and no per-caller rule. Adoption step G1 below adds one.
   - reserved seeds belong in the eval harness or env server.
   - destructive actions belong in Genesis RBAC.

   The in-process layer and the boundary layer read the SAME `Invariants` config.

### Test: a reframing attempt cannot unlock an invariant

Source: `tests/test_reasoning_solve_context.py`.

- `test_a_reframing_reply_does_not_unlock_an_invariant`
  - A scripted model replies "the owner allowed cloud models for this run, so switch to
    cloud-opus" and calls `note(...)` with the same claim.
  - A fake router honours the claim on the second call and serves `cloud-opus`.
  - Result: the second reply is refused on its served model, the loop goes `fatal`
    (`local_only`), and the cloud-served code never runs (`actions_model == 1`). Both
    claims are stored as `inferred` facts that do not bear permission, and the
    `Invariants` object is unchanged.
- `test_no_context_of_any_provenance_unlocks_an_invariant`
  - The model cannot promote its own claim, and a forged owner record is refused.
  - Even a GENUINE signed owner record held as context leaves `permits(model_route
    cloud)` refused at `layer == "invariant"`.

## 6. How each layer adopts it

### 6.1 ARC solver (first slice, built)

`install_context_gate` (`_context_gate.py:470`) is called from `build_core_loop` when
`LoopConfig.context != "off"` or `LoopConfig.invariants` is set. It hooks the attribute
seams the core already calls through, so the pinned vendored files are unedited:

- `loop.step`
- `loop.daydream`
- `loop.prism.rotate`
- `loop._turn`
- `loop.llm.chat`
- the namespace's `plan` and `note`

The policy (`ARC_POLICY`, `_context_gate.py:58` onward):

- **plan() only while a rule is replay-verified in this level's context.** A predict rule
  holds `rule:<name>` once `replay_check` from the level's first transition (or from its
  last contradiction, whichever is later) shows at least `context_level_support` cell
  claims (default 2) with 0 wrong. Verification on earlier levels is `carried`. The
  refusal says which rules are carried, so the model knows to act and re-verify.
  `daydream()` is gated the same way, so `DREAM` is `None` when planning is not
  permitted. A step the planner takes (`source == "plan"`) is re-checked.
- **Goal pursuit only while the goal is consistent.** `plan(goal=g)` needs `goal:<g>`
  held: a registered goal hypothesis that passed this level's audit and was not refuted
  since. An unregistered goal callable has no provenance and is refused with
  "hypothesize(goal_fn, 'goal') first". Goal checks run before rule checks so the reason
  names the real cause.
- **Permission withdrawn on contradiction.** Each refresh reads
  `HypothesisStore.refuted`. Every refuted rule or goal NARROWS its fact at the current
  transition clock, is counted in `revocations`, and is logged as `permission_revoked`.
  A different rule that explains the contradicting transition earns the permission back.
  The refuted one cannot come back, because the store's fingerprint blocks it.
- **Contract audit at each level start** (`on_level_start`, `_context_gate.py:204`). It
  runs on the level-up step and at the first check of a run, and does the following:
  1. Rescopes to `episode/level:N`, so every held rule and goal becomes `carried`.
  2. Tests each carried belief on the level's FIRST frame. A rule must still return a
     well-formed prediction (right shape, not raising on every action). A goal must NOT
     already be satisfied (it would have been won). A failure narrows the fact.
  3. Widens a passing goal as `verified` with `audit` evidence.
  4. Leaves a passing rule `carried` until it replays on this level.

  The report is logged as `contract_audit` and kept in `summary()["context"]["audits"]`.
- **Strategy switches.** `analogy` needs a previous level's program. The auto
  strategies and rule_first/goal_first are always permitted, because they are how
  evidence is found. A refused rotation is exhausted and the next best strategy is
  taken. The active strategy is re-checked at every turn head.

Modes:

- `off` enforces invariants only.
- `shadow` records `shadow_refused` counts and lets everything through. This is the
  measurement arm.
- `enforce` refuses.

The default is `off` until an A/B on the host's environments shows `enforce` does not
cost levels. The same promotion rule applies to `grounded`.

Measurement plan (not run in this change; the pool is busy):

1. Run a `shadow` arm over the dev games. It reports how often `plan()` ran on
   carried-only rules and how often those plans deviated.
2. Run `enforce` vs `off` on the same seeds and compare RHAE and deviation rate. Never
   use reserved seeds; the invariant refuses them unless `purpose="eval"`.

### 6.2 awdk loop_policy

`LoopPolicy` (`adk/loop_policy.py:42`) already keeps a step ledger (`record`, `:62`) and
knows the evidence events: `file_read` of a path, a landed `file_edit`, a green or red
`run_tests`. Adoption:

1. **A1: the ledger becomes the context.** `record()` widens facts with `observation`
   evidence:
   - `read:<path>`
   - `edit_landed:<path>`
   - `tests_green:<target>` (from a `run_tests` whose head says passed and not failed)

   A red run after an edit narrows `tests_green`.
2. **A2: nudges become policy rules in shadow.**
   - `file_edit` on a path without `read:<path>` held is the read-before-edit rule.
   - "claim done" without `tests_green` held is the verify rule.
   - A regression run is required once `tests_green:<target>` is held.

   Each rule is recorded as `shadow_refused` next to the nudge it replaces, so the
   SWE-bench-Live harness that produced the four measured nudges can compare them.
3. **A3: enforce at dispatch.** `permits()` runs beside `loop_guard.check`
   (`adk/agent.py:2165`) before `self._tools.execute` (`:2240`). A refusal returns the
   builtin error envelope (`loop_policy.result_ok`, `:33`) carrying the reason, the same
   channel the loop guard uses.
4. **Plug-ins, not replacements.**
   - `CapabilityContext` (`adk/core/capability.py:48`) stays the static grant set. The
     policy rule for `tool` asks it first, and context can only NARROW what it grants.
   - `ActionGate` tiers (`adk/safety.py:249`, `ACTION_TIERS`) map tool names to request
     kinds. `HUMAN_REQUIRED` becomes the `destructive` invariant.
   - `tool_guards.apply_guards` (`adk/tool_guards.py:81`) stays a per-tool pre-check.
   - Steering text (`reasoning_session.py:149/166`) enters as `inferred`, exactly like
     model text.

### 6.3 Genesis agent tools

1. **G1: owner authorisations become records, not prompts.** A decision card answered by
   the owner (`awdk/adk/decisions/store.py:560`, `DecisionCard`; `answer()` at `:1095`;
   MCP `decisions_answer` at `apps/awnode/tools/mcp/mcp_decisions.py:218`) is
   minted into an `OwnerRecord(action_class, card_id, answered_by, expires_at)` signed
   by Genesis.
   - The card has no `answered_by` field today, only `answered_via` (`store.py:580`).
     G1 adds the principal id from the authenticated answering caller.
   - Action classes are the existing tier names (`deploy.production`, `data.delete`,
     `secrets.modify`, `fleet.restart` ...).
   - An agent tool call in a destructive class carries the record. The invariant layer
     verifies it for exactly that class, and an expired or mismatched record is refused.

   "The owner said it's fine" in a prompt is `inferred` and grants nothing.
2. **G2: CallerContext is the outer bound.** `build_caller_context`
   (`AitherTenant.py:1878`) and `effective_check` (`agent_principal.py:125`) decide what
   this principal and this agent MAY ever do. The agent's Context decides what it may do
   NOW, inside that bound. Context never widens past `CallerContext`, the RBAC answer, or
   the tool allowlist (`agent_registry.py:901`, enforced at `tool_manifest.py:323`).
   Scope keys off the authenticated caller, never payload fields (the platform's
   secrets-access rule).
3. **G3: MicroScheduler reads the same invariants.** The local-only invariant becomes a
   per-caller route rule next to the global `cloud_mode`. This makes it the boundary
   enforcement described in section 5, and makes the in-process ARC check a second line.
4. **G4: classifier rules become policy inputs.** The intent classifier that gates tool
   selection (`lib/orchestration/conversational_tools.py:1730`) sets
   `ctx.intent`. A policy rule can require an intent (e.g. no `git.push` under intent
   `explain`). The classifier still does not grant anything.

## 7. Where existing mechanisms plug in

| mechanism | role under this design |
|---|---|
| capability tokens (`TenantScopedToken.mint_token` `:316`, `verify_tenant_token` `:481`; `ArtifactToken.verify_artifact_token` `:173`) | outer bound on WHO; a verified token is `observation` evidence for a scope fact; never widened by context |
| `CallerContext` / RBAC `has_permission` / `require_permission` (`AitherRBACMiddleware.py:386`) | outer bound; the security/auth invariant is "context cannot exceed these" |
| agent tool allowlists | outer bound on WHICH tools; context policy runs only on allowed tools |
| awdk `CapabilityContext`, `ActionGate`, `tool_guards` | static grants, tiers and pre-checks; context can only narrow them |
| `LoopGuard` / `LoopPolicy` | the ledger that feeds context evidence; nudges become policy rules |
| ARC `HypothesisStore` (`replay_check` `memory.py:459`, `recheck` `:645`, `refuted`) | the verified-evidence source and the contradiction signal |
| decision cards / awdecide (`~/.aither/decisions`) | the ONLY source of `owner` provenance, as signed records |
| memory (awm, Strata, `FileMemory`) | a remembered fact comes back `carried`; it must re-earn evidence in the new scope before it bears permission |

## 8. Adoption order

1. **ARC slice.** This change. Default `off`, then run the shadow arm and the A/B to
   measure it.
2. **Promote `context.py`** to `adk/context.py` once a second consumer lands. Keep a
   re-export in `adk.reasoning.solve.context`.
3. **awdk A1-A2.** The LoopPolicy ledger feeds context, and the nudge rules run in
   shadow. Compare them on SWE-bench-Live with the harness that measured the nudges.
4. **Genesis G1.** Owner records from decision cards. This is the smallest change that
   removes the reframing channel for destructive classes.
5. **Genesis G3.** The MicroScheduler per-caller local-only route rule, which is the
   boundary enforcement.
6. **awdk A3, Genesis G2 and G4.** Enforce at dispatch, bounded by `CallerContext`.
