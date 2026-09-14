# Coordination Skill

Orchestrate complex multi-step workflows by delegating tasks to appropriate tools
and managing dependencies between steps.

## Pattern: Request → Analyze → Delegate → Verify → Remember

When faced with a coordination challenge:

1. **Parse the request** — What is the end goal? What constraints/dependencies exist?
2. **Check memory** — Have I handled similar workflows before? What worked?
3. **Plan steps** — Break into atomic operations. Route each to the right tool.
4. **Delegate** — Call file_io, shell, web tools in sequence. Capture results.
5. **Verify** — Check that each step succeeded. Handle failures gracefully.
6. **Remember** — Store the workflow outcome and learned patterns for next time.

## Example: ETL Workflow

- **Extract**: `web_fetch(url)` or `file_read(path)`
- **Transform**: `shell(python script.py < input)` or inline processing
- **Load**: `file_write(output_path, result)`
- **Remember**: `graph.remember("ETL workflow", "succeeded on", datetime, metadata={url, script, outcome})`

## Key Heuristics

- Always assume downstream agents need explicit context (don't skip explanation steps).
- When a tool fails, escalate rather than retry infinitely.
- Document what you learned for the next agent who handles a similar task.
