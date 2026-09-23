"""The owner verdict reaches the daemon ONLY from a tunnel frame field, never a header."""

from adk import node_link


def test_a_caller_sent_actor_header_is_dropped_by_the_allowlist():
    out = node_link.NodeLink._local_headers(
        {"X-Aither-Link-Actor": "owner", "content-type": "application/json"}, "tok",
    )
    assert node_link.LINK_ACTOR_HEADER not in {k.lower() for k in out}
    assert out["authorization"] == "Bearer tok"
