"""Bootstrap the single local administrator without exposing a password in argv."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import re
import stat
import sys
import uuid
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import settings
from app.db.models import AuthUser
from app.db.session import SessionLocal
from app.services.password_auth import password_hash_async


def read_password_file(filename: str) -> str:
    path = Path(filename)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("Password file must be a private regular file owned by the current user")
    if info.st_size > 1024:
        raise ValueError("Password file is too large")
    return path.read_text(encoding="utf-8").rstrip("\r\n")


async def create_admin(username: str, password: str) -> dict:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{2,63}", username):
        raise ValueError("Invalid username")
    if not 12 <= len(password) <= 128 or "\n" in password or "\r" in password:
        raise ValueError("Administrator password must contain 12–128 characters")
    async with SessionLocal() as db:
        # Concurrent bootstrap calls cannot create multiple administrator accounts.
        await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended('rag-admin-bootstrap', 0))"))
        admin = await db.scalar(select(AuthUser).where(AuthUser.is_admin.is_(True)))
        if admin is not None:
            if admin.username != username or admin.tenant_id != settings.auth_local_tenant:
                raise ValueError("An administrator already exists with a different identity")
            return {"created": False, "username": admin.username, "password_changed": False}
        existing = await db.scalar(select(AuthUser.id).where(AuthUser.username == username))
        if existing is not None:
            raise ValueError("Username is already occupied; refusing to promote an existing user")
        user = AuthUser(
            id=uuid.uuid4(), username=username, subject=settings.auth_local_subject,
            tenant_id=settings.auth_local_tenant, display_name="Администратор",
            password_hash=await password_hash_async(password), is_admin=True, is_active=True,
        )
        db.add(user)
        await db.commit()
        return {"created": True, "username": username, "password_changed": False}


def main() -> int:
    parser = argparse.ArgumentParser(description="Bootstrap the one local administrator")
    parser.add_argument("command", choices=["create-admin"])
    parser.add_argument("--username", default="admin")
    parser.add_argument("--password-file", help="Private 0600 UTF-8 password file; password is never printed")
    args = parser.parse_args()
    try:
        password = read_password_file(args.password_file) if args.password_file else getpass.getpass("New administrator password: ")
        result = asyncio.run(create_admin(args.username.lower(), password))
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ValueError, OSError):
        print("ERROR: invalid input or existing administrator; no password was displayed", file=sys.stderr)
    except SQLAlchemyError:
        print("ERROR: database operation failed; no credentials were displayed", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
