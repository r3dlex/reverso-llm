"""Unit tests for daemon startup imports."""


def test_session_daemon_imports():
    from reverso.daemon import session_daemon

    assert session_daemon.app.title == "reverso-daemon"
