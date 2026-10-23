# Iris Cloud

The web side of [Iris](https://github.com/h55n/iris). Iris records, transcribes and writes meeting notes **on your own device** with local models. This site holds the finished notes so you can read and edit them from any device, like Notion or Granola. Audio is never uploaded in the normal path.

- **Linked notes and a note graph** - `[[links]]` between synced notes, and a Graph page that maps them.

## How it works
1. Sign up here, then make a sync token on the Account page.
2. In the local app run `python3 iris/cli.py sync --server https://iris-cloud-one.vercel.app/sync --notes-only` with `IRIS_SYNC_TOKEN` set.
3. Notes show up in the sidebar under "From your devices". Edits made on the web come back to your device on its next sync.

The sync endpoints (`/sync/push`, `/sync/list`, `/sync/pull`) speak the same protocol as the self-hosted relay in the main repo. The server drops transcripts unless you turn them on in Account, and refuses anything that looks like audio. If you run `iris encrypt` locally first, the site stores ciphertext it cannot read and says so.

## Backup recorder (opt-in)
For a device that cannot run Iris locally, `/record` records in the browser in 5-minute parts, sends each to Whisper (large-v3-turbo) on Groq's free tier, and an open model on Groq writes the notes, which the Iris verifier then checks quote by quote. In this mode audio goes to Groq. It is not stored by us, but transcripts and notes are. Do not use it for confidential meetings.

The same consent rule covers a second backup: `POST /api/notes` takes a transcript you made locally (`[MM:SS] text` lines, max 16000 characters) with `consent_groq: true` and a sync token, has Groq write the notes, verifies every quote, and returns them. Nothing is stored. 30 calls per user per day.

## Privacy
Default path: only notes are stored (and transcripts if you opt in). Backup recorder: audio goes to Groq, as above. Accounts and their data can be deleted from the Account page.

## Limits (free tier)
200 accounts, 20 meetings per user per day, 3h20 per meeting, plus Groq's free rate limits. No email verification (no free mail sender). Password reset works with a recovery code shown once at signup; lose both and the account is gone. Accounts can be deleted from the Account page.

## Deploy your own
Vercel project, a Neon Postgres (`DATABASE_URL`), and `GROQ_API_KEY`. `vercel.json` routes everything to `api/index.py`.
