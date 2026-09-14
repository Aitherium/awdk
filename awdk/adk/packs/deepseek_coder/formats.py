"""DeepSeek Coder prompt formats — the four shapes, spelled exactly.

Derived from https://github.com/deepseek-ai/DeepSeek-Coder (MIT). See NOTICE.

This module is deliberately pure: no network, no tokenizer, no model. Getting
these strings wrong is the single most expensive thing about driving this model
family, because every mistake fails SILENTLY — the model still answers, just
worse, and nothing anywhere reports a malformed prompt.

Three traps, all of them measured from the upstream source rather than retyped
from the README:

**The FIM tokens are not ASCII.** They use U+FF5C (FULLWIDTH VERTICAL LINE) for
the bars and U+2581 (LOWER ONE EIGHTH BLOCK, SentencePiece's word-boundary mark)
for the separator — NOT ``|`` and NOT ``_``. They are assembled from ``chr()``
here rather than typed, so this file stays pure ASCII and no editor, console or
re-encoding step can quietly alter them. That is not theoretical: writing this
module on Windows, a cp1252 console turned the literals into ``?`` twice, and a
mangled FIM token does not raise. It tokenises as ordinary text, the model reads
the whole prompt as prose, and you get a fluent completion that ignores the
suffix entirely — a wrong answer with no error anywhere.

**The instruct template is load-bearing whitespace.** Upstream builds it with a
leading newline that is then ``lstrip()``-ed and a trailing newline that is not.
Reproduced exactly.

**Instruct models CAN complete code, but only if you move the EOS.** The
instruct config stops on ``<|EOT|>`` (32021); raw completion needs 32014. Leave
it at the default and every completion terminates at the first turn boundary,
which reads as "this model is bad at completion" rather than "wrong stop token".
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ── FIM (fill-in-the-middle) ──────────────────────────────────────────
# Built from codepoints deliberately — see module docstring.
# Do not "clean these up" into literal characters.
_BAR = chr(0xFF5C)      # FULLWIDTH VERTICAL LINE, not "|"
_SEP = chr(0x2581)      # LOWER ONE EIGHTH BLOCK, not "_"

FIM_BEGIN = f"<{_BAR}fim{_SEP}begin{_BAR}>"
FIM_HOLE = f"<{_BAR}fim{_SEP}hole{_BAR}>"
FIM_END = f"<{_BAR}fim{_SEP}end{_BAR}>"

#: End-of-turn token used by the instruct models.
EOT_TOKEN = "<|EOT|>"

#: Stop-token ids. The default in the instruct config is EOS_INSTRUCT; raw
#: completion with an instruct model requires EOS_COMPLETION instead.
EOS_INSTRUCT = 32021
EOS_COMPLETION = 32014

#: Upstream's system preamble, verbatim from `finetune_deepseekcoder.py`.
SYSTEM_PROMPT = (
    "You are an AI programming assistant, utilizing the DeepSeek Coder model, "
    "developed by DeepSeek Company, and you only answer questions related to "
    "computer science. For politically sensitive questions, security and privacy "
    "issues, and other non-computer science questions, you will refuse to answer."
)


def build_fim_prompt(prefix: str, suffix: str) -> str:
    """Assemble a fill-in-the-middle prompt.

    The model completes the HOLE between ``prefix`` and ``suffix``. Note the
    ordering: prefix, hole, suffix, end — the suffix goes AFTER the hole marker,
    which is the opposite of the intuitive "give me both sides" reading and is
    the second most common way to get this wrong.
    """
    return f"{FIM_BEGIN}{prefix}{FIM_HOLE}{suffix}{FIM_END}"


def parse_fim_completion(raw: str, prompt: str = "") -> str:
    """Strip the echoed prompt and any trailing sentinel from a FIM answer."""
    out = raw[len(prompt):] if prompt and raw.startswith(prompt) else raw
    for token in (FIM_END, FIM_HOLE, FIM_BEGIN, EOT_TOKEN):
        out = out.replace(token, "")
    return out


def build_instruction_prompt(instruction: str) -> str:
    """Single-turn instruct format, byte-identical to upstream's builder."""
    return (
        "\n" + SYSTEM_PROMPT + "\n### Instruction:\n"
        + instruction.strip()
        + "\n### Response:\n"
    ).lstrip()


def build_chat_prompt(messages: Sequence[Dict[str, str]]) -> str:
    """Multi-turn instruct format.

    Upstream's own guidance for callers who cannot use
    ``tokenizer.apply_chat_template``. Assistant turns are terminated with
    ``<|EOT|>``; the final assistant turn is left open for generation.
    """
    parts: List[str] = [SYSTEM_PROMPT]
    for message in messages:
        role = str(message.get("role") or "").lower()
        content = str(message.get("content") or "").strip()
        if not content:
            continue
        if role in ("user", "human"):
            parts.append(f"### Instruction:\n{content}")
        elif role in ("assistant", "gpt"):
            parts.append(f"### Response:\n{content}\n{EOT_TOKEN}")
        elif role == "system":
            # A caller-supplied system message REPLACES the preamble rather
            # than stacking with it; two system voices produce a model that
            # hedges between them.
            parts[0] = content
    parts.append("### Response:\n")
    return "\n".join(parts)


# ── repo-level packing ────────────────────────────────────────────────

#: Invocation relationships, by regex — the upstream choice. A regex degrades to
#: "no edge found" on a file it cannot read, where a parser would fail the whole
#: repo; at this scale a missed edge costs ordering quality, an exception costs
#: the document.
_IMPORT_PATTERNS = [
    re.compile(r"^\s*from\s+([.\w]+)\s+import", re.MULTILINE),        # python
    re.compile(r"^\s*import\s+([.\w]+)", re.MULTILINE),               # python/go/java
    re.compile(r"^\s*using\s+([.\w]+)\s*;", re.MULTILINE),            # c#
    re.compile(r'^\s*#\s*include\s*[<"]([^>"]+)[>"]', re.MULTILINE),  # c/c++
    re.compile(r"""from\s+['"]([^'"]+)['"]"""),                       # js/ts
    re.compile(r"""require\(\s*['"]([^'"]+)['"]\s*\)"""),             # js
]

_SOURCE_SUFFIXES = (
    ".py", ".js", ".jsx", ".ts", ".tsx", ".cs", ".c", ".h", ".cc", ".cpp",
    ".hpp", ".go", ".java", ".rs", ".rb", ".php",
)


def _stem(path: str) -> str:
    """Filename without directory or extension."""
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    for suffix in _SOURCE_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _module_name(path: str) -> str:
    """Importable name for a path — the stem, for every language handled here."""
    return _stem(path)


def _local_imports(source: str, known: Dict[str, str]) -> List[str]:
    """Which of ``known`` this source imports. Intra-project references only.

    An import is matched on its LAST path segment as well as its first, because
    the same file is referenced as ``utils``, ``./utils``, ``app/utils`` and
    ``pkg/app/utils`` depending on the language and the caller's location.
    """
    found: List[str] = []
    for pattern in _IMPORT_PATTERNS:
        for match in pattern.finditer(source):
            raw = (match.group(1) or "").strip()
            if not raw:
                continue
            segments = [s for s in re.split(r"[./\\]", raw.lstrip("./\\")) if s]
            for candidate in (segments[-1] if segments else "", segments[0] if segments else ""):
                target = known.get(candidate)
                if target and target not in found:
                    found.append(target)
                    break
    return found


def dependency_graph(files: Dict[str, str]) -> Dict[str, Any]:
    """The intra-project dependency graph, as data.

    Edges point B -> A ("A depends on B"), matching Algorithm 1 of the paper, so
    a topological walk emits dependencies before dependents. Returned separately
    from the ordering because the graph is useful on its own: it is the
    structural view of a codebase that a change-capture or world-model layer
    wants, and recomputing it from the packed string is not possible.

    Multi-language by regex, as upstream: "import" (Python), "using" (C#),
    "include" (C/C++), plus JS/TS "from '...'" and Go quoted imports. Regex, not
    a parser, is the upstream choice and is deliberate — it degrades to "no edge"
    rather than failing on a file it cannot parse.
    """
    known: Dict[str, str] = {}
    for path in files:
        known.setdefault(_module_name(path), path)
        known.setdefault(_stem(path), path)

    graphs: Dict[str, List[str]] = {path: [] for path in files}
    in_degree: Dict[str, int] = {path: 0 for path in files}

    for path, source in files.items():
        for dep in _local_imports(source, known):
            if dep == path or path in graphs[dep]:
                continue
            graphs[dep].append(path)      # edge B -> A
            in_degree[path] += 1

    return {"graphs": graphs, "in_degree": in_degree}


def _subgraphs(graphs: Dict[str, List[str]]) -> List[List[str]]:
    """Connected components, treating edges as undirected (paper step 18)."""
    neighbours: Dict[str, set] = {node: set() for node in graphs}
    for node, targets in graphs.items():
        for target in targets:
            neighbours[node].add(target)
            neighbours[target].add(node)

    seen: set = set()
    components: List[List[str]] = []
    for node in sorted(graphs):
        if node in seen:
            continue
        stack, component = [node], []
        seen.add(node)
        while stack:
            current = stack.pop()
            component.append(current)
            for other in sorted(neighbours[current]):
                if other not in seen:
                    seen.add(other)
                    stack.append(other)
        components.append(sorted(component))
    return components


def order_by_dependency(files: Dict[str, str]) -> Tuple[List[str], List[str]]:
    """Order ``{path: source}`` so dependencies precede dependents.

    Implements Algorithm 1 ("Topological Sort for Dependency Analysis") from the
    DeepSeek-Coder paper, because that is the layout the model was PRE-TRAINED
    on — a different-but-valid topological order is not equivalent input.

    Two details of the paper's algorithm are easy to miss and both are kept:

    * It partitions the graph into disconnected subgraphs first and sorts each
      independently, so unrelated files stay clustered instead of interleaving.
    * It selects ``argmin(in_degree)`` among unplaced nodes rather than
      requiring in-degree zero. That is what makes it total: a cyclic import
      never stalls it, it simply takes the least-depended-upon file next.

    Returns ``(ordered_paths, cycles)``. Cycles cannot break the ordering, but
    they ARE reported, because in a cycle the "dependencies first" guarantee no
    longer holds for those files and a caller reading the packed context should
    know the order is approximate there.
    """
    graph = dependency_graph(files)
    graphs, in_degree = graph["graphs"], dict(graph["in_degree"])
    cycles = _find_cycles(graphs)

    ordered: List[str] = []
    for component in _subgraphs(graphs):
        placed: set = set()
        remaining = list(component)
        while len(placed) != len(component):
            # argmin in-degree, ties broken by path for determinism.
            nxt = min(
                (n for n in remaining if n not in placed),
                key=lambda n: (in_degree[n], n),
            )
            placed.add(nxt)
            ordered.append(nxt)
            for dependent in graphs[nxt]:
                if dependent not in placed:
                    in_degree[dependent] -= 1
    return ordered, cycles


def _find_cycles(graphs: Dict[str, List[str]]) -> List[str]:
    """Report dependency cycles. Diagnostic only — never changes the ordering."""
    colour: Dict[str, int] = {}
    cycles: List[str] = []

    def walk(node: str, stack: List[str]) -> None:
        state = colour.get(node, 0)
        if state == 2:
            return
        if state == 1:
            cycles.append(" -> ".join(stack[stack.index(node):] + [node]))
            return
        colour[node] = 1
        for target in graphs.get(node, []):
            walk(target, stack + [node])
        colour[node] = 2

    for node in sorted(graphs):
        walk(node, [])
    return cycles


def build_repo_context(
    files: Dict[str, str],
    order: Optional[Sequence[str]] = None,
) -> str:
    """Concatenate a project into one prompt, dependency-ordered.

    Each file is introduced by a ``#path`` comment, which is the marker the
    model was pre-trained to read as a file boundary. Without it the files run
    together and the model cannot tell where one ends.
    """
    paths = list(order) if order else order_by_dependency(files)[0]
    blocks = [f"#{path}\n{files[path].rstrip()}" for path in paths if path in files]
    return "\n\n".join(blocks) + "\n"


def eos_token_id(model: str, mode: str = "chat") -> int:
    """The stop-token id for this model in this mode.

    An instruct model asked to COMPLETE needs the completion EOS, or generation
    halts at the first turn boundary and looks like a weak model.
    """
    if mode in ("completion", "fim", "infill"):
        return EOS_COMPLETION
    return EOS_INSTRUCT if "instruct" in model.lower() else EOS_COMPLETION


def describe_traps() -> List[Dict[str, str]]:
    """The failure modes, as data — so a surface can show them to a human."""
    return [
        {
            "id": "fim-tokens-not-ascii",
            "summary": "FIM markers use U+FF5C and U+2581, not '|' and '_'",
            "symptom": "completion ignores the suffix and reads as prose",
        },
        {
            "id": "fim-suffix-after-hole",
            "summary": "order is prefix, HOLE, suffix, END",
            "symptom": "model completes the wrong side of the gap",
        },
        {
            "id": "eos-32014-vs-32021",
            "summary": "instruct models need eos 32014 to do raw completion",
            "symptom": "completion stops immediately; reads as a weak model",
        },
        {
            "id": "missing-file-headers",
            "summary": "repo context needs '#path' markers before each file",
            "symptom": "model cannot find file boundaries in a packed repo",
        },
    ]
