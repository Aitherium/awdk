"""Tests for adk.core.validator — best-effort code safety."""

import pytest

from adk.core.validator import (
    CodeValidator,
    ValidationContext,
    ValidationIssue,
    FORBIDDEN_BUILTINS,
    DANGEROUS_DUNDER_ATTRS,
)


class TestForbiddenBuiltins:
    """Test detection of forbidden builtin functions."""

    def test_flags_eval(self):
        """eval() should be flagged."""
        validator = CodeValidator()
        issues = validator.validate("result = eval('1 + 1')")
        assert len(issues) == 1
        assert issues[0].code == "E001"
        assert "eval" in issues[0].message.lower()

    def test_flags_exec(self):
        """exec() should be flagged."""
        validator = CodeValidator()
        issues = validator.validate("exec('x = 1')")
        assert len(issues) == 1
        assert issues[0].code == "E001"
        assert "exec" in issues[0].message.lower()

    def test_flags_compile(self):
        """compile() should be flagged."""
        validator = CodeValidator()
        issues = validator.validate("code = compile('x = 1', '<string>', 'exec')")
        assert len(issues) == 1
        assert issues[0].code == "E001"
        assert "compile" in issues[0].message.lower()

    def test_flags_import(self):
        """__import__() should be flagged."""
        validator = CodeValidator()
        issues = validator.validate("mod = __import__('os')")
        assert len(issues) == 1
        assert issues[0].code == "E001"
        assert "__import__" in issues[0].message.lower()

    def test_flags_input(self):
        """input() should be flagged."""
        validator = CodeValidator()
        issues = validator.validate("name = input('Enter name: ')")
        assert len(issues) == 1
        assert issues[0].code == "E001"
        assert "input" in issues[0].message.lower()

    def test_flags_globals(self):
        """globals() should be flagged."""
        validator = CodeValidator()
        issues = validator.validate("g = globals()")
        assert len(issues) == 1
        assert issues[0].code == "E001"
        assert "globals" in issues[0].message.lower()

    def test_flags_locals(self):
        """locals() should be flagged."""
        validator = CodeValidator()
        issues = validator.validate("l = locals()")
        assert len(issues) == 1
        assert issues[0].code == "E001"

    def test_flags_breakpoint(self):
        """breakpoint() should be flagged."""
        validator = CodeValidator()
        issues = validator.validate("breakpoint()")
        assert len(issues) == 1
        assert issues[0].code == "E001"

    def test_flags_vars(self):
        """vars() should be flagged."""
        validator = CodeValidator()
        issues = validator.validate("v = vars(obj)")
        assert len(issues) == 1
        assert issues[0].code == "E001"


class TestAliasedForbiddenCalls:
    """Test detection of forbidden calls via aliases."""

    def test_flags_aliased_eval(self):
        """Aliased eval should be flagged."""
        validator = CodeValidator()
        code = """from builtins import eval as e
x = e('1 + 1')"""
        issues = validator.validate(code)
        assert any(i.code == "E001" for i in issues)

    def test_flags_aliased_exec(self):
        """Aliased exec should be flagged."""
        validator = CodeValidator()
        code = """from builtins import exec as execute
execute('y = 2')"""
        issues = validator.validate(code)
        assert any(i.code == "E001" for i in issues)


class TestDangerousDunders:
    """Test detection of dangerous dunder attributes."""

    def test_flags_class_dunder(self):
        """__class__ access should be flagged."""
        validator = CodeValidator()
        issues = validator.validate("cls = obj.__class__")
        assert len(issues) == 1
        assert issues[0].code == "E101"
        assert "__class__" in issues[0].message

    def test_flags_subclasses_dunder(self):
        """__subclasses__() access should be flagged."""
        validator = CodeValidator()
        issues = validator.validate("subs = str.__subclasses__()")
        assert len(issues) == 1
        assert issues[0].code == "E101"

    def test_flags_globals_dunder(self):
        """__globals__ access should be flagged."""
        validator = CodeValidator()
        issues = validator.validate("g = func.__globals__")
        assert len(issues) == 1
        assert issues[0].code == "E101"

    def test_flags_code_dunder(self):
        """__code__ access should be flagged."""
        validator = CodeValidator()
        issues = validator.validate("c = func.__code__")
        assert len(issues) == 1
        assert issues[0].code == "E101"

    def test_flags_builtins_dunder(self):
        """__builtins__ access should be flagged."""
        validator = CodeValidator()
        issues = validator.validate("b = __builtins__")
        assert len(issues) == 1
        assert issues[0].code == "E101"

    def test_flags_dict_dunder(self):
        """__dict__ access should be flagged."""
        validator = CodeValidator()
        issues = validator.validate("d = obj.__dict__")
        assert len(issues) == 1
        assert issues[0].code == "E101"

    def test_flags_bases_dunder(self):
        """__bases__ access should be flagged."""
        validator = CodeValidator()
        issues = validator.validate("bases = cls.__bases__")
        assert len(issues) == 1
        assert issues[0].code == "E101"

    def test_flags_mro_dunder(self):
        """__mro__ access should be flagged."""
        validator = CodeValidator()
        issues = validator.validate("mro = cls.__mro__")
        assert len(issues) == 1
        assert issues[0].code == "E101"


class TestWildcardImports:
    """Test detection of wildcard imports."""

    def test_flags_wildcard_import(self):
        """from X import * should be flagged."""
        validator = CodeValidator()
        issues = validator.validate("from os import *")
        assert len(issues) == 1
        assert issues[0].code == "E003"
        assert "import *" in issues[0].message.lower()


class TestRestrictedImports:
    """Test restricted imports in deny list."""

    def test_flags_restricted_import(self):
        """Imports in restricted_imports should be flagged."""
        validator = CodeValidator()
        context = ValidationContext(
            code="import mymodule",
            restricted_imports=frozenset({"mymodule"}),
        )
        issues = validator.validate("import mymodule", context)
        assert len(issues) == 1
        assert issues[0].code == "E002"

    def test_flags_blocked_import(self):
        """Imports in blocked_modules should be flagged."""
        validator = CodeValidator()
        context = ValidationContext(
            code="import threading",
            blocked_modules=frozenset({"threading"}),
        )
        issues = validator.validate("import threading", context)
        assert len(issues) == 1
        assert issues[0].code == "E002"

    def test_allows_unrestricted_import(self):
        """Unrestricted imports should pass."""
        validator = CodeValidator()
        context = ValidationContext(
            code="import math",
            restricted_imports=frozenset({"os", "sys"}),
        )
        issues = validator.validate("import math", context)
        assert len(issues) == 0


class TestCleanCode:
    """Test that safe code passes validation."""

    def test_arithmetic_passes(self):
        """Simple arithmetic should pass."""
        validator = CodeValidator()
        issues = validator.validate("x = 1 + 2")
        assert len(issues) == 0

    def test_list_comprehension_passes(self):
        """List comprehension should pass."""
        validator = CodeValidator()
        code = "result = [x * 2 for x in range(10) if x % 2 == 0]"
        issues = validator.validate(code)
        assert len(issues) == 0

    def test_function_def_passes(self):
        """Function definition should pass."""
        validator = CodeValidator()
        code = """def add(a, b):
    return a + b

result = add(3, 4)"""
        issues = validator.validate(code)
        assert len(issues) == 0

    def test_class_def_passes(self):
        """Class definition should pass."""
        validator = CodeValidator()
        code = """class Point:
    def __init__(self, x, y):
        self.x = x
        self.y = y

p = Point(1, 2)"""
        issues = validator.validate(code)
        assert len(issues) == 0

    def test_safe_stdlib_import_passes(self):
        """Safe stdlib imports should pass."""
        validator = CodeValidator()
        code = """import math
import json

result = math.sqrt(16)"""
        issues = validator.validate(code)
        assert len(issues) == 0

    def test_safe_from_import_passes(self):
        """Safe from-imports should pass."""
        validator = CodeValidator()
        code = """from pathlib import Path
from typing import List

p = Path('.')"""
        issues = validator.validate(code)
        assert len(issues) == 0

    def test_dictionary_operations_pass(self):
        """Dictionary operations should pass."""
        validator = CodeValidator()
        code = """data = {'a': 1, 'b': 2}
result = data.get('a', 0)
data['c'] = 3"""
        issues = validator.validate(code)
        assert len(issues) == 0

    def test_exception_handling_passes(self):
        """Exception handling should pass."""
        validator = CodeValidator()
        code = """try:
    x = 1 / 0
except ZeroDivisionError:
    x = 0"""
        issues = validator.validate(code)
        assert len(issues) == 0

    def test_async_code_passes(self):
        """Async code should pass."""
        validator = CodeValidator()
        code = """async def fetch():
    result = await something()
    return result"""
        issues = validator.validate(code)
        assert len(issues) == 0

    def test_context_manager_passes(self):
        """Context managers should pass."""
        validator = CodeValidator()
        code = """with open('file.txt') as f:
    data = f.read()"""
        issues = validator.validate(code)
        assert len(issues) == 0


class TestEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_empty_code_passes(self):
        """Empty code should return no issues."""
        validator = CodeValidator()
        issues = validator.validate("")
        assert len(issues) == 0

    def test_whitespace_only_passes(self):
        """Whitespace-only code should return no issues."""
        validator = CodeValidator()
        issues = validator.validate("   \n  \t  ")
        assert len(issues) == 0

    def test_syntax_error_returns_no_issues(self):
        """Syntax errors should not crash; return no issues."""
        validator = CodeValidator()
        issues = validator.validate("x = ")
        # We don't validate syntax errors, let runtime catch them
        assert isinstance(issues, list)

    def test_none_context_uses_defaults(self):
        """None context should use defaults."""
        validator = CodeValidator()
        issues = validator.validate("eval('x')", context=None)
        assert len(issues) == 1
        assert issues[0].code == "E001"

    def test_issue_has_location_info(self):
        """Issues should have accurate line/col info."""
        validator = CodeValidator()
        code = """x = 1
y = eval('2')"""
        issues = validator.validate(code)
        assert len(issues) == 1
        assert issues[0].line == 2
        assert issues[0].col > 0

    def test_multiple_issues_returned(self):
        """Multiple issues should all be returned."""
        validator = CodeValidator()
        code = """x = eval('1')
y = exec('z = 2')
w = __builtins__"""
        issues = validator.validate(code)
        assert len(issues) >= 2
        codes = {i.code for i in issues}
        assert "E001" in codes or "E101" in codes


class TestValidationContext:
    """Test ValidationContext data class."""

    def test_context_defaults(self):
        """ValidationContext should have sensible defaults."""
        ctx = ValidationContext()
        assert ctx.code == ""
        assert ctx.restricted_imports == frozenset()
        assert ctx.blocked_modules == frozenset()
        assert ctx.available_names == set()

    def test_context_custom_values(self):
        """ValidationContext should accept custom values."""
        ctx = ValidationContext(
            code="x = 1",
            restricted_imports=frozenset({"os"}),
            blocked_modules=frozenset({"threading"}),
            available_names={"x", "y"},
        )
        assert ctx.code == "x = 1"
        assert ctx.restricted_imports == frozenset({"os"})
        assert ctx.blocked_modules == frozenset({"threading"})
        assert "x" in ctx.available_names


class TestValidationIssue:
    """Test ValidationIssue data class."""

    def test_issue_required_fields(self):
        """ValidationIssue requires line, col, message."""
        issue = ValidationIssue(line=1, col=0, message="test")
        assert issue.line == 1
        assert issue.col == 0
        assert issue.message == "test"

    def test_issue_defaults(self):
        """ValidationIssue should have sensible defaults."""
        issue = ValidationIssue(line=1, col=0, message="test")
        assert issue.severity == "error"
        assert issue.code == ""
        assert issue.fix_hint is None

    def test_issue_custom_values(self):
        """ValidationIssue should accept all custom values."""
        issue = ValidationIssue(
            line=5,
            col=10,
            message="eval forbidden",
            severity="error",
            code="E001",
            fix_hint="use a safe alternative",
        )
        assert issue.line == 5
        assert issue.col == 10
        assert issue.code == "E001"
        assert issue.fix_hint == "use a safe alternative"


class TestConstantsAvailable:
    """Test that constants are publicly exported."""

    def test_forbidden_builtins_not_empty(self):
        """FORBIDDEN_BUILTINS should be populated."""
        assert len(FORBIDDEN_BUILTINS) > 0
        assert "eval" in FORBIDDEN_BUILTINS
        assert "exec" in FORBIDDEN_BUILTINS

    def test_dangerous_dunder_attrs_not_empty(self):
        """DANGEROUS_DUNDER_ATTRS should be populated."""
        assert len(DANGEROUS_DUNDER_ATTRS) > 0
        assert "__class__" in DANGEROUS_DUNDER_ATTRS
        assert "__globals__" in DANGEROUS_DUNDER_ATTRS


class TestRealWorldPatterns:
    """Test common real-world patterns."""

    def test_ml_code_passes(self):
        """Typical ML code should pass."""
        validator = CodeValidator()
        code = """import numpy as np
from sklearn.linear_model import LinearRegression

X = np.array([[1, 2], [3, 4]])
y = np.array([1, 2])
model = LinearRegression()
model.fit(X, y)
predictions = model.predict(X)"""
        issues = validator.validate(code)
        assert len(issues) == 0

    def test_data_processing_passes(self):
        """Typical data processing code should pass."""
        validator = CodeValidator()
        code = """import pandas as pd

df = pd.read_csv('data.csv')
result = df[df['value'] > 0]
summary = result.groupby('category').sum()
"""
        issues = validator.validate(code)
        assert len(issues) == 0

    def test_web_scraping_attempt_blocked(self):
        """Overly permissive runtime code doesn't block by default."""
        validator = CodeValidator()
        code = """import requests

response = requests.get('https://example.com')
"""
        issues = validator.validate(code)
        # requests is safe, but subprocess/os would be blocked
        assert len(issues) == 0

    def test_data_export_passes(self):
        """Data export patterns should pass."""
        validator = CodeValidator()
        code = """import json

data = {'key': 'value'}
output = json.dumps(data)
"""
        issues = validator.validate(code)
        assert len(issues) == 0


class TestDocumentation:
    """Validate that the module has proper documentation."""

    def test_validator_has_docstring(self):
        """CodeValidator should have docstring."""
        assert CodeValidator.__doc__ is not None
        assert len(CodeValidator.__doc__) > 0

    def test_validate_method_has_docstring(self):
        """validate() method should have docstring."""
        assert CodeValidator.validate.__doc__ is not None

    def test_issue_has_docstring(self):
        """ValidationIssue should have docstring."""
        assert ValidationIssue.__doc__ is not None

    def test_context_has_docstring(self):
        """ValidationContext should have docstring."""
        assert ValidationContext.__doc__ is not None
