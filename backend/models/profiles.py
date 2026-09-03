"""Auth/profiles domain models.

Supabase Auth owns `auth.users` (managed by GoTrue). We keep a thin
`public.profiles` mirror synced on login (id = auth.users.id UUID). This is
where app-specific fields live and where foreign keys should point.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from database import Base


class Profile(Base):
    __tablename__ = "profiles"

    # Matches auth.users.id (UUID v4 from Supabase)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)

    # Supabase auth already guarantees email uniqueness; cache here for queries
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)

    # App display name — set from user_metadata or onboarding
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def to_dict(self):
        return {
            "id": str(self.id),
            "username": self.username or (self.email.split("@")[0] if self.email else "adventurer"),
            "email": self.email,
        }
