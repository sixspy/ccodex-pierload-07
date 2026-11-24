"""五页账实同态：血缘页 / 过磅页 / 对账页 / 快照页 / 地磅页同一口径。

断链（有父票无正式边，或影子边未晋升）必须在五页同时可见，
任何一页不得单独把该票当成血统齐全。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import db
from app import app

ROOT = Path(__file__).resolve().parents[1]
PAGES = [
    "frontend/js/views/lineage_view.js",
    "frontend/js/views/tickets_view.js",
    "frontend/js/views/reconcile_view.js",
    "frontend/js/views/seal_view.js",
    "frontend/js/views/bridges_view.js",
]


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


def _lane(code: str) -> int:
    with db.connect() as conn:
        return int(
            conn.execute(
                "SELECT id FROM weigh_lanes WHERE lane_code=?", (code,)
            ).fetchone()["id"]
        )


def _make_broken_ticket() -> int:
    """造一张‘票行带父票声明、但正式边为零’的断链票。"""
    with db.connect() as conn:
        parent = int(
            conn.execute(
                "SELECT id FROM weigh_tickets WHERE ticket_code=?",
                ("WT-LN-N-01-IN-001",),
            ).fetchone()["id"]
        )
        conn.execute(
            """INSERT INTO bl_manifests(bl_code,cargo_desc,declared_gross_t,
               vessel_voyage,status,created_at) VALUES (?,?,?,?,?,?)""",
            ("BL-2026-TONGTAI", "同态夹具", 5.0, "夹具轮/9", "在港", db.now_iso()),
        )
        cur = conn.execute(
            """INSERT INTO weigh_tickets(ticket_code,lane_id,bl_ref,vehicle_plate,
               axle_weights,gross_t,generation,parent_ticket_id,status,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                "PL-TT-001",
                _lane("LN-N-01-IN"),
                "BL-2026-TONGTAI",
                "沪A·同态",
                '[{"axle":1,"w":2.5},{"axle":2,"w":2.5}]',
                5.0,
                1,
                parent,
                "已裁定",
                db.now_iso(),
            ),
        )
        conn.execute(
            """INSERT INTO gate_seals(ticket_id,ticket_generation,manifest,seal_ref,
               status,sealed_at) VALUES (?,?,?,?,?,?)""",
            (cur.lastrowid, 1, "夹具快照", "GS-PL-TT-001-G1", "已封闸", db.now_iso()),
        )
        conn.commit()
        return int(cur.lastrowid)


def test_five_pages_share_one_lineage_verdict():
    c, h = _client()
    ticket_id = _make_broken_ticket()

    tickets_page = c.get("/api/tickets", headers=h).json()["items"]
    row = next(t for t in tickets_page if t["ticket_code"] == "PL-TT-001")
    assert row["lineage_complete"] is False, "过磅页未标断链"

    detail = c.get(f"/api/tickets/{ticket_id}", headers=h).json()
    assert detail["lineage"]["lineage_complete"] is False

    recon = c.get("/api/reconcile/BL-2026-TONGTAI", headers=h).json()
    assert recon["lineage_complete"] is False and recon["proof_ready"] is False
    proof = c.get("/api/proof/ticket/PL-TT-001", headers=h).json()
    assert proof["coherent"] is False and proof["lineage_complete"] is False

    seals_page = c.get("/api/seals", headers=h).json()["items"]
    seal = next(s for s in seals_page if s["ticket_code"] == "PL-TT-001")
    assert seal["lineage_complete"] is False, "快照页未标断链"

    bridges = c.get("/api/bridges", headers=h).json()["items"]
    bridge_id = next(b["id"] for b in bridges if b["bridge_code"] == "WB-N-01")
    bridge = c.get(f"/api/bridges/{bridge_id}", headers=h).json()
    assert "PL-TT-001" in bridge["lineage"]["broken_tickets"], "地磅页未标断链"

    integrity = c.get("/api/lineage/integrity", headers=h).json()
    assert "PL-TT-001" in integrity["broken_tickets"], "血缘页未标断链"
    assert integrity["lineage_complete"] is False

    # 封闸服务同口径：断链票不得封闸
    blocked = c.post("/api/seals", headers=h, json={"ticket_id": ticket_id})
    assert blocked.status_code == 409
    assert "血统" in blocked.json()["detail"]


def test_five_page_templates_carry_lineage_state():
    """五页模板都必须有血统态展示，杜绝只改一页或只调连线视觉。"""
    for rel in PAGES:
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert "lineage_complete" in text or "lineageIntegrity" in text, (
            f"{rel} 未展示血统态"
        )
    lineage = (ROOT / "frontend/js/views/lineage_view.js").read_text(
        encoding="utf-8"
    )
    # 影子边必须与正式链分开呈现，并给出晋升入口
    assert "影子" in lineage and "待校验" in lineage
    assert "promoteEdge" in lineage
    tickets = (ROOT / "frontend/js/views/tickets_view.js").read_text(
        encoding="utf-8"
    )
    assert "parent_ticket_id" in tickets and "fateBook" in tickets


def test_migration_deduped_shadow_copy_stays_complete():
    """旧库重复正式边降级出的影子副本，不得把已齐套的票判成断链。"""
    from edge_write import ticket_lineage_complete

    with db.connect() as conn:
        parent = int(
            conn.execute(
                "SELECT id FROM weigh_tickets WHERE ticket_code=?",
                ("WT-LN-N-01-IN-001",),
            ).fetchone()["id"]
        )
        child = int(
            conn.execute(
                "SELECT id FROM weigh_tickets WHERE ticket_code=?",
                ("WT-LN-N-01-IN-002",),
            ).fetchone()["id"]
        )
        # 已有正式边（种子）；补一条与正式边对位的影子副本（模拟迁移去重）
        conn.execute(
            """UPDATE weigh_tickets SET parent_ticket_id=? WHERE id=?""",
            (parent, child),
        )
        conn.execute(
            """INSERT INTO axle_lineage(from_ticket_id,to_ticket_id,edge_type,
               detail,edge_status,created_at) VALUES (?,?,?,?,?,?)""",
            (parent, child, "驳载分票", "迁移去重副本", "影子", db.now_iso()),
        )
        conn.commit()
        assert ticket_lineage_complete(conn, child) is True

        # 真正未决（无对位正式边）的影子仍须判断链
        other = conn.execute(
            "SELECT id FROM weigh_tickets WHERE ticket_code=?",
            ("WT-LN-S-01-IN-001",),
        ).fetchone()["id"]
        conn.execute(
            """INSERT INTO axle_lineage(from_ticket_id,to_ticket_id,edge_type,
               detail,edge_status,created_at) VALUES (?,?,?,?,?,?)""",
            (parent, other, "续磅", "未决影子", "影子", db.now_iso()),
        )
        conn.execute(
            "UPDATE weigh_tickets SET parent_ticket_id=? WHERE id=?",
            (parent, other),
        )
        conn.commit()
        assert ticket_lineage_complete(conn, other) is False
