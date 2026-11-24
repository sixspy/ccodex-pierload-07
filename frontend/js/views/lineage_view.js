export const lineageViewTemplate = `
<section class="panel">
  <h2>轴重血缘</h2>
  <p class="hint">
    正式链与影子连边草稿分离：影子边只是“待校验”，晋升后才计入正式血统；
    断链票在过磅、对账、快照、地磅四页同口径可见。
  </p>
  <div v-if="fateBook" class="detail-box">
    <p>
      当班写边路径
      <span class="badge pass">{{ fateBook.locked_path === '甲' ? '甲 · 落票同事务写正式边' : '乙 · 影子草稿待校验晋升' }}</span>
    </p>
    <p class="hint">{{ fateBook.note }}</p>
    <p class="hint">锁定 {{ fateBook.locked_by }} · {{ fateBook.locked_at }}</p>
  </div>
  <div v-if="lineageIntegrity" class="hero-cards">
    <div class="hero-card"><span class="t">在册票</span><div class="n">{{ lineageIntegrity.tickets_total }}</div></div>
    <div class="hero-card"><span class="t">正式边</span><div class="n">{{ lineageIntegrity.formal_edges }}</div></div>
    <div class="hero-card"><span class="t">影子待校验</span><div class="n">{{ lineageIntegrity.shadow_edges }}</div></div>
    <div class="hero-card"><span class="t">断链票</span><div class="n" :class="lineageIntegrity.broken_count ? 'recon-warn' : 'recon-ok'">{{ lineageIntegrity.broken_count }}</div></div>
  </div>
  <p v-if="lineageIntegrity && lineageIntegrity.broken_tickets.length" class="recon-warn">
    断链：{{ lineageIntegrity.broken_tickets.join('、') }}
  </p>
  <div class="form-row">
    <div><label>起点票</label>
      <select v-model.number="lineageForm.from_ticket_id">
        <option v-for="t in tickets" :key="t.id" :value="t.id">{{ t.ticket_code }}</option>
      </select>
    </div>
    <div><label>终点票(可空)</label>
      <select v-model.number="lineageForm.to_ticket_id">
        <option :value="null">—</option>
        <option v-for="t in tickets" :key="t.id" :value="t.id">{{ t.ticket_code }}</option>
      </select>
    </div>
    <div><label>类型</label><input v-model="lineageForm.edge_type"/></div>
    <div><label>说明</label><input v-model="lineageForm.detail"/></div>
  </div>
  <div class="actions">
    <button @click="addLineage">登记影子草稿</button>
    <button class="secondary" @click="refresh">刷新</button>
  </div>
  <table class="data-table">
    <thead><tr><th>自</th><th>至</th><th>类型</th><th>边态</th><th>说明</th><th></th></tr></thead>
    <tbody>
      <tr v-for="e in lineageEdges" :key="e.id">
        <td>{{ e.from_code || e.from_ticket_id }}</td>
        <td>{{ e.to_code || '—' }}</td>
        <td>{{ e.edge_type }}</td>
        <td>
          <span class="badge" :class="e.edge_status === '正式' ? 'pass' : 'warn'">
            {{ e.edge_status === '正式' ? '正式链' : '影子 · 待校验' }}
          </span>
        </td>
        <td>{{ e.detail }}</td>
        <td>
          <button v-if="e.edge_status !== '正式'" class="mini-btn" @click="promoteEdge(e.id)">晋升正式</button>
        </td>
      </tr>
    </tbody>
  </table>
</section>
`;
