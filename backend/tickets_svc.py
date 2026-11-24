"""过磅票：轴重采集与裁定入闸。"""
from __future__ import annotations

import json
import sqlite3

from fastapi import APIRouter, HTTPException

import edge_write
from auth import AuthHeader, require_user, require_writer
from db import connect, now_iso, write_audit
from locks import acquire_business_locks, release_business_locks, session_holder
from models import TicketIn
from validators import (
    adjudicate_overload,
    validate_axle_count,
    validate_axle_weights,
    validate_gross_cap,
    validate_lane_open,
)

router = APIRouter(tags=["tickets"])


def _next_ticket_code(conn, lane_id: int) -> str:
    lane = conn.execute(
        "SELECT lane_code FROM weigh_lanes WHERE id=?", (lane_id,)
    ).fetchone()
    prefix = lane["lane_code"] if lane else f"LN-{lane_id}"
    n = conn.execute(
        "SELECT COUNT(*) c FROM weigh_tickets WHERE lane_id=?", (lane_id,)
    ).fetchone()["c"]
    return f"WT-{prefix}-{n + 1:03d}"


def _resolve_edge_path(conn, body: TicketIn, user: str) -> tuple[str, str]:
    """返回 (命运册锁定路径, 本次请求请求路径)。偏离即记偏航并拒写。"""
    book = edge_write.fate_book(conn)
    locked = book["locked_path"]
    requested = edge_write.normalize_path(body.edge_mode) or locked
    if requested != locked:
        write_audit(
            conn,
            user,
            "写边偏航",
            body.ticket_code or "",
            {
                "locked_path": locked,
                "requested_path": requested,
                "parent_ticket_id": body.parent_ticket_id,
                "reason": "偏离港区写边命运册",
            },
        )
        conn.commit()
        raise HTTPException(409, "偏离命运册：写边偏航，拒写")
    return locked, requested


def _ticket_locks(body: TicketIn) -> list[str]:
    keys = [f"lane:{body.lane_id}"]
    if body.parent_ticket_id is not None:
        # 同父票续磅：以父票锁串行化，后到只记争用痕
        keys.append(f"ticket:{body.parent_ticket_id}")
    return keys


@router.get("/api/tickets")
def list_tickets(authorization: AuthHeader = None, status: str | None = None):
    require_user(authorization)
    with connect() as conn:
        q = """SELECT t.*, l.lane_code, b.bridge_code FROM weigh_tickets t
               JOIN weigh_lanes l ON l.id=t.lane_id
               JOIN weigh_bridges b ON b.id=l.bridge_id"""
        if status:
            rows = conn.execute(
                q + " WHERE t.status=? ORDER BY t.id DESC", (status,)
            ).fetchall()
        else:
            rows = conn.execute(q + " ORDER BY t.id DESC").fetchall()
        items = []
        for r in rows:
            d = dict(r)
            d["axle_weights"] = json.loads(d["axle_weights"] or "[]")
            d["lineage_complete"] = edge_write.ticket_lineage_complete(
                conn, int(d["id"])
            )
            items.append(d)
        return {"items": items}


@router.get("/api/tickets/{ticket_id}")
def get_ticket(ticket_id: int, authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        row = conn.execute(
            """SELECT t.*, l.lane_code, l.overload_threshold_t, b.name bridge_name
               FROM weigh_tickets t
               JOIN weigh_lanes l ON l.id=t.lane_id
               JOIN weigh_bridges b ON b.id=l.bridge_id
               WHERE t.id=?""",
            (ticket_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "过磅票不存在")
        data = dict(row)
        data["axle_weights"] = json.loads(data["axle_weights"] or "[]")
        holds = conn.execute(
            "SELECT * FROM overload_holds WHERE ticket_id=? ORDER BY id DESC",
            (ticket_id,),
        ).fetchall()
        seals = conn.execute(
            "SELECT * FROM gate_seals WHERE ticket_id=? ORDER BY id DESC",
            (ticket_id,),
        ).fetchall()
        releases = conn.execute(
            "SELECT * FROM gate_releases WHERE ticket_id=? ORDER BY id DESC",
            (ticket_id,),
        ).fetchall()
        complete = edge_write.ticket_lineage_complete(conn, ticket_id)
        return {
            "ticket": data,
            "overload_holds": [dict(h) for h in holds],
            "gate_seals": [dict(s) for s in seals],
            "gate_releases": [dict(r) for r in releases],
            "lineage": {
                "lineage_complete": complete,
                "broken": not complete,
                "reason": None
                if complete
                else (
                    "存在未晋升影子连边"
                    if edge_write.pending_inbound(conn, ticket_id)
                    else "父票已登记但正式血缘边缺失"
                    if data.get("parent_ticket_id")
                    else "票已存在未晋升影子连边"
                ),
                "formal_inbound": edge_write.formal_inbound(conn, ticket_id),
                "pending_inbound": edge_write.pending_inbound(conn, ticket_id),
            },
        }


@router.post("/api/tickets")
def create_ticket(body: TicketIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    # 会话级持锁：同账号两路并发写也必须互斥，不得因同名持有而“双成”
    holder = session_holder(user)
    lock_keys = _ticket_locks(body)
    held = acquire_business_locks(lock_keys, holder)
    try:
        with connect() as conn:
            lane = conn.execute(
                """SELECT l.*, b.max_gross_t, b.axle_limit FROM weigh_lanes l
                   JOIN weigh_bridges b ON b.id=l.bridge_id WHERE l.id=?""",
                (body.lane_id,),
            ).fetchone()
            if not lane:
                raise HTTPException(404, "车道不存在")
            validate_lane_open(dict(lane))
            axles = [a.model_dump() for a in body.axle_weights]
            validate_axle_count(axles, int(lane["axle_limit"]))
            gross, _peak, _spread = validate_axle_weights(axles)
            validate_gross_cap(gross, float(lane["max_gross_t"]))
            bl = conn.execute(
                "SELECT * FROM bl_manifests WHERE bl_code=?", (body.bl_ref,)
            ).fetchone()
            if not bl:
                raise HTTPException(422, "提单册未登记")
            parent_id = body.parent_ticket_id
            if parent_id is not None:
                parent = conn.execute(
                    "SELECT id FROM weigh_tickets WHERE id=?", (parent_id,)
                ).fetchone()
                if not parent:
                    raise HTTPException(404, "父票不存在")
            path, _requested = _resolve_edge_path(conn, body, user)
            is_over, overweight = adjudicate_overload(
                gross, float(lane["overload_threshold_t"])
            )
            code = body.ticket_code or _next_ticket_code(conn, body.lane_id)
            if conn.execute(
                "SELECT id FROM weigh_tickets WHERE ticket_code=?", (code,)
            ).fetchone():
                raise HTTPException(409, "票号已存在")
            now = now_iso()
            if is_over:
                status = "旁路挂起"
            else:
                status = "已裁定"
            ticket_id = None
            edge_id = None
            try:
                cur = conn.execute(
                    """INSERT INTO weigh_tickets(ticket_code,lane_id,bl_ref,vehicle_plate,
                       axle_weights,gross_t,generation,parent_ticket_id,status,
                       adjudicated_at,created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        code,
                        body.lane_id,
                        body.bl_ref,
                        body.vehicle_plate,
                        json.dumps(axles, ensure_ascii=False),
                        gross,
                        1,
                        parent_id,
                        status,
                        now if status == "已裁定" else None,
                        now,
                    ),
                )
                ticket_id = int(cur.lastrowid)
                if parent_id is not None:
                    if path == edge_write.PATH_JIA:
                        edge_id = edge_write.write_ingest_edge(
                            conn,
                            parent_id,
                            ticket_id,
                            edge_type=body.edge_type,
                            detail=body.edge_detail,
                            actor=user,
                        )
                    else:
                        edge_id = edge_write.write_shadow_edge(
                            conn,
                            parent_id,
                            ticket_id,
                            edge_type=body.edge_type,
                            detail=body.edge_detail,
                            actor=user,
                        )
                if is_over:
                    hold_key = f"HOLD-{code}"
                    conn.execute(
                        """INSERT INTO overload_holds(ticket_id,hold_key,overweight_t,
                           threshold_t,status,opened_at,side_lane_note)
                           VALUES (?,?,?,?,?,?,?)""",
                        (
                            ticket_id,
                            hold_key,
                            overweight,
                            float(lane["overload_threshold_t"]),
                            "开放",
                            now,
                            lane["side_lane_ref"] or "旁路待驳",
                        ),
                    )
                write_audit(
                    conn,
                    user,
                    "ticket_weigh",
                    code,
                    {
                        "ticket_id": ticket_id,
                        "gross_t": gross,
                        "status": status,
                        "overload": is_over,
                        "parent_ticket_id": parent_id,
                        "edge_id": edge_id,
                        "edge_path": path,
                    },
                )
                conn.commit()
            except (edge_write.EdgeWriteError, sqlite3.IntegrityError) as exc:
                conn.rollback()
                ticket_id = None
                write_audit(
                    conn,
                    user,
                    "写边拒收",
                    code,
                    {
                        "reason": str(exc),
                        "parent_ticket_id": parent_id,
                        "rolled_back": True,
                    },
                )
                conn.commit()
                raise HTTPException(409, f"入库写边失败，整笔回滚：{exc}") from exc
            except Exception:
                conn.rollback()
                raise
            if ticket_id is None:
                raise HTTPException(409, "入库写边失败，整笔回滚")
        return {
            "id": ticket_id,
            "ticket_code": code,
            "gross_t": gross,
            "status": status,
            "overload": is_over,
            "parent_ticket_id": parent_id,
            "edge_id": edge_id,
            "edge_path": path,
        }
    finally:
        release_business_locks(held, holder)