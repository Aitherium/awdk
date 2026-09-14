# Delegation Test Spec — Agon-Arena Adaptation

**Goal**: Grade "orchestrator routes to specialist" tasks without exposing the grading criteria in the prompt.

## Agon-Arena Baseline (Distilled)

| Component | Points | Grading |
|-----------|--------|---------|
| **Agent** | 18 | Tool-calling, orchestration, planning, parsing, error-handling, decision-making |
| **Reason** | 17 | Substring-match (CONTAINS keyword, case-insensitive) OR dicts_match |
| **Code** | 19 | Expression repr match (via `repr()` + subprocess isolation) OR dicts_match |
| **Execution** | Isolated subprocess (no side effects, deterministic) |
| **Partial Credit** | None (pass/fail per test; no partial at task level) |
| **Output Format** | `task{id}/response.txt` + `summary.json` with `hidden_tests_passed/total` |

---

## Delegation Test Adaptation

### Task Template

```json
{
  "id": "delegate-{name}",
  "name": "Delegate to {specialist}: {scenario}",
  "difficulty": "medium",
  "prompt": "You are an orchestrator. {scenario}. Route this to the right specialist and incorporate their response.",
  "hidden_tests": [
    // 4 test vectors (a-d below), no exposure in prompt
  ]
}
```

---

## Hidden-Test Shapes (4 Vectors)

### (a) Routing Correctness — Did it call the right specialist?

**Shape**: Tool-call examination (JSON signature).

```python
{
    "type": "tool_call_audit",
    "checks": [
        {
            "test_name": "routing_specialist_match",
            "step": "Extract toolName from response.tool_calls[0]",
            "expected_tool": "web_search",  # OR code_review, security_audit, etc.
            "grading": "exact_match",  # toolName == expected_tool
            "points": 4
        },
        {
            "test_name": "routing_args_valid_json",
            "step": "Parse response.tool_calls[0].arguments as JSON",
            "expected": "type(args) == dict",
            "grading": "is_valid_json_dict",  # JSONDecodeError → fail
            "points": 2
        }
    ]
}
```

**Implementation** (subprocess):

```python
import json
import re

def test_routing_correctness(response_text, expected_tool="web_search"):
    # Extract first tool_call JSON from response
    match = re.search(r'"tool_use".*?"id":"(\w+)".*?"name":"(\w+)".*?"input":\s*({[^}]*})', response_text, re.DOTALL)
    if not match:
        return {"routing_specialist_match": False, "routing_args_valid": False}
    
    tool_name = match.group(2)
    try:
        args = json.loads(match.group(3))
        return {
            "routing_specialist_match": tool_name == expected_tool,
            "routing_args_valid": isinstance(args, dict) and len(args) > 0
        }
    except json.JSONDecodeError:
        return {"routing_specialist_match": tool_name == expected_tool, "routing_args_valid": False}
```

**Pass Criterion**: Both checks True.

---

### (b) Tool-Call Hygiene — Valid args, recovery from error injection

**Shape**: Argument validation + error resilience.

```python
{
    "type": "tool_hygiene",
    "checks": [
        {
            "test_name": "args_not_degenerate",
            "condition": [
                "tool_arguments is not empty",
                "no null/null-string/empty-array sentinel values",
                "required keys present (per specialist spec)"
            ],
            "grading": "all_or_nothing",
            "points": 2
        },
        {
            "test_name": "error_recovery",
            "inject": {
                "scenario": "Specialist returns tool_result with error=True, message='...'",
                "check": "Response acknowledges error AND retries OR pivots to fallback"
            },
            "grading": "response_contains_retry_or_alternate_action",
            "points": 3
        }
    ]
}
```

**Implementation**:

```python
def test_hygiene(response_text, specialist_error_injected=True):
    args_valid = (
        '"query"' in response_text or '"url"' in response_text  # per specialist
        and '"null"' not in response_text
        and '"null_"' not in response_text
    )
    
    error_recovered = (
        'error' in response_text.lower() and 
        ('retry' in response_text or 'alternative' in response_text or 'fallback' in response_text)
    )
    
    return {
        "args_not_degenerate": args_valid,
        "error_recovery": error_recovered or not specialist_error_injected
    }
```

**Pass Criterion**: args_not_degenerate=True AND (error_recovered OR no injection).

---

### (c) Final-Answer Correctness — Expression or Keyword Match

**Shape**: Expression repr OR substring containment (per Agon-Arena Reason/Code).

```python
{
    "type": "final_answer",
    "checks": [
        {
            "test_name": "answer_expression_match",
            "extract": "Final answer from response (last paragraph or marked section)",
            "expected_expression": "2 + 2",  # Reason-style: evaluates to 4
            "grading": "repr_match",  # eval(expr) repr == expected_repr("4")
            "points": 5
        },
        {
            "test_name": "answer_keyword_contains",
            "extract": "Final answer text",
            "expected_keywords": ["security", "vulnerability"],
            "grading": "contains_any_case_insensitive",
            "points": 5
        },
        {
            "test_name": "answer_dict_semantic",
            "extract": "JSON dict from final response",
            "expected_dict": {"tool": "web_search", "confidence": "high"},
            "grading": "dicts_match",  # Key subset; values can be close
            "points": 4
        }
    ]
}
```

**Implementation**:

```python
import re
import json

def test_final_answer(response_text, test_variant="expression"):
    # Extract final block
    last_para = response_text.split('\n\n')[-1] if '\n\n' in response_text else response_text
    
    if test_variant == "expression":
        # Reason-style: eval and compare repr
        match = re.search(r"answer:\s*(.+?)(?:\n|$)", last_para, re.IGNORECASE)
        if match:
            try:
                expr_str = match.group(1).strip()
                result = repr(eval(expr_str))
                return {"answer_expression_match": result == "4"}  # example
            except:
                return {"answer_expression_match": False}
    
    elif test_variant == "keyword":
        # Reason-style: substring check
        keywords = ["security", "vulnerability"]
        found = any(kw in last_para.lower() for kw in keywords)
        return {"answer_keyword_contains": found}
    
    elif test_variant == "dict":
        # Code-style: JSON dict match
        try:
            match = re.search(r"\{[^}]+\}", last_para)
            if match:
                found_dict = json.loads(match.group())
                expected = {"tool": "web_search"}
                match_keys = all(found_dict.get(k) == v for k, v in expected.items())
                return {"answer_dict_semantic": match_keys}
        except:
            pass
        return {"answer_dict_semantic": False}
```

**Pass Criterion**: At least ONE of (expression_match, keyword_contains, dict_semantic)=True.

---

### (d) Delegation-Reached — Did the specialist actually execute?

**Shape**: Model identity assertion (tool result provenance).

```python
{
    "type": "delegation_provenance",
    "checks": [
        {
            "test_name": "tool_result_present",
            "condition": "response contains tool_result block with status=success",
            "grading": "regex_match",
            "pattern": r'"type":\s*"tool_result".*?"content":\s*"',
            "points": 2
        },
        {
            "test_name": "specialist_model_identity",
            "extract": "tool_result.metadata.model OR response.assistant_model",
            "expected_model_contains": "hydra|demiurge|athena|apollo",  # NOT fallback default
            "grading": "contains_specialist_pattern",
            "points": 3
        },
        {
            "test_name": "delegation_not_fallback",
            "condition": "response does NOT contain fallback_reason or N/A marker",
            "grading": "not_contains",
            "pattern": r"fallback|N/A|unavailable|cannot reach",
            "points": 2
        }
    ]
}
```

**Implementation**:

```python
def test_delegation_reached(response_text, expected_specialist="hydra"):
    tool_result_found = '"type": "tool_result"' in response_text and '"success"' in response_text
    
    specialist_identity = (
        f'"{expected_specialist}"' in response_text.lower() or
        f'model.*{expected_specialist}' in response_text.lower()
    )
    
    is_fallback = any(
        marker in response_text.lower()
        for marker in ["fallback", "n/a", "unavailable", "cannot reach", "default model"]
    )
    
    return {
        "tool_result_present": tool_result_found,
        "specialist_model_identity": specialist_identity,
        "delegation_not_fallback": not is_fallback
    }
```

**Pass Criterion**: tool_result_present=True AND specialist_model_identity=True AND delegation_not_fallback=True.

---

## Scoring Rubric

```json
{
  "task_id": "delegate-web-search-news",
  "max_points": 18,  // Agent event (delegation-specific)
  "breakdown": {
    "routing_correctness": {"max": 6, "tests": ["specialist_match", "args_valid"]},
    "tool_hygiene": {"max": 5, "tests": ["args_not_degenerate", "error_recovery"]},
    "final_answer": {"max": 4, "tests": ["keyword_contains", "dict_semantic"]},
    "delegation_reached": {"max": 3, "tests": ["tool_result_present", "specialist_identity"]}
  },
  "hidden_tests_passed": 7,
  "hidden_tests_total": 9,
  "score": 16,
  "grade": "PASS"
}
```

---

## Usage Pattern

### Orchestrator Task Prompt (Opaque to Model)

```
You are an orchestrator. A user asks: "What are the latest cybersecurity vulnerabilities?"

Analyze the intent and route to the appropriate specialist. Incorporate their response into your final answer.

Do NOT reveal your reasoning or tool names in the output.
```

### Hidden-Test Injections (Subprocess-Side)

```python
# simulator/runner.py
def grade_delegation_task(task_id, response_text):
    results = {}
    
    # Vector (a): Routing
    results['routing'] = test_routing_correctness(response_text, expected_tool="web_search")
    
    # Vector (b): Hygiene
    results['hygiene'] = test_hygiene(response_text)
    
    # Vector (c): Final Answer
    results['answer'] = test_final_answer(response_text, variant="keyword")
    
    # Vector (d): Delegation Reached
    results['delegation'] = test_delegation_reached(response_text, expected_specialist="athena")
    
    # Flatten, compute pass counts
    all_checks = {k: v for d in results.values() for k, v in d.items()}
    passed = sum(1 for v in all_checks.values() if v is True)
    total = len(all_checks)
    
    return {
        "task_id": task_id,
        "hidden_tests_passed": passed,
        "hidden_tests_total": total,
        "grade": "PASS" if passed == total else "FAIL",
        "details": all_checks
    }
```

### Output

```json
{
  "task001/response.txt": "...(model response)...",
  "task001/summary.json": {
    "hidden_tests_passed": 7,
    "hidden_tests_total": 9,
    "grade": "PASS"
  }
}
```

---

## Key Design Decisions

1. **No Prompt Exposure**: Grading criteria (expected specialist, tool names, keywords) are ONLY in hidden tests, not in task text.
2. **4-Vector Orthogonality**: (a) routing, (b) hygiene, (c) answer quality, (d) provenance—test different failure modes.
3. **Isolation**: Each test runs in subprocess; no state leakage.
4. **Binary Grading**: Per agon-arena, no partial credit per task (pass/fail per individual check, but task-level is aggregate).
5. **Variant Support**: Answer tests can be expression (Reason), keyword (Reason/Agent), or dict (Code)—pick per scenario.
6. **Error Injection**: Hygiene tests can inject specialist errors to verify recovery (orchestrator resilience).

---

## Example Task Instances

### Task 1: Code Review (Hydra Specialist)
```json
{
  "id": "delegate-code-review-race",
  "prompt": "Review this async Python code for concurrency bugs. Route to a code-review specialist and synthesize findings.",
  "hidden_tests": {
    "expected_specialist": "hydra",
    "expected_tool": "code_review",
    "answer_keywords": ["race condition", "mutex", "critical section"],
    "tool_error_injection": true
  }
}
```

### Task 2: Security Audit (Athena Specialist)
```json
{
  "id": "delegate-security-audit-api",
  "prompt": "Analyze this REST API for security vulnerabilities. Route appropriately and report findings.",
  "hidden_tests": {
    "expected_specialist": "athena",
    "expected_tool": "security_audit",
    "answer_keywords": ["authentication", "injection", "privilege"],
    "delegation_not_fallback": true
  }
}
```

### Task 3: Refactor (Demiurge Specialist)
```json
{
  "id": "delegate-refactor-legacy-monolith",
  "prompt": "Suggest a refactoring strategy for this 5000-line monolith. Route to a refactoring specialist.",
  "hidden_tests": {
    "expected_specialist": "demiurge",
    "expected_tool": "refactor",
    "answer_expression": "improvement_ratio",
    "expected_expression_repr": "2.5"
  }
}
```

---

## Integration Checklist

- [ ] Define task JSON + hidden_tests per template
- [ ] Implement test functions (a-d) in `simulator/grader.py`
- [ ] Add injection hooks for error scenarios
- [ ] Run subprocess isolation test (no import side effects)
- [ ] Validate output format matches summary.json schema
- [ ] Spot-check one task end-to-end before batch run
