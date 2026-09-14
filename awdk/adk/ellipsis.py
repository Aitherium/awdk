"""Ellipsis detection and strategy decorator for NOOA ergonomics.

This module provides functions to detect and work with functions that use `...`
as a marker for LLM code generation, and a @strategy decorator for attaching
method metadata.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
import tokenize
from collections.abc import Callable
from typing import Any, TypeVar

F = TypeVar("F", bound=Callable[..., Any])


def _get_function_ast(func: Callable[..., Any]) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """Get the AST node for a function.

    Handles both regular source-based functions and dynamically-generated ones
    that have _generated_source attached.

    Returns:
        The FunctionDef or AsyncFunctionDef node, or None if not found.
    """
    source = None

    # Try to get source from inspect
    try:
        source = inspect.getsource(func)
        source = textwrap.dedent(source)
    except (OSError, IndentationError, SyntaxError, tokenize.TokenError):
        # Fall back to _generated_source for dynamically generated functions
        if hasattr(func, "_generated_source"):
            source = getattr(func, "_generated_source")  # noqa: B009

    if source is None:
        return None

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None

    # Find the function definition in the AST
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == func.__name__:
                return node

    return None


def _get_body_without_docstring(body: list[ast.stmt]) -> list[ast.stmt]:
    """Return function body with docstring removed if present."""
    if (
        len(body) > 0
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        return body[1:]
    return body


def _is_ellipsis_stmt(stmt: ast.stmt) -> bool:
    """Check if a statement is an ellipsis expression."""
    return (
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Constant)
        and stmt.value.value is ...
    )


def has_ellipsis_body(func: Callable[..., Any]) -> bool:
    """Check if a function body ends with `...` (ellipsis).

    This returns True if the function ends with ellipsis, even if there's
    other setup code before it (e.g., variable initialization).

    Args:
        func: Function to check

    Returns:
        True if body ends with ellipsis (may have code before it)
    """
    func_def = _get_function_ast(func)

    if func_def is not None:
        body = _get_body_without_docstring(func_def.body)

        if len(body) == 0:
            return False

        # Check if last statement is ellipsis
        return _is_ellipsis_stmt(body[-1])

    # For dynamically generated functions without source, check bytecode
    # A function with just `...` compiles to just RESUME, LOAD_CONST(None), RETURN_VALUE
    # This is a heuristic - if the function is very short, it might be ellipsis
    try:
        code = func.__code__
        # Very short bytecode (<=3 instructions after RESUME) often indicates ellipsis
        if code.co_code and len(code.co_code) <= 12:  # ~3-4 instructions
            return True
    except Exception:
        pass

    # If we can't determine, assume it's not ellipsis (safer default)
    return False


def strategy(
    loop: Any | None = None,
    model: Any | None = None,
    **opts: Any,
) -> Callable[[F], F]:
    """Strategy decorator for agent methods.

    Attaches metadata to a function for use by orchestration layers without
    changing its runtime behavior.

    Args:
        loop: Optional AgentLoop/Strategy instance to use for this method
        model: Optional model override for this method
        **opts: Additional strategy options (e.g., max_steps, max_preview_chars)

    Returns:
        Decorator function that attaches metadata to the decorated function

    Example:
        @strategy(loop=ReActLoop(max_steps=10), model=my_model)
        async def solve(self, problem: str): ...
    """

    def decorator(func: F) -> F:
        # Check for duplicate @strategy decorators
        if hasattr(func, "_adk_strategy"):
            raise ValueError(f"Cannot stack multiple @strategy decorators on {func.__name__}")

        # Attach metadata for downstream use
        setattr(func, "_adk_strategy", loop)  # noqa: B010
        setattr(func, "_adk_model", model)  # noqa: B010
        setattr(func, "_adk_strategy_opts", opts)  # noqa: B010

        return func

    return decorator


def get_strategy_meta(func: Callable[..., Any]) -> dict[str, Any]:
    """Retrieve strategy metadata from a decorated function.

    Args:
        func: Function to retrieve metadata from

    Returns:
        Dictionary with keys: loop, model, opts. Values are None if not set.
    """
    return {
        "loop": getattr(func, "_adk_strategy", None),
        "model": getattr(func, "_adk_model", None),
        "opts": getattr(func, "_adk_strategy_opts", {}),
    }
