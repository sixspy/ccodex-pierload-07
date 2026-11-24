export const ticketsViewTemplate = `
<section class="panel">
  <h2>过磅票采集</h2>
  <p class="hint">
    录入各轴重 JSON；超重自动旁路挂起并开超重记录。
    续磅票须选父票，入库即按当班命运册路径同写血缘边；断链票不得封闸出证。
  </p>
  <div class="form-row">
    <div><label>车道</label>
      <select v-model.number="ticketForm.lane_id">
        <option v-for="l in lanes" :key="l.id" :value="l.id">{{ l.lane_code }}</option>
      </select>
    </div>
    <div><label>提单号</label>
      <select v-model="ticketForm.bl_ref">
        <option v-for="b in blList" :key="b.bl_code" :value="b.bl_code">{{ b.bl_code }}</option>
      </select>
    </div>
    <div><label>车牌</label><input v-model="ticketForm.vehicle_plate"/></div>
    <div><label>票号(可空)</label><input v-model="ticketForm.ticket_code"/></div>
    <div><label>父票(续磅才选)</label>
      <select v-model="ticketForm.parent_ticket_id">
        <option :value="null">— 非续磅 —</option>
        <option v-for="t in tickets" :key="t.id" :value="t.id">{{ t.ticket_code }}</option>
      </select>
    </div>
    <div><label>边类型</label><input v-model="ticketForm.edge_type"/></div>
  </div>
  <label>轴重 JSON</label>
  <textarea v-model="ticketForm.axles_json" rows="4" placeholder='[{"axle":1,"w":8.2}]'></textarea>
  <div class="actions">
    <button @click="createTicket">过磅入闸</button>
    <button class="secondary" @click="refresh">刷新</button>
  </div>
  <p v-if="fateBook" class="hint">
    当班写边路径
    <span class="badge pass">{{ fateBook.locked_path === '甲' ? '甲 · 同事务写正式边' : '乙 · 影子待晋升' }}</span>
    —— 偏离路径的写边一律记偏航并拒写。
  </p>
  <table class="data-table">
    <thead><tr><th>票号</th><th>车道</th><th>提单</th><th>毛重</th><th>状态</th><th>代</th><th>血统</th><th></th></tr></thead>
    <tbody>
      <tr v-for="t in tickets" :key="t.id" @click="selectTicket(t)">
        <td>{{ t.ticket_code }}</td>
        <td>{{ t.lane_code }}</td>
        <td>{{ t.bl_ref }}</td>
        <td>{{ fmtT(t.gross_t) }}</td>
        <td><span class="badge" :class="ticketClass(t.status)">{{ t.status }}</span></td>
        <td>{{ t.generation }}</td>
        <td>
          <span class="badge" :class="t.lineage_complete ? 'pass' : 'fail'">
            {{ t.lineage_complete ? '齐全' : '断链' }}
          </span>
          <span v-if="t.parent_ticket_id" class="mono"> 父#{{ t.parent_ticket_id }}</span>
        </td>
        <td><button class="mini-btn" @click.stop="loadTicketDetail(t.id)">详情</button></td>
      </tr>
    </tbody>
  </table>
  <div v-if="ticketDetail" class="detail-box">
    <p>车牌 {{ ticketDetail.ticket.vehicle_plate }} · 超重挂起 {{ ticketDetail.overload_holds.length }} 条</p>
    <p>
      血统
      <span class="badge" :class="ticketDetail.lineage && ticketDetail.lineage.lineage_complete ? 'pass' : 'fail'">
        {{ ticketDetail.lineage && ticketDetail.lineage.lineage_complete ? '齐全' : '断链' }}
      </span>
      <span v-if="ticketDetail.lineage && ticketDetail.lineage.reason" class="recon-warn">
        {{ ticketDetail.lineage.reason }}
      </span>
    </p>
  </div>
</section>
`;
