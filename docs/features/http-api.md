# HTTP API

## Versioning

Every module route is mounted under `/v1` in [`main.py`](../../src/app/main.py). Only the health probes, `/health/live` and `/health/ready`, sit outside it (see [Deployment](../deployment.md)). Treat `/v1` as a public contract and change it additively: new endpoints, new optional fields, new error codes. Clients should ignore fields and enum values they do not know. Trailing slashes are not redirected, so `/v1/users/` is a 404.

## Routers

Every route must say who may call it. A module's `router.py` exposes `router`, where each route declares `dependencies=[require(Permission.X)]`. Public endpoints such as login go on an optional `anonymous_router`, built with `route_class=AnonymousRoute`. [`tests/architecture/test_routes.py`](../../tests/architecture/test_routes.py) fails on any route that is neither.

```python
@router.post("", status_code=201, dependencies=[require(Permission.USERS_WRITE)])
async def create_user(body: UserCreate, session: DbSession, jobs: Jobs) -> UserRead:
    user = await service.create_user(session, jobs, body)
    return UserRead.model_validate(user)
```

Routes stay thin: they take dependencies from [`core/http/deps.py`](../../src/app/core/http/deps.py), call the service and convert the result into a response schema. Permissions and `require(...)` are covered in [Users and roles](users-and-roles.md). To add a route step by step, see [Development](../development.md#adding-an-endpoint).

## Schemas

Every request and response model subclasses `Schema` from [`core/schemas.py`](../../src/app/core/schemas.py). It sets `extra="forbid"`, so unknown fields are a 422, plus `frozen=True` and `from_attributes=True`. It also drops `additionalProperties` from the OpenAPI document, so generated clients keep accepting fields you add later. The same module holds the shared field types:

| Type | Behaviour |
|---|---|
| `Email` | Trims, lowercases, at most 254 characters, must look like `name@domain.tld` |
| `Name` | Trims, 1 to 100 characters, no control characters |
| `Timestamp` | Requires a timezone on input; always serialized as UTC with milliseconds, `2026-10-14T09:30:00.000Z` |

[`tests/architecture/test_schemas.py`](../../tests/architecture/test_schemas.py) enforces the rest:

- Models never loosen the `Schema` config, and routes never accept or return job payloads or events.
- Every request input has a type; every request string has a `max_length`, enum, literal or a uuid/date-time format; every request list has a maximum length.
- JSON field names and path and query parameters are snake_case.
- Datetime fields use `Timestamp`, never a bare `datetime`.

PATCH bodies default each field to pydantic's `MISSING`, and the service applies only the fields that were sent. Sending `null` for a field that is not nullable is a 422.

## Error responses

Every error is an [RFC 9457](https://www.rfc-editor.org/rfc/rfc9457) problem document with `Content-Type: application/problem+json`, built in [`core/http/problems.py`](../../src/app/core/http/problems.py):

```json
{
  "type": "about:blank",
  "title": "Not Found",
  "status": 404,
  "code": "users.not_found",
  "request_id": "019a7c3e-5b2f-7d41-9c0e-3f6a2b8d1e47"
}
```

`title` is the HTTP reason phrase. A `detail` string is added only when the error was raised with a message. Clients should branch and localize on `code`, never on `title` or `detail`.

### Raising errors

Services raise subclasses of `AppError` from [`core/errors.py`](../../src/app/core/errors.py), never HTTP exceptions, because only routers and `core.http` may import FastAPI ([Architecture](../architecture.md)). Define module errors in the module's `errors.py` with a code prefixed by the module name:

```python
class UserNotFoundError(NotFoundError):
    code = "users.not_found"
```

`raise UserNotFoundError` gives no `detail`; `raise UserNotFoundError("...")` sends the message to the client, so keep it free of internal data. Base classes:

| Class | Status |
|---|---|
| `UnauthorizedError` | 401 (adds `WWW-Authenticate: Bearer`) |
| `ForbiddenError` | 403 |
| `NotFoundError` | 404 |
| `ConflictError` | 409 |
| `TooManyRequestsError` | 429 |

For any other status, subclass `AppError` and set both `status` and `code`.

### Error codes

| Code | Status | Raised when |
|---|---|---|
| `bad_request` | 400 | The body is not valid UTF-8 |
| `unauthenticated` | 401 | No valid bearer token or session cookie |
| `auth.invalid_credentials` | 401 | Login failed |
| `permission_denied` | 403 | The principal lacks the route's permission |
| `untrusted_origin` | 403 | A cookie-authenticated unsafe request came from an origin outside `CORS_ORIGINS` |
| `auth.invalid_code`, `auth.invalid_token`, `auth.wrong_password` | 403 | A verification code or token is invalid, or the current password is wrong ([Authentication](auth.md)) |
| `not_found`, `users.not_found` | 404 | No route, or no such user |
| `method_not_allowed` | 405 | Wrong method; the `Allow` header lists the right ones |
| `users.email_taken` | 409 | The email belongs to another user |
| `content_too_large` | 413 | The body exceeds 1 MiB |
| `validation_failed` | 422 | The request failed schema validation |
| `auth.too_many_attempts` | 429 | An auth rate limit was hit |
| `internal_server_error` | 500 | An unhandled exception |
| `service_unavailable` | 503 | `/health/ready` cannot reach the database |

Codes are append-only. A snapshot test fails if a published code disappears or changes status, and a new code needs `make snapshots` (see [Testing](../testing.md)). An unhandled exception becomes a bare 500 with no `detail`, still carrying CORS headers, and is re-raised so the server logs it and Sentry reports it. If a streaming response has already started, the client gets a broken stream instead of a problem body.

## Validation errors

A 422 adds an `errors` array to the problem body. Body errors carry a JSON Pointer to the field, as a URI fragment; query, path and header errors name the `parameter` instead:

```json
"errors": [
  {"pointer": "#/password", "detail": "Field required"},
  {"pointer": "#/role", "detail": "Extra inputs are not permitted"}
]
```

An unreadable JSON body points at `#`. Nested query values use dots, for example `ids.1`. The response never echoes the submitted value, so a rejected password is never sent back.

## Pagination

List endpoints use keyset pagination from [`core/pagination.py`](../../src/app/core/pagination.py). Take `PageQuery` as the query model: `limit` (1 to 100, default 20) and an opaque `cursor`. Return `{"items": [...], "next_cursor": "..."}`, where `next_cursor` is `null` on the last page. Declare a response schema with `items: list[XRead]` and `next_cursor: str | None` (see `UserPage`) and return `XPage.model_validate(page)` from the route. There are no totals, because counting gets slow as tables grow.

Follow `list_users` in [`users/service.py`](../../src/app/modules/users/service.py): fetch `limit + 1` rows ordered by `id` descending, filtered by `id < decode_cursor(cursor)` when a cursor is sent. If the extra row comes back, drop it and set `next_cursor` to `encode_cursor` of the last item's `id`. Primary keys are UUIDv7, so this lists newest first. A malformed cursor is a 422 on `cursor`.

## Request IDs

Each request gets a fresh UUIDv7, returned in the `X-Request-ID` header and in every problem body. An incoming `X-Request-ID` is ignored. The ID is bound to every log line written during the request and stored on audit events, so a client can quote it and you can find the matching logs. Once a request is authenticated, `user_id` is bound to its log lines too.

## Logging

Logs go to stdout through structlog, configured in [`core/logging.py`](../../src/app/core/logging.py): JSON lines when `APP_ENV=production`, readable console output otherwise, at level `INFO`. Get a logger with `structlog.stdlib.get_logger(__name__)`.

- Use dotted event names such as `health.not_ready` and pass data as keyword arguments, not inside the message.
- Values under any key containing `password`, `secret`, `token`, `authorization`, `cookie`, `session`, `csrf`, `api_key`, `credential`, `private_key`, `dsn`, `passwd` or `apikey` are replaced with `[REDACTED]`, at any nesting depth. Only keys are checked, so never format a secret into the event string. Matching ignores case and treats `-` as `_`.
- Every request except `/health/*` produces one `http.request` line with `method`, `path`, `status`, `client_ip` and `duration_ms`. Uvicorn's own access log is off.

## CORS and security headers

Cross-origin access is limited to the origins in `CORS_ORIGINS` ([Configuration](../configuration.md)):

- Credentials are allowed and `X-Request-ID` is exposed.
- Methods: `GET`, `POST`, `PUT`, `PATCH`, `DELETE`.
- No extra request headers are allowed, so a browser on another origin authenticates with the session cookie, not `Authorization`.
- Cookie-authenticated unsafe requests must send an `Origin` from the list ([Authentication](auth.md)).

Every response carries `X-Content-Type-Options: nosniff`, `Cache-Control: no-store` and `Referrer-Policy: no-referrer` unless the route sets its own. Production adds `Strict-Transport-Security: max-age=63072000; includeSubDomains`.

## OpenAPI

Outside production the app serves the schema at `/openapi.json`, Swagger UI at `/docs` and ReDoc at `/redoc`. Production serves none of them.

- Operation IDs are `<module>_<function>`, for example `users_list_users`, so generated clients get stable method names.
- Every operation documents `4XX` and `5XX` responses as `Problem`, replacing FastAPI's default 422 schema.
- The schema is snapshotted in `tests/architecture/snapshots/openapi.json`. When you change the API on purpose, run `make snapshots` and review the diff.

## Rate limiting and client IP

There is no global rate limit. The auth endpoints use `Limiter` from `core/http/deps.py`, an in-memory moving-window limiter. Reuse it for any endpoint that needs throttling. Counts live in each process, so every replica enforces its own limit. The `ClientIp` dependency returns the caller's address. Behind a proxy, set `TRUSTED_PROXY_HOPS` ([Deployment](../deployment.md)).
