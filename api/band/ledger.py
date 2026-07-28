"""The audit trail, written down.

A collaboration lived in a dict on the running process. Every claim made for it
-- that this is the governance record, that a caseworker can reconstruct a
rejected filing months later -- was false the moment the service restarted, and
on a free Render instance that is roughly every time nobody calls for a while.

So it goes to the same durable store the applicant's memory does: Supabase where
it is configured, SQLite otherwise. Under its own scope, keyed by session rather
than by caller, because these are two different records with two different
lifetimes -- an applicant may ask to be forgotten, and a filing's audit trail is
not theirs to erase.

Turns are episodic: they accumulate, they are never a correction of an earlier
turn. That is the tier's whole meaning, and it is also what makes replay honest,
because a trail that could be rewritten in place is not evidence of anything.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from api.memory_store import Record, get_backend

logger = logging.getLogger(__name__)

#: Prefix keeps audit scopes from ever colliding with an applicant's memory
#: scope, which is `zt_`. Same table, disjoint namespaces, one query each.
SCOPE_PREFIX = "zaud_"

#: Turn keys sort lexically, so a filing that ran the full turn budget still
#: replays in the order it happened. Three digits is well past MAX_TURNS.
_KEY = "turn-{:03d}"


def scope_for(tenant_id: str, session_id: str) -> str:
    material = f"{tenant_id.strip()}\x1f{session_id.strip()}".encode()
    return f"{SCOPE_PREFIX}{hashlib.sha256(material).hexdigest()[:24]}"


async def record(collab: Any) -> int:
    """Persist a finished collaboration. Returns how many turns were kept.

    Never raises. A store that is down must cost the trail, not the filing --
    the applicant's PDF is already written by the time this runs.
    """
    scope = scope_for(collab.principal.tenant_id, collab.session_id)
    backend = get_backend()
    header = {
        "session_id": collab.session_id,
        "room_id": collab.room_id,
        "tenant_id": collab.principal.tenant_id,
        "started": collab.started,
        "turn_count": len(collab.turns),
    }
    kept = 0
    try:
        await backend.put(
            scope,
            Record(tier="semantic", key="collaboration", value=json.dumps(header), meta=header),
        )
        for index, turn in enumerate(collab.turns):
            payload = turn.as_dict()
            ok = await backend.put(
                scope,
                Record(
                    tier="episodic",
                    key=_KEY.format(index),
                    value=turn.said or turn.because,
                    meta=payload,
                    at=turn.at,
                ),
            )
            kept += 1 if ok else 0
    except Exception as exc:  # a trail is never worth an exception on the way out
        logger.warning("audit trail not persisted for %s: %s", collab.session_id, exc)
    return kept


async def replay(tenant_id: str, session_id: str) -> dict[str, Any] | None:
    """The stored trail for a session, in the shape the live one has.

    Same shape on purpose: the endpoint should not have to explain to its caller
    whether the process that ran this filing is still alive.
    """
    scope = scope_for(tenant_id, session_id)
    try:
        rows = await get_backend().all(scope)
    except Exception as exc:
        logger.warning("audit trail not readable for %s: %s", session_id, exc)
        return None
    if not rows:
        return None

    header: dict[str, Any] = {}
    turns: list[tuple[str, dict[str, Any]]] = []
    for row in rows:
        if row.tier == "semantic" and row.key == "collaboration":
            header = row.meta or {}
        elif row.tier == "episodic":
            turns.append((row.key, row.meta or {"said": row.value, "at": row.at}))
    turns.sort(key=lambda item: item[0])
    ordered = [turn for _, turn in turns]

    started = float(header.get("started") or (ordered[0]["at"] if ordered else 0.0))
    last = float(ordered[-1]["at"]) if ordered else started
    return {
        "session_id": session_id,
        "room_id": header.get("room_id", ""),
        "tenant_id": tenant_id,
        "turns": ordered,
        "turn_count": len(ordered),
        "seconds": round(last - started, 2),
        "finished": True,
        # The caller is reading history, not watching a room. Saying so is the
        # difference between "nothing is happening" and "this already happened".
        "replayed": True,
    }
