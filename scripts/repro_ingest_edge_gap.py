"""写边案庚 · 假绿复现：入库已成功但轴重血缘边表空行。

复现步骤：
  1. 临时库 + 标准种子；
  2. 直接写入 PL-G41 正式票行（等价于“入库写口已成功落正式行”），
     同时写闸代快照，但**不写任何 axle_lineage 边**；
  3. 分别读取血缘页接口、对账页接口、单票证明接口，记录它们对“血统齐全”的判断。

期望（修复后）：
  - 血缘页必须暴露断链，不得把该票画成正式链；
  - 对账 / 证明必须把“正式边为零”的票判为血统不齐，不得 proof_ready。

当前（复现时）：边表 0 行，���对账仍给 proof_ready / coherent，即假绿。
   案例要点：正式票行已落库且票行上带有父票声明（续磅来源），
   但 axle_lineage 没有对应父子边 —— 这对账仍在旧实现里判“血统齐全”。
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import db  # noqa: E402

FIXTURE = ROOT / "fixtures" / "ingest_edge_gap_pl_g41.json"


def main() -> int:
    case = json.loads(FIXTURE.read_text(encoding="utf-8"))
    tmp = tempfile.mkdtemp(prefix="pierload-g41-")
    db.DB_PATH = Path(tmp) / "repro.db"
    db.init_db()

    with db.connect() as conn:
        lane = conn.execute(
            "SELECT id FROM weigh_lanes WHERE lane_code=?", (case["lane_code"],)
        ).fetchone()
        parent = conn.execute(
            "SELECT id FROM weigh_tickets WHERE ticket_code=?",
            (case["parent_ticket_code"],),
        ).fetchone()
        # 该提单只为 PL-G41 单票申报，令对账金额天然吻合，从而只暴露血统缺口
        conn.execute(
            """INSERT INTO bl_manifests(bl_code,cargo_desc,declared_gross_t,
               vessel_voyage,status,created_at) VALUES (?,?,?,?,?,?)""",
            (
                case["bl_ref"],
                "写边案庚夹具货",
                case["bl_declared_gross_t"],
                "夹具轮/0001",
                "在港",
                db.now_iso(),
            ),
        )
        # 入库写口已成功：正式票行已落库（票行带父票声明），但边表零增
        cur = conn.execute(
            """INSERT INTO weigh_tickets(ticket_code,lane_id,bl_ref,vehicle_plate,
               axle_weights,gross_t,generation,parent_ticket_id,status,
               adjudicated_at,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                case["ticket_code"],
                lane["id"],
                case["bl_ref"],
                case["vehicle_plate"],
                json.dumps(case["axle_weights"], ensure_ascii=False),
                case["gross_t"],
                case["generation"],
                parent["id"],
                case["status"],
                db.now_iso(),
                db.now_iso(),
            ),
        )
        ticket_id = cur.lastrowid
        conn.execute(
            """INSERT INTO gate_seals(ticket_id,ticket_generation,manifest,seal_ref,
               status,sealed_at) VALUES (?,?,?,?,?,?)""",
            (
                ticket_id,
                case["generation"],
                case["seal_manifest"],
                case["seal_ref"],
                "已封闸",
                db.now_iso(),
            ),
        )
        conn.commit()
        edges = conn.execute(
            "SELECT COUNT(*) c FROM axle_lineage WHERE from_ticket_id=? OR to_ticket_id=?",
            (ticket_id, ticket_id),
        ).fetchone()["c"]

    from fastapi.testclient import TestClient

    from app import app

    c = TestClient(app)
    tok = c.post(
        "/api/login", json={"username": "admin", "password": "admin123"}
    ).json()["token"]
    h = {"Authorization": "Bearer " + tok}

    recon = c.get(f"/api/reconcile/{case['bl_ref']}", headers=h).json()
    proof = c.get(f"/api/proof/ticket/{case['ticket_code']}", headers=h).json()
    lineage_page = c.get("/api/lineage", headers=h).json()["items"]
    lineage_ticket = c.get(f"/api/lineage/ticket/{ticket_id}", headers=h).json()

    print("== 写边案庚 · 假绿复现 ==")
    print(f"夹具: {FIXTURE.relative_to(ROOT)}")
    print(f"PL-G41 ticket_id={ticket_id} parent={parent['id'] if parent else None}")
    print(f"正式票行: 已落库 (status={case['status']})")
    print(f"轴重血缘边行数: {edges}  (夹具声明应是空行)")
    print(f"血缘页边总数: {len(lineage_page)}")
    print(f"血缘页/该票 outbound={len(lineage_ticket['outbound'])} "
          f"inbound={len(lineage_ticket['inbound'])}")
    print(f"对账页 balanced={recon.get('balanced')} "
          f"proof_ready={recon.get('proof_ready')} "
          f"lineage_complete字段={'有' if 'lineage_complete' in recon else '缺失'}")
    print(f"证明页 coherent={proof.get('coherent')} "
          f"lineage_complete字段={'有' if 'lineage_complete' in proof else '缺失'}")

    false_green = (
        edges == 0
        and recon.get("lineage_complete") is not False
        and recon.get("proof_ready") is True
        and "lineage_complete" not in recon
        and proof.get("coherent") is True
    )
    fixed = (
        edges == 0
        and recon.get("lineage_complete") is False
        and recon.get("proof_ready") is False
        and proof.get("coherent") is False
        and case["ticket_code"] in (recon.get("broken_tickets") or [])
    )
    print()
    if false_green:
        print("复现结果: 假绿成立 —— 边表 0 行，对账/证明仍判齐全，血缘页断链不可见。")
        return 0
    if fixed:
        print("复现结果: 缺陷已识别 —— 边表 0 行现被对账判为血统断链，证明不再放行。")
        return 2
    print("复现结果: 未复现（当前实现已非假绿）。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
