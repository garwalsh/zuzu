"""Add an organisation to the tenant registry and mint its API key.

    uv run python tools/add_tenant.py clinic-a --name "Bay Area Legal Aid"

Prints the key once. Only its SHA-256 goes into `data/tenants.json`, so the key
cannot be recovered from the repo -- if it is lost, mint another and drop the old
hash. That is the same posture as any other credential and the reason the
registry is safe to commit.

The registry is what turns a single-organisation install into a multi-tenant
one. While it does not exist, Zuzu serves one tenant and derives keys through it
anyway, so adding this file later renames nobody's stored data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.tenancy import _SLUG_RE, REGISTRY_PATH  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("tenant_id", help="lowercase slug, e.g. bay-area-legal-aid")
    parser.add_argument("--name", default="", help="human-readable organisation name")
    parser.add_argument(
        "--forms",
        default="",
        help="comma-separated form ids this tenant may file; empty means all",
    )
    parser.add_argument(
        "--store-sensitive",
        action="store_true",
        help="allow sensitive values to persist to the memory store for this tenant",
    )
    parser.add_argument("--registry", default=str(REGISTRY_PATH))
    args = parser.parse_args()

    if not _SLUG_RE.match(args.tenant_id):
        sys.exit(
            f"{args.tenant_id!r} is not a usable slug: lowercase letters, digits and "
            "hyphens, 3-40 characters, not starting or ending with a hyphen"
        )

    path = Path(args.registry)
    registry = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"tenants": []}
    tenants = registry.setdefault("tenants", [])

    entry = next((t for t in tenants if t["id"] == args.tenant_id), None)
    if entry is None:
        entry = {"id": args.tenant_id, "name": args.name or args.tenant_id, "api_key_hashes": []}
        tenants.append(entry)
    if args.name:
        entry["name"] = args.name
    if args.forms:
        entry["allowed_forms"] = [f.strip().upper() for f in args.forms.split(",") if f.strip()]
    if args.store_sensitive:
        entry["store_sensitive"] = True

    # Prefixed so a leaked key is recognisable in a log or a bug report.
    key = f"ztk_{args.tenant_id}_{secrets.token_urlsafe(24)}"
    entry.setdefault("api_key_hashes", []).append(hashlib.sha256(key.encode("utf-8")).hexdigest())

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(registry, indent=2) + "\n", encoding="utf-8")

    print(f"tenant {args.tenant_id!r} written to {path}")
    print(f"  keys on file: {len(entry['api_key_hashes'])}")
    print("\nIts API key, shown once and never stored:\n")
    print(f"  {key}\n")
    print("Callers send it as:  X-Zuzu-Tenant-Key: <key>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
