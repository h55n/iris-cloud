# Security

Last audit: 23 Oct 2026, by the author. Every claim below has a test in `tests/test_hosted.py` or was checked on the live site. Not a third-party audit.

## What is protected, and how
- **Passwords**: scrypt with a random salt. Sessions are random 256-bit tokens, stored as SHA-256 hashes, in an HttpOnly, Secure, SameSite=Lax cookie, 30 days, deleted on logout, on password reset and on account deletion.
- **Brute force**: login is limited per email (8 per 10 min) and per IP (40 per 10 min). Reset is limited per email (5) and per IP (20). Signup is limited per IP (10 per hour). An unknown email costs the same time as a real one.
- **Password reset**: a 64-bit recovery code shown once at signup, stored hashed, used up on reset. No email is involved.
- **CSRF**: POSTs that carry a session cookie are refused when `Origin` is another site or `null`, or when the browser says `Sec-Fetch-Site: cross-site`. Token-authenticated sync calls use a header, not the cookie.
- **Headers**: Content-Security-Policy (self only, no framing, no plugins, forms post to self), nosniff, X-Frame-Options DENY, Referrer-Policy same-origin, Permissions-Policy, HSTS from Vercel. Pages and API responses are `no-store`.
- **Isolation**: every query that reads or writes a meeting, synced note or token is scoped to the signed-in user. Tests cover viewing, finishing, deleting, editing, token use and the sync list across two users.
- **XSS**: titles, notes, transcript lines and the web editor text are HTML-escaped. Tests post script, img and svg payloads through the viewer, editor, sidebar and titles.
- **Sync tokens**: random, stored hashed, shown once, at most 5 per user, revocable. Audio fields are refused. Transcripts are dropped unless the user opts in, and turning that off deletes stored ones.
- **Groq**: audio goes to Groq only in the backup recorder, behind a checkbox. The notes backup needs `consent_groq: true`. Nothing from either is stored except the resulting transcript and notes of a recorded meeting. We do not keep audio.
- **Secrets**: `GROQ_API_KEY` is a sensitive Vercel variable. Database credentials come from the Neon integration. No secrets are in the repo or its history (scanned).
- **Dependencies**: `pip-audit` on `requirements.txt` found nothing on 22 Oct 2026. The versions are not pinned.
- **Share links**: random 128-bit tokens, read-only, `noindex,nofollow` in both meta and header, revocable from the note page. Browser-encrypted notes are refused. Anyone with the link can read the note until it is revoked.
- **Browser encryption**: AES-GCM with a key derived from the passphrase by PBKDF2 (600k rounds), all in the browser. The passphrase never leaves the browser; only ciphertext and a salt/IV reach the server. The note's existence, timestamps and size stay visible. Encrypting does not rewrite older server backups or logs. Losing the passphrase means the note is unrecoverable - there is no reset.
- **Team participation**: invites are single-account bearer links (first account to accept binds it atomically), expire after 7 days, and can be revoked before acceptance. An accepted account sees nothing until the note owner approves that specific account; the note title and content are not disclosed before approval. The typed email is a label only and grants nothing. Members get read and comment access only while the note stays plaintext; browser-encrypting the note cuts members off, and removing a member cuts access immediately. Tests cover pending-member invisibility, approval, revocation, expiry, concurrent acceptance, the encryption cutoff, comment privacy and owner-only team pages.

## Known limits (not fixed)
- Synced notes are plain text in the database unless encrypted on the device first. The site cannot show encrypted notes.
- There is no email verification. Signup tells you when an email is already taken (account enumeration). Lose the password and the recovery code and the account is gone.
- Sync tokens expire after 90 days. Sessions are not listed per device.
- Scripts are locked to self plus a per-response CSP nonce (inline event handlers are gone). Styles are still inline, which is the remaining CSP gap.
- Backup mode sends audio to Groq. What Groq does with it is governed by Groq's terms, not ours. Do not use it for confidential meetings.
- The database is a shared free-tier Neon instance. We have no extra encryption layer on top of Neon's.

Report problems by opening a GitHub issue.
