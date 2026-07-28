"""Register the Zuzu agent fleet on Band and save their credentials.

Build-time tool, not part of the running service.

    uv run python tools/register_band_agents.py            # register what is missing
    uv run python tools/register_band_agents.py --prune    # also delete stray Zuzu agents
    uv run python tools/register_band_agents.py --force    # re-register everything

Band shows an agent's own API key exactly once, at registration. That key is
what lets the agent connect and act as itself, so it is written to
`.band-agents.json` (gitignored) and must be moved into the deployment's
secrets from there.

Registering is idempotent by name: an agent that already exists is left alone
unless --force is passed, because re-registering mints a new identity and the
old id is what every historical audit entry points at.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.band import protocol  # noqa: E402
from api.band.fleet import CREDENTIALS_PATH  # noqa: E402
from api.band.roles import ROLES  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="re-register even if present")
    parser.add_argument("--prune", action="store_true", help="delete Zuzu agents not in the fleet")
    args = parser.parse_args()

    user_key = os.environ.get("BAND_USER_API_KEY", "")
    if not user_key:
        sys.exit("BAND_USER_API_KEY is not set")

    existing = await protocol.list_registered(user_key)
    by_name = {a["name"]: a for a in existing}
    print(f"{len(existing)} agent(s) already registered on this account\n")

    stored: dict[str, dict[str, str]] = {}
    if CREDENTIALS_PATH.exists():
        stored = json.loads(CREDENTIALS_PATH.read_text(encoding="utf-8")).get("agents", {})

    for role in ROLES:
        name = role.agent_name
        have_key = role.key in stored
        if name in by_name and have_key and not args.force:
            print(f"  {name:22} already registered, key on file")
            continue
        if name in by_name and not args.force:
            print(
                f"  {name:22} registered but its key is not on file -- "
                "re-register with --force to mint a new one"
            )
            continue
        created = await protocol.register_agent(user_key, name, role.description)
        stored[role.key] = created
        print(f"  {name:22} registered  id={created['id']}")

    CREDENTIALS_PATH.write_text(json.dumps({"agents": stored}, indent=2) + "\n", encoding="utf-8")
    CREDENTIALS_PATH.chmod(0o600)
    print(f"\ncredentials written to {CREDENTIALS_PATH} (mode 600)")

    if args.prune:
        keep = {r.agent_name for r in ROLES}
        for agent in existing:
            if agent["name"].startswith("Zuzu-") and agent["name"] not in keep:
                ok = await protocol.delete_agent(user_key, agent["id"])
                print(f"  pruned {agent['name']}: {'ok' if ok else 'failed'}")

    print("\nEnvironment form, for a deployment that keeps these as secrets:")
    for key, value in stored.items():
        print(f"  BAND_AGENT_{key.upper()}_ID={value['id']}")
        print(f"  BAND_AGENT_{key.upper()}_KEY={value['api_key']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
