import pytest
from pydantic import ValidationError

from app.modules.auth.schemas import PasswordSet


@pytest.mark.parametrize("length", [15, 128])
def test_new_passwords_have_15_to_128_characters(length: int) -> None:
    assert PasswordSet(password="x" * length).password == "x" * length


@pytest.mark.parametrize("length", [14, 129])
def test_refused_passwords_stay_out_of_the_error(length: int) -> None:
    password = "hunter" + "x" * (length - 6)
    with pytest.raises(ValidationError) as refused:
        PasswordSet(password=password)

    assert "hunter" not in str(refused.value)
