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
#:
#: The room id leads, because the key was positional ONLY and both backends
#: upsert on (scope, tier, key). A second collaboration on the same session --
#: a retry after a 503, or after the 300s timeout -- overwrote turn-000 onward
#: of the first and left its later turns behind, producing one record attributed
#: to one room that contained turns from two and, in the case actually
#: reproduced, both "collaboration timed out" and "auditor sealed the record".
#: A trail that can be rewritten in place is not evidence of anything, which is
#: what the docstring above already said.
_KEY = "{room}/turn-{index:03d}"


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
            Record(
                tier="semantic",
                key=f"collaboration/{collab.room_id}",
                value=json.dumps(header),
                meta=header,
            ),
        )
        for index, turn in enumerate(collab.turns):
            payload = turn.as_dict()
            payload["room_id"] = collab.room_id
            ok = await backend.put(
                scope,
                Record(
                    tier="episodic",
                    key=_KEY.format(room=collab.room_id, index=index),
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

    # Group by room. A session can be orchestrated more than once, and two runs
    # are two collaborations -- merging them into one list produces a record
    # that never happened.
    headers: dict[str, dict[str, Any]] = {}
    by_room: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for row in rows:
        if row.tier == "semantic" and row.key.startswith("collaboration"):
            meta = row.meta or {}
            headers[str(meta.get("room_id", ""))] = meta
        elif row.tier == "episodic":
            meta = row.meta or {"said": row.value, "at": row.at}
            room = str(meta.get("room_id") or row.key.rsplit("/", 1)[0])
            by_room.setdefault(room, []).append((row.key, meta))
    # A header with no turns is a collaboration that opened and recorded
    # nothing. Rare -- close() always appends a closing turn -- but "it ran and
    # produced nothing" and "it never ran" are different answers, and only one
    # of them is a 404.
    for room in headers:
        by_room.setdefault(room, [])
    if not by_room:
        return None

    # The most recent run is the one a caseworker means by "the audit trail";
    # the earlier ones are still there under their own room ids.
    def _started(room: str) -> float:
        header = headers.get(room) or {}
        turns = by_room[room]
        if header.get("started"):
            return float(header["started"])
        return min((float(t[1].get("at") or 0.0) for t in turns), default=0.0)

    room_id = max(by_room, key=_started)
    turns = sorted(by_room[room_id], key=lambda item: item[0])
    ordered = [turn for _, turn in turns]
    header = headers.get(room_id, {})

    started = float(header.get("started") or (ordered[0].get("at") if ordered else 0.0) or 0.0)
    last = float((ordered[-1].get("at") if ordered else started) or started)
    return {
        "session_id": session_id,
        "room_id": room_id,
        "tenant_id": tenant_id,
        "turns": ordered,
        "turn_count": len(ordered),
        "seconds": round(last - started, 2),
        "finished": True,
        # A session orchestrated more than once has more than one trail. Saying
        # so beats silently presenting the latest as though it were the only.
        "collaborations": len(by_room),
        # The caller is reading history, not watching a room. Saying so is the
        # difference between "nothing is happening" and "this already happened".
        "replayed": True,
    }
