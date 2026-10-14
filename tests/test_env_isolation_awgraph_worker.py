"""adk.server.main() setdefaults ADK_AWGRAPH_WORKER for the daemon. A test that ran
main() leaked it into every later test, and test_crystal_names_why_awgraph_is_unavailable
then spawned a real graph worker and failed only in a full run (2026-10-14). The conftest
isolation strips it before each test; these two run in file order to prove it.
"""

import os


def test_a_test_that_runs_server_main_leaks_the_flag():
    os.environ["ADK_AWGRAPH_WORKER"] = "1"  # exactly what server.main() does


def test_the_next_test_starts_without_it():
    assert "ADK_AWGRAPH_WORKER" not in os.environ
