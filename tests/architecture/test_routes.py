from fastapi.routing import APIRoute, iter_route_contexts

from app.core.config import Settings
from app.core.http.authorization import AnonymousRoute, RequirePermission
from app.main import create_app


def test_every_route_requires_a_permission_or_is_anonymous(settings: Settings) -> None:
    app = create_app(settings)
    contexts = [
        context
        for context in iter_route_contexts(app.routes)
        if isinstance(context.original_route, APIRoute)
    ]
    unmarked = [
        f"{' '.join(sorted(context.methods or ()))} {context.path}"
        for context in contexts
        if not isinstance(context.original_route, AnonymousRoute)
        and not any(
            isinstance(dependency.dependency, RequirePermission)
            for dependency in context.dependencies
        )
    ]

    assert contexts
    assert unmarked == []
