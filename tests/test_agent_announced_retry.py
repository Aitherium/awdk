"""Text-ReAct robustness (2026-10-05): a reply that only ANNOUNCES another attempt is
not the turn's answer, and tool arguments holding braces survive parsing."""

from adk.agent import announces_retry, parse_react_input

NL = chr(10)
BS = chr(92)


def test_announced_retries_are_caught():
    for text in (
        "It seems there was an issue retrieving the free space on your C drive. "
        "Let me try again to get the accurate information for you.",
        "That failed. I'll try a different command.",
        "Let me check the logs:",
        "I'm going to run it once more.",
        "Let me start by finding the path. I'll now list the files in this folder.",
    ):
        assert announces_retry(text), text


def test_finished_answers_are_not_retries():
    for text in (
        "Your C drive has 83.7 GB free of 1.86 TB.",
        "I tried three commands and none could read the disk; the volume may be locked.",
        "Let me know if you want more detail.",
        "",
    ):
        assert not announces_retry(text), text


def test_react_input_keeps_braces_inside_arguments():
    cmd = "Get-ChildItem | ForEach-Object { $_.Name }"
    reply = "ACTION: shell_exec" + NL + 'INPUT: {"command": "' + cmd + '"}' + NL
    assert parse_react_input(reply) == {"command": cmd}


def test_react_input_unreadable_is_none_not_empty():
    truncated = "ACTION: shell_exec" + NL + 'INPUT: {"command": "dir C:' + BS + BS + '"'
    assert parse_react_input(truncated) is None
    assert parse_react_input("ACTION: shell_exec") is None
    assert parse_react_input("ACTION: x" + NL + "INPUT: {}") == {}
