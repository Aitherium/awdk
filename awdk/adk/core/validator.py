"""Best-effort code safety validator for adk agent code.

This module provides AST-based validation for code-as-action patterns.
It catches common unsafe patterns (eval, exec, dangerous dunders, etc.)
and returns structured issues for each problem found.

**IMPORTANT: This is best-effort code safety, NOT a security boundary.**
Real isolation requires an OS sandbox (container, VM, etc.).
See VALIDATOR_NOTICE for attribution and license.

Usage:
    validator = CodeValidator()
    issues = validator.validate("x = eval(input())")
    for issue in issues:
        print(f"Line {issue.line}: {issue.message}")

Public API:
    - CodeValidator: Main validator class
    - ValidationIssue: Issue descriptor with line, col, message, code, severity
    - ValidationContext: Validation context (code, restricted_imports, etc.)
"""

import ast
from dataclasses import dataclass, field
from typing import Any, Literal

__all__ = [
    "CodeValidator",
    "ValidationIssue",
    "ValidationContext",
]


# =============================================================================
# Data Classes
# =============================================================================

@dataclass
class ValidationIssue:
    """A single code validation issue with location and severity."""

    line: int
    col: int
    message: str
    severity: Literal["error", "warning"] = "error"
    code: str = ""  # e.g. "E001", "E101"
    fix_hint: str | None = None


@dataclass
class ValidationContext:
    """Shared context for code validation."""

    code: str = ""
    restricted_imports: frozenset[str] = field(default_factory=frozenset)
    blocked_modules: frozenset[str] = field(default_factory=frozenset)
    available_names: set[str] = field(default_factory=set)


# =============================================================================
# Security Validator
# =============================================================================

# Functions always forbidden in generated code
FORBIDDEN_BUILTINS = frozenset({
    # Dynamic code execution (security risk)
    "exec",
    "eval",
    "compile",
    "__import__",
    # Blocking stdin operations
    "input",
    "breakpoint",
    # Namespace access (security risk)
    "globals",
    "locals",
    "vars",
})

# Dangerous dunder attributes that enable sandbox escapes
DANGEROUS_DUNDER_ATTRS = frozenset({
    "__class__",
    "__bases__",
    "__subclasses__",
    "__mro__",
    "__globals__",
    "__code__",
    "__builtins__",
    "__dict__",
})


class CodeValidator:
    """Best-effort AST validator for agent-generated code.

    Checks for:
    - Forbidden builtins (eval, exec, compile, __import__, input, etc.)
    - Restricted/blocked imports
    - Dangerous dunder attribute access
    - Wildcard imports

    This is a best-effort tool, NOT a security boundary. Real isolation
    requires OS-level sandboxing (container, VM, etc.).
    """

    def validate(
        self,
        code: str,
        context: ValidationContext | None = None,
    ) -> list[ValidationIssue]:
        """Validate code and return list of issues.

        Args:
            code: Python source code to validate
            context: ValidationContext (optional). If None, uses defaults.

        Returns:
            List of ValidationIssue objects found (empty list if valid).
        """
        if context is None:
            context = ValidationContext(code=code)
        else:
            context.code = code

        # Handle empty/whitespace code
        if not code or not code.strip():
            return []

        # Parse AST
        try:
            tree = ast.parse(code)
        except SyntaxError:
            # Return no issues for syntax errors; let the runtime catch them
            return []

        # Run visitor
        visitor = _SecurityVisitor(context)
        visitor.visit(tree)
        return visitor.issues


class _SecurityVisitor(ast.NodeVisitor):
    """AST visitor for security validation."""

    def __init__(self, context: ValidationContext):
        self.context = context
        self.issues: list[ValidationIssue] = []
        # Track aliases: local_name -> original_forbidden_name
        self.forbidden_aliases: dict[str, str] = {}

    def visit_Import(self, node: ast.Import) -> Any:
        """Check import statements."""
        for alias in node.names:
            if self._is_import_blocked(alias.name):
                self.issues.append(
                    ValidationIssue(
                        line=node.lineno,
                        col=node.col_offset,
                        message=(
                            f"Import of '{alias.name}' is restricted. "
                            f"Forbidden builtins: eval(), exec(), compile(), __import__(), "
                            f"input(), globals(), locals(), breakpoint()"
                        ),
                        code="E002",
                    )
                )
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> Any:
        """Check from-import statements."""
        # from X import * is always forbidden
        if any(alias.name == "*" for alias in node.names):
            self.issues.append(
                ValidationIssue(
                    line=node.lineno,
                    col=node.col_offset,
                    message="'from ... import *' is forbidden for security reasons",
                    code="E003",
                )
            )
            self.generic_visit(node)
            return

        # Check if module is blocked
        module_name = node.module or ""
        if self._is_import_blocked(module_name):
            self.issues.append(
                ValidationIssue(
                    line=node.lineno,
                    col=node.col_offset,
                    message=(
                        f"Import of '{module_name}' is restricted. "
                        f"This module is in the restricted or blocked imports list."
                    ),
                    code="E002",
                )
            )
        else:
            # Track aliases of forbidden builtins
            for alias in node.names:
                local_name = alias.asname or alias.name
                if alias.name in FORBIDDEN_BUILTINS:
                    self.forbidden_aliases[local_name] = alias.name

        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> Any:
        """Check function calls."""
        if isinstance(node.func, ast.Name):
            func_name = node.func.id

            # Check direct forbidden calls
            if func_name in FORBIDDEN_BUILTINS:
                self.issues.append(
                    ValidationIssue(
                        line=node.lineno,
                        col=node.col_offset,
                        message=(
                            f"{func_name}() is forbidden — "
                            f"it allows arbitrary code execution or blocks input"
                        ),
                        code="E001",
                    )
                )
            # Check aliased forbidden calls
            elif func_name in self.forbidden_aliases:
                original = self.forbidden_aliases[func_name]
                self.issues.append(
                    ValidationIssue(
                        line=node.lineno,
                        col=node.col_offset,
                        message=(
                            f"{func_name}() is forbidden (alias for {original}) — "
                            f"it allows arbitrary code execution or blocks input"
                        ),
                        code="E001",
                    )
                )

        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> Any:
        """Check attribute access for dangerous dunders."""
        if node.attr in DANGEROUS_DUNDER_ATTRS:
            self.issues.append(
                ValidationIssue(
                    line=node.lineno,
                    col=node.col_offset,
                    message=(
                        f"Access to '{node.attr}' is forbidden — "
                        f"this could bypass security restrictions"
                    ),
                    code="E101",
                )
            )
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> Any:
        """Check name access for __builtins__."""
        if node.id == "__builtins__":
            self.issues.append(
                ValidationIssue(
                    line=node.lineno,
                    col=node.col_offset,
                    message=(
                        "Access to '__builtins__' is forbidden — "
                        "this could bypass security restrictions"
                    ),
                    code="E101",
                )
            )
        self.generic_visit(node)

    def _is_import_blocked(self, module_name: str) -> bool:
        """Check if module is in blocked or restricted imports."""
        if not module_name:
            return False

        # Check blocked_modules (higher tier)
        if self.context.blocked_modules:
            root = module_name.split(".", 1)[0]
            if root in self.context.blocked_modules:
                return True
            if module_name in self.context.blocked_modules:
                return True

        # Check restricted_imports (lower tier)
        if self.context.restricted_imports:
            root = module_name.split(".", 1)[0]
            if root in self.context.restricted_imports:
                return True
            if module_name in self.context.restricted_imports:
                return True

        return False
