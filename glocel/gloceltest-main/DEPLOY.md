# GLOCEL — Neon + Vercel deployment

The app runs as a single Vercel Function (Python/Flask) backed by Neon Postgres.
Vercel detects Flask from `pyproject.toml`; the entrypoint is `GLOCEL.app:app`.

## 1. Neon database

1. Create a project at [console.neon.tech](https://console.neon.tech).
2. In **Connect**, choose your branch, role and database, then copy the
   **pooled** connection string (hostname contains `-pooler`).
3. It looks like:
   ```
   postgresql://alex:PASSWORD@ep-cool-darkness-123456-pooler.us-east-2.aws.neon.tech/neondb?sslmode=require&channel_binding=require
   ```
   Keep it secret. Tables are created automatically on the first request.

## 2. Vercel project

1. Push this folder to GitHub/GitLab/Bitbucket. The **repository root is the
   project root** — don't set a subdirectory.
2. Import it at [vercel.com/new](https://vercel.com/new). Vercel detects Flask
   and uses Python 3.12+ (pinned by `requires-python`).
3. Add these Environment Variables (Production, and Preview/Development if
   those deployments also need a database):

   | Key | Value |
   | --- | --- |
   | `DATABASE_URL` | the Neon pooled connection string |
   | `SECRET_KEY` | a random 64-hex string (`python -c "import secrets; print(secrets.token_hex(32))"`) |
   | `ADMIN_USERNAME` | initial admin username (optional) |
   | `ADMIN_PASSWORD` | initial admin password (optional) |

   `SECRET_KEY` is mandatory — the app refuses to start in production without
   it. If `ADMIN_*` are omitted, open the deployed URL once to create the first
   admin via the setup page.
4. Deploy. `maxDuration` is set to 60s in `vercel.json`.

If you use a Neon ↔ Vercel integration instead of a manual `DATABASE_URL`, the
integration injects the connection string and creates a database branch per
preview deployment; the app works with that unchanged.

## 3. How the database is handled

- Driver: `psycopg` 3 (`postgresql+psycopg://`), normalised automatically from
  the plain `postgresql://` URL Neon gives you.
- `pool_pre_ping=True` handles Neon's scale-to-zero reconnects. On Vercel the
  app uses `NullPool` so serverless instances don't hold idle connections —
  Neon's pooler does the pooling.
- `init_db()` is idempotent and retried on cold starts. It runs `create_all()`
  plus additive column checks; there's no destructive migration.
- Static assets (`/static/style.css`, `/static/icon.svg`) live in `public/static`
  because Vercel serves static files from `public/`. Flask also serves them
  locally from the same folder.

## 4. Local development

From this folder:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
copy GLOCEL\.env.example GLOCEL\.env   # then edit it
python GLOCEL\app.py                    # http://localhost:5000
```

Without `DATABASE_URL` the app uses a local SQLite file. With a `DATABASE_URL`
set it uses Neon exactly as in production. You can also run `vercel dev` to
mirror the deployed entrypoint.

## 5. Moving data from the old Supabase/Render database

Neon is a different Postgres instance, so data must be copied if you want to
keep it:

```powershell
pg_dump "<old-postgresql-url>" --no-owner --no-acl -f glocel.sql
psql "<neon-pooled-url>" -f glocel.sql
```

Then deploy. The app's schema check will add any newer columns automatically.
Leave the old database in place until you've verified the new deployment.
