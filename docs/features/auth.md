# Authentication

The `auth` module owns passwords, login sessions, verification codes and reset tokens. The `users` module owns the profile, email address, role and status (see [Users and roles](users-and-roles.md)).

## Endpoints

All paths are under `/v1/auth`. Request bodies are JSON and reject unknown fields.

| Method | Path | Access | Body | Success |
|---|---|---|---|---|
| `POST` | `/session` | Anonymous, trusted `Origin` | `email`, `password` | `204`, sets the session cookie |
| `POST` | `/tokens` | Anonymous | `email`, `password` | `201`, `{token, expires_at}` |
| `DELETE` | `/session` | `auth:log_out` | none | `204`, clears the cookie |
| `POST` | `/signup` | Anonymous | `email` | `202` |
| `POST` | `/signup/confirm` | Anonymous | `email`, `code`, `name`, `password` | `204` |
| `POST` | `/password-reset` | Anonymous | `email` | `202` |
| `POST` | `/password-reset/confirm` | Anonymous | `token`, `password` | `204` |
| `POST` | `/password-change` | `users:write_self` | `current_password`, `new_password` | `204`, clears the cookie |
| `POST` | `/email-change` | `users:write_self` | `email`, `password` | `202` |
| `POST` | `/email-change/confirm` | `users:write_self` | `email`, `code` | `204` |

Email addresses are trimmed and lowercased before use.

## Sessions and tokens

Both login endpoints create the same server-side session. The secret is `secrets.token_urlsafe(32)`, the database stores only its SHA-256 hash in `auth_sessions`, and it expires 30 days after login with no sliding renewal.

- **Browsers** call `POST /v1/auth/session`. The response has no body and sets a `__Host-session` cookie (`Secure`, `HttpOnly`, `SameSite=Lax`, 30-day `Max-Age`), so JavaScript never sees the token. Serve the API on the web app's registrable domain (for example `api.example.com` next to `app.example.com`), because a `SameSite=Lax` cookie is not sent on cross-site requests.
- **Native and server clients** call `POST /v1/auth/tokens` and send the token as `Authorization: Bearer <token>`.

```sh
curl -X POST http://127.0.0.1:8000/v1/auth/tokens \
  -H 'content-type: application/json' \
  -d '{"email": "ada@example.com", "password": "a long and uncommon password"}'
```

On each protected request, `current_principal` in [`core/http/deps.py`](../../src/app/core/http/deps.py) reads the bearer header first and falls back to the cookie. It rejects expired sessions and deactivated users with `401 unauthenticated` and returns a `Principal` with the user ID, session ID and role. The role is read on every request, so a role change applies on the next one.

The cookie login and every cookie-authenticated `POST`, `PUT`, `PATCH` or `DELETE` must send an `Origin` listed in `CORS_ORIGINS`, or they get `403 untrusted_origin`. This blocks CSRF. Bearer requests skip the check, but a cookie login from `curl` needs an `Origin` header too.

Other modules authenticate through `current_principal`, never by importing `auth`; see [Architecture](../architecture.md).

A session ends when:

- the user logs out (only the current session is deleted);
- the password is changed or reset (every session of the user is deleted);
- the user is deactivated (requests are rejected at once, and the `auth.delete_sessions` subscriber deletes the rows);
- it expires (the hourly `auth.prune_expired_rows` task deletes expired sessions, codes and reset tokens).

## Passwords

New passwords must be 15 to 128 characters and must not appear in [`common_passwords.txt`](../../src/app/modules/auth/common_passwords.txt), compared case-insensitively. A failed check returns `422`. Login accepts any password from 1 to 128 characters, so tightening the rules never locks out existing users.

Hashing uses argon2id at the OWASP minimum (`time_cost=2`, `memory_cost=19456` KiB, `parallelism=1`), defined as `PASSWORD_HASHER` in [`service/credentials.py`](../../src/app/modules/auth/service/credentials.py).

- Hashing runs in a pool of four threads per process (`HASHING_THREADS` in [`bootstrap.py`](../../src/app/bootstrap.py)). Login, signup confirmation and reset confirmation queue for those four slots, which caps the memory that concurrent hashing uses.
- When `PASSWORD_HASHER` parameters change, each hash is upgraded on the user's next successful login.
- Unknown or deactivated accounts are checked against a dummy hash, so response time does not reveal which emails are registered.

## Signup

1. The client sends `POST /v1/auth/signup` with an email. The API always answers `202`.
2. The worker mails a verification code. If the address already has an account, it mails a notice telling the owner to sign in instead. The response is identical in both cases, so signup does not reveal registered emails.
3. The client sends `POST /v1/auth/signup/confirm` with the email, code, name and password. The API creates a `member` user, stores the password, queues a welcome email and answers `204`.
4. The client logs in with one of the login endpoints. Confirmation does not start a session.

A wrong or expired code, or an address taken in the meantime, returns `403 auth.invalid_code`.

## Verification codes

Signup and email change share one code mechanism in [`service/verification.py`](../../src/app/modules/auth/service/verification.py).

- A code is 6 digits and expires after 15 minutes.
- Each email address has at most one pending code. A new request replaces the old one.
- The API stores a random seed. The worker derives the code from it with HMAC under `SECRET_KEY`.
- The database holds an HMAC-SHA256 of the email and code, never the code, because a plain hash of six digits cracks offline in moments.
- A code works once and is deleted on success.
- Guesses are limited to 5 per email per 15 minutes. The count lives in the rate limiter, outside the database, so rolling back a failed request does not reset it.

Rotating `SECRET_KEY` invalidates every pending code.

## Password reset

1. The client sends `POST /v1/auth/password-reset` with an email. The API always answers `202`.
2. If an active user has that address, the worker mails a 43-character reset token. Otherwise nothing is sent.
3. The client sends `POST /v1/auth/password-reset/confirm` with the token and a new password. The API answers `204`.

- A token works once and expires after 30 minutes.
- It is stored as a SHA-256 hash in `auth_reset_tokens`. A new request replaces the previous token.
- A successful reset deletes every session of the user and writes an `auth.password_reset` audit event.
- An invalid, used or expired token returns `403 auth.invalid_token`.

The email contains the bare token, not a link. Your frontend needs a page where users paste it, or you change the message in [`tasks.py`](../../src/app/modules/auth/tasks.py) to build a link.

## Password change

`POST /v1/auth/password-change` takes the current and the new password.

- A wrong current password returns `403 auth.wrong_password`.
- On success the API deletes every session of the user, including the current one, and clears the cookie. The client must log in again.
- It also voids pending reset tokens, writes an `auth.password_changed` audit event and mails a notice to the user.

## Email change

1. The signed-in client sends `POST /v1/auth/email-change` with the new address and the current password. The API answers `202`.
2. The worker mails a code to the new address, or the "already has an account" notice if the address is taken.
3. The client sends `POST /v1/auth/email-change/confirm` with the new address and the code. The API answers `204`.

- A wrong password returns `403 auth.wrong_password`.
- On confirmation, `users.change_email()` updates the address, writes a `users.email_changed` audit event and mails a notice to the old address.
- Pending reset tokens are voided. Existing sessions stay valid.

## Rate limits

Limits use the `limits` library with a moving window, defined in [`service/limits.py`](../../src/app/modules/auth/service/limits.py). Every limited endpoint counts the attempt before it checks a password, code or token. Exceeding a limit returns `429 auth.too_many_attempts`.

| Action | Per network | Per email |
|---|---|---|
| Login (both endpoints), password change, email change request | 5 an hour per email | 100 an hour |
| Code request (signup, email change to the new address) | 10 an hour | 5 an hour |
| Code guess (signup and email change confirmation) | 20 an hour | 5 per 15 minutes |
| Reset request | 10 an hour | 5 an hour |
| Reset confirmation | 20 an hour | none |

The login network limit counts each network and email pair separately. A network is the client's IPv4 address or its IPv6 /64. Behind a reverse proxy, set `TRUSTED_PROXY_HOPS` so the client IP is correct (see [Deployment](../deployment.md)).

Counters live in memory (`MemoryStorage` in [`bootstrap.py`](../../src/app/bootstrap.py)). Each API process counts on its own and a restart resets the counts. If you run several API processes, swap in a shared `limits` storage such as Redis.

## Errors

Error bodies and the generic codes (`unauthenticated`, `untrusted_origin`) are in [HTTP API](http-api.md).

| Code | Status | Cause |
|---|---|---|
| `auth.invalid_credentials` | 401 | Wrong email or password, or a deactivated account |
| `auth.too_many_attempts` | 429 | A rate limit was hit |
| `auth.invalid_code` | 403 | Wrong, expired or used code, or the address was taken before confirmation |
| `auth.invalid_token` | 403 | Wrong, expired or used reset token |
| `auth.wrong_password` | 403 | Wrong current password on a password or email change |

## Data and jobs

| Table | Holds |
|---|---|
| `auth_credentials` | One argon2 hash per user |
| `auth_sessions` | Session token hashes and expiry |
| `auth_verification_codes` | Pending codes, one per email |
| `auth_reset_tokens` | Pending reset tokens, one per email |

Rows that reference a user are deleted with it. The module defines these jobs in [`tasks.py`](../../src/app/modules/auth/tasks.py), all described in [Background jobs](background-jobs.md):

- `auth.send_code`, `auth.send_reset_token` and `auth.send_password_changed_notice` send the emails above.
- `auth.delete_sessions` subscribes to `UserDeactivated`.
- `auth.prune_expired_rows` runs hourly.
