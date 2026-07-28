# Putting Zuzu's memory in Supabase

Zuzu's three memory tiers work out of the box on SQLite with no configuration.
The reason to move them to Supabase is durability: Render's filesystem is
ephemeral, so SQLite memory survives a restart but not a redeploy. Point Zuzu at
Supabase and a returning caller is still remembered next week.

Nothing else changes. Same three tiers, same tenant scoping, same agent tools.

## 1. Create the table

In the Supabase dashboard, open **SQL Editor** and run this:

```sql
create table if not exists memories (
    scope text not null,
    tier  text not null,
    key   text not null,
    value text not null,
    meta  jsonb not null default '{}'::jsonb,
    at    double precision not null,
    primary key (scope, tier, key)
);

-- Scope leads every query Zuzu makes. Without this index one organisation's
-- recall scans every other organisation's rows.
create index if not exists memories_scope_tier on memories (scope, tier);

-- Nothing but the service key should ever touch this table. Enabling row level
-- security with no policy denies every anon and authenticated request, while
-- the service key bypasses RLS entirely -- which is exactly the split we want,
-- because this table holds applicants' names and dates of birth.
alter table memories enable row level security;
```

`scope` is already a hash of `(tenant, caller)`, so the table never holds a raw
phone number and one row cannot be traced back to a person from the database
alone.

## 2. Send me two values

From **Project Settings → API**:

| What | Where it is | Looks like |
|---|---|---|
| `SUPABASE_URL` | "Project URL" | `https://abcdefgh.supabase.co` |
| `SUPABASE_SERVICE_KEY` | "Project API keys" → **`service_role`**, revealed with *Reveal* | a long JWT starting `eyJ...` |

Take the **`service_role`** key, not `anon`. The anon key is subject to row level
security, which the SQL above sets to deny everything — so anon would read
nothing and every caller would look new.

That key can read and write the whole project. Treat it like a password: it goes
into Render's environment, never into the repository.

## 3. What happens then

Set both on the Render service and redeploy. On startup Zuzu checks the store
can actually be reached and says which one it is using:

```
startup complete; band fleet up, memory supabase
```

If the credentials are wrong you get this instead, loudly, rather than a service
that quietly treats every returning caller as a stranger:

```
memory backend supabase is NOT reachable (...) -- every caller will look new
until this is fixed
```

`GET /health` reports the same thing, so it can be checked without reading logs:

```json
{"status": "ok", "memory": {"backend": "supabase", "reachable": true}}
```

## Moving what is already stored

Nothing is migrated automatically. The SQLite file only ever held demo runs, so
the simplest thing is to let it go — the first real call repopulates the tiers.
If you do want to carry it across:

```bash
uv run python - <<'PY'
import asyncio, os
from api.memory_store import SqliteMemory, SupabaseMemory

async def main():
    src = SqliteMemory()
    dst = SupabaseMemory(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
    import sqlite3
    scopes = {r[0] for r in sqlite3.connect(src._path).execute("select distinct scope from memories")}
    for scope in scopes:
        for record in await src.all(scope):
            await dst.put(scope, record)
    print(f"copied {len(scopes)} scope(s)")

asyncio.run(main())
PY
```

## Why not mem0 open source

mem0's own Supabase store needs `vecs`, which wants a Postgres connection string
rather than the REST API, and mem0 needs an embedder — TokenRouter serves one
model and no embeddings, so that would mean an OpenAI key or a local ONNX model
on a 512 MB instance.

More to the point, mem0's value is semantic search over conversational text.
Zuzu's memory is `given_name → "Maria"`: exact keys, looked up by exact keys.
The embedder and the vector index would be paying for a similarity search that
nothing here performs.
