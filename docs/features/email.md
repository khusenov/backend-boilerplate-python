# Email

The app sends plain-text email over SMTP, and only from background tasks. [`core/email.py`](../../src/app/core/email.py) defines the `EmailSender` protocol and its SMTP implementation, `SmtpEmailSender`. [`bootstrap.py`](../../src/app/bootstrap.py) builds the sender from the `SMTP_*` and `EMAIL_FROM` settings and puts it on `Resources.email`.

## Sending an email

Call `resources.email.send(to=..., subject=..., text=...)` in a task handler; an architecture test fails if `email.send()` appears outside a `tasks.py`. This way mail leaves only after the change that caused it commits, and a failed send retries.

Payloads carry IDs, never secrets: tasks that mail a code or token derive it in the worker, so it never sits in the job table. Load what you need, close the transaction, then send. [Background jobs](background-jobs.md) walks through the full `send_welcome_email` task and how to defer it in the same transaction as the change it reports.

## Messages

There is no template engine. Each task builds its subject and body inline, so you change the wording in the task itself.

| Task | Subject | Sent to | When |
|---|---|---|---|
| `auth.send_code` | Your verification code | The address being verified | Signup or email change request |
| `auth.send_code` | This email address already has an account | The address being verified | The same requests, when the address is taken |
| `auth.send_reset_token` | Reset your password | The account's address | Password reset request for an active user; otherwise nothing is sent |
| `auth.send_password_changed_notice` | Your password was changed | The account's address | Password change, if the user is still active |
| `users.send_welcome_email` | Welcome | The new user | Signup confirmed or `POST /v1/users`, if the user is still active |
| `users.send_email_changed_notice` | Your email address was changed | The old address | Email change confirmed |

[Authentication](auth.md) explains the flows these messages belong to.

Tests find the code by six digits and the reset token by the text `use this token: ` (see [`tests/outbox.py`](../../tests/outbox.py)), so keep both when you reword those messages.

The sender handles plain text only. To send HTML, extend the `EmailSender` protocol, `SmtpEmailSender` and the test `Outbox` together.

## Delivery

`SmtpEmailSender` runs the standard library's `smtplib` in a worker thread to keep the event loop free.

- Each message opens its own connection, with a 10 second timeout.
- With `SMTP_TLS=true` it issues STARTTLS and verifies the server certificate against the system trust store. A server that does not offer STARTTLS fails the send; it never falls back to plain text.
- It logs in only when `SMTP_USERNAME` and `SMTP_PASSWORD` are set.
- Every message gets `From: EMAIL_FROM`, a UTC `Date` and a `Message-ID` on the sender's domain.
- When the server refuses the recipient or the message, the send raises `EmailRefusedError` with the SMTP code only, such as `recipient refused: [550]`. The address stays out of the error, so it never reaches logs or Sentry.

Any failure, refusals included, fails the job, which retries with backoff. Only the final failure is reported to Sentry.

## Mailpit locally

`make dev` starts [Mailpit](https://mailpit.axllent.org/), a local SMTP server that catches every message. `make mailpit` starts it on its own. `.env.example` already points the app at it on `127.0.0.1:1025` with TLS off, and the inbox is at http://127.0.0.1:8025.

With the API and worker running, request a signup and the code appears in the inbox:

```sh
curl -X POST http://127.0.0.1:8000/v1/auth/signup \
  -H 'content-type: application/json' -d '{"email": "ada@example.com"}'
```

Tests swap the sender for the in-memory `outbox` fixture; see [Testing](../testing.md).

## SMTP in production

1. Pick a provider that accepts SMTP submission with STARTTLS, usually on port 587. Implicit TLS on port 465 is not supported.
2. Verify the domain of your `EMAIL_FROM` address with the provider and publish its SPF, DKIM and DMARC records, or messages will land in spam.
3. Set `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD` and `EMAIL_FROM` for every process, as listed in [Configuration](../configuration.md). The API and migrate refuse to boot without `SMTP_HOST` and `EMAIL_FROM` even though only the worker sends. Keep `SMTP_TLS=true`; production refuses to boot with it off.
4. Trigger a signup or password reset against the deployed app and check that the message arrives.
