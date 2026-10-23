# Iris Cloud

The web side of [Iris](https://github.com/h55n/iris). Iris records, transcribes and writes meeting notes **on your own device** with local models. This site holds the finished notes so you can read and edit them from any device, like Notion or Granola. Audio is never uploaded in the normal path.

- **Linked notes and a note graph** - `[[links]]` between synced notes, and a Graph page that maps them.
- **Browser encryption** - any synced note can be encrypted in your browser (AES-GCM, key from your passphrase with PBKDF2, 600k rounds). The passphrase never leaves the browser and the site stores only ciphertext. Lose the passphrase and the note is unrecoverable.
- **Share links** - a read-only public link per note, revocable any time, marked noindex. Encrypted notes cannot be shared.
- **Weekly recap** - a page that gathers the last 7 days of notes, with done items struck through.
- **Team participation** - invite people to a note from its People page. Whoever opens the invite link while signed in asks to join; you approve that specific account before it sees anything. Approved members can read the note and comment.

## How it works
1. Sign up here, then make a sync token on the Account page.
2. In the local app run `python3 iris/cli.py sync --server https://iris-cloud-one.vercel.app/sync --notes-only` with `IRIS_SYNC_TOKEN` set.
3. Notes show up in the sidebar under "From your devices". Edits made on the web come back to your device on its next sync.

Invites are bearer links you send yourself, through any channel you trust. The email you type when creating one is only a label for you - it is never checked. Approve only accounts you recognize: an approved account proves control of that account, not a person's identity. Invites expire after 7 days and can be revoked; removing a member cuts access immediately, and encrypting a note cuts its members off.

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
