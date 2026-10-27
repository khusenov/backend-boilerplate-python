from dataclasses import dataclass

from fastapi import params
from fastapi.routing import APIRoute

from app.core.errors import PermissionDeniedError
from app.core.http.deps import CurrentPrincipal
from app.core.security import Permission


class AnonymousRoute(APIRoute):
    pass


@dataclass(frozen=True, slots=True)
class RequirePermission:
    permission: Permission

    async def __call__(self, principal: CurrentPrincipal) -> None:
        if not principal.can(self.permission):
            raise PermissionDeniedError


def require(permission: Permission) -> params.Depends:
    return params.Depends(RequirePermission(permission))
