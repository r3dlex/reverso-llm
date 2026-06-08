"""Integration tests for session continuity (US-013).

These tests require a live Reverso gateway and session daemon running locally.
They are skipped in CI unless REVERSO_INTEGRATION=1 is set.

Run manually:
    REVERSO_INTEGRATION=1 pytest tests/integration/test_session_continuity.py -v

Gateway must be listening on http://127.0.0.1:64946 and the session daemon
must have its UDS at ~/Library/Application Support/reverso/daemon.sock.
"""

from __future__ import annotations

import os

import pytest

# Skip all tests in this module unless running integration tests explicitly.
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("REVERSO_INTEGRATION"),
        reason="requires live gateway (set REVERSO_INTEGRATION=1 to run)",
    ),
]

GATEWAY_URL = os.environ.get("REVERSO_GATEWAY_URL", "http://127.0.0.1:64946")
# A workspace directory that exists on the developer machine.
TEST_WORKSPACE = os.environ.get(
    "REVERSO_TEST_WORKSPACE", os.path.expanduser("~/tmp/reverso-test")
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _chat(model: str, message: str, workspace: str) -> dict:
    """Send one chat completion request and return the parsed JSON response."""
    import httpx

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": message}],
        "x_gateway": {"workspace": workspace},
    }
    resp = httpx.post(
        f"{GATEWAY_URL}/v1/chat/completions",
        json=payload,
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Test plan: multi-turn session continuity
# ---------------------------------------------------------------------------


class TestMultiTurnSessionContinuity:
    """Send three sequential prompts and verify session_id is stable.

    Test plan:
    1. Turn 1 - introduce a name.
       Prompt: "My name is AlphaTest. Please acknowledge."
       Assert: response contains "AlphaTest" or similar acknowledgement.
       Assert: x_gateway.session_id is not None.

    2. Turn 2 - ask the model to recall the name.
       Prompt: "What is my name?"
       Assert: response contains "AlphaTest".
       Assert: x_gateway.session_id == session_id from Turn 1.
       (Same session_id proves the daemon reused the existing session.)

    3. Turn 3 - context check with a different question.
       Prompt: "Repeat my name one more time."
       Assert: response contains "AlphaTest".
       Assert: x_gateway.session_id == session_id from Turn 1.
    """

    def test_three_turn_claude(self) -> None:
        """Three-turn session continuity using claude-sonnet-4-6."""
        model = "claude-sonnet-4-6"
        workspace = TEST_WORKSPACE
        os.makedirs(workspace, exist_ok=True)

        # Turn 1
        r1 = _chat(model, "My name is AlphaTest. Please acknowledge.", workspace)
        assert "choices" in r1
        text1 = r1["choices"][0]["message"]["content"]
        assert "AlphaTest" in text1, f"Turn 1 did not acknowledge name: {text1!r}"
        sid1 = r1.get("x_gateway", {}).get("session_id")
        assert sid1 is not None, "Turn 1 returned no session_id"

        # Turn 2
        r2 = _chat(model, "What is my name?", workspace)
        text2 = r2["choices"][0]["message"]["content"]
        assert "AlphaTest" in text2, f"Turn 2 did not recall name: {text2!r}"
        sid2 = r2.get("x_gateway", {}).get("session_id")
        assert sid2 == sid1, f"session_id changed between turns: {sid1!r} -> {sid2!r}"

        # Turn 3
        r3 = _chat(model, "Repeat my name one more time.", workspace)
        text3 = r3["choices"][0]["message"]["content"]
        assert "AlphaTest" in text3, f"Turn 3 did not recall name: {text3!r}"
        sid3 = r3.get("x_gateway", {}).get("session_id")
        assert sid3 == sid1, f"session_id changed on turn 3: {sid1!r} -> {sid3!r}"

    def test_three_turn_codex(self) -> None:
        """Three-turn session continuity using gpt-4.1 (Codex CLI)."""
        model = "gpt-4.1"
        workspace = TEST_WORKSPACE
        os.makedirs(workspace, exist_ok=True)

        r1 = _chat(model, "My name is BetaTest. Please acknowledge.", workspace)
        text1 = r1["choices"][0]["message"]["content"]
        assert "BetaTest" in text1, f"Turn 1 did not acknowledge name: {text1!r}"
        sid1 = r1.get("x_gateway", {}).get("session_id")
        assert sid1 is not None, "Turn 1 returned no session_id"

        r2 = _chat(model, "What is my name?", workspace)
        text2 = r2["choices"][0]["message"]["content"]
        assert "BetaTest" in text2, f"Turn 2 did not recall name: {text2!r}"
        sid2 = r2.get("x_gateway", {}).get("session_id")
        assert sid2 == sid1, f"session_id changed between turns: {sid1!r} -> {sid2!r}"

        r3 = _chat(model, "Repeat my name one more time.", workspace)
        text3 = r3["choices"][0]["message"]["content"]
        assert "BetaTest" in text3, f"Turn 3 did not recall name: {text3!r}"
        sid3 = r3.get("x_gateway", {}).get("session_id")
        assert sid3 == sid1, f"session_id changed on turn 3: {sid1!r} -> {sid3!r}"


# ---------------------------------------------------------------------------
# Test plan: idle timeout / session recycling
# ---------------------------------------------------------------------------


class TestIdleSessionRecycling:
    """Verify that idle sessions are recycled after the timeout elapses.

    Test plan (manual / long-running - not suitable for automated CI):
    1. Send one prompt to create a session; capture session_id S1.
    2. Wait 35 minutes (idle_threshold is 30 minutes).
    3. Send another prompt to the same workspace.
    4. Assert that x_gateway.session_id is different from S1.
       (Different session_id means the daemon created a fresh session after
        recycling the idle one, confirming the recycler ran.)

    Implementation note: this test is marked as skip with a descriptive
    reason because automated runs cannot wait 35 minutes.  Run manually
    by commenting out the skip decorator and waiting.
    """

    @pytest.mark.skip(
        reason="idle timeout test requires 35+ minute wait - run manually"
    )
    def test_idle_session_recycled(self) -> None:
        """Session created, left idle 35 min, then a new turn creates a new session."""
        import time

        model = "claude-sonnet-4-6"
        workspace = TEST_WORKSPACE
        os.makedirs(workspace, exist_ok=True)

        r1 = _chat(model, "Starting idle timeout test.", workspace)
        sid1 = r1.get("x_gateway", {}).get("session_id")
        assert sid1 is not None

        # Wait for idle threshold + sweeper cycle (30 min idle + up to 60 min sweep).
        # In practice, reduce session_idle_timeout_minutes in config.yaml to 1 and
        # RecycleSweeper._SWEEP_INTERVAL_SECONDS to 120 for faster manual testing.
        wait_seconds = 35 * 60
        print(f"Waiting {wait_seconds}s for idle timeout...")
        time.sleep(wait_seconds)

        r2 = _chat(model, "Are you still there?", workspace)
        sid2 = r2.get("x_gateway", {}).get("session_id")
        assert sid2 != sid1, (
            f"Expected a new session_id after idle recycling, "
            f"but got the same: {sid1!r}"
        )


# ---------------------------------------------------------------------------
# Test plan: daemon fallback when daemon is unavailable
# ---------------------------------------------------------------------------


class TestDaemonFallback:
    """Verify that providers fall back to stateless mode when daemon is down.

    Test plan:
    1. Stop the session daemon (kill or unload the launchd agent).
    2. Send a prompt via the gateway.
    3. Assert: response contains valid assistant text.
    4. Assert: x_gateway.warnings contains a "daemon_unavailable" entry.
    5. Assert: x_gateway.session_id is not None (stateless mode returns
       the one-shot session_id from the CLI).
    """

    @pytest.mark.skip(reason="fallback test requires manually stopping the daemon")
    def test_stateless_fallback_when_daemon_down(self) -> None:
        """Gateway returns a valid response even if the daemon is not running."""
        r = _chat("claude-sonnet-4-6", "Say hello in one word.", TEST_WORKSPACE)
        text = r["choices"][0]["message"]["content"]
        assert text.strip(), "Empty response during fallback"
        warnings = r.get("x_gateway", {}).get("warnings", [])
        assert any("daemon_unavailable" in w for w in warnings), (
            f"Expected daemon_unavailable warning, got: {warnings!r}"
        )
