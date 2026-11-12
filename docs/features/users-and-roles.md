# Users and roles

The `users` module owns the `users` table: email, name, role and status. Passwords, sessions and tokens belong to `auth`, covered in [Authentication](auth.md).

| Field | Rules |
|---|---|
| `email` | Unique. Trimmed and lowercased on input, at most 254 characters. |
| `name` | 1 to 100 characters after trimming, no control characters. |
| `role` | `admin` or `member`. New users are members. |
| `status` | `active` or `deactivated`. |

## Endpoints

All routes live under `/v1/users` and need an authenticated caller. Pagination and error responses follow [HTTP API](http-api.md).

| Method | Path | Permission | Effect |
|---|---|---|---|
| `GET` | `/v1/users` | `users:read` | Lists the users the caller may see, newest first, with cursor pagination |
| `POST` | `/v1/users` | `users:write` | Creates a member and queues a welcome email |
| `GET` | `/v1/users/me` | `users:read` | Returns the caller |
| `PATCH` | `/v1/users/me` | `users:write_self` | Updates the caller's `name` |
| `GET` | `/v1/users/{user_id}` | `users:read` | Returns one user |
| `PATCH` | `/v1/users/{user_id}` | `users:write` | Updates `name` and `role` |
| `DELETE` | `/v1/users/{user_id}` | `users:write` | Deactivates the user, returns `204` |

- Members see only themselves, and deactivated users are hidden from everyone. A user you cannot see returns `404 users.not_found`, not `403`, so IDs never leak.
- `PATCH` changes only the fields you send. `null` values and unknown fields return `422`, so `/me` rejects `role` and neither route accepts `email`.
- Email changes go through the verified flow in [Authentication](auth.md).
- A user created with `POST` has no password yet. They set one through password reset.
- A taken email returns `409 users.email_taken`.

## Roles and permissions

`Role`, `Permission` and the `ROLE_PERMISSIONS` map live in [`core/security.py`](../../src/app/core/security.py), so `core` can check them without importing any module.

| Permission | Member | Admin | Grants |
|---|---|---|---|
| `auth:log_out` | yes | yes | Ending the current session |
| `users:read` | yes | yes | Reading users the caller may see |
| `users:read_all` | | yes | Seeing every active user, not only yourself |
| `users:write` | | yes | Creating, updating and deactivating any user |
| `users:write_self` | yes | yes | Updating your own profile, email and password |

The role is read from the `users` row on every request, so a role change applies on the caller's next request without a new login.

### Protecting a route

Add `require(...)` from [`core/http/authorization.py`](../../src/app/core/http/authorization.py) to the route's dependencies. A caller without the permission gets `403 permission_denied`.

```python
@router.patch("/{user_id}", dependencies=[require(Permission.USERS_WRITE)])
async def update_user(...) -> UserRead: ...
```

Public routes go on a router built with `route_class=AnonymousRoute` instead (see [HTTP API](http-api.md)).

`require` checks the role only. Scoping rows to what the caller may see is the service's job: start every query from a scoped statement, as `visible_users(principal)` does in [`users/service.py`](../../src/app/modules/users/service.py).

### Adding a permission or role

1. Add a `Permission` member named `<module>:<action>` in `core/security.py`.
2. Grant it to non-admin roles in `ROLE_PERMISSIONS`. Admins already hold every permission.
3. Use it with `require(...)` on the route, or `principal.can(...)` in a service.

To add a role, add a `Role` member and its `ROLE_PERMISSIONS` entry. A value longer than six characters needs a migration that widens the `VARCHAR(6)` `role` column. Then run `make snapshots`, since roles appear in the OpenAPI schema.

## Audit events

`audit.record(session, action, actor_id=..., target_id=..., client_ip=..., changes=...)` in [`core/audit.py`](../../src/app/core/audit.py) adds an `audit_events` row to the current session, so it commits or rolls back with the change it describes. Each row also stores the request ID from the log context. Never put secrets in `changes`.

| Action | Written when | `changes` |
|---|---|---|
| `auth.logged_in` | A login succeeds | `{}` |
| `auth.password_reset` | A password reset completes | `{}` |
| `auth.password_changed` | A user changes their password | `{}` |
| `users.admin_created` | `create-admin` runs (actor is empty) | `role` |
| `users.role_changed` | `PATCH` sets a different role | `role` |
| `users.email_changed` | An email change is confirmed | new `email` |
| `users.deactivated` | A user is deactivated | `status` |

Rows are kept for 365 days. The hourly `prune_audit_events` task deletes older ones (see [Background jobs](background-jobs.md)). No endpoint reads the table; query it in the database.

## Deactivation

`DELETE /v1/users/{user_id}` keeps the row but anonymizes it. In one transaction it:

1. Sets `status` to `deactivated`.
2. Replaces the email with `<user_id>@deactivated` and clears the name.
3. Writes a `users.deactivated` audit row.
4. Publishes `users.deactivated.v1`.

Authentication rejects deactivated users at once, so their sessions stop working on the next request. The `auth.delete_sessions` subscriber then deletes those sessions (see [Events](events.md)). Login, password reset and pending welcome emails skip deactivated users.

The original email is free to sign up again. The replacement address fails email validation, so no account can claim it. There is no undo, and nothing stops an admin from deactivating their own account. Audit rows are not anonymized: a `users.email_changed` row keeps the address it recorded until it is pruned.

## Creating an admin

`create-admin` creates an admin from the command line.

```sh
uv run --env-file .env backend-boilerplate create-admin ada@example.com --name "Ada Lovelace"
```

- Inside the container image, run `backend-boilerplate create-admin ...` with the same environment as the API.
- It prompts for the name if you omit `--name`, then for the password twice without echoing it.
- The password follows the signup rules in [Authentication](auth.md).
- It is safe to re-run. An existing user with that email becomes an admin, takes the new name and password, and loses all their sessions.
- It writes a `users.admin_created` audit row and sends no email.
- Invalid input prints the reason, exits with code `2` and writes nothing.

## Using users from other modules

Other modules import only [`users/public.py`](../../src/app/modules/users/public.py) (see [Architecture](../architecture.md)).

| Name | Purpose |
|---|---|
| `get_user(session, user_id)` | Load any user as a `UserDTO`, without visibility scoping |
| `find_user_by_email(session, email, for_share=False)` | Look up by email; `for_share=True` takes a `FOR SHARE` row lock |
| `create_user(session, jobs, email, name)` | Create a member and queue the welcome email |
| `create_admin(session, email, name)` | Create or promote an admin |
| `change_email(session, jobs, principal, client_ip, email)` | Change the caller's email, audit it and notify the old address |
| `UserDTO` | `id`, `email`, `role`, `active` |
| `UserDeactivated` | The `users.deactivated.v1` event, carrying `user_id` |

Check `UserDTO.active` before acting on a user loaded this way, since these functions also return deactivated users.
