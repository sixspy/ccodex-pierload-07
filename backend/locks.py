"""码头锁与争用登记（写入路径嵌入）。"""
from __future__ import annotations

import sqlite3
import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from auth import AuthHeader, require_user, require_writer
from db import connect, now_iso, write_audit

router = APIRouter(tags=["locks"])


class LockIn(BaseModel):
    lock_key: str
    action: str = "acquire"


def session_holder(user: str) -> str:
    """会话级持锁标识：同账号两路并发也必须互斥，不得靠同名互认放行。"""
    return f"{user}#{uuid.uuid4().hex[:12]}"


def try_acquire(conn, lock_key: str, holder: str, actor: str | None = None) -> bool:
    now = now_iso()
    actor = actor or holder
    row = conn.execute(
        "SELECT holder FROM pier_locks WHERE lock_key=?", (lock_key,)
    ).fetchone()
    if row is None:
        try:
            conn.execute(
                "INSERT INTO pier_locks(lock_key,holder,acquired_at) VALUES (?,?,?)",
                (lock_key, holder, now),
            )
            return True
        except sqlite3.IntegrityError:
            row = conn.execute(
                "SELECT holder FROM pier_locks WHERE lock_key=?", (lock_key,)
            ).fetchone()
            if row is None:
                raise
    if row["holder"] == holder:
        return True
    conn.execute(
        """INSERT INTO contention_log(lock_key,actor,action,detail,created_at)
           VALUES (?,?,?,?,?)""",
        (lock_key, actor, "争用", f"持有者={row['holder']}", now),
    )
    return False


def release(conn, lock_key: str, holder: str) -> None:
    conn.execute(
        "DELETE FROM pier_locks WHERE lock_key=? AND holder=?",
        (lock_key, holder),
    )


def require_lock(conn, lock_key: str, holder: str, actor: str | None = None) -> None:
    if not try_acquire(conn, lock_key, holder, actor=actor):
        raise HTTPException(409, "争用：后到只记争用，不写业务")


def _contention_barrier(lock_key: str, holder: str) -> None:
    """争用观测点：生产为空操作；并发回归可注入等待以固定交错。"""
    return None


def acquire_business_locks(lock_keys, holder: str) -> list[str]:
    """按序取业务锁：每把锁单独短事务提交，后到争用只记痕并拒写。

    holder 应为“会话级”标识（同一账号的两个并发会话也必须互斥），
    争用记录的 actor 仍记真实账号。
    """
    acquired: list[str] = []
    for key in sorted(set(lock_keys)):
        conn = connect()
        try:
            ok = try_acquire(conn, key, holder, actor=holder.split("#")[0])
            conn.commit()
        finally:
            conn.close()
        if not ok:
            release_business_locks(acquired, holder)
            raise HTTPException(409, "争用：后到只记争用，不写业务")
        acquired.append(key)
        _contention_barrier(key, holder)
    return acquired


def release_business_locks(lock_keys, holder: str) -> None:
    for key in lock_keys:
        conn = connect()
        try:
            release(conn, key, holder)
            conn.commit()
        finally:
            conn.close()


@router.get("/api/locks")
def list_locks(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM pier_locks ORDER BY acquired_at DESC"
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.get("/api/contention")
def list_contention(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM contention_log ORDER BY id DESC LIMIT 200"
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.post("/api/locks/acquire")
def acquire_lock(body: LockIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    with connect() as conn:
        ok = try_acquire(conn, body.lock_key, user)
        write_audit(conn, user, "lock_acquire", body.lock_key, {"ok": ok})
        conn.commit()
        if not ok:
            raise HTTPException(409, "争用：未能获得锁")
        return {"ok": True, "lock_key": body.lock_key, "holder": user}


@router.post("/api/locks/release")
def release_lock(body: LockIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    with connect() as conn:
        release(conn, body.lock_key, user)
        write_audit(conn, user, "lock_release", body.lock_key, {})
        conn.commit()
        return {"ok": True}