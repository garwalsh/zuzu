"""Where the fleet's Band credentials come from.

Separate from fleet.py on purpose. The audit trail needs to know which agent id
belongs to which stage, and fleet.py imports the tool layer, which imports the
orchestrator -- so reading credentials from there made a cycle. Credentials
depend on nothing but the role list, which is what this module reflects.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from api.band.roles import ROLES

#: Where agent credentials land once registered. Not in the repo: each of these
#: keys can act as that agent.
CREDENTIALS_PATH = Path(
    os.environ.get(
        "BAND_CREDENTIALS_PATH",
        Path(__file__).resolve().parent.parent.parent / ".band-agents.json",
    )
)


def load_credentials() -> dict[str, dict[str, str]]:
    """Agent ids and keys by role key.

    Environment first, so a deployment holds them as secrets; the file is the
    developer convenience that `tools/register_band_agents.py` writes.
    """
    creds: dict[str, dict[str, str]] = {}
    for role in ROLES:
        env_id = os.environ.get(f"BAND_AGENT_{role.key.upper()}_ID")
        env_key = os.environ.get(f"BAND_AGENT_{role.key.upper()}_KEY")
        if env_id and env_key:
            creds[role.key] = {"id": env_id, "api_key": env_key}
    if len(creds) == len(ROLES):
        return creds
    if CREDENTIALS_PATH.exists():
        stored = json.loads(CREDENTIALS_PATH.read_text(encoding="utf-8"))
        for key, value in stored.get("agents", {}).items():
            creds.setdefault(key, value)
    return creds


def agent_ids() -> dict[str, str]:
    """Just the ids, for anything that only needs to attribute work."""
    return {key: value["id"] for key, value in load_credentials().items()}
