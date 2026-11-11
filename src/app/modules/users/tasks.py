from procrastinate import Blueprint, JobContext

from app.core.jobs.tasks import resources_of, task
from app.modules.users import service
from app.modules.users.schemas import SendEmailChangedNotice, SendWelcomeEmail

blueprint = Blueprint()


@task(blueprint, SendWelcomeEmail)
async def send_welcome_email(context: JobContext, payload: SendWelcomeEmail) -> None:
    resources = resources_of(context)
    async with resources.sessionmaker.begin() as session:
        user = await service.get_user_unscoped(session, payload.user_id)
    if user.active:
        await resources.email.send(
            to=user.email,
            subject="Welcome",
            text=f"Hi {user.name},\n\nYour account is ready.\n",
        )


@task(blueprint, SendEmailChangedNotice)
async def send_email_changed_notice(
    context: JobContext, payload: SendEmailChangedNotice
) -> None:
    await resources_of(context).email.send(
        to=payload.old_email,
        subject="Your email address was changed",
        text=(
            "Your account no longer uses this email address. "
            "If you did not change it, contact us at once.\n"
        ),
    )
