# -*- coding: utf-8 -*-
"""Transactional runtime state for multi-provider LLM calls.

The database contains operational metadata only.  Prompts, responses and API
keys must never be written here.
"""

from __future__ import annotations

import json
import hashlib
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo


DEFAULT_DB_PATH = (
    Path(__file__).resolve().parents[2]
    / "data"
    / "00_management"
    / "llm_runtime.sqlite"
)
AVAILABILITY_SCOPE = "__availability__"
QUALITY_ERRORS = {"empty_response", "json_parse", "validation", "refusal"}
IMMEDIATE_OPEN_ERRORS = {"auth", "model_unavailable", "quota", "rate_limit"}


class RuntimeStateError(RuntimeError):
    """Runtime-state operation could not be completed safely."""


class BudgetUnavailable(RuntimeStateError):
    """The configured local safety budget cannot reserve another attempt."""


@dataclass(frozen=True)
class Reservation:
    reservation_id: str
    provider: str
    model: str
    quota_group: str
    stage: str
    logical_job_id: str
    estimated_tokens: int
    day_bucket: str
    started_at: float


def _utc_now() -> float:
    return time.time()


def _clean_error(message: str | None) -> str | None:
    if not message:
        return None
    # Operational errors are deliberately short; response bodies are not logs.
    return " ".join(str(message).split())[:500]


class LLMRuntimeStore:
    """SQLite-backed reservations, attempts, usage and circuit breakers."""

    def __init__(
        self,
        path: str | Path = DEFAULT_DB_PATH,
        *,
        now: Callable[[], float] = _utc_now,
    ) -> None:
        self.path = Path(path).expanduser().resolve()
        self._now = now
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS reservations (
                    reservation_id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    quota_group TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    logical_job_id TEXT NOT NULL,
                    estimated_tokens INTEGER NOT NULL,
                    day_bucket TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS reservations_budget_idx
                ON reservations(quota_group, day_bucket, expires_at);

                CREATE TABLE IF NOT EXISTS attempts (
                    attempt_id TEXT PRIMARY KEY,
                    reservation_id TEXT NOT NULL,
                    logical_job_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    actual_model TEXT,
                    quota_group TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    day_bucket TEXT NOT NULL,
                    started_at REAL NOT NULL,
                    finished_at REAL NOT NULL,
                    latency_ms INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    error_kind TEXT,
                    http_status INTEGER,
                    estimated_tokens INTEGER NOT NULL,
                    input_tokens INTEGER,
                    output_tokens INTEGER,
                    total_tokens INTEGER NOT NULL,
                    validation_decision TEXT,
                    error_message TEXT
                );

                CREATE INDEX IF NOT EXISTS attempts_usage_idx
                ON attempts(quota_group, day_bucket, provider, model);

                CREATE INDEX IF NOT EXISTS attempts_job_idx
                ON attempts(logical_job_id, started_at);

                CREATE TABLE IF NOT EXISTS circuits (
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    state TEXT NOT NULL,
                    reason TEXT,
                    consecutive_failures INTEGER NOT NULL DEFAULT 0,
                    opened_at REAL,
                    reset_at REAL,
                    probe_claim_until REAL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(provider, model, scope)
                );

                CREATE TABLE IF NOT EXISTS runtime_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                """
            )

    @staticmethod
    def day_bucket(timestamp: float, timezone_name: str = "UTC") -> str:
        zone = ZoneInfo(timezone_name)
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone(zone).date().isoformat()

    def reserve(
        self,
        *,
        provider: str,
        model: str,
        quota_group: str,
        stage: str,
        logical_job_id: str,
        estimated_tokens: int,
        limits: Mapping[str, Any] | None = None,
        reset_timezone: str = "UTC",
        ttl_seconds: int = 900,
    ) -> Reservation:
        now = self._now()
        day = self.day_bucket(now, reset_timezone)
        limits = limits or {}
        estimated = max(1, int(estimated_tokens))
        max_per_call = int(limits.get("max_tokens_per_call") or 0)
        max_calls = int(limits.get("max_calls_per_day") or 0)
        max_tokens = int(limits.get("max_tokens_per_day") or 0)
        if max_per_call and estimated > max_per_call:
            raise BudgetUnavailable(
                f"{provider}:{model} exceeds local per-call limit "
                f"({estimated} > {max_per_call} tokens)"
            )

        reservation_id = "llmr_" + uuid.uuid4().hex
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM reservations WHERE expires_at <= ?", (now,))
            committed = connection.execute(
                """
                SELECT COUNT(*) AS calls, COALESCE(SUM(total_tokens), 0) AS tokens
                FROM attempts WHERE quota_group = ? AND day_bucket = ?
                """,
                (quota_group, day),
            ).fetchone()
            active = connection.execute(
                """
                SELECT COUNT(*) AS calls, COALESCE(SUM(estimated_tokens), 0) AS tokens
                FROM reservations WHERE quota_group = ? AND day_bucket = ?
                """,
                (quota_group, day),
            ).fetchone()
            used_calls = int(committed["calls"]) + int(active["calls"])
            used_tokens = int(committed["tokens"]) + int(active["tokens"])
            if max_calls and used_calls + 1 > max_calls:
                raise BudgetUnavailable(
                    f"{quota_group} daily call safety limit reached "
                    f"({used_calls}/{max_calls})"
                )
            if max_tokens and used_tokens + estimated > max_tokens:
                raise BudgetUnavailable(
                    f"{quota_group} daily token safety limit would be exceeded "
                    f"({used_tokens}+{estimated}>{max_tokens})"
                )
            connection.execute(
                """
                INSERT INTO reservations(
                    reservation_id, provider, model, quota_group, stage,
                    logical_job_id, estimated_tokens, day_bucket, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    reservation_id, provider, model, quota_group, stage,
                    logical_job_id, estimated, day, now, now + max(30, ttl_seconds),
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return Reservation(
            reservation_id=reservation_id,
            provider=provider,
            model=model,
            quota_group=quota_group,
            stage=stage,
            logical_job_id=logical_job_id,
            estimated_tokens=estimated,
            day_bucket=day,
            started_at=now,
        )

    def release(self, reservation: Reservation) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM reservations WHERE reservation_id = ?",
                (reservation.reservation_id,),
            )

    def record_external_attempt(
        self,
        *,
        provider: str,
        model: str,
        quota_group: str,
        stage: str,
        total_tokens: int,
        reset_timezone: str = "UTC",
        logical_job_id: str = "legacy_direct",
        status: str = "accepted",
    ) -> str:
        """Record a completed call made by a compatibility transport.

        Legacy Gemini helpers cannot use a router reservation without changing
        their public calling convention.  They still write into the same
        attempts ledger so both paths share the next reservation decision.
        Prompts, responses, and credentials are never accepted here.
        """

        now = self._now()
        attempt_id = "llma_" + uuid.uuid4().hex
        total = max(1, int(total_tokens or 0))
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO attempts(
                    attempt_id, reservation_id, logical_job_id, provider, model,
                    actual_model, quota_group, stage, day_bucket, started_at,
                    finished_at, latency_ms, status, error_kind, http_status,
                    estimated_tokens, input_tokens, output_tokens, total_tokens,
                    validation_decision, error_message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, NULL, NULL,
                          ?, NULL, NULL, ?, NULL, NULL)
                """,
                (
                    attempt_id, "external_" + uuid.uuid4().hex,
                    logical_job_id, provider, model, model, quota_group, stage,
                    self.day_bucket(now, reset_timezone), now, now, status,
                    total, total,
                ),
            )
        return attempt_id

    def import_legacy_usage(
        self,
        usage: Mapping[str, Any],
        *,
        source_id: str,
        provider: str = "gemini",
        model: str = "legacy-gemini",
        quota_group: str = "google-ai-project",
        stage: str = "legacy_json_migration",
    ) -> int:
        """Import the old aggregate JSON ledger exactly once.

        One synthetic attempt is created per historical call so call and token
        limits retain their original meaning.  A transactional metadata marker
        makes repeated startup checks idempotent.  The source file is never
        modified or deleted.
        """

        marker = "legacy_usage_import:" + hashlib.sha256(
            str(source_id).encode("utf-8")
        ).hexdigest()
        now = self._now()
        connection = self._connect()
        inserted = 0
        try:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM runtime_metadata WHERE key=?", (marker,)
            ).fetchone() is not None:
                connection.commit()
                return 0
            for day, aggregate in sorted(usage.items()):
                if not isinstance(aggregate, Mapping):
                    continue
                try:
                    datetime.fromisoformat(str(day))
                    calls = max(0, int(aggregate.get("calls") or 0))
                    tokens = max(0, int(aggregate.get("tokens") or 0))
                except (TypeError, ValueError):
                    continue
                if calls <= 0:
                    continue
                quotient, remainder = divmod(tokens, calls)
                timestamp = datetime.fromisoformat(
                    f"{day}T12:00:00+00:00"
                ).timestamp()
                for index in range(calls):
                    total = max(1, quotient + (1 if index < remainder else 0))
                    identity = f"{source_id}|{day}|{index}"
                    attempt_id = "llma_legacy_" + hashlib.sha256(
                        identity.encode("utf-8")
                    ).hexdigest()[:24]
                    cursor = connection.execute(
                        """
                        INSERT OR IGNORE INTO attempts(
                            attempt_id, reservation_id, logical_job_id, provider,
                            model, actual_model, quota_group, stage, day_bucket,
                            started_at, finished_at, latency_ms, status, error_kind,
                            http_status, estimated_tokens, input_tokens,
                            output_tokens, total_tokens, validation_decision,
                            error_message
                        ) VALUES (?, 'legacy_json', 'legacy_json_import', ?, ?, ?,
                                  ?, ?, ?, ?, ?, 0, 'accepted', NULL, NULL, ?,
                                  NULL, NULL, ?, NULL, NULL)
                        """,
                        (
                            attempt_id, provider, model, model, quota_group,
                            stage, str(day), timestamp, timestamp, total, total,
                        ),
                    )
                    inserted += max(0, int(cursor.rowcount))
            connection.execute(
                "INSERT INTO runtime_metadata(key, value, updated_at) VALUES (?, ?, ?)",
                (marker, json.dumps({"attempts": inserted}), now),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return inserted

    def finalize(
        self,
        reservation: Reservation,
        *,
        status: str,
        actual_model: str | None = None,
        error_kind: str | None = None,
        http_status: int | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        total_tokens: int | None = None,
        validation_decision: str | None = None,
        error_message: str | None = None,
    ) -> str:
        finished = self._now()
        total = max(1, int(total_tokens or reservation.estimated_tokens))
        attempt_id = "llma_" + uuid.uuid4().hex
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            exists = connection.execute(
                "SELECT 1 FROM reservations WHERE reservation_id = ?",
                (reservation.reservation_id,),
            ).fetchone()
            if exists is None:
                raise RuntimeStateError(
                    f"Reservation {reservation.reservation_id} is no longer active"
                )
            connection.execute(
                """
                INSERT INTO attempts(
                    attempt_id, reservation_id, logical_job_id, provider, model,
                    actual_model, quota_group, stage, day_bucket, started_at,
                    finished_at, latency_ms, status, error_kind, http_status,
                    estimated_tokens, input_tokens, output_tokens, total_tokens,
                    validation_decision, error_message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt_id, reservation.reservation_id,
                    reservation.logical_job_id, reservation.provider,
                    reservation.model, actual_model, reservation.quota_group,
                    reservation.stage, reservation.day_bucket,
                    reservation.started_at, finished,
                    max(0, round((finished - reservation.started_at) * 1000)),
                    status, error_kind, http_status,
                    reservation.estimated_tokens, input_tokens, output_tokens,
                    total, validation_decision, _clean_error(error_message),
                ),
            )
            connection.execute(
                "DELETE FROM reservations WHERE reservation_id = ?",
                (reservation.reservation_id,),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return attempt_id

    def _claim_scope(self, provider: str, model: str, scope: str, now: float) -> bool:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM circuits WHERE provider=? AND model=? AND scope=?",
                (provider, model, scope),
            ).fetchone()
            if row is None or row["state"] == "closed":
                connection.commit()
                return True
            if row["state"] == "open" and row["reset_at"] is not None and float(row["reset_at"]) <= now:
                connection.execute(
                    """
                    UPDATE circuits SET state='half_open', probe_claim_until=?, updated_at=?
                    WHERE provider=? AND model=? AND scope=?
                    """,
                    (now + 300, now, provider, model, scope),
                )
                connection.commit()
                return True
            if row["state"] == "half_open" and (
                row["probe_claim_until"] is None or float(row["probe_claim_until"]) <= now
            ):
                connection.execute(
                    """
                    UPDATE circuits SET probe_claim_until=?, updated_at=?
                    WHERE provider=? AND model=? AND scope=?
                    """,
                    (now + 300, now, provider, model, scope),
                )
                connection.commit()
                return True
            connection.commit()
            return False
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def claim(self, provider: str, model: str, stage: str) -> bool:
        now = self._now()
        if not self._claim_scope(provider, model, AVAILABILITY_SCOPE, now):
            return False
        return self._claim_scope(provider, model, stage, now)

    def record_success(self, provider: str, model: str, stage: str) -> None:
        now = self._now()
        with self._connect() as connection:
            for scope in (AVAILABILITY_SCOPE, stage):
                connection.execute(
                    """
                    INSERT INTO circuits(
                        provider, model, scope, state, reason, consecutive_failures,
                        opened_at, reset_at, probe_claim_until, updated_at
                    ) VALUES (?, ?, ?, 'closed', NULL, 0, NULL, NULL, NULL, ?)
                    ON CONFLICT(provider, model, scope) DO UPDATE SET
                        state='closed', reason=NULL, consecutive_failures=0,
                        opened_at=NULL, reset_at=NULL, probe_claim_until=NULL,
                        updated_at=excluded.updated_at
                    """,
                    (provider, model, scope, now),
                )

    def record_failure(
        self,
        provider: str,
        model: str,
        stage: str,
        error_kind: str,
        *,
        reset_at: float | None = None,
    ) -> None:
        now = self._now()
        quality = error_kind in QUALITY_ERRORS or error_kind in {"capability", "context"}
        scope = stage if quality else AVAILABILITY_SCOPE
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT consecutive_failures FROM circuits WHERE provider=? AND model=? AND scope=?",
                (provider, model, scope),
            ).fetchone()
            failures = int(row["consecutive_failures"]) + 1 if row else 1
            immediate = error_kind in IMMEDIATE_OPEN_ERRORS or error_kind in {"capability", "context"}
            threshold = 2 if quality else 3
            should_open = immediate or failures >= threshold
            state = "open" if should_open else "closed"
            if should_open and reset_at is None:
                if error_kind == "auth":
                    reset_at = now + 86400
                elif error_kind in {"model_unavailable", "capability", "context"}:
                    reset_at = now + 86400
                elif quality:
                    reset_at = now + 1800
                else:
                    reset_at = now + min(3600, 300 * (2 ** max(0, failures - threshold)))
            connection.execute(
                """
                INSERT INTO circuits(
                    provider, model, scope, state, reason, consecutive_failures,
                    opened_at, reset_at, probe_claim_until, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
                ON CONFLICT(provider, model, scope) DO UPDATE SET
                    state=excluded.state, reason=excluded.reason,
                    consecutive_failures=excluded.consecutive_failures,
                    opened_at=excluded.opened_at, reset_at=excluded.reset_at,
                    probe_claim_until=NULL, updated_at=excluded.updated_at
                """,
                (
                    provider, model, scope, state, error_kind, failures,
                    now if should_open else None, reset_at if should_open else None, now,
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def usage(self, *, day_bucket: str | None = None) -> list[dict[str, Any]]:
        day = day_bucket or self.day_bucket(self._now())
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT provider, model, quota_group, COUNT(*) AS calls,
                       COALESCE(SUM(input_tokens), 0) AS input_tokens,
                       COALESCE(SUM(output_tokens), 0) AS output_tokens,
                       COALESCE(SUM(total_tokens), 0) AS total_tokens,
                       COALESCE(SUM(CASE
                           WHEN status='accepted'
                             OR input_tokens IS NOT NULL
                             OR output_tokens IS NOT NULL
                           THEN total_tokens ELSE 0 END), 0) AS reported_tokens,
                       COALESCE(SUM(CASE
                           WHEN status<>'accepted'
                             AND input_tokens IS NULL
                             AND output_tokens IS NULL
                           THEN total_tokens ELSE 0 END), 0) AS estimated_error_tokens,
                       SUM(CASE WHEN status='accepted' THEN 1 ELSE 0 END) AS accepted
                FROM attempts WHERE day_bucket=?
                GROUP BY provider, model, quota_group
                ORDER BY provider, model
                """,
                (day,),
            ).fetchall()
        return [dict(row) for row in rows]

    def usage_totals(
        self,
        *,
        quota_group: str,
        day_bucket: str,
        include_reservations: bool = False,
    ) -> dict[str, int]:
        """Return one quota group's committed (and optionally reserved) use."""

        now = self._now()
        connection = self._connect()
        try:
            if include_reservations:
                connection.execute("DELETE FROM reservations WHERE expires_at <= ?", (now,))
            row = connection.execute(
                """
                SELECT COUNT(*) AS calls, COALESCE(SUM(total_tokens), 0) AS tokens
                FROM attempts WHERE quota_group=? AND day_bucket=?
                """,
                (quota_group, day_bucket),
            ).fetchone()
            calls = int(row["calls"])
            tokens = int(row["tokens"])
            if include_reservations:
                active = connection.execute(
                    """
                    SELECT COUNT(*) AS calls,
                           COALESCE(SUM(estimated_tokens), 0) AS tokens
                    FROM reservations WHERE quota_group=? AND day_bucket=?
                    """,
                    (quota_group, day_bucket),
                ).fetchone()
                calls += int(active["calls"])
                tokens += int(active["tokens"])
            return {"calls": calls, "tokens": tokens}
        finally:
            connection.close()

    def usage_days(self, *, quota_group: str) -> dict[str, dict[str, int]]:
        """Return backward-compatible day aggregates for one quota group."""

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT day_bucket, COUNT(*) AS calls,
                       COALESCE(SUM(total_tokens), 0) AS tokens
                FROM attempts WHERE quota_group=?
                GROUP BY day_bucket ORDER BY day_bucket
                """,
                (quota_group,),
            ).fetchall()
        return {
            str(row["day_bucket"]): {
                "calls": int(row["calls"]), "tokens": int(row["tokens"]),
            }
            for row in rows
        }

    def circuits(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT provider, model, scope, state, reason,
                       consecutive_failures, reset_at, updated_at
                FROM circuits ORDER BY provider, model, scope
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def reset_circuit(self, provider: str, model: str, scope: str | None = None) -> int:
        """Remove persisted circuit state after an operator-approved reset."""
        with self._connect() as connection:
            if scope is None:
                cursor = connection.execute(
                    "DELETE FROM circuits WHERE provider=? AND model=?",
                    (provider, model),
                )
            else:
                cursor = connection.execute(
                    "DELETE FROM circuits WHERE provider=? AND model=? AND scope=?",
                    (provider, model, scope),
                )
        return max(0, int(cursor.rowcount))

    def status_json(self) -> str:
        return json.dumps(
            {"usage": self.usage(), "circuits": self.circuits()},
            ensure_ascii=False,
            indent=2,
        )


__all__ = [
    "AVAILABILITY_SCOPE",
    "BudgetUnavailable",
    "DEFAULT_DB_PATH",
    "LLMRuntimeStore",
    "Reservation",
    "RuntimeStateError",
]
