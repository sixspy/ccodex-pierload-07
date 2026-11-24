"""写边案庚 · 第7轮三红：入库同写血缘边。

红测口径（改前必修）：
  甲. 正式票行已落库、血缘边仍为零 —— 必须整笔回滚，不得收票；
  乙. 影子连边未晋升却被血缘页当成正式链 —— 必须分离待校验与正式链；
  争用. 双会话同一父票续磅 —— 必须抢父票锁，后到只记争用痕。

本文件在修复前应 3 红；修复后 3 绿，且既有测试保持绿。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import db
from app import app

ROOT = Path(__file__).resolve().parents[1]
FORMAL = "正式"
SHADOW = "影子"


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    yield


def _client() -> tuple[TestClient, dict]:
    c = TestClient(app)
    tok = c.post(
        "/api/login", json={"username": "admin", "password": "admin123"}
    ).json()["token"]
    return c, {"Authorization": "Bearer " + tok}


def _lane_id(code: str) -> int:
    with db.connect() as conn:
        return int(
            conn.execute(
                "SELECT id FROM weigh_lanes WHERE lane_code=?", (code,)
            ).fetchone()["id"]
        )


def _no_parent_ticket(c: TestClient, h: dict, code: str, lane_code: str) -> int:
    res = c.post(
        "/api/tickets",
        headers=h,
        json={
            "lane_id": _lane_id(lane_code),
            "bl_ref": "BL-2026-0101",
            "vehicle_plate": "沪A·父车",
            "axle_weights": [{"axle": 1, "w": 5.0}, {"axle": 2, "w": 5.0}],
            "ticket_code": code,
        },
    )
    assert res.status_code == 200, res.text
    return int(res.json()["id"])


def _ingest_child(
    c: TestClient,
    h: dict,
    code: str,
    lane_code: str,
    parent_id: int | None,
    edge_type: str = "续磅",
):
    payload = {
        "lane_id": _lane_id(lane_code),
        "bl_ref": "BL-2026-0101",
        "vehicle_plate": "沪A·子车",
        "axle_weights": [{"axle": 1, "w": 5.0}, {"axle": 2, "w": 5.0}],
        "ticket_code": code,
        "edge_type": edge_type,
    }
    if parent_id is not None:
        payload["parent_ticket_id"] = parent_id
    return c.post("/api/tickets", headers=h, json=payload)


def _formal_edge_rows(from_id: int, to_id: int) -> list[sqlite3.Row]:
    with db.connect() as conn:
        return conn.execute(
            """SELECT * FROM axle_lineage
               WHERE from_ticket_id=? AND to_ticket_id=?""",
            (from_id, to_id),
        ).fetchall()


def _table_cols(table: str) -> set[str]:
    with db.connect() as conn:
        return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


# ---------------------------------------------------------------- 甲
def test_fail_jia_ticket_never_lands_without_formal_edge():
    """甲：票落正式行与正式边同事务；边零增则整笔回滚，绝不留“有票无边”。"""
    c, h = _client()
    parent_id = _no_parent_ticket(c, h, "WT-R7-PARENT", "LN-N-01-IN")

    child_code = "PL-G41-JIA"
    ok = _ingest_child(c, h, child_code, "LN-N-01-OUT", parent_id)
    assert ok.status_code == 200, ok.text
    child_id = int(ok.json()["id"])

    edges = _formal_edge_rows(parent_id, child_id)
    assert len(edges) == 1, (
        "入库已收票但正式血缘边为零：正是假绿缺陷（正式票在、边表空）"
    )
    assert edges[0]["edge_status"] == FORMAL
    with db.connect() as conn:
        row = conn.execute(
            "SELECT parent_ticket_id FROM weigh_tickets WHERE id=?", (child_id,)
        ).fetchone()
    assert row["parent_ticket_id"] == parent_id

    # 故障注入：正式边插入被拒 → 整笔回滚，不得留下正式票
    blocked_code = "PL-G41-ROLLBACK"
    with db.connect() as conn:
        has_status = "edge_status" in _table_cols("axle_lineage")
        when = " WHEN NEW.edge_status='正式'" if has_status else ""
        conn.execute(
            f"""CREATE TRIGGER r7_block_edge BEFORE INSERT ON axle_lineage{when}
                BEGIN SELECT RAISE(ABORT, 'edge write blocked'); END"""
        )
        conn.commit()
    bad = _ingest_child(c, h, blocked_code, "LN-N-01-OUT", parent_id)
    assert bad.status_code >= 400, "边写失败仍收了票（正式票在、血缘边零）"
    with db.connect() as conn:
        landed = conn.execute(
            "SELECT id FROM weigh_tickets WHERE ticket_code=?", (blocked_code,)
        ).fetchone()
        assert landed is None, "边写失败后正式票行未回滚"

    # 全局不变量：不存在“申报了父票却没有正式边”的非作废票
    with db.connect() as conn:
        has_status = "edge_status" in _table_cols("axle_lineage")
        status_filter = "AND e.edge_status='正式'" if has_status else ""
        broken = conn.execute(
            f"""SELECT t.ticket_code FROM weigh_tickets t
                WHERE t.parent_ticket_id IS NOT NULL AND t.status!='作废'
                  AND NOT EXISTS (
                    SELECT 1 FROM axle_lineage e
                    WHERE e.to_ticket_id=t.id
                      AND e.from_ticket_id=t.parent_ticket_id {status_filter}
                  )"""
        ).fetchall()
    assert [r["ticket_code"] for r in broken] == [], "存在有票无边的正式票"

    # 命运册已拍板且当班锁定甲路径
    book = c.get("/api/lineage/fate-book", headers=h)
    assert book.status_code == 200, "港区写边命运册未落接口"
    assert book.json()["locked_path"] == "甲"

    # 偏离命运册（试图走乙路径写边）→ 记写边偏航并拒写
    dev = c.post(
        "/api/tickets",
        headers=h,
        json={
            "lane_id": _lane_id("LN-N-01-OUT"),
            "bl_ref": "BL-2026-0101",
            "vehicle_plate": "沪A·偏航",
            "axle_weights": [{"axle": 1, "w": 4.0}, {"axle": 2, "w": 4.0}],
            "ticket_code": "PL-G41-DEVIATE",
            "parent_ticket_id": parent_id,
            "edge_mode": "shadow",
        },
    )
    assert dev.status_code == 409, "偏离命运册未拒写"
    with db.connect() as conn:
        assert conn.execute(
            "SELECT id FROM weigh_tickets WHERE ticket_code='PL-G41-DEVIATE'"
        ).fetchone() is None
        assert conn.execute(
            "SELECT id FROM audit_trail WHERE action='写边偏航'"
        ).fetchone() is not None


# ---------------------------------------------------------------- 乙
def test_fail_yi_shadow_edge_never_rendered_as_formal_chain():
    """乙：影子连边未晋升，血缘页/对账不得当成正式链。"""
    c, h = _client()
    parent_id = _no_parent_ticket(c, h, "WT-R7-PARENT-YI", "LN-N-01-IN")
    # 若此时入库带父票，甲路径必须当场写正式边；故此处建根票，
    # 再以“影子草稿”方式登记连边，专门钉住“未晋升不得当正式链”。
    child = _ingest_child(c, h, "PL-G41-YI", "LN-N-01-OUT", None)
    assert child.status_code == 200, child.text
    child_id = int(child.json()["id"])

    draft = c.post(
        "/api/lineage",
        headers=h,
        json={
            "from_ticket_id": parent_id,
            "to_ticket_id": child_id,
            "edge_type": "续磅",
            "detail": "影子草稿待校验",
            "edge_status": SHADOW,
        },
    )
    assert draft.status_code == 200, draft.text
    assert draft.json().get("edge_status") == SHADOW, (
        "手工边未落影子态：被当成正式边直接写进正式链"
    )
    edge_id = int(draft.json()["id"])

    listing = c.get("/api/lineage", headers=h).json()
    edge = next(e for e in listing["items"] if e["id"] == edge_id)
    assert edge["edge_status"] == SHADOW
    assert edge["formal"] is False, "影子边被血缘页当成正式链"

    ticket_chain = c.get(f"/api/lineage/ticket/{child_id}", headers=h).json()
    assert ticket_chain["formal_inbound"] == [], "未晋升影子边混入正式入边"
    assert [e["id"] for e in ticket_chain["pending_inbound"]] == [edge_id]
    assert ticket_chain["integrity"]["lineage_complete"] is False

    recon = c.get("/api/reconcile/BL-2026-0101", headers=h).json()
    assert recon["lineage_complete"] is False, "对账把未晋升影子边算作血统齐全"
    assert "PL-G41-YI" in recon["broken_tickets"]

    view = (ROOT / "frontend/js/views/lineage_view.js").read_text(encoding="utf-8")
    assert "待校验" in view and "影子" in view, "血缘页未分开展示影子/待校验边"

    promoted = c.post(f"/api/lineage/{edge_id}/promote", headers=h)
    assert promoted.status_code == 200, promoted.text
    after = c.get(f"/api/lineage/ticket/{child_id}", headers=h).json()
    assert [e["id"] for e in after["formal_inbound"]] == [edge_id]
    assert after["integrity"]["lineage_complete"] is True
    recon2 = c.get("/api/reconcile/BL-2026-0101", headers=h).json()
    assert recon2["lineage_complete"] is True


# ---------------------------------------------------------------- 争用
def test_fail_contention_two_sessions_same_parent_must_not_double_win(monkeypatch):
    """争用：双会话同父票抢建，须抢票锁；后到只记痕，不得双成。"""
    c, h = _client()
    parent_id = _no_parent_ticket(c, h, "WT-R7-PARENT-LOCK", "LN-N-01-IN")

    import locks

    entered = threading.Event()
    release_ev = threading.Event()

    def barrier(lock_key: str, holder: str) -> None:
        # 首路拿到父票锁后在此停顿，令后到必定撞上父票锁
        if lock_key == f"ticket:{parent_id}" and not entered.is_set():
            entered.set()
            release_ev.wait(10)

    monkeypatch.setattr(locks, "_contention_barrier", barrier)

    results: dict[str, object] = {}

    def worker(name: str, lane_code: str, code: str, user: str, pwd: str) -> None:
        cc = TestClient(app)
        hh = {
            "Authorization": "Bearer "
            + cc.post(
                "/api/login", json={"username": user, "password": pwd}
            ).json()["token"]
        }
        results[name] = cc.post(
            "/api/tickets",
            headers=hh,
            json={
                "lane_id": _lane_id(lane_code),
                "bl_ref": "BL-2026-0101",
                "vehicle_plate": "沪A·抢锁",
                "axle_weights": [{"axle": 1, "w": 5.0}, {"axle": 2, "w": 5.0}],
                "ticket_code": code,
                "parent_ticket_id": parent_id,
                "edge_type": "续磅",
            },
        )

    first = threading.Thread(
        target=worker,
        args=("first", "LN-N-01-IN", "PL-G41-LOCK-A", "admin", "admin123"),
        daemon=True,
    )
    first.start()
    assert entered.wait(10), "首路未进入父票锁临界区"
    second = threading.Thread(
        target=worker,
        args=("second", "LN-N-01-OUT", "PL-G41-LOCK-B", "writer", "writer123"),
        daemon=True,
    )
    second.start()
    second.join(15)
    release_ev.set()
    first.join(15)

    assert len(results) == 2, "两路会话未都返回"
    codes = sorted(r.status_code for r in results.values())
    assert codes == [200, 409], f"同父票续磅未抢票锁（双成/无痕）：{codes}"

    with db.connect() as conn:
        children = conn.execute(
            "SELECT id, ticket_code FROM weigh_tickets WHERE parent_ticket_id=?",
            (parent_id,),
        ).fetchall()
        assert len(children) == 1, "同父票下产生多张续磅票（无锁双成）"
        child_id = int(children[0]["id"])
        edges = conn.execute(
            """SELECT * FROM axle_lineage
               WHERE from_ticket_id=? AND to_ticket_id=?""",
            (parent_id, child_id),
        ).fetchall()
        assert len(edges) == 1
        traces = conn.execute(
            "SELECT * FROM contention_log WHERE lock_key=?",
            (f"ticket:{parent_id}",),
        ).fetchall()
    assert traces, "后到争用未记痕"
    assert any(t["action"] == "争用" for t in traces)


# ---------------------------------------------------------------- 路径锁死
def test_fate_book_yi_path_writes_shadow_only_and_cannot_mix():
    """命运册锁乙：入库只写影子草稿；未 force 不得改道甲（禁两边混用）。"""
    c, h = _client()
    parent_id = _no_parent_ticket(c, h, "WT-R7-PARENT-YI-BOOK", "LN-N-01-IN")

    switched = c.post(
        "/api/lineage/fate-book",
        headers=h,
        json={"locked_path": "乙", "note": "写边案庚·乙路径当班", "force": True},
    )
    assert switched.status_code == 200, switched.text
    assert switched.json()["locked_path"] == "乙"
    # 当班锁死：此后再改道（无论甲乙）仍须 force
    assert (
        c.post(
            "/api/lineage/fate-book", headers=h, json={"locked_path": "甲"}
        ).status_code
        == 409
    )

    child = _ingest_child(c, h, "PL-G41-YIBOOK", "LN-N-01-OUT", parent_id)
    assert child.status_code == 200, child.text
    assert child.json()["edge_path"] == "乙"
    child_id = int(child.json()["id"])

    with db.connect() as conn:
        formal = conn.execute(
            """SELECT COUNT(*) c FROM axle_lineage
               WHERE to_ticket_id=? AND edge_status='正式'""",
            (child_id,),
        ).fetchone()["c"]
        shadow = conn.execute(
            """SELECT COUNT(*) c FROM axle_lineage
               WHERE to_ticket_id=? AND edge_status='影子'""",
            (child_id,),
        ).fetchone()["c"]
    assert formal == 0, "乙路径入库却写了正式边（两边混用）"
    assert shadow == 1

    # 乙路径下血统未齐；对账不得放行
    recon = c.get("/api/reconcile/BL-2026-0101", headers=h).json()
    assert recon["lineage_complete"] is False
    assert "PL-G41-YIBOOK" in recon["broken_tickets"]

    # 未 force 改道甲 → 拒写并记偏航
    dev = c.post(
        "/api/lineage/fate-book", headers=h, json={"locked_path": "甲"}
    )
    assert dev.status_code == 409
    # 混用检测：请求甲却走乙，必须被入库口拒绝（不得双写、不得折中）
    mixed = c.post(
        "/api/tickets",
        headers=h,
        json={
            "lane_id": _lane_id("LN-N-01-OUT"),
            "bl_ref": "BL-2026-0101",
            "vehicle_plate": "沪A·混用",
            "axle_weights": [{"axle": 1, "w": 3.0}, {"axle": 2, "w": 3.0}],
            "ticket_code": "PL-G41-MIX",
            "parent_ticket_id": parent_id,
            "edge_mode": "甲",
        },
    )
    assert mixed.status_code == 409, "命运册锁乙时请求甲仍被接受（两边混用）"
    with db.connect() as conn:
        assert conn.execute(
            "SELECT id FROM audit_trail WHERE action='写边偏航'"
        ).fetchone() is not None
        assert conn.execute(
            "SELECT id FROM weigh_tickets WHERE ticket_code='PL-G41-MIX'"
        ).fetchone() is None


# ---------------------------------------------------------------- 手工边不许直写正式
def test_manual_lineage_cannot_write_formal_edge():
    """血缘页手工入口只收影子草稿；正式边仅可由入库同写或晋升产生。"""
    c, h = _client()
    parent_id = _no_parent_ticket(c, h, "WT-R7-MANUAL-P", "LN-N-01-IN")
    child = _ingest_child(c, h, "PL-G41-MANUAL", "LN-N-01-OUT", None)
    assert child.status_code == 200, child.text
    child_id = int(child.json()["id"])

    formal = c.post(
        "/api/lineage",
        headers=h,
        json={
            "from_ticket_id": parent_id,
            "to_ticket_id": child_id,
            "edge_type": "续磅",
            "detail": "试图直写正式边",
            "edge_status": "正式",
        },
    )
    assert formal.status_code == 409, "手工写口仍可直写正式边"
    shadow = c.post(
        "/api/lineage",
        headers=h,
        json={
            "from_ticket_id": parent_id,
            "to_ticket_id": child_id,
            "edge_type": "续磅",
            "detail": "影子草稿",
        },
    )
    assert shadow.status_code == 200
    assert shadow.json()["edge_status"] == "影子"


def test_same_account_two_sessions_still_contend(monkeypatch):
    """同账号两路会话：持锁标识须会话级，不得因同名而互认放行。"""
    c, h = _client()
    parent_id = _no_parent_ticket(c, h, "WT-R7-SAME-USER", "LN-N-01-IN")

    import locks

    entered = threading.Event()
    release_ev = threading.Event()

    def barrier(lock_key: str, holder: str) -> None:
        if lock_key == f"ticket:{parent_id}" and not entered.is_set():
            entered.set()
            release_ev.wait(10)

    monkeypatch.setattr(locks, "_contention_barrier", barrier)
    results: dict[str, object] = {}

    def worker(name: str, lane: str, code: str) -> None:
        cc = TestClient(app)
        hh = {
            "Authorization": "Bearer "
            + cc.post(
                "/api/login", json={"username": "admin", "password": "admin123"}
            ).json()["token"]
        }
        results[name] = cc.post(
            "/api/tickets",
            headers=hh,
            json={
                "lane_id": _lane_id(lane),
                "bl_ref": "BL-2026-0101",
                "vehicle_plate": "沪A·同号",
                "axle_weights": [{"axle": 1, "w": 5.0}, {"axle": 2, "w": 5.0}],
                "ticket_code": code,
                "parent_ticket_id": parent_id,
            },
        )

    t1 = threading.Thread(
        target=worker, args=("a", "LN-N-01-IN", "PL-G41-SAME-A"), daemon=True
    )
    t1.start()
    assert entered.wait(10)
    t2 = threading.Thread(
        target=worker, args=("b", "LN-N-01-OUT", "PL-G41-SAME-B"), daemon=True
    )
    t2.start()
    t2.join(15)
    release_ev.set()
    t1.join(15)

    codes = sorted(r.status_code for r in results.values())
    assert codes == [200, 409], f"同账号两路双成：{codes}"
    with db.connect() as conn:
        kids = conn.execute(
            "SELECT id FROM weigh_tickets WHERE parent_ticket_id=?", (parent_id,)
        ).fetchall()
    assert len(kids) == 1
