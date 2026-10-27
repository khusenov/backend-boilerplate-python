import asyncio
from typing import Annotated

import typer
from pydantic import ValidationError

from app.bootstrap import build_resources
from app.core.config import Settings
from app.core.logging import configure_logging
from app.modules.auth import public as auth

cli = typer.Typer(add_completion=False)


@cli.callback()
def main() -> None:
    pass


async def save_admin(settings: Settings, email: str, name: str, password: str) -> str:
    resources = build_resources(settings)
    try:
        async with resources.sessionmaker.begin() as session:
            admin = await auth.create_admin(
                session, resources.hashing, email, name, password
            )
    finally:
        await resources.close()
    return admin.email


@cli.command()
def create_admin(email: str, name: Annotated[str, typer.Option(prompt=True)]) -> None:
    settings = Settings.model_validate({})
    configure_logging(settings)
    password: str = typer.prompt("Password", hide_input=True, confirmation_prompt=True)
    try:
        saved = asyncio.run(save_admin(settings, email, name, password))
    except ValidationError as error:
        for detail in error.errors(include_input=False):
            typer.echo(f"{detail['loc'][0]}: {detail['msg']}", err=True)
        raise typer.Exit(2) from error
    typer.echo(f"{saved} is an admin")
