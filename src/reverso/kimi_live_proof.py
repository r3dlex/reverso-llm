"""Fail-closed credentialed Kimi proof orchestration and manifest validation."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import subprocess
import time
import tomllib
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx

LIVE_PROOF_ENV = "REVERSO_KIMI_LIVE_PROOF"
REQUIRED_CHECK_IDS: tuple[str, ...] = (
    "model_discovery",
    "responses_continuity",
    "messages",
    "codex",
    "claude_code",
    "headroom",
    "redaction",
    "loopback",
)
ALLOWED_TOP_LEVEL_KEYS = frozenset(
    {"schema_version", "provider", "loopback", "overall", "checks"}
)
ALLOWED_CHECK_KEYS = frozenset(
    {
        "id",
        "result",
        "provider",
        "route",
        "model_id",
        "response_id_shape",
        "duration_ms",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "status",
        "model_discovery_source",
        "headroom_deltas",
        "error_category",
    }
)
FORBIDDEN_FIELD_FRAGMENTS = (
    "authorization",
    "access_token",
    "refresh_token",
    "api_key",
    "auth_token",
    "oauth_token",
    "prompt",
    "response_text",
    "raw_body",
    "raw_log",
    "headers",
)
_SECRET_VALUE_RE = re.compile(
    r"(?i)(authorization\s*:|bearer\s+[a-z0-9._~+/=-]+|"
    r"access[_ -]?token|refresh[_ -]?token|api[_ -]?key|oauth[_ -]?token|sk-[a-z0-9])"
)
_RESPONSE_ID_RE = re.compile(r"^resp_[A-Za-z0-9_-]+$")
_CONTROLLED_PROMPT = (
    "Reply with exactly KIMI_PROOF_OK after reading this controlled proof request."
)
_PROOF_HEADER = "x-reverso-kimi-proof"
_KIMI_CREDENTIAL_FIELDS = frozenset(
    {"access_token", "refresh_token", "token", "api_key", "auth_token"}
)


def _client_challenge() -> tuple[str, str]:
    """Return a prompt and exact answer where the answer is absent from the prompt."""
    while True:
        token = secrets.token_hex(16)
        expected = token[::-1]
        prompt = (
            "Reverse this lowercase hexadecimal token and output only the result: "
            f"{token}"
        )
        if expected not in prompt:
            return prompt, expected


class ProofFailure(RuntimeError):
    """A secret-free categorized proof failure."""

    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


class ProofProbe(Protocol):
    """Injectable live operations used by the manifest orchestrator."""

    def model_discovery(self) -> dict[str, Any]: ...

    def responses_continuity(self, model_id: str | None) -> dict[str, Any]: ...

    def messages(self, model_id: str | None) -> dict[str, Any]: ...

    def codex(self, model_id: str | None) -> dict[str, Any]: ...

    def claude_code(self, model_id: str | None) -> dict[str, Any]: ...

    def headroom(self) -> dict[str, Any]: ...

    def redaction(self) -> dict[str, Any]: ...

    def loopback(self) -> dict[str, Any]: ...


def require_live_opt_in(env: Mapping[str, str] | None = None) -> None:
    """Require the exact opt-in value before any credential or network access."""
    source = os.environ if env is None else env
    if source.get(LIVE_PROOF_ENV) != "1":
        raise ProofFailure("live_proof_not_enabled")


def _failure_check(check_id: str, category: str) -> dict[str, Any]:
    return {"id": check_id, "result": "fail", "error_category": category}


def _passed_check(check_id: str, observation: Mapping[str, Any]) -> dict[str, Any]:
    row = {"id": check_id, "result": "pass", **dict(observation)}
    if not set(row) <= ALLOWED_CHECK_KEYS:
        raise ProofFailure("forbidden_evidence_field")
    return row


def _validate_passed_row(check_id: str, row: Mapping[str, Any]) -> None:
    if check_id == "model_discovery" and row.get("model_discovery_source") != "live":
        raise ProofFailure("fallback_model_discovery")
    if check_id == "headroom":
        deltas = row.get("headroom_deltas")
        if (
            not isinstance(deltas, dict)
            or set(deltas) != {"responses", "messages"}
            or deltas != {"responses": 2, "messages": 1}
        ):
            raise ProofFailure("non_attributable_headroom")
    if check_id == "loopback":
        route = row.get("route")
        parsed = urlparse(route) if isinstance(route, str) else None
        if (
            parsed is None
            or parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.port != 64946
        ):
            raise ProofFailure("non_loopback_route")


def run_proof(probe: ProofProbe) -> dict[str, Any]:
    """Run every required lane and return a validated schema-v1 manifest."""
    checks: list[dict[str, Any]] = []
    model_id: str | None = None
    lanes: tuple[tuple[str, Callable[[], dict[str, Any]]], ...] = (
        ("model_discovery", probe.model_discovery),
        ("responses_continuity", lambda: probe.responses_continuity(model_id)),
        ("messages", lambda: probe.messages(model_id)),
        ("codex", lambda: probe.codex(model_id)),
        ("claude_code", lambda: probe.claude_code(model_id)),
        ("headroom", probe.headroom),
        ("redaction", probe.redaction),
        ("loopback", probe.loopback),
    )
    for check_id, operation in lanes:
        try:
            observation = operation()
            row = _passed_check(check_id, observation)
            _validate_passed_row(check_id, row)
            if check_id == "model_discovery":
                discovered = row.get("model_id")
                if isinstance(discovered, str) and discovered:
                    model_id = discovered
        except ProofFailure as exc:
            row = _failure_check(check_id, exc.category)
        except Exception:
            row = _failure_check(check_id, "unexpected_failure")
        checks.append(row)

    manifest: dict[str, Any] = {
        "schema_version": 1,
        "provider": "kimi",
        "loopback": next(
            (row["result"] == "pass" for row in checks if row["id"] == "loopback"),
            False,
        ),
        "overall": "pass" if all(row["result"] == "pass" for row in checks) else "fail",
        "checks": checks,
    }
    validate_manifest(manifest)
    return manifest


def validate_manifest(manifest: Mapping[str, Any]) -> None:
    """Reject incomplete, unsafe, fallback, or non-attributable evidence."""
    if set(manifest) != ALLOWED_TOP_LEVEL_KEYS:
        raise ProofFailure("invalid_manifest_fields")
    if manifest.get("schema_version") != 1 or manifest.get("provider") != "kimi":
        raise ProofFailure("invalid_manifest_identity")
    checks = manifest.get("checks")
    if not isinstance(checks, list):
        raise ProofFailure("invalid_manifest_checks")
    ids = [row.get("id") for row in checks if isinstance(row, dict)]
    if tuple(ids) != REQUIRED_CHECK_IDS or len(set(ids)) != len(REQUIRED_CHECK_IDS):
        raise ProofFailure("invalid_required_checks")
    for row in checks:
        if not isinstance(row, dict) or not set(row) <= ALLOWED_CHECK_KEYS:
            raise ProofFailure("forbidden_evidence_field")
        if row.get("result") not in {"pass", "fail"}:
            raise ProofFailure("invalid_check_result")
        for key in row:
            lowered = key.lower()
            if any(fragment in lowered for fragment in FORBIDDEN_FIELD_FRAGMENTS):
                raise ProofFailure("forbidden_evidence_field")
    by_id = {row["id"]: row for row in checks}
    if by_id["model_discovery"].get("model_discovery_source") != "live":
        if by_id["model_discovery"].get("result") == "pass":
            raise ProofFailure("fallback_model_discovery")
    deltas = by_id["headroom"].get("headroom_deltas")
    if by_id["headroom"].get("result") == "pass" and (
        not isinstance(deltas, dict)
        or set(deltas) != {"responses", "messages"}
        or deltas != {"responses": 2, "messages": 1}
    ):
        raise ProofFailure("non_attributable_headroom")
    serialized = json.dumps(manifest, sort_keys=True)
    if _SECRET_VALUE_RE.search(serialized):
        raise ProofFailure("redaction_failure")
    expected_pass = all(row.get("result") == "pass" for row in checks)
    if manifest.get("overall") != ("pass" if expected_pass else "fail"):
        raise ProofFailure("invalid_overall_result")
    if manifest.get("loopback") is not (by_id["loopback"].get("result") == "pass"):
        raise ProofFailure("invalid_loopback_result")


def write_manifest(path: Path, manifest: Mapping[str, Any]) -> None:
    """Atomically write validated allowlisted evidence with mode 0600."""
    validate_manifest(manifest)
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        fd = os.open(temp, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temp, path)
        path.chmod(0o600)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


class HttpLiveProofProbe:
    """Trusted-machine Kimi proof implementation with redacted observations."""

    def __init__(
        self,
        *,
        base_url: str = "http://127.0.0.1:64946",
        timeout: float = 30.0,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        log_paths: Sequence[Path] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.runner = runner
        self.log_paths = tuple(
            log_paths
            if log_paths is not None
            else (
                Path.home() / "Library/Logs/reverso/proxy.stdout.log",
                Path.home() / "Library/Logs/reverso/proxy.stderr.log",
            )
        )
        self._captured_text: list[str] = []
        self._headroom_deltas: dict[str, int] = {}
        self._live_models: tuple[str, ...] = ()
        self._credential_sentinels: set[str] = set()
        self._log_checkpoints = {
            path: self._log_checkpoint(path) for path in self.log_paths
        }

    @staticmethod
    def _log_checkpoint(path: Path) -> tuple[int, int, int, str] | None:
        try:
            stat_result = path.stat()
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            return None
        return (
            stat_result.st_dev,
            stat_result.st_ino,
            stat_result.st_size,
            digest,
        )

    def _read_log_window(self, path: Path) -> str:
        checkpoint = self._log_checkpoints[path]
        try:
            stat_result = path.stat()
            start = 0
            if checkpoint is not None:
                old_device, old_inode, old_size, old_digest = checkpoint
                if (
                    stat_result.st_dev == old_device
                    and stat_result.st_ino == old_inode
                    and stat_result.st_size >= old_size
                ):
                    with path.open("rb") as handle:
                        unchanged = hashlib.sha256(handle.read(old_size)).hexdigest()
                    if unchanged == old_digest:
                        start = old_size
            with path.open("r", encoding="utf-8") as handle:
                handle.seek(start)
                return handle.read()
        except (OSError, UnicodeError) as exc:
            raise ProofFailure("log_scan_unavailable") from exc

    def _json_request(
        self,
        method: str,
        route: str,
        *,
        payload: dict[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> tuple[dict[str, Any], int]:
        self._require_loopback()
        started = time.monotonic()
        try:
            response = httpx.request(
                method,
                f"{self.base_url}{route}",
                json=payload,
                headers=dict(headers) if headers is not None else None,
                timeout=self.timeout,
            )
            self._captured_text.append(response.text)
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ProofFailure("gateway_request_failed") from exc
        if not isinstance(body, dict):
            raise ProofFailure("invalid_gateway_payload")
        return body, round((time.monotonic() - started) * 1000)

    def _correlated_headroom(self, nonce: str, lane: str) -> int:
        body, _ = self._json_request("GET", f"/usage/headroom/proof/{nonce}")
        proof = body.get("proof")
        if not isinstance(proof, dict) or set(proof) != {"responses", "messages"}:
            raise ProofFailure("invalid_headroom_metrics")
        other = "messages" if lane == "responses" else "responses"
        value = proof.get(lane)
        if not isinstance(value, int) or proof.get(other) != 0:
            raise ProofFailure("non_attributable_headroom")
        return value

    @staticmethod
    def _model(model_id: str | None) -> str:
        if not isinstance(model_id, str) or not model_id or "/" in model_id:
            raise ProofFailure("missing_live_model")
        return model_id

    def _require_loopback(self) -> None:
        parsed = urlparse(self.base_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.port != 64946
        ):
            raise ProofFailure("non_loopback_route")

    def model_discovery(self) -> dict[str, Any]:
        self._credential_sentinels.update(self._load_credential_sentinels())
        nonce = secrets.token_hex(32)
        body, duration = self._json_request(
            "GET", "/kimi/v1/models", headers={_PROOF_HEADER: nonce}
        )
        proof = body.get("proof")
        upstream_status = (
            proof.get("authenticated_upstream_status")
            if isinstance(proof, dict)
            else None
        )
        if (
            body.get("model_discovery_source") != "live"
            or not isinstance(proof, dict)
            or proof.get("nonce") != nonce
            or not isinstance(upstream_status, int)
            or not 200 <= upstream_status < 300
            or proof.get("payload_validated") is not True
        ):
            raise ProofFailure("fallback_model_discovery")
        data = body.get("data")
        ids = (
            [row.get("id") for row in data if isinstance(row, dict)]
            if isinstance(data, list)
            else []
        )
        models = [
            value
            for value in ids
            if isinstance(value, str) and value and "/" not in value
        ]
        if not models:
            raise ProofFailure("missing_live_model")
        self._live_models = tuple(models)
        model = "kimi-k2.5" if "kimi-k2.5" in models else models[0]
        return {
            "provider": "kimi",
            "route": "/kimi/v1/models",
            "model_id": model,
            "model_discovery_source": "live",
            "duration_ms": duration,
            "status": 200,
        }

    def responses_continuity(self, model_id: str | None) -> dict[str, Any]:
        model = self._model(model_id)
        nonce = secrets.token_hex(32)
        proof_headers = {_PROOF_HEADER: nonce}
        first, first_ms = self._json_request(
            "POST",
            "/kimi/v1/responses",
            payload={"model": model, "input": _CONTROLLED_PROMPT},
            headers=proof_headers,
        )
        response_id = first.get("id")
        if not isinstance(response_id, str) or not _RESPONSE_ID_RE.match(response_id):
            raise ProofFailure("invalid_response_id")
        second, second_ms = self._json_request(
            "POST",
            "/kimi/v1/responses",
            payload={
                "model": model,
                "input": _CONTROLLED_PROMPT,
                "previous_response_id": response_id,
            },
            headers=proof_headers,
        )
        if second.get("previous_response_id") != response_id:
            raise ProofFailure("continuity_failed")
        self._headroom_deltas["responses"] = self._correlated_headroom(
            nonce, "responses"
        )
        usage = second.get("usage") if isinstance(second.get("usage"), dict) else {}
        return {
            "provider": "kimi",
            "route": "/kimi/v1/responses",
            "model_id": model,
            "response_id_shape": "resp_*",
            "duration_ms": first_ms + second_ms,
            "input_tokens": int(usage.get("input_tokens", 0)),
            "output_tokens": int(usage.get("output_tokens", 0)),
            "total_tokens": int(usage.get("total_tokens", 0)),
            "status": 200,
        }

    def messages(self, model_id: str | None) -> dict[str, Any]:
        model = self._model(model_id)
        nonce = secrets.token_hex(32)
        body, duration = self._json_request(
            "POST",
            "/kimi/v1/messages",
            payload={
                "model": model,
                "max_tokens": 64,
                "messages": [{"role": "user", "content": _CONTROLLED_PROMPT}],
            },
            headers={_PROOF_HEADER: nonce},
        )
        if body.get("type") != "message":
            raise ProofFailure("invalid_messages_payload")
        self._headroom_deltas["messages"] = self._correlated_headroom(nonce, "messages")
        usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
        return {
            "provider": "kimi",
            "route": "/kimi/v1/messages",
            "model_id": model,
            "duration_ms": duration,
            "input_tokens": int(usage.get("input_tokens", 0)),
            "output_tokens": int(usage.get("output_tokens", 0)),
            "status": 200,
        }

    def _subprocess(
        self, argv: list[str], *, env: Mapping[str, str] | None = None
    ) -> tuple[subprocess.CompletedProcess[str], int]:
        started = time.monotonic()
        try:
            result = self.runner(
                argv,
                check=False,
                capture_output=True,
                text=True,
                timeout=180,
                env=dict(env) if env is not None else None,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ProofFailure("client_unavailable") from exc
        self._captured_text.extend((result.stdout, result.stderr))
        if result.returncode != 0:
            raise ProofFailure("client_failed")
        return result, round((time.monotonic() - started) * 1000)

    @staticmethod
    def _codex_completed_marker(stdout: str, expected: str) -> bool:
        saw_completed = False
        texts: list[str] = []
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") == "turn.completed":
                saw_completed = True
            item = event.get("item")
            if (
                event.get("type") == "item.completed"
                and isinstance(item, dict)
                and item.get("type") == "agent_message"
                and item.get("role", "assistant") == "assistant"
                and isinstance(item.get("text"), str)
            ):
                texts.append(item["text"])
        return saw_completed and len(texts) == 1 and texts[0].strip() == expected

    def codex(self, model_id: str | None) -> dict[str, Any]:
        model = self._model(model_id)
        config_path = Path(
            os.environ.get("REVERSO_CODEX_CONFIG", Path.home() / ".codex/config.toml")
        ).expanduser()
        if config_path.name != "config.toml":
            raise ProofFailure("codex_profile_unavailable")
        profile_path = config_path.with_name("kimi.config.toml")
        try:
            config_text = config_path.read_text(encoding="utf-8")
            config = tomllib.loads(config_text)
            profile_text = profile_path.read_text(encoding="utf-8")
            profile = tomllib.loads(profile_text)
            catalog_path = Path(profile["model_catalog_json"]).expanduser()
            catalog_text = catalog_path.read_text(encoding="utf-8")
            catalog = json.loads(catalog_text)
        except (OSError, KeyError, ValueError, tomllib.TOMLDecodeError) as exc:
            raise ProofFailure("codex_profile_unavailable") from exc
        self._captured_text.extend((config_text, profile_text, catalog_text))
        providers = config.get("model_providers") if isinstance(config, dict) else None
        provider = (
            providers.get("reverso_kimi") if isinstance(providers, dict) else None
        )
        catalog_models = catalog.get("models") if isinstance(catalog, dict) else None
        catalog_slugs = (
            {row.get("slug") for row in catalog_models if isinstance(row, dict)}
            if isinstance(catalog_models, list)
            else set()
        )
        if (
            profile.get("model") != model
            or profile.get("model_provider") != "reverso_kimi"
            or not isinstance(provider, dict)
            or provider.get("base_url") != "http://127.0.0.1:64946/kimi/v1"
            or provider.get("wire_api") != "responses"
            or not self._live_models
            or catalog_slugs != set(self._live_models)
        ):
            raise ProofFailure("codex_profile_mismatch")
        env = os.environ.copy()
        env["CODEX_HOME"] = str(config_path.parent)
        prompt, expected = _client_challenge()
        result, duration = self._subprocess(
            [
                "codex",
                "exec",
                "-p",
                "kimi",
                "--skip-git-repo-check",
                "--json",
                prompt,
            ],
            env=env,
        )
        if not self._codex_completed_marker(result.stdout, expected):
            raise ProofFailure("client_completion_invalid")
        return {
            "provider": "kimi",
            "route": "/kimi/v1/responses",
            "model_id": model,
            "duration_ms": duration,
            "status": 0,
        }

    def claude_code(self, model_id: str | None) -> dict[str, Any]:
        model = self._model(model_id)
        env = os.environ.copy()
        env["REVERSO_KIMI_MODEL"] = model
        prompt, expected = _client_challenge()
        result, duration = self._subprocess(
            [
                "scripts/claude-kimi.sh",
                "--print",
                "--output-format",
                "json",
                prompt,
            ],
            env=env,
        )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ProofFailure("client_completion_invalid") from exc
        if (
            not isinstance(payload, dict)
            or payload.get("type") != "result"
            or payload.get("subtype") != "success"
            or payload.get("is_error") is not False
            or not isinstance(payload.get("result"), str)
            or payload["result"].strip() != expected
        ):
            raise ProofFailure("client_completion_invalid")
        return {
            "provider": "kimi",
            "route": "/kimi/v1/messages",
            "model_id": model,
            "duration_ms": duration,
            "status": 0,
        }

    def headroom(self) -> dict[str, Any]:
        if self._headroom_deltas != {"responses": 2, "messages": 1}:
            raise ProofFailure("non_attributable_headroom")
        return {"provider": "kimi", "headroom_deltas": dict(self._headroom_deltas)}

    def redaction(self) -> dict[str, Any]:
        for path in self.log_paths:
            self._captured_text.append(self._read_log_window(path))
        self._credential_sentinels.update(self._load_credential_sentinels())
        if not self._credential_sentinels:
            raise ProofFailure("redaction_sentinel_unavailable")
        for text in self._captured_text:
            if _SECRET_VALUE_RE.search(text) or any(
                sentinel in text for sentinel in self._credential_sentinels
            ):
                raise ProofFailure("redaction_failure")
        return {"provider": "kimi", "status": 0}

    @staticmethod
    def _load_credential_sentinels() -> set[str]:
        sentinels: set[str] = set()
        bearer = os.environ.get("KIMI_BEARER_TOKEN", "").strip()
        if bearer:
            sentinels.add(bearer)
        kimi_home = Path(os.environ.get("KIMI_CODE_HOME") or Path.home() / ".kimi-code")
        path = kimi_home / "credentials" / "kimi-code.json"
        try:
            artifact = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            artifact = None
        if isinstance(artifact, dict):
            for key, value in artifact.items():
                if (
                    key.lower() in _KIMI_CREDENTIAL_FIELDS
                    and isinstance(value, str)
                    and value.strip()
                ):
                    sentinels.add(value.strip())
        return sentinels

    def loopback(self) -> dict[str, Any]:
        self._require_loopback()
        return {"provider": "kimi", "route": self.base_url, "status": 0}
