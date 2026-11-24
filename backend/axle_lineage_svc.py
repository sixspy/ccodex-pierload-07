"""轴重血缘边：正式链与影子连边分离，待校验后原子晋升。"""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, HTTPException

import edge_write
from auth import AuthHeader, require_user, require_writer
from db import FATE_SCOPE, connect, now_iso, write_audit
from models import FateBookIn, LineageIn

router = APIRouter(tags=["lineage"])


def _edge_dict(row) -> dict:
    d = dict(row)
    d["formal"] = d.get("edge_status") == edge_write.FORMAL
    return d


@router.get("/api/lineage")
def list_lineage(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            """SELECT e.*, a.ticket_code from_code, b.ticket_code to_code
               FROM axle_lineage e
               JOIN weigh_tickets a ON a.id=e.from_ticket_id
               LEFT JOIN weigh_tickets b ON b.id=e.to_ticket_id
               ORDER BY e.id DESC"""
        ).fetchall()
        return {"items": [_edge_dict(r) for r in rows]}


@router.get("/api/lineage/integrity")
def lineage_integrity(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        return edge_write.integrity_summary(conn)


@router.get("/api/lineage/fate-book")
def get_fate_book(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        return edge_write.fate_book(conn)


@router.post("/api/lineage/fate-book")
def lock_fate_book(body: FateBookIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    path = edge_write.normalize_path(body.locked_path)
    if path not in (edge_write.PATH_JIA, edge_write.PATH_YI):
        raise HTTPException(422, "写边路径只能是甲或乙")
    with connect() as conn:
        current = edge_write.fate_book(conn)
        if current["locked_path"] != path and not body.force:
            write_audit(
                conn,
                user,
                "写边偏航",
                FATE_SCOPE,
                {"locked_path": current["locked_path"], "requested_path": path},
            )
            conn.commit()
            raise HTTPException(409, "当班命运册已锁死，改道须显式 force")
        conn.execute(
            """UPDATE write_fate_book SET locked_path=?, note=?, locked_by=?,
               locked_at=? WHERE scope=?""",
            (
                path,
                body.note or current["note"],
                user,
                now_iso(),
                FATE_SCOPE,
            ),
        )
        write_audit(
            conn, user, "写边命运册", FATE_SCOPE, {"locked_path": path}
        )
        conn.commit()
        return edge_write.fate_book(conn)


@router.get("/api/lineage/ticket/{ticket_id}")
def lineage_for_ticket(ticket_id: int, authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        ticket = conn.execute(
            "SELECT id, parent_ticket_id FROM weigh_tickets WHERE id=?",
            (ticket_id,),
        ).fetchone()
        if not ticket:
            raise HTTPException(404, "过磅票不存在")
        out_edges = conn.execute(
            """SELECT e.*, b.ticket_code to_code FROM axle_lineage e
               LEFT JOIN weigh_tickets b ON b.id=e.to_ticket_id
               WHERE e.from_ticket_id=?
               ORDER BY e.id""",
            (ticket_id,),
        ).fetchall()
        incoming = conn.execute(
            """SELECT e.*, a.ticket_code from_code FROM axle_lineage e
               JOIN weigh_tickets a ON a.id=e.from_ticket_id
               WHERE e.to_ticket_id=?
               ORDER BY e.id""",
            (ticket_id,),
        ).fetchall()
        formal_in = [
            _edge_dict(r)
            for r in incoming
            if r["edge_status"] == edge_write.FORMAL
        ]
        pending_in = [
            _edge_dict(r)
            for r in incoming
            if r["edge_status"] == edge_write.SHADOW
        ]
        complete = edge_write.ticket_lineage_complete(conn, ticket_id)
        return {
            "ticket_id": ticket_id,
            "parent_ticket_id": ticket["parent_ticket_id"],
            "outbound": [_edge_dict(x) for x in out_edges],
            "inbound": [_edge_dict(x) for x in incoming],
            "formal_inbound": formal_in,
            "pending_inbound": pending_in,
            "integrity": {
                "lineage_complete": complete,
                "broken": not complete,
                "reason": None
                if complete
                else (
                    "存在未晋升影子连边"
                    if pending_in
                    else "父票已登记但正式血缘边缺失"
                ),
            },
        }


@router.post("/api/lineage")
def add_lineage(body: LineageIn, authorization: AuthHeader = None):
    """手工登记连边：只收影子草稿；正式边只能由入库同写或晋升产生。"""
    user = require_writer(authorization)
    if body.edge_status != edge_write.SHADOW:
        raise HTTPException(
            409, "正式边只能由入库同写或影子晋升产生，禁止手工直写"
        )
    with connect() as conn:
        fr = conn.execute(
            "SELECT id, ticket_code FROM weigh_tickets WHERE id=?",
            (body.from_ticket_id,),
        ).fetchone()
        if not fr:
            raise HTTPException(404, "起点票不存在")
        if body.to_ticket_id:
            to = conn.execute(
                "SELECT id FROM weigh_tickets WHERE id=?", (body.to_ticket_id,)
            ).fetchone()
            if not to:
                raise HTTPException(404, "终点票不存在")
        now = now_iso()
        try:
            cur = conn.execute(
                """INSERT INTO axle_lineage(from_ticket_id,to_ticket_id,edge_type,
                   detail,edge_status,created_at) VALUES (?,?,?,?,?,?)""",
                (
                    body.from_ticket_id,
                    body.to_ticket_id,
                    body.edge_type,
                    body.detail,
                    body.edge_status,
                    now,
                ),
            )
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            raise HTTPException(409, "该父子票已存在正式血缘边") from exc
        write_audit(
            conn,
            user,
            "lineage_edge",
            fr["ticket_code"],
            {
                "edge_id": cur.lastrowid,
                "edge_type": body.edge_type,
                "edge_status": body.edge_status,
            },
        )
        conn.commit()
        return {"id": cur.lastrowid, "edge_status": body.edge_status}


@router.post("/api/lineage/{edge_id}/promote")
def promote_edge(edge_id: int, authorization: AuthHeader = None):
    """影子连边原子晋升为正式边；已正式则幂等返回。"""
    user = require_writer(authorization)
    with connect() as conn:
        edge = conn.execute(
            "SELECT * FROM axle_lineage WHERE id=?", (edge_id,)
        ).fetchone()
        if not edge:
            raise HTTPException(404, "血缘边不存在")
        if edge["edge_status"] == edge_write.FORMAL:
            return {"id": edge_id, "edge_status": edge_write.FORMAL,
                    "promoted": False}
        try:
            cur = conn.execute(
                """UPDATE axle_lineage SET edge_status=?
                   WHERE id=? AND edge_status=?""",
                (edge_write.FORMAL, edge_id, edge_write.SHADOW),
            )
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            raise HTTPException(409, "晋升冲突：该父子票已有正式边") from exc
        if cur.rowcount != 1:
            conn.rollback()
            raise HTTPException(409, "晋升失败：边态已变")
        write_audit(
            conn,
            user,
            "lineage_promote",
            str(edge_id),
            {
                "from_ticket_id": edge["from_ticket_id"],
                "to_ticket_id": edge["to_ticket_id"],
            },
        )
        conn.commit()
        return {"id": edge_id, "edge_status": edge_write.FORMAL,
                "promoted": True}
