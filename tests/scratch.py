import uuid

from sqlalchemy import CheckConstraint, ForeignKey, Index, MetaData, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class ScratchBase(Base):
    __abstract__ = True
    metadata = MetaData(naming_convention=Base.metadata.naming_convention)


class ScratchNote(ScratchBase):
    __tablename__ = "scratch_notes"

    body: Mapped[str]


class ScratchTag(ScratchBase):
    __tablename__ = "scratch_tags"
    __table_args__ = (
        UniqueConstraint("note_id", "name"),
        Index(None, "name", "created_at"),
        CheckConstraint("length(name) > 0", name="name_not_empty"),
    )

    note_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("scratch_notes.id"), index=True
    )
    name: Mapped[str]
