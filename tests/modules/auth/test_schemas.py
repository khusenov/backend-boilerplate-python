import pytest
from pydantic import ValidationError

from app.modules.auth.schemas import COMMON_PASSWORDS, PasswordSet
from tests.factories import PASSWORD


def phrase(length: int) -> str:
    return (PASSWORD * 5)[:length]


@pytest.mark.parametrize("length", [15, 128])
def test_new_passwords_have_15_to_128_characters(length: int) -> None:
    assert PasswordSet(password=phrase(length)).password == phrase(length)


@pytest.mark.parametrize("length", [14, 129])
def test_refused_passwords_stay_out_of_the_error(length: int) -> None:
    password = "hunter" + "x" * (length - 6)
    with pytest.raises(ValidationError) as refused:
        PasswordSet(password=password)

    assert "hunter" not in str(refused.value)


@pytest.mark.parametrize(
    "password",
    ["correcthorsebatterystaple", "CorrectHorseBatteryStaple", "xxxxxxxxxxxxxxx"],
)
def test_common_passwords_are_refused_in_any_case(password: str) -> None:
    with pytest.raises(ValidationError, match="this password is too common") as refused:
        PasswordSet(password=password)

    assert password not in str(refused.value)


def test_the_common_list_holds_casefolded_passwords_of_15_or_more_characters() -> None:
    unusable = [
        password
        for password in COMMON_PASSWORDS
        if len(password) < 15 or password != password.casefold()
    ]

    assert COMMON_PASSWORDS
    assert unusable == []
