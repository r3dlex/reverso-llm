"""Tests for nonce-bound live-proof request attribution."""

from reverso.protocols.proof_correlation import (
    ProofCorrelationStore,
    proof_nonce_from_headers,
)


def test_nonce_bound_counts_cannot_be_replaced_by_unrelated_traffic() -> None:
    store = ProofCorrelationStore()
    controlled = "a" * 64
    unrelated = "b" * 64

    store.record(unrelated, "responses")
    store.record(unrelated, "responses")
    store.record(controlled, "responses")

    assert store.consume(controlled) == {"responses": 1, "messages": 0}
    assert store.consume(unrelated) == {"responses": 2, "messages": 0}


def test_proof_header_rejects_invalid_or_duplicate_nonce() -> None:
    nonce = b"a" * 64
    assert (
        proof_nonce_from_headers([(b"x-reverso-kimi-proof", nonce)]) == nonce.decode()
    )
    assert proof_nonce_from_headers([(b"x-reverso-kimi-proof", b"short")]) is None
    assert (
        proof_nonce_from_headers(
            [(b"x-reverso-kimi-proof", nonce), (b"x-reverso-kimi-proof", nonce)]
        )
        is None
    )
