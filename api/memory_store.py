"""Where the three memory tiers actually live.

Zuzu's memory is a contract, not a vendor: semantic facts, episodic calls and
procedural rules, scoped to one user inside one tenant, written and recalled.
Which service holds them is a deployment decision, and it has already had to
change once.

    mem0        The original. Writes still return 200; every read -- v1 and v2
                alike -- answers 429 "Usage quota exceeded", and the quota is on
                the account rather than the key, so a fresh key does not lift it.
                A write-only memory cannot recall a returning caller, which is
                the entire feature.

    sqlite      The working default. No credentials, no quota, no network on the
                recall path, and it is genuinely durable within a deployment.
                Not durable across a Render redeploy, whose filesystem is
                ephemeral -- that is what the Supabase backend is for.

    supabase    Postgres over HTTP, for a deployment that needs memory to
                outlive the container. Reads and writes the same three tiers
                through the same interface; needs a project URL and service key.

The interface below is what all three implement, so moving between them is a
config change. Everything is keyed by (tenant, user) and there is no method that
takes a bare caller id -- that shape is what allowed one global namespace, and
the type is the fix.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger(__name__)

#: Where the SQLite backend keeps its file. On Render's ephemeral disk this
#: survives restarts but not redeploys, which is stated rather than hidden.
DB_PATH = Path(
    os.environ.get("ZUZU_MEMORY_DB", Path(__file__).resolve().parent.parent / "out" / "memory.db")
)


@dataclass(frozen=True)
class Record:
    """One remembered thing."""

    tier: str
    key: str
    value: str
    #: Everything else worth keeping: the field id, the form, a confidence.
    meta: dict[str, Any] = field(default_factory=dict)
    at: float = field(default_factory=time.time)

    def as_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier,
            "key": self.key,
            "value": self.value,
            "meta": self.meta,
            "at": self.at,
        }


class MemoryBackend(Protocol):
    """What a store has to be able to do. Three tiers, two directions."""

    name: str

    async def put(self, scope: str, record: Record) -> bool: ...

    async def all(self, scope: str) -> list[Record]: ...

    async def drop(self, scope: str, tier: str | None = None) -> int: ...

    async def healthy(self) -> tuple[bool, str]: ...


class SqliteMemory:
    """Durable, local, and always available.

    Semantic and procedural records are corrections of a previous value for the
    same key, so they upsert. Episodes are events and accumulate -- that
    distinction is the reason `tier` is part of the primary key rather than just
    a column to filter on.
    """

    name = "sqlite"

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or DB_PATH
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    scope   TEXT NOT NULL,
                    tier    TEXT NOT NULL,
                    key     TEXT NOT NULL,
                    value   TEXT NOT NULL,
                    meta    TEXT NOT NULL DEFAULT '{}',
                    at      REAL NOT NULL
                )
                """
            )
            # Scope leads every query, so it leads the index. Without this a
            # tenant's recall scans every other tenant's rows.
            conn.execute("CREATE INDEX IF NOT EXISTS ix_scope ON memories(scope, tier)")
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_upsert ON memories(scope, tier, key)"
            )

    async def put(self, scope: str, record: Record) -> bool:
        try:
            with self._connect() as conn:
                if record.tier == "episodic":
                    # Events accumulate; the key is unique per call already.
                    conn.execute(
                        "INSERT OR REPLACE INTO memories VALUES (?,?,?,?,?,?)",
                        (
                            scope,
                            record.tier,
                            record.key,
                            record.value,
                            json.dumps(record.meta),
                            record.at,
                        ),
                    )
                else:
                    conn.execute(
                        """
                        INSERT INTO memories VALUES (?,?,?,?,?,?)
                        ON CONFLICT(scope, tier, key) DO UPDATE SET
                            value = excluded.value, meta = excluded.meta, at = excluded.at
                        """,
                        (
                            scope,
                            record.tier,
                            record.key,
                            record.value,
                            json.dumps(record.meta),
                            record.at,
                        ),
                    )
            return True
        except sqlite3.Error as exc:
            logger.warning("sqlite memory write failed: %s", exc)
            return False

    async def all(self, scope: str) -> list[Record]:
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT tier, key, value, meta, at FROM memories WHERE scope = ? ORDER BY at",
                    (scope,),
                ).fetchall()
        except sqlite3.Error as exc:
            logger.warning("sqlite memory read failed: %s", exc)
            return []
        out = []
        for r in rows:
            try:
                meta = json.loads(r["meta"])
            except json.JSONDecodeError:
                meta = {}
            out.append(
                Record(tier=r["tier"], key=r["key"], value=r["value"], meta=meta, at=r["at"])
            )
        return out

    async def drop(self, scope: str, tier: str | None = None) -> int:
        try:
            with self._connect() as conn:
                if tier:
                    cur = conn.execute(
                        "DELETE FROM memories WHERE scope = ? AND tier = ?", (scope, tier)
                    )
                else:
                    cur = conn.execute("DELETE FROM memories WHERE scope = ?", (scope,))
                return cur.rowcount
        except sqlite3.Error as exc:
            logger.warning("sqlite memory delete failed: %s", exc)
            return 0

    async def healthy(self) -> tuple[bool, str]:
        try:
            with self._connect() as conn:
                conn.execute("SELECT 1").fetchone()
            return True, f"sqlite at {self._path}"
        except sqlite3.Error as exc:
            return False, str(exc)


class SupabaseMemory:
    """Postgres over HTTP, for memory that outlives the container.

    Uses PostgREST, so it needs no client library -- a service key and a table:

        create table memories (
            scope text not null,
            tier  text not null,
            key   text not null,
            value text not null,
            meta  jsonb not null default '{}',
            at    double precision not null,
            primary key (scope, tier, key)
        );

    Left unconfigured this simply reports unhealthy and the factory picks
    something else; it is never a hard dependency.
    """

    name = "supabase"

    def __init__(self, url: str, key: str, table: str = "memories") -> None:
        self._url = url.rstrip("/")
        self._key = key
        self._table = table

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "apikey": self._key,
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/json",
        }

    async def put(self, scope: str, record: Record) -> bool:
        import httpx

        row = {
            "scope": scope,
            "tier": record.tier,
            "key": record.key,
            "value": record.value,
            "meta": record.meta,
            "at": record.at,
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as c:
                resp = await c.post(
                    f"{self._url}/rest/v1/{self._table}",
                    headers={**self._headers, "Prefer": "resolution=merge-duplicates"},
                    params={"on_conflict": "scope,tier,key"},
                    json=row,
                )
            return resp.status_code < 300
        except Exception as exc:
            logger.warning("supabase memory write failed: %s", type(exc).__name__)
            return False

    async def all(self, scope: str) -> list[Record]:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=10.0) as c:
                resp = await c.get(
                    f"{self._url}/rest/v1/{self._table}",
                    headers=self._headers,
                    params={"scope": f"eq.{scope}", "order": "at.asc"},
                )
                resp.raise_for_status()
                rows = resp.json()
        except Exception as exc:
            logger.warning("supabase memory read failed: %s", type(exc).__name__)
            return []
        return [
            Record(
                tier=r.get("tier", ""),
                key=r.get("key", ""),
                value=r.get("value", ""),
                meta=r.get("meta") or {},
                at=float(r.get("at") or 0),
            )
            for r in rows
        ]

    async def drop(self, scope: str, tier: str | None = None) -> int:
        import httpx

        params = {"scope": f"eq.{scope}"}
        if tier:
            params["tier"] = f"eq.{tier}"
        try:
            async with httpx.AsyncClient(timeout=10.0) as c:
                resp = await c.delete(
                    f"{self._url}/rest/v1/{self._table}",
                    headers={**self._headers, "Prefer": "return=representation"},
                    params=params,
                )
                return len(resp.json()) if resp.status_code < 300 else 0
        except Exception as exc:
            logger.warning("supabase memory delete failed: %s", type(exc).__name__)
            return 0

    async def healthy(self) -> tuple[bool, str]:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=8.0) as c:
                resp = await c.get(
                    f"{self._url}/rest/v1/{self._table}",
                    headers=self._headers,
                    params={"limit": "1"},
                )
            return resp.status_code < 300, f"supabase HTTP {resp.status_code}"
        except Exception as exc:
            return False, type(exc).__name__


_backend: MemoryBackend | None = None


#: Consecutive failed probes before a configured store is abandoned. One blip
#: is a blip; three in a row is an outage.
FALLBACK_AFTER = 3

_fallback_since = 0


def _is_configured() -> bool:
    """Whether a durable store was asked for at all."""
    return bool(os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_SERVICE_KEY"))


def get_backend() -> MemoryBackend:
    """The store this deployment uses.

    Supabase when it is configured, because it is the only one that survives a
    redeploy; SQLite otherwise, because it always works and needs nothing.
    """
    global _backend
    if _backend is not None:
        return _backend
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
    if url and key:
        _backend = SupabaseMemory(url, key)
        logger.info("memory backend: supabase")
    else:
        _backend = SqliteMemory()
        logger.info("memory backend: sqlite at %s", DB_PATH)
    return _backend


async def check_backend() -> dict[str, Any]:
    """Whether the configured store can be reached, falling back if it cannot.

    A misconfigured Supabase is indistinguishable from an applicant with no
    history: every read returns an empty list and every caller looks new. That
    failure has already happened twice here under different names, so it is
    never allowed to be silent.

    Neither extreme is right, though. Failing hard means one wrong character in
    a key takes memory down for everybody; failing quietly is the bug itself.
    So a store that cannot be reached is swapped for SQLite -- which always
    works -- and both the log and /health say what happened and why. Somebody
    gets a working service and an accurate reason, rather than one or the other.
    """
    global _backend, _fallback_since
    backend = get_backend()

    # If we fell back but the configured store is reachable again, come back.
    # Without this the swap is one-way for the life of the process, so a
    # thirty-second blip becomes a deploy-long outage of the thing the whole
    # returning-caller story rests on -- and the applicant's real history sits
    # in Postgres, untouched, while a second divergent copy accumulates on a
    # disk the next deploy erases.
    if backend.name == "sqlite" and _is_configured():
        candidate = SupabaseMemory(
            os.environ["SUPABASE_URL"].strip(), os.environ["SUPABASE_SERVICE_KEY"].strip()
        )
        recovered, recovered_detail = await candidate.healthy()
        if recovered:
            logger.info("memory backend recovered: supabase is reachable again")
            _backend = candidate
            _fallback_since = 0
            return {"backend": candidate.name, "reachable": True, "detail": recovered_detail}

    ok, detail = await backend.healthy()
    if ok:
        logger.info("memory backend %s ready: %s", backend.name, detail)
        _fallback_since = 0
        return {"backend": backend.name, "reachable": True, "detail": detail}

    # A configured store that answers one bad probe is not a broken store.
    #
    # /health calls this on every poll, and Render polls constantly, so a single
    # transient ConnectTimeout used to swap the process-wide backend to SQLite
    # PERMANENTLY -- nothing in production ever calls reset_backend, and
    # get_backend returns the cached instance forever after. The applicant's
    # real history stays in Postgres, unreachable, while Zuzu greets them as new
    # and writes a second, divergent copy to a disk the next deploy erases.
    #
    # So: tolerate a blip, and swap only once the store has failed
    # FALLBACK_AFTER consecutive probes. And when it is configured, keep
    # re-probing so the swap is reversible -- the previous version had no way
    # back at all.
    if _is_configured() and backend.name != "sqlite":
        _fallback_since += 1
        if _fallback_since < FALLBACK_AFTER:
            logger.warning(
                "memory backend %s failed a probe (%s); %d of %d before falling back",
                backend.name,
                detail,
                _fallback_since,
                FALLBACK_AFTER,
            )
            return {
                "backend": backend.name,
                "reachable": False,
                "detail": detail,
                "degraded_reason": "",
                "note": f"probe {_fallback_since} of {FALLBACK_AFTER}; not falling back yet",
            }

    logger.error(
        "memory backend %s is NOT reachable (%s) -- falling back to sqlite. "
        "Recall will not survive a redeploy until this is fixed.",
        backend.name,
        detail,
    )
    failed, failed_detail = backend.name, detail
    _backend = SqliteMemory()
    fell_back_ok, fell_back_detail = await _backend.healthy()
    return {
        "backend": _backend.name,
        "reachable": fell_back_ok,
        "detail": fell_back_detail,
        "degraded_from": failed,
        "degraded_reason": failed_detail,
    }


def reset_backend() -> None:
    """Drop the cached backend. For tests and for a config change.

    The consecutive-failure counter goes with it: leaving it set meant a fresh
    backend inherited the previous one's strikes and fell back on its first
    probe, which is the opposite of what the counter is for.
    """
    global _backend, _fallback_since
    _backend = None
    _fallback_since = 0
