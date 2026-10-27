import uuid

from app.core.security import ROLE_PERMISSIONS, Permission, Principal, Role


def test_every_role_has_permissions() -> None:
    assert set(ROLE_PERMISSIONS) == set(Role)


def test_admins_hold_every_permission() -> None:
    assert ROLE_PERMISSIONS[Role.ADMIN] == set(Permission)


def test_members_manage_only_themselves() -> None:
    member = Principal(user_id=uuid.uuid7(), session_id=uuid.uuid7(), role=Role.MEMBER)

    allowed = {permission for permission in Permission if member.can(permission)}

    assert allowed == {
        Permission.AUTH_LOG_OUT,
        Permission.USERS_READ,
        Permission.USERS_WRITE_SELF,
    }
