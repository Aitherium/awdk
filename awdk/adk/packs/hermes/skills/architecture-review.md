# Skill: Architecture Review

A systematic procedure for evaluating systems and designs for scalability,
maintainability, and alignment with goals.

## When to use

When you need to review or design a system, architecture, service topology,
database schema, API surface, or major refactoring effort. Use this skill to
spot trade-offs, hidden costs, and long-term risks early.

## Procedure

1. **Clarify constraints.** Ask about:
   - Scale (QPS, data volume, geography, team size)
   - Latency, cost, compliance, reliability targets
   - Known pain points or tech debt
   - Team's expertise and infrastructure maturity

2. **Read the current design.** If code/config exists:
   - Map the service topology and data flow
   - Identify the critical path (where latency matters)
   - Spot points of coupling and single-point-of-failure
   - Note non-obvious operational burden (backups, monitoring, incident response)

3. **Document assumptions.** State what you're optimizing for and what you're
   accepting as a trade-off.

4. **Propose alternatives.** For each major component or decision, sketch 2–3
   approaches. For each:
   - Cost (infra, compute, team effort)
   - Scalability ceiling (when does it break?)
   - Operational burden (monitoring, incident response, on-call)
   - Learning curve (how fast can a new hire ship?)

5. **Make the call.** Recommend one design and explain why the others don't fit
   *your* constraints.

6. **Document the decision.** Write an ADR or design doc so future decisions
   reference this one.

## Quality bar

- Never trade off maintainability for a short-term speed gain.
- Boring, proven patterns beat novel ones — earn the right to be clever.
- If an approach makes the system easier to change later, weight that heavily.
- Scalability is not binary; estimate the real ceiling where you'll hit pain.
