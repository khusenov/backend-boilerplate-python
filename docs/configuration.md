# Configuration

All configuration comes from environment variables, read into one frozen `Settings` object in [`core/config.py`](../src/app/core/config.py). Each entry point (the API, the worker, migrations and the CLI) builds it at startup, so a missing or invalid value stops the process before it serves anything.

## How values are read

- Only process environment variables are read. `Settings` never opens a `.env` file itself; see [Development](development.md#the-env-file) for how local commands load it.
- An empty value counts as unset: `SENTRY_DSN=` means no Sentry, and `SECRET_KEY=` fails as missing.
- Validation errors name the setting; secret values are never echoed.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `APP_ENV` | required | `local`, `test` (used by the test suite, behaves like `local`) or `production`. See [Production mode](#production-mode). |
| `SECRET_KEY` | required | Key for the HMACs behind email verification codes and password reset tokens. |
| `DATABASE_URL` | required | PostgreSQL URL, for example `postgresql://app:app@127.0.0.1:5433/app`. The app sets the driver itself, so use a plain `postgresql://` scheme. |
| `CORS_ORIGINS` | empty | Comma-separated browser origins allowed to call the API with credentials. |
| `TRUSTED_PROXY_HOPS` | `0` | Number of reverse proxies in front of the app that append to `X-Forwarded-For`. |
| `SENTRY_DSN` | unset | Sentry project DSN. Unset turns error reporting off. |
| `SENTRY_ENVIRONMENT` | unset | Sentry environment name. Falls back to `APP_ENV`. |
| `SMTP_HOST` | required | SMTP server host. |
| `SMTP_PORT` | `587` | SMTP server port, 1 to 65535. |
| `SMTP_USERNAME` | unset | SMTP login. Set together with `SMTP_PASSWORD` or not at all. |
| `SMTP_PASSWORD` | unset | SMTP password. |
| `SMTP_TLS` | `true` | Upgrade the connection with STARTTLS before sending. |
| `EMAIL_FROM` | required | Plain sender address such as `noreply@example.com`, without a display name. |
| `GIT_SHA` | `unknown` | Release identifier sent to Sentry. The image sets it at build time. |

### SECRET_KEY

Verification codes are stored as an HMAC under this key, so rotating it invalidates codes that are already in users' inboxes. Password reset tokens already sent stay valid, and login sessions and password hashes do not depend on it.

### CORS_ORIGINS

Each entry must be a bare origin: scheme, lowercase host and optional port, with no path or trailing slash (`https://app.example.com`, `http://localhost:3000`). Outside production, `*` is also accepted as an entry.

The same list is the allowlist for the `Origin` check on cookie login (`POST /v1/auth/session`) and cookie-authenticated unsafe requests; a literal `*` never matches it, so list your frontend origin even locally. See [Authentication](features/auth.md#sessions-and-tokens).

### TRUSTED_PROXY_HOPS

The client IP feeds rate limits, audit events and logs, so it must not be spoofable. `0` ignores `X-Forwarded-For` and uses the TCP peer address; use it when nothing sits in front of the app. Otherwise set it to exactly the number of proxies you run. [Deployment](deployment.md#reverse-proxy-and-client-ip) explains how the IP is resolved.

### SMTP

`SMTP_TLS=true` connects in plain text and then issues STARTTLS, which matches port 587. Implicit TLS on port 465 is not supported. Locally, `.env.example` points at Mailpit on `127.0.0.1:1025` with TLS off. See [Email](features/email.md).

## Production mode

`APP_ENV=production` changes behaviour:

- Logs are JSON instead of the console format.
- `/openapi.json` and the interactive docs are not served ([HTTP API](features/http-api.md#openapi)).
- Responses carry a `Strict-Transport-Security` header ([HTTP API](features/http-api.md)).

It also refuses to boot unless every rule below holds. All violations are reported in one error.

| Rule | Why |
|---|---|
| `SECRET_KEY` has at least 32 characters | Short keys make the HMACs guessable. |
| `SENTRY_DSN` and `SENTRY_ENVIRONMENT` are set | Production errors must reach someone. |
| `TRUSTED_PROXY_HOPS` is above 0 | Production runs behind a proxy (see [TRUSTED_PROXY_HOPS](#trusted_proxy_hops)). |
| `SMTP_TLS` is on | Codes and reset links must not cross the network in plain text. |
| `CORS_ORIGINS` lists only `https://` origins, and at least one | Cookie sessions need an explicit, encrypted frontend origin. |

## The `.env.example` file

[`.env.example`](../.env.example) lists every setting exactly once with working local values for the Compose services. A test enforces both: it fails if a field is added to `Settings` without a line here, and if the file stops producing valid `local` settings.

## Secrets

- Keep production values in your platform's secret store and inject them as environment variables. Never commit them or bake them into the image; `.dockerignore` keeps `.env` out of the build context.
- Credential fields (`SECRET_KEY`, `DATABASE_URL`, `SENTRY_DSN`, `SMTP_USERNAME`, `SMTP_PASSWORD`) are `SecretStr`, so they print as `**********` in reprs and logs. A test fails if a field whose name looks like a credential is not a `SecretStr`.

Generate a secret key with:

```sh
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

## Adding a setting

1. Add a typed field to `Settings`. Use `SecretStr` for anything secret.
2. Add a validator if a bad value should stop the process, and a production rule in `check_production` if the safe value differs in production.
3. Add the variable to `.env.example` with a working local value.
4. If it is required, add it to `TEST_ENVIRONMENT` in `tests/environment.py`, to the `settings` and `production_settings` fixtures in `tests/conftest.py`, and to the `x-app` environment in `compose.yaml` so `make smoke` still boots.
5. Read it through `get_settings(request)` from `core/http/deps.py` in HTTP code, or `settings_of(context)` from `core/jobs/tasks.py` in tasks.
