from datetime import timedelta

from procrastinate import Blueprint, JobContext

from app.core.jobs.tasks import resources_of, settings_of, subscriber, task
from app.modules.auth.schemas import SendCode, SendPasswordChangedNotice, SendResetToken
from app.modules.auth.service import credentials, passwords, verification
from app.modules.users import public as users

CODE_MINUTES = verification.CODE_LIFETIME // timedelta(minutes=1)
RESET_MINUTES = passwords.RESET_LIFETIME // timedelta(minutes=1)

blueprint = Blueprint()


@task(blueprint, SendCode)
async def send_code(context: JobContext, payload: SendCode) -> None:
    resources = resources_of(context)
    secret_key = settings_of(context).secret_key
    async with resources.sessionmaker.begin() as session:
        verification_code = await verification.find_verification_code(
            session, payload.code_id
        )
        if verification_code is None:
            return
        code = await verification.issue_code(session, secret_key, verification_code)
    if code is None:
        await resources.email.send(
            to=verification_code.email,
            subject="This email address already has an account",
            text=(
                "Someone asked to use this email address, which already belongs "
                "to an account. If it was you, sign in with it.\n"
            ),
        )
        return
    await resources.email.send(
        to=verification_code.email,
        subject="Your verification code",
        text=(
            f"Your verification code is {code}. "
            f"It expires in {CODE_MINUTES} minutes.\n\n"
            "If you did not ask for it, you can ignore this email.\n"
        ),
    )


@task(blueprint, SendResetToken)
async def send_reset_token(context: JobContext, payload: SendResetToken) -> None:
    resources = resources_of(context)
    secret_key = settings_of(context).secret_key
    async with resources.sessionmaker.begin() as session:
        reset = await passwords.find_reset_token(session, payload.reset_id)
        if reset is None:
            return
        token = await passwords.issue_reset_token(session, secret_key, reset)
    if token is None:
        return
    await resources.email.send(
        to=reset.email,
        subject="Reset your password",
        text=(
            f"To choose a new password, use this token: {token}\n\n"
            f"It expires in {RESET_MINUTES} minutes. "
            "If you did not ask for it, you can ignore this email.\n"
        ),
    )


@task(blueprint, SendPasswordChangedNotice)
async def send_password_changed_notice(
    context: JobContext, payload: SendPasswordChangedNotice
) -> None:
    resources = resources_of(context)
    async with resources.sessionmaker.begin() as session:
        user = await users.get_user(session, payload.user_id)
    if user.active:
        await resources.email.send(
            to=user.email,
            subject="Your password was changed",
            text=(
                "The password of your account was changed. "
                "If you did not change it, reset it at once.\n"
            ),
        )


@subscriber(blueprint, users.UserDeactivated, name="auth.delete_sessions")
async def delete_sessions(context: JobContext, event: users.UserDeactivated) -> None:
    async with resources_of(context).sessionmaker.begin() as session:
        await credentials.delete_sessions(session, event.user_id)
