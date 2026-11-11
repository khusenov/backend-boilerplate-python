from datetime import timedelta

from procrastinate import Blueprint, JobContext

from app.core.jobs.tasks import resources_of, settings_of, task
from app.modules.auth import service
from app.modules.auth.schemas import SendCode

CODE_MINUTES = service.CODE_LIFETIME // timedelta(minutes=1)

blueprint = Blueprint()


@task(blueprint, SendCode)
async def send_code(context: JobContext, payload: SendCode) -> None:
    resources = resources_of(context)
    secret_key = settings_of(context).secret_key
    async with resources.sessionmaker.begin() as session:
        verification = await service.find_verification_code(session, payload.code_id)
        if verification is None:
            return
        code = await service.issue_code(session, secret_key, verification)
    if code is None:
        await resources.email.send(
            to=verification.email,
            subject="This email address already has an account",
            text=(
                "Someone asked to use this email address, which already belongs "
                "to an account. If it was you, sign in with it.\n"
            ),
        )
        return
    await resources.email.send(
        to=verification.email,
        subject="Your verification code",
        text=(
            f"Your verification code is {code}. "
            f"It expires in {CODE_MINUTES} minutes.\n\n"
            "If you did not ask for it, you can ignore this email.\n"
        ),
    )
