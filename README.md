# Iris Cloud

Hosted, multi-user Iris: sign up, record a meeting in the browser, get a transcript and notes where every decision quotes the line it came from.

Self-hosting (fully private, audio never leaves your machine) lives in [h55n/iris](https://github.com/h55n/iris). This repo is the hosted version.

## How the hosted path works
1. Email + password accounts (scrypt hashes, server-side sessions, HttpOnly cookies). Postgres on Neon.
2. The browser records in 5-minute parts (Vercel allows about 4.5 MB per request) and uploads each part.
3. Each part is transcribed by Whisper (large-v3-turbo) on Groq's free tier. Timestamps are stitched together.
4. On Stop, an open model on Groq writes the notes, then Iris's verifier demotes any claim whose quote or owner is not in the transcript.

## Privacy
Audio is sent to Groq for transcription and is not stored by us. Transcripts and notes are stored in our database, tied to your account. Do not use hosted mode for confidential meetings; self-host instead.

## Limits (free tier)
200 accounts, 20 meetings per user per day, 3h20 per meeting, plus Groq's free rate limits. No email verification or password reset yet.

## Deploy your own
Vercel project, a Neon Postgres (`DATABASE_URL`), and `GROQ_API_KEY`. `vercel.json` routes everything to `api/index.py`.
