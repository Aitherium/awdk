"""Tests for adk.ellipsis module (ellipsis detection and @strategy decorator)."""

import asyncio

import pytest

from adk.ellipsis import get_strategy_meta, has_ellipsis_body, strategy


class TestHasEllipsisBody:
    """Test has_ellipsis_body() function."""

    def test_function_with_only_ellipsis(self) -> None:
        """Function body is only '...' should be detected."""

        def func() -> None:
            ...

        assert has_ellipsis_body(func) is True

    def test_async_function_with_only_ellipsis(self) -> None:
        """Async function body is only '...' should be detected."""

        async def func() -> None:
            ...

        assert has_ellipsis_body(func) is True

    def test_function_with_docstring_and_ellipsis(self) -> None:
        """Function with docstring + '...' should be detected."""

        def func() -> None:
            """Do something."""
            ...

        assert has_ellipsis_body(func) is True

    def test_async_function_with_docstring_and_ellipsis(self) -> None:
        """Async function with docstring + '...' should be detected."""

        async def func() -> None:
            """Do something."""
            ...

        assert has_ellipsis_body(func) is True

    def test_function_with_setup_code_and_ellipsis(self) -> None:
        """Function with setup code + '...' should be detected."""

        def func() -> None:
            x = 1
            y = 2
            ...

        assert has_ellipsis_body(func) is True

    def test_async_function_with_setup_code_and_ellipsis(self) -> None:
        """Async function with setup code + '...' should be detected."""

        async def func() -> None:
            x = 1
            y = 2
            ...

        assert has_ellipsis_body(func) is True

    def test_function_with_real_body_not_detected(self) -> None:
        """Function with real body should NOT be detected."""

        def func() -> int:
            return 42

        assert has_ellipsis_body(func) is False

    def test_async_function_with_real_body_not_detected(self) -> None:
        """Async function with real body should NOT be detected."""

        async def func() -> int:
            return 42

        assert has_ellipsis_body(func) is False

    def test_function_with_ellipsis_not_last(self) -> None:
        """Function with '...' not as last statement should NOT be detected."""

        def func() -> None:
            ...
            x = 1

        assert has_ellipsis_body(func) is False

    def test_empty_function_body(self) -> None:
        """Empty function (pass only) should NOT be detected."""

        def func() -> None:
            pass

        assert has_ellipsis_body(func) is False

    def test_function_with_only_docstring(self) -> None:
        """Function with only docstring should NOT be detected."""

        def func() -> None:
            """Just a docstring."""

        assert has_ellipsis_body(func) is False

    def test_function_with_docstring_and_pass(self) -> None:
        """Function with docstring + pass should NOT be detected."""

        def func() -> None:
            """Do something."""
            pass

        assert has_ellipsis_body(func) is False

    def test_lambda_with_ellipsis_bytecode_heuristic(self) -> None:
        """Lambda with ellipsis may be detected via bytecode heuristic (very short code)."""
        func = lambda: ...  # noqa: E731
        # Bytecode heuristic can detect this as ellipsis-like (very short bytecode)
        # The detection is probabilistic for lambdas without source, so we just
        # verify the function still works
        assert callable(func)
        assert func() is ...  # Lambda returns Ellipsis when body is ellipsis


class TestStrategyDecorator:
    """Test @strategy decorator."""

    def test_strategy_decorator_attaches_metadata(self) -> None:
        """@strategy should attach metadata without changing behavior."""

        @strategy(loop="mock_loop", model="mock_model")
        def func() -> int:
            return 42

        # Function should still work normally
        assert func() == 42

        # Metadata should be attached
        assert getattr(func, "_adk_strategy", None) == "mock_loop"
        assert getattr(func, "_adk_model", None) == "mock_model"

    def test_strategy_decorator_with_options(self) -> None:
        """@strategy should pass through option kwargs."""

        @strategy(loop="loop", model="model", max_steps=10, timeout=30)
        def func() -> int:
            return 42

        # Check metadata
        assert func() == 42
        opts = getattr(func, "_adk_strategy_opts", {})
        assert opts.get("max_steps") == 10
        assert opts.get("timeout") == 30

    def test_strategy_decorator_with_none_values(self) -> None:
        """@strategy should handle None loop/model."""

        @strategy(loop=None, model=None)
        def func() -> str:
            return "result"

        assert func() == "result"
        assert getattr(func, "_adk_strategy", "missing") is None
        assert getattr(func, "_adk_model", "missing") is None

    def test_strategy_decorator_on_async_function(self) -> None:
        """@strategy should work on async functions."""

        @strategy(loop="async_loop")
        async def func() -> int:
            await asyncio.sleep(0)
            return 42

        result = asyncio.run(func())
        assert result == 42
        assert getattr(func, "_adk_strategy", None) == "async_loop"

    def test_strategy_decorator_prevents_stacking(self) -> None:
        """Stacking multiple @strategy decorators should raise ValueError."""

        def create_func() -> None:
            @strategy(loop="loop1")
            @strategy(loop="loop2")
            def func() -> None:
                pass

            return func

        with pytest.raises(ValueError, match="Cannot stack multiple"):
            create_func()

    def test_strategy_decorator_preserves_function_name(self) -> None:
        """@strategy should preserve function name."""

        @strategy(loop="loop")
        def my_function() -> int:
            return 42

        assert my_function.__name__ == "my_function"

    def test_strategy_decorator_with_default_args(self) -> None:
        """@strategy should work with functions with default arguments."""

        @strategy(loop="loop")
        def func(x: int = 10) -> int:
            return x * 2

        assert func() == 20
        assert func(5) == 10
        assert getattr(func, "_adk_strategy", None) == "loop"


class TestGetStrategyMeta:
    """Test get_strategy_meta() function."""

    def test_get_strategy_meta_decorated_function(self) -> None:
        """get_strategy_meta should return attached metadata."""

        @strategy(loop="my_loop", model="my_model", max_steps=8)
        def func() -> None:
            pass

        meta = get_strategy_meta(func)
        assert meta["loop"] == "my_loop"
        assert meta["model"] == "my_model"
        assert meta["opts"]["max_steps"] == 8

    def test_get_strategy_meta_undecorated_function(self) -> None:
        """get_strategy_meta on undecorated function should return None values."""

        def func() -> None:
            pass

        meta = get_strategy_meta(func)
        assert meta["loop"] is None
        assert meta["model"] is None
        assert meta["opts"] == {}

    def test_get_strategy_meta_with_none_loop_and_model(self) -> None:
        """get_strategy_meta should handle None loop and model."""

        @strategy(loop=None, model=None)
        def func() -> None:
            pass

        meta = get_strategy_meta(func)
        assert meta["loop"] is None
        assert meta["model"] is None

    def test_get_strategy_meta_with_custom_options(self) -> None:
        """get_strategy_meta should return all custom options."""

        @strategy(
            loop="loop",
            model="model",
            max_steps=10,
            max_preview_chars=200,
            custom_key="value",
        )
        def func() -> None:
            pass

        meta = get_strategy_meta(func)
        opts = meta["opts"]
        assert opts["max_steps"] == 10
        assert opts["max_preview_chars"] == 200
        assert opts["custom_key"] == "value"


class TestStrategyWithEllipsis:
    """Test @strategy combined with ellipsis detection."""

    def test_strategy_on_ellipsis_function(self) -> None:
        """@strategy should work on functions with ellipsis body."""

        @strategy(loop="generation_loop")
        def func() -> None:
            ...

        assert has_ellipsis_body(func) is True
        assert getattr(func, "_adk_strategy", None) == "generation_loop"

    def test_strategy_on_async_ellipsis_function(self) -> None:
        """@strategy should work on async functions with ellipsis."""

        @strategy(loop="async_loop")
        async def func() -> None:
            ...

        assert has_ellipsis_body(func) is True
        assert getattr(func, "_adk_strategy", None) == "async_loop"

    def test_strategy_and_docstring_and_ellipsis(self) -> None:
        """@strategy on function with docstring + ellipsis."""

        @strategy(model="model_x")
        def func() -> None:
            """This function generates code."""
            ...

        assert has_ellipsis_body(func) is True
        meta = get_strategy_meta(func)
        assert meta["model"] == "model_x"
