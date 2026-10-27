import pytest
from pydantic import ValidationError

from app.modules.users.schemas import UserCreate


@pytest.mark.parametrize("name", [" ", "x" * 101, "Ada\x00"])
def test_malformed_names_are_rejected(name: str) -> None:
    with pytest.raises(ValidationError):
        UserCreate(email="ada@example.com", name=name)
