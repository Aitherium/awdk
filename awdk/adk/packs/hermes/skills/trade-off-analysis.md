# Skill: Trade-off Analysis

A procedure for comparing competing technical approaches and making defensible
architecture decisions under constraint.

## When to use

When choosing between competing solutions: "Redis vs Memcached?", "Monolith or
microservices?", "SQL or NoSQL?", "Roll our own or use a SaaS?". Use this to
build a rational comparison and avoid analysis paralysis.

## Procedure

1. **List the options.** Name each approach clearly and briefly. Avoid false
   dichotomies; often there's a third way.

2. **Define the criteria.** What matters most to your team? Common ones:
   - Cost (hardware, software licensing, development, ops)
   - Latency and throughput
   - Scalability ceiling (when does it break?)
   - Reliability (availability, recovery time)
   - Operational complexity (monitoring, alerting, on-call incidents)
   - Team expertise (how fast can we ship, how risky is it?)
   - Time to market
   - Flexibility (how hard to change later?)

3. **Weight the criteria.** Assign relative weights: 1 = nice-to-have, 5 = critical.
   Be honest. If cost doesn't matter, say so. If latency does, say it first.

4. **Score each option.** For each criterion, rate each option on a scale (1–5 or
   1–10). Document your reasoning; do not hand-wave.

5. **Calculate.** Multiply each score by its weight and sum. The highest total
   wins — if it's close, there's a real tie to break (often by gut feel or team
   risk tolerance).

6. **Sensitivity analysis.** What if the top-weight criterion changes? What if
   we're wrong about scalability? Does the recommendation still hold?

7. **Document the matrix.** Include all options and the reasoning for close calls.

## Quality bar

- Scores must be defensible — they are future arguments for why this choice was
  made.
- Acknowledge unknowns: "We haven't tested this at scale, so our latency estimate
  is a guess."
- Include the decision date and the assumptions that were current then.
- If a new constraint arrives later, you may need to revisit.
