"""Run with RUN_DATABASE_TESTS=1 against the migrated disposable CI database."""

import asyncio
import os
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select

from app.db.models import ChatHistoryCheckpoint, ChatMessage, ChatSession, Workspace
from app.db.session import SessionLocal, engine
from app.services.history_checkpoint import latest_checkpoint, save_checkpoint


@pytest.mark.skipif(os.getenv("RUN_DATABASE_TESTS") != "1", reason="uses the disposable test database")
async def test_checkpoints_follow_branch_scope_and_cascade_without_blocking_other_writers():
    workspace_id, session_id, other_id = uuid4(), uuid4(), uuid4()
    root, fork_a, fork_b, foreign = uuid4(), uuid4(), uuid4(), uuid4()
    try:
        async with SessionLocal.begin() as db:
            db.add(Workspace(id=workspace_id, user_id="test", tenant_id="test", name="Memory test"))
            await db.flush()
            db.add_all([ChatSession(id=sid, workspace_id=workspace_id) for sid in (session_id, other_id)])
            await db.flush()
            db.add(ChatMessage(id=root, session_id=session_id, role="user", content="Root"))
            await db.flush()
            db.add_all([
                ChatMessage(id=fork_a, session_id=session_id, parent_message_id=root, role="assistant", content="A"),
                ChatMessage(id=fork_b, session_id=session_id, parent_message_id=root, role="assistant", content="B"),
                ChatMessage(id=foreign, session_id=other_id, role="assistant", content="Other conversation"),
            ])
        async with SessionLocal.begin() as db:
            assert await save_checkpoint(db, session_id, root, "general", "Shared ancestor") == "Shared ancestor"
            assert await save_checkpoint(db, session_id, root, "grounded", "Document context") == "Document context"
            assert await save_checkpoint(db, session_id, foreign, "general", "Wrong chat") is None
            assert await save_checkpoint(db, other_id, foreign, "general", "Other") == "Other"
        async with SessionLocal.begin() as first:
            assert await save_checkpoint(first, session_id, fork_a, "general", "A only") == "A only"
            async with SessionLocal.begin() as concurrent:
                competing = save_checkpoint(concurrent, session_id, fork_a, "general", "Different")
                assert await asyncio.wait_for(competing, timeout=3) is None
        async with SessionLocal.begin() as db:
            assert await save_checkpoint(db, session_id, fork_a, "general", "Overwrite") == "A only"
            assert await latest_checkpoint(db, session_id, [root, fork_a], "general") == (fork_a, "A only")
            assert await latest_checkpoint(db, session_id, [root, fork_b, foreign], "general") == (root, "Shared ancestor")
            assert await latest_checkpoint(db, session_id, [root, fork_a], "grounded") == (root, "Document context")
            assert await latest_checkpoint(db, session_id, [foreign], "general") is None
        async with SessionLocal.begin() as deleting:
            await deleting.execute(delete(ChatMessage).where(ChatMessage.id == fork_b))
            async with SessionLocal.begin() as concurrent:
                unavailable = save_checkpoint(concurrent, session_id, fork_b, "grounded", "Deleted")
                assert await asyncio.wait_for(unavailable, timeout=3) is None
                # The original request transaction remains usable.
                assert await concurrent.get(ChatSession, session_id) is not None
        async with SessionLocal.begin() as db:
            await db.execute(delete(ChatSession).where(ChatSession.id == session_id))
            anchored = select(func.count()).select_from(ChatHistoryCheckpoint).where(
                ChatHistoryCheckpoint.message_id.in_([root, fork_a, fork_b])
            )
            assert await db.scalar(anchored) == 0
            assert await latest_checkpoint(db, other_id, [foreign], "general") == (foreign, "Other")
    finally:
        async with SessionLocal.begin() as db:
            await db.execute(delete(Workspace).where(Workspace.id == workspace_id))
        await engine.dispose()
