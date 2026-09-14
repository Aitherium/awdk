"""
AitherShell Script Runner
=========================

Runs .aither script files — sequential prompts with variables,
control flow, and mode switching. Like shell scripts but for AI.

Script format:
    # Comments start with #
    @will private-mode          # Switch will
    @effort 5                   # Set effort level
    @model aither-orchestrator  # Set model
    @set NAME=value             # Set variable

    What is {{NAME}}?           # Variable substitution
    Explain {{$PREV}}           # $PREV = last response

    @if {{$PREV}} contains error
        Fix the error: {{$PREV}}
    @end

    @for item in alpha beta gamma
        Describe {{item}} briefly
    @end

Usage:
    aither run script.aither
    aither run deploy-check.aither --var ENV=prod
    cat prompts.txt | aither run -     # stdin as script
"""

import re
import sys
import json
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field


@dataclass
class ScriptContext:
    """Runtime context for script execution."""
    variables: Dict[str, str] = field(default_factory=dict)
    prev_response: str = ""
    effort: Optional[int] = None
    model: Optional[str] = None
    will: Optional[str] = None
    system_prompt: Optional[str] = None
    output_mode: str = "print"  # print | json | quiet
    results: List[Dict[str, Any]] = field(default_factory=list)


def parse_script(text: str) -> List[dict]:
    """Parse a .aither script into a list of instructions."""
    instructions = []
    lines = text.strip().split("\n")
    i = 0

    while i < len(lines):
        line = lines[i].strip()
        i += 1

        # Skip empty lines and comments
        if not line or line.startswith("#"):
            continue

        # Directives
        if line.startswith("@"):
            directive = _parse_directive(line, lines, i)
            if directive:
                instructions.append(directive)
                if directive.get("type") == "block":
                    # Skip lines consumed by block
                    i = directive["end_line"]
            continue

        # Regular prompt line
        instructions.append({"type": "prompt", "text": line})

    return instructions


def _parse_directive(line: str, lines: List[str], current_idx: int) -> Optional[dict]:
    """Parse an @ directive."""
    parts = line.split(None, 1)
    cmd = parts[0].lower()
    arg = parts[1] if len(parts) > 1 else ""

    if cmd == "@set":
        if "=" in arg:
            key, val = arg.split("=", 1)
            return {"type": "set", "key": key.strip(), "value": val.strip()}
    elif cmd == "@will":
        return {"type": "will", "value": arg.strip()}
    elif cmd == "@effort":
        return {"type": "effort", "value": int(arg.strip())}
    elif cmd == "@model":
        return {"type": "model", "value": arg.strip()}
    elif cmd == "@system":
        return {"type": "system_prompt", "value": arg.strip()}
    elif cmd == "@print":
        return {"type": "output_mode", "value": "print"}
    elif cmd == "@json":
        return {"type": "output_mode", "value": "json"}
    elif cmd == "@quiet":
        return {"type": "output_mode", "value": "quiet"}
    elif cmd == "@sleep":
        return {"type": "sleep", "value": float(arg.strip())}
    elif cmd == "@for":
        # @for VAR in val1 val2 val3
        m = re.match(r"(\w+)\s+in\s+(.+)", arg)
        if m:
            var_name = m.group(1)
            values = m.group(2).strip().split()
            # Collect body until @end
            body_lines = []
            end_idx = current_idx
            while end_idx < len(lines):
                if lines[end_idx].strip().lower() == "@end":
                    end_idx += 1
                    break
                body_lines.append(lines[end_idx])
                end_idx += 1
            return {
                "type": "block",
                "block_type": "for",
                "var": var_name,
                "values": values,
                "body": "\n".join(body_lines),
                "end_line": end_idx,
            }
    elif cmd == "@if":
        # @if {{$PREV}} contains <text>
        condition = arg.strip()
        body_lines = []
        end_idx = current_idx
        while end_idx < len(lines):
            if lines[end_idx].strip().lower() == "@end":
                end_idx += 1
                break
            body_lines.append(lines[end_idx])
            end_idx += 1
        return {
            "type": "block",
            "block_type": "if",
            "condition": condition,
            "body": "\n".join(body_lines),
            "end_line": end_idx,
        }

    return {"type": "unknown", "raw": line}


def substitute(text: str, ctx: ScriptContext) -> str:
    """Replace {{VAR}} and {{$PREV}} in text."""
    text = text.replace("{{$PREV}}", ctx.prev_response)
    text = text.replace("{{$prev}}", ctx.prev_response)
    for key, val in ctx.variables.items():
        text = text.replace("{{" + key + "}}", val)
    # Environment variables
    import os
    for m in re.finditer(r"\{\{\$(\w+)\}\}", text):
        env_val = os.environ.get(m.group(1), "")
        text = text.replace(m.group(0), env_val)
    return text


def evaluate_condition(condition: str, ctx: ScriptContext) -> bool:
    """Evaluate an @if condition."""
    condition = substitute(condition, ctx)
    # "X contains Y"
    m = re.match(r"(.+?)\s+contains\s+(.+)", condition, re.IGNORECASE)
    if m:
        return m.group(2).strip().lower() in m.group(1).strip().lower()
    # "X == Y"
    m = re.match(r"(.+?)\s*==\s*(.+)", condition)
    if m:
        return m.group(1).strip() == m.group(2).strip()
    # "X != Y"
    m = re.match(r"(.+?)\s*!=\s*(.+)", condition)
    if m:
        return m.group(1).strip() != m.group(2).strip()
    # "X" (truthy)
    return bool(condition.strip())


async def run_script(
    script_text: str,
    client,  # AitherClient or compatible
    ctx: Optional[ScriptContext] = None,
    on_output=None,
) -> ScriptContext:
    """Execute an .aither script.

    Args:
        script_text: The script content
        client: AitherClient instance with .chat() method
        ctx: Optional pre-initialized context (for chaining)
        on_output: Callback(text, mode) for each output line

    Returns:
        ScriptContext with results and final state
    """
    import asyncio

    if ctx is None:
        ctx = ScriptContext()

    instructions = parse_script(script_text)

    for instr in instructions:
        t = instr["type"]

        if t == "set":
            ctx.variables[instr["key"]] = substitute(instr["value"], ctx)

        elif t == "will":
            ctx.will = instr["value"]
            if hasattr(client, "set_will"):
                await client.set_will(instr["value"])

        elif t == "effort":
            ctx.effort = instr["value"]

        elif t == "model":
            ctx.model = instr["value"]

        elif t == "system_prompt":
            ctx.system_prompt = substitute(instr["value"], ctx)

        elif t == "output_mode":
            ctx.output_mode = instr["value"]

        elif t == "sleep":
            await asyncio.sleep(instr["value"])

        elif t == "prompt":
            text = substitute(instr["text"], ctx)
            response = await client.chat(
                text,
                model=ctx.model,
                effort=ctx.effort,
                system_prompt=ctx.system_prompt,
            )
            resp_text = response.text if hasattr(response, "text") else str(response.get("response", response))

            ctx.prev_response = resp_text
            ctx.results.append({
                "prompt": text,
                "response": resp_text,
                "model": getattr(response, "model", None) or (response.get("model_used") if isinstance(response, dict) else None),
            })

            if on_output:
                if ctx.output_mode == "json":
                    on_output(json.dumps(ctx.results[-1], default=str), "json")
                elif ctx.output_mode == "print":
                    on_output(resp_text, "text")
                # quiet = no output

        elif t == "block":
            if instr["block_type"] == "for":
                for val in instr["values"]:
                    ctx.variables[instr["var"]] = val
                    body = substitute(instr["body"], ctx)
                    await run_script(body, client, ctx, on_output)

            elif instr["block_type"] == "if":
                if evaluate_condition(instr["condition"], ctx):
                    body = substitute(instr["body"], ctx)
                    await run_script(body, client, ctx, on_output)

    return ctx


def load_script_file(path: str) -> str:
    """Load a script from file or stdin."""
    if path == "-":
        return sys.stdin.read()
    with open(path, "r", encoding="utf-8") as f:
        return f.read()
