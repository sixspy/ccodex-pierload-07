"""入库写边：过磅票落库与轴重血缘边同事务（写边案庚）。

口径（命运册当班锁死）：
  甲. 票落正式行与正式血缘边同一事务提交；边零增则整笔回滚，票不得入库。
  乙. 票可先落正式行、边只进影子连边草稿；正式边零改，待校验后原子晋升。

两条路径由命运册 locked_path 决定，调用方不得混用、不得折中双写。
"""
from __future__ import annotations

from db import FATE_SCOPE, now_iso

FORMAL = "正式"
SHADOW = "影子"

PATH_JIA = "甲"
PATH_YI = "乙"

_PATH_ALIASES = {
    "甲": PATH_JIA,
    "formal": PATH_JIA,
    "正式": PATH_JIA,
    "乙": PATH_YI,
    "shadow": PATH_YI,
    "影子": PATH_YI,
}


def normalize_path(value: str | None) -> str | None:
    if value is None:
        return None
    return _PATH_ALIASES.get(str(value).strip(), str(value).strip())


def fate_book(conn) -> dict:
    row = conn.execute(
        "SELECT * FROM write_fate_book WHERE scope=?", (FATE_SCOPE,)
    ).fetchone()
    if row is None:
        raise EdgeWriteError("港区写边命运册未登记")
    return dict(row)


def locked_path(conn) -> str:
    return fate_book(conn)["locked_path"]


class EdgeWriteError(RuntimeError):
    """血缘边未落库 / 落库数量不符，调用方须整笔回滚。"""


class DuplicateFormalEdge(EdgeWriteError):
    """同一父子票已存在正式边，禁止重复写。"""


def _ticket_exists(conn, ticket_id: int) -> bool:
    return (
        conn.execute(
            "SELECT id FROM weigh_tickets WHERE id=?", (ticket_id,)
        ).fetchone()
        is not None
    )


def _check_pair(conn, from_ticket_id: int, to_ticket_id: int) -> None:
    if from_ticket_id == to_ticket_id:
        raise EdgeWriteError("血缘边起终点不得相同")
    if not _ticket_exists(conn, from_ticket_id):
        raise EdgeWriteError("血缘边起点票不存在")
    if not _ticket_exists(conn, to_ticket_id):
        raise EdgeWriteError("血缘边终点票不存在")


def _insert_edge(
    conn,
    from_ticket_id: int,
    to_ticket_id: int,
    edge_type: str,
    detail: str,
    edge_status: str,
) -> int:
    cur = conn.execute(
        """INSERT INTO axle_lineage(from_ticket_id,to_ticket_id,edge_type,detail,
           edge_status,created_at) VALUES (?,?,?,?,?,?)""",
        (
            from_ticket_id,
            to_ticket_id,
            edge_type,
            detail,
            edge_status,
            now_iso(),
        ),
    )
    if cur.rowcount != 1 or not cur.lastrowid:
        raise EdgeWriteError(f"血缘边未落库（{edge_status}）")
    return int(cur.lastrowid)


def write_ingest_edge(
    conn,
    parent_ticket_id: int,
    child_ticket_id: int,
    edge_type: str = "续磅",
    detail: str = "",
    actor: str = "",
) -> int:
    """甲路径：在入库事务内写正式血缘边，零增即抛错由调用方回滚。"""
    _check_pair(conn, parent_ticket_id, child_ticket_id)
    exists = conn.execute(
        """SELECT id FROM axle_lineage
           WHERE from_ticket_id=? AND to_ticket_id=? AND edge_type=?
             AND edge_status=?""",
        (parent_ticket_id, child_ticket_id, edge_type, FORMAL),
    ).fetchone()
    if exists:
        raise DuplicateFormalEdge("该父子票已存在正式血缘边")
    return _insert_edge(
        conn,
        parent_ticket_id,
        child_ticket_id,
        edge_type,
        detail or "入库同写：续磅血缘",
        FORMAL,
    )


def write_shadow_edge(
    conn,
    parent_ticket_id: int,
    child_ticket_id: int,
    edge_type: str = "续磅",
    detail: str = "",
    actor: str = "",
) -> int:
    """乙路径：只写影子连边草稿，正式边零改。"""
    _check_pair(conn, parent_ticket_id, child_ticket_id)
    return _insert_edge(
        conn,
        parent_ticket_id,
        child_ticket_id,
        edge_type,
        detail or "影子连边草稿：待校验",
        SHADOW,
    )


def formal_inbound(conn, ticket_id: int) -> list[dict]:
    rows = conn.execute(
        """SELECT e.*, a.ticket_code from_code FROM axle_lineage e
           JOIN weigh_tickets a ON a.id=e.from_ticket_id
           WHERE e.to_ticket_id=? AND e.edge_status=?
           ORDER BY e.id""",
        (ticket_id, FORMAL),
    ).fetchall()
    return [dict(r) for r in rows]


def pending_inbound(conn, ticket_id: int) -> list[dict]:
    """未决影子入边：同一父子票/边型尚无正式边的影子草稿。

    迁移去重产生的“已有正式对位”的影子副本不算未决，避免旧库升级后
    把已齐套的票永久判成断链。
    """
    rows = conn.execute(
        """SELECT e.*, a.ticket_code from_code FROM axle_lineage e
           JOIN weigh_tickets a ON a.id=e.from_ticket_id
           WHERE e.to_ticket_id=? AND e.edge_status=?
             AND NOT EXISTS (
               SELECT 1 FROM axle_lineage f
               WHERE f.from_ticket_id=e.from_ticket_id
                 AND f.to_ticket_id=e.to_ticket_id
                 AND f.edge_type=e.edge_type
                 AND f.edge_status=?
             )
           ORDER BY e.id""",
        (ticket_id, SHADOW, FORMAL),
    ).fetchall()
    return [dict(r) for r in rows]


def ticket_lineage_complete(conn, ticket_id: int) -> bool:
    """血统齐全口径：无未晋升影子入边，且父票（若有）已有正式入边。"""
    row = conn.execute(
        "SELECT parent_ticket_id FROM weigh_tickets WHERE id=?", (ticket_id,)
    ).fetchone()
    if row is None:
        return False
    if pending_inbound(conn, ticket_id):
        return False
    parent = row["parent_ticket_id"]
    if parent is None:
        return True
    return (
        conn.execute(
            """SELECT id FROM axle_lineage
               WHERE to_ticket_id=? AND from_ticket_id=? AND edge_status=?""",
            (ticket_id, int(parent), FORMAL),
        ).fetchone()
        is not None
    )


def broken_tickets(conn) -> list[str]:
    rows = conn.execute(
        """SELECT id, ticket_code FROM weigh_tickets
           WHERE status!='作废' ORDER BY id"""
    ).fetchall()
    return [
        r["ticket_code"]
        for r in rows
        if not ticket_lineage_complete(conn, int(r["id"]))
    ]


def integrity_summary(conn) -> dict:
    tickets_total = conn.execute(
        "SELECT COUNT(*) c FROM weigh_tickets WHERE status!='作废'"
    ).fetchone()["c"]
    with_parent = conn.execute(
        """SELECT COUNT(*) c FROM weigh_tickets
           WHERE parent_ticket_id IS NOT NULL AND status!='作废'"""
    ).fetchone()["c"]
    formal_edges = conn.execute(
        "SELECT COUNT(*) c FROM axle_lineage WHERE edge_status=?", (FORMAL,)
    ).fetchone()["c"]
    shadow_edges = conn.execute(
        "SELECT COUNT(*) c FROM axle_lineage WHERE edge_status=?", (SHADOW,)
    ).fetchone()["c"]
    broken = broken_tickets(conn)
    return {
        "tickets_total": int(tickets_total),
        "tickets_with_parent": int(with_parent),
        "formal_edges": int(formal_edges),
        "shadow_edges": int(shadow_edges),
        "broken_tickets": broken,
        "broken_count": len(broken),
        "lineage_complete": not broken,
    }
