"""core/models/connection.py: a user's database connection.

Navigation:
  Connection   type, non-secret config, and ONLY the encrypted secret

Invariants (enforced by triggers in migration 0002):
  - No plaintext credential column exists; `encrypted_secret` holds a Fernet token.
  - Append-only: a row never changes. To change a host or rotate a secret, register a new
    connection (Q17). A job's pinned connection therefore cannot change underneath it.
    Cost: retrying an old job after a rotation uses the old (possibly revoked) secret.
"""
from sqlalchemy import CheckConstraint, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from core.db.base import Base, TimestampMixin
from core.models.base import UuidPk


class Connection(UuidPk, TimestampMixin, Base):
    __tablename__ = "connection"
    __table_args__ = (
        CheckConstraint("name <> '' AND type <> ''", name="name_and_type_not_empty"),
    )

    name: Mapped[str] = mapped_column(String, nullable=False)
    type: Mapped[str] = mapped_column(String, nullable=False)
    config: Mapped[dict] = mapped_column(JSONB, nullable=False)
    encrypted_secret: Mapped[str] = mapped_column(Text, nullable=False)
