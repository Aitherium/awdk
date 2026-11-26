"""Bonsai-2 (Qwen-coder template) writes tool calls as XML, not JSON; they must parse."""
from adk.llm.base import extract_tool_calls_from_text


def test_qwen_xml_calls_parse_with_typed_params():
    text = ("I'll read it.\n<tool_call>\n<function=file_read>\n<parameter=path>\n/d/x.py\n"
            "</parameter>\n<parameter=start_line>\n125\n</parameter>\n</function>\n</tool_call>\n"
            "<tool_call><function=file_edit><parameter=path>/d/x.py</parameter>"
            "<parameter=old>a = 1</parameter></function></tool_call>")
    calls, cleaned = extract_tool_calls_from_text(text)
    assert [c.name for c in calls] == ["file_read", "file_edit"]
    assert calls[0].arguments == {"path": "/d/x.py", "start_line": 125}
    assert calls[1].arguments["old"] == "a = 1"
    assert cleaned == "I'll read it."


def test_plain_text_is_not_a_call():
    calls, cleaned = extract_tool_calls_from_text("no tools here, function= mentioned")
    assert calls == [] and cleaned == "no tools here, function= mentioned"
