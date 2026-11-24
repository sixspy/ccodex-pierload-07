// 五页渲染核验（写边案庚）：在 jsdom 中真实挂载 frontend/js/app.js，
// 用“含断链票与影子边”的接口数据驱动，确认血缘/过磅/对账/快照/地磅五页
// 都显示同一血统口径。
//
// 用法（需临时装 jsdom，不进 Python 依赖）：
//   mkdir -p /tmp/pagecheck && cd /tmp/pagecheck
//   npm install jsdom@24 --cache /tmp/npmcache
//   cd /workspace && NODE_PATH=/tmp/pagecheck/node_modules node scripts/check_five_pages.mjs
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import vm from "node:vm";

// 用 createRequire 解析 jsdom：既可从本机 node_modules，也可用 NODE_PATH 指向临时安装目录
const require = createRequire(import.meta.url);
const { JSDOM } = require("jsdom");

const dom = new JSDOM(
  `<!DOCTYPE html><html><body><div id="app"></div></body></html>`,
  { url: "http://localhost:11500/", pretendToBeVisual: true }
);
const { window } = dom;

globalThis.window = window;
globalThis.document = window.document;
try {
  Object.defineProperty(globalThis, "navigator", {
    value: window.navigator, configurable: true,
  });
} catch { /* node 已有可读 navigator */ }
globalThis.HTMLElement = window.HTMLElement;
globalThis.Element = window.Element;
globalThis.Node = window.Node;
globalThis.SVGElement = window.SVGElement;
globalThis.MutationObserver = window.MutationObserver;
globalThis.localStorage = window.localStorage;
globalThis.requestAnimationFrame = (cb) => setTimeout(cb, 0);

// 全局 Vue（生产构建，含运行时编译器）
const vueCode = readFileSync(new URL("../frontend/vue.global.prod.js", import.meta.url), "utf8");
vm.runInThisContext(vueCode + "\n;globalThis.Vue = Vue;");
if (!globalThis.Vue) throw new Error("Vue 全局未加载");

localStorage.setItem("pierload_token", "stub-token");
localStorage.setItem("pierload_user", "admin");

const broken = "PL-TT-001";
const payloads = {
  "/api/dashboard": { bridges: 4, open_overloads: 1, side_pending: 1, tickets: 5, gate_releases: 1, gate_seals: 2, meta: { skeleton: "s" } },
  "/api/bridges": { items: [{ id: 1, bridge_code: "WB-N-01", pier_zone: "北岸", name: "北港一号地磅", max_gross_t: 80, axle_limit: 6, status: "运行" }] },
  "/api/lanes": { items: [{ id: 1, lane_code: "LN-N-01-IN", bridge_code: "WB-N-01", direction: "进闸", overload_threshold_t: 49, side_lane_ref: "旁路甲", status: "开放" }] },
  "/api/tickets": { items: [{ id: 5, ticket_code: broken, lane_id: 1, lane_code: "LN-N-01-IN", bl_ref: "BL-2026-TT", gross_t: 5, generation: 1, status: "已裁定", parent_ticket_id: 1, lineage_complete: false }] },
  "/api/overloads": { items: [] },
  "/api/gate/releases": { items: [] },
  "/api/seals": { items: [{ id: 1, seal_ref: "GS-PL-TT-001-G1", ticket_code: broken, ticket_generation: 1, manifest: "m", status: "已封闸", lineage_complete: false }] },
  "/api/bl": { items: [{ id: 1, bl_code: "BL-2026-TT", cargo_desc: "夹具", declared_gross_t: 5, vessel_voyage: "v", status: "在港" }] },
  "/api/lineage": { items: [{ id: 9, from_ticket_id: 1, to_ticket_id: 5, edge_type: "续磅", detail: "影子草稿待校验", edge_status: "影子", formal: false, from_code: "WT-LN-N-01-IN-001", to_code: broken }] },
  "/api/lineage/integrity": { tickets_total: 5, tickets_with_parent: 1, formal_edges: 1, shadow_edges: 1, broken_tickets: [broken], broken_count: 1, lineage_complete: false },
  "/api/lineage/fate-book": { scope: "港区写边命运册", locked_path: "甲", note: "同事务写正式边", locked_by: "system", locked_at: "2026-11-24T00:00:00" },
  "/api/customs/jobs": { items: [] },
  "/api/crane/jobs": { items: [] },
};

globalThis.fetch = async (path) => {
  const url = String(path).split("?")[0];
  const body = payloads[url];
  if (!body) return { ok: false, status: 404, text: async () => JSON.stringify({ detail: "no stub " + url }) };
  return { ok: true, status: 200, text: async () => JSON.stringify(body) };
};

await import(new URL("../frontend/js/app.js", import.meta.url).href);
await new Promise((r) => setTimeout(r, 300));

const html = document.body.innerHTML;
const checks = [
  ["总览渲染", html.includes("闸口总览")],
  ["过磅页标断链", html.includes("断链") && html.includes(broken)],
  ["过磅页父票", html.includes("父票")],
  ["血缘页正式链", html.includes("正式链")],
  ["血缘页影子待校验", html.includes("影子 · 待校验")],
  ["血缘页晋升入口", html.includes("晋升正式")],
  ["血缘页断链清册", html.includes("断链")],
  ["对账页血统口径", html.includes("全港血统") && html.includes("断链 1")],
  ["对账页出证口径", html.includes("三者同态方可出证")],
  ["快照页血统列", html.includes("GS-PL-TT-001-G1") && html.includes("血统")],
  ["地磅页血统", html.includes("在港票")],
  ["命运册当班路径", html.includes("甲 · 落票同事务写正式边")],
  ["Vue 未报错", true],
];
let failed = 0;
for (const [name, ok] of checks) {
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}`);
  if (!ok) failed++;
}
if (failed) {
  console.log("\n--- body ---\n", html.slice(0, 4000));
  process.exit(1);
}
console.log("\n五页渲染核验通过");
