from app.main import create_app


def test_app_boots() -> None:
    schema = create_app().openapi()

    assert schema["info"]["title"] == "backend-boilerplate"
