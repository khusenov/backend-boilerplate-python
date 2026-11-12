# Security policy

## Reporting a vulnerability

Building a product from this template? Replace this section with your own contact and turn on private vulnerability reporting in your repository settings.

Report vulnerabilities privately through GitHub: open the repository's **Security** tab and choose **Report a vulnerability**. Do not open a public issue or pull request.

Include the affected file or endpoint, steps to reproduce, and the impact. Report against the current `main` branch; the template has no versioned releases.

## Built-in protections

| Area | What the template does | Details |
|---|---|---|
| Passwords | argon2id hashing (OWASP minimum parameters); 15 to 128 characters; common passwords rejected | [Authentication](docs/features/auth.md#passwords) |
| Sessions | Random tokens stored only as SHA-256 hashes; browser sessions use a `__Host-session` cookie that is `Secure`, `HttpOnly` and `SameSite=Lax`; changing or resetting a password revokes every session | [Authentication](docs/features/auth.md#sessions-and-tokens) |
| CSRF | Cookie login and cookie-authenticated unsafe requests must send an `Origin` listed in `CORS_ORIGINS` | [Authentication](docs/features/auth.md#sessions-and-tokens) |
| Account enumeration | Signup, password reset and email-change requests answer `202` whether or not the account exists; login checks unknown emails against a dummy hash so timing does not reveal them | [Authentication](docs/features/auth.md#signup) |
| Brute force | Rate limits keyed by client network and email on login, codes and resets | [Authentication](docs/features/auth.md#rate-limits) |
| Authorization | Every route declares a permission or is explicitly anonymous; an architecture test fails otherwise | [Users and roles](docs/features/users-and-roles.md) |
| Input | Schemas reject unknown fields and bound every string and list; bodies over 1 MiB get `413` | [HTTP API](docs/features/http-api.md#schemas) |
| Headers | `nosniff`, `no-store` and `no-referrer` on every response; HSTS in production | [HTTP API](docs/features/http-api.md#cors-and-security-headers) |
| API docs | The OpenAPI schema and docs UI are off in production | [HTTP API](docs/features/http-api.md#openapi) |
| Secret handling | Credential settings are `SecretStr`; logs redact credential-like keys; Sentry gets no request bodies or local variables | [Configuration](docs/configuration.md#secrets) |
| Unsafe config | `APP_ENV=production` refuses to boot with weak or missing security settings (secret key, Sentry, proxy hops, SMTP TLS, https CORS origins) | [Configuration](docs/configuration.md#production-mode) |
| Container | Runs as the non-root `app` user; `.env` never enters the build context | [Deployment](docs/deployment.md#container-image) |
| Supply chain | CI runs gitleaks over the full git history and OSV-Scanner over `uv.lock`; Dependabot updates dependencies weekly | [`ci.yml`](.github/workflows/ci.yml) |

## What you must configure

These are the security items from the [production checklist](docs/deployment.md#production-checklist), which has the full rollout list.

1. Generate a random `SECRET_KEY` of at least 32 characters. Keep it and every other credential in your platform's secret store.
2. Terminate TLS at a reverse proxy and set `TRUSTED_PROXY_HOPS` to the exact number of proxies, so clients cannot spoof the IP that rate limits use.
3. Set `CORS_ORIGINS` to your frontend's `https://` origins. Serve the API on the same registrable domain as the frontend, so browsers send the `SameSite=Lax` session cookie.
4. Use an SMTP provider that supports STARTTLS, so codes and reset tokens are not sent in plain text.
5. Point `SENTRY_DSN` at your project, set `SENTRY_ENVIRONMENT`, and alert on new issues and the `worker-heartbeat` monitor.
6. Rate-limit counters live in each process's memory. If you run more than one API process, swap in a shared `limits` storage such as Redis (see [Authentication](docs/features/auth.md#rate-limits)).
7. Enable Dependabot alerts and secret scanning in your repository settings.
