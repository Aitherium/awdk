"""Tests for the tool system."""

import pytest
from adk.tools import ToolRegistry, tool, get_global_registry, _extract_parameters


class TestToolRegistry:
    def test_register_function(self):
        reg = ToolRegistry()
        def my_fn(x: str) -> str:
            """Do something."""
            return x
        td = reg.register(my_fn)
        assert td.name == "my_fn"
        assert "Do something" in td.description

    def test_register_with_custom_name(self):
        reg = ToolRegistry()
        def my_fn(): pass
        td = reg.register(my_fn, name="custom", description="Custom tool")
        assert td.name == "custom"
        assert td.description == "Custom tool"

    def test_list_tools(self):
        reg = ToolRegistry()
        reg.register(lambda: None, name="a", description="A")
        reg.register(lambda: None, name="b", description="B")
        assert len(reg.list_tools()) == 2

    def test_get_tool(self):
        reg = ToolRegistry()
        reg.register(lambda: None, name="test", description="Test")
        assert reg.get("test") is not None
        assert reg.get("missing") is None

    def test_openai_format(self):
        reg = ToolRegistry()
        def search(query: str) -> str:
            """Search for things."""
            return query
        reg.register(search)
        fmt = reg.to_openai_format()
        assert len(fmt) == 1
        assert fmt[0]["type"] == "function"
        assert fmt[0]["function"]["name"] == "search"
        assert "query" in fmt[0]["function"]["parameters"]["properties"]

    @pytest.mark.asyncio
    async def test_execute_sync(self):
        reg = ToolRegistry()
        def add(a: int, b: int) -> str:
            return str(a + b)
        reg.register(add)
        result = await reg.execute("add", {"a": 2, "b": 3})
        assert result == "5"

    @pytest.mark.asyncio
    async def test_execute_async(self):
        reg = ToolRegistry()
        async def greet(name: str) -> str:
            return f"Hello {name}"
        reg.register(greet)
        result = await reg.execute("greet", {"name": "world"})
        assert result == "Hello world"

    @pytest.mark.asyncio
    async def test_execute_unknown_tool(self):
        reg = ToolRegistry()
        result = await reg.execute("nonexistent", {})
        assert "error" in result.lower()

    @pytest.mark.asyncio
    async def test_execute_error_handling(self):
        reg = ToolRegistry()
        def broken():
            raise ValueError("oops")
        reg.register(broken)
        result = await reg.execute("broken", {})
        assert "oops" in result

    @pytest.mark.asyncio
    async def test_execute_returns_dict(self):
        reg = ToolRegistry()
        def info() -> dict:
            return {"key": "value"}
        reg.register(info)
        result = await reg.execute("info", {})
        assert '"key"' in result


class TestToolDecorator:
    def test_basic_decorator(self):
        @tool
        def my_tool(x: str) -> str:
            """A test tool."""
            return x

        assert hasattr(my_tool, "_tool_def")
        assert my_tool._tool_def.name == "my_tool"

    def test_decorator_with_args(self):
        @tool(name="custom_name", description="Custom desc")
        def my_tool(x: str) -> str:
            return x

        assert my_tool._tool_def.name == "custom_name"
        assert my_tool._tool_def.description == "Custom desc"

    def test_global_registry_populated(self):
        @tool
        def unique_test_tool_xyz() -> str:
            """Unique tool."""
            return "ok"

        reg = get_global_registry()
        assert reg.get("unique_test_tool_xyz") is not None


class TestParameterExtraction:
    def test_string_param(self):
        def fn(query: str): pass
        params = _extract_parameters(fn)
        assert params["properties"]["query"]["type"] == "string"
        assert "query" in params.get("required", [])

    def test_int_param(self):
        def fn(count: int): pass
        params = _extract_parameters(fn)
        assert params["properties"]["count"]["type"] == "integer"

    def test_float_param(self):
        def fn(score: float): pass
        params = _extract_parameters(fn)
        assert params["properties"]["score"]["type"] == "number"

    def test_bool_param(self):
        def fn(flag: bool): pass
        params = _extract_parameters(fn)
        assert params["properties"]["flag"]["type"] == "boolean"

    def test_default_value_not_required(self):
        def fn(x: str, y: int = 5): pass
        params = _extract_parameters(fn)
        assert "x" in params["required"]
        assert "y" not in params.get("required", [])

    def test_list_param(self):
        def fn(items: list[str]): pass
        params = _extract_parameters(fn)
        assert params["properties"]["items"]["type"] == "array"

    def test_self_excluded(self):
        def fn(self, x: str): pass
        params = _extract_parameters(fn)
        assert "self" not in params["properties"]
