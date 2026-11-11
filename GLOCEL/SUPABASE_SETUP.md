# GLOCEL + Supabase Setup

Follow these steps to connect your GLOCEL app to a Supabase PostgreSQL database.

## Step 1: Create a Supabase Project
1. Go to [supabase.com](https://supabase.com) and sign in (or create an account).
2. Click **New Project**.
3. Choose your organization, give it a name (e.g. `glocel`), and set a **database password** — save this somewhere safe.
4. Select a region close to you and click **Create new project**.
5. Wait a minute or two for the project to be ready.

## Step 2: Get Your Database Connection String
1. In the Supabase dashboard, go to **Settings → Database** (left sidebar).
2. Under **Connection string**, find **URI** (or Connection string).
3. Copy the full string. It looks like:
   ```
   postgresql://postgres.xxxxxxxx:your-password@aws-0-xxxx.pooler.supabase.com:6543/postgres
   ```
   > If you see a **Connection Pooler** section, use that URL (port `6543`). Otherwise use the direct connection (port `5432`).

## Step 3: Configure Your App
1. In your `GLOCEL` folder, make a copy of `.env.example` and name it `.env`:
   ```powershell
   copy .env.example .env
   ```
2. Open `.env` in a text editor.
3. Replace `DATABASE_URL=...` with the string you copied from Supabase.
4. Add `?sslmode=require` to the end of the URL if it's not already there:
   ```
   DATABASE_URL=postgresql://postgres.xxxxxxxx:your-password@aws-0-xxxx.pooler.supabase.com:6543/postgres?sslmode=require
   ```
5. Change `SECRET_KEY` to something random (e.g. `glocel-abc123-xyz`).

Save the file.

## Step 4: Install and Run Locally
```powershell
pip install -r requirements.txt
python app.py
```
Open `http://localhost:5000` in your browser.

The app will automatically create all tables in your Supabase database on first start.

## Step 5: Deploy to Render (Optional)
If you want to host it online on Render:

1. Push your `GLOCEL` folder to GitHub.
2. In Render, create a **Web Service** and connect your GitHub repo.
3. Add these environment variables in Render:
   - `DATABASE_URL` = your Supabase connection string (with `?sslmode=require`)
   - `SECRET_KEY` = your random secret
   - `FLASK_ENV` = `production`
   - `ADMIN_USERNAME` / `ADMIN_PASSWORD` = the first admin login (only used if no users exist yet)
4. Render will run `pip install -r requirements.txt` and `gunicorn app:app`.

## Users & sign-in
- Every page requires sign-in. The first admin comes from `ADMIN_USERNAME` / `ADMIN_PASSWORD`;
  if those aren't set, the first visit shows a one-time **setup** page to create the admin.
- Admins can add staff, reset passwords and disable accounts under **Users** (account menu, top right).
- Staff can sell, scan, add/edit/restock parts. Only admins can change settings, manage users and delete parts.

## Troubleshooting
| Problem | Fix |
|---|---|
| `sslmode=require` error | Add `?sslmode=require` to the end of `DATABASE_URL` |
| `password authentication failed` | Double-check the database password you set when creating the Supabase project |
| `relation "parts" does not exist` | The app creates tables automatically — just refresh the page after starting |
| Port already in use | Change `port=5000` in `app.py` to another port like `5001` |
