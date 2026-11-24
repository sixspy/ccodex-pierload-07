export const reconcileViewTemplate = `
<section class="grid-2">
  <section class="panel">
    <h2>提单对账</h2>
    <p class="hint">汇总过磅毛重与提单申报吨比对；金额、血统、快照三者同态方可出证。</p>
    <div class="form-row">
      <div><label>提单号</label><input v-model="reconCode"/></div>
    </div>
    <div class="actions">
      <button @click="loadReconcile">对账</button>
      <button class="secondary" @click="loadAllReconcile">全量</button>
    </div>
    <div v-if="reconOne" class="detail-box">
      <p>申报 {{ reconOne.declared_gross_t }}t · 过磅合计 {{ reconOne.ticket_sum_t }}t</p>
      <p :class="reconOne.balanced ? 'recon-ok' : 'recon-warn'">{{ reconOne.balanced ? '账实平衡' : '偏差待查' }}</p>
      <p :class="reconOne.lineage_complete ? 'recon-ok' : 'recon-warn'">
        血统 {{ reconOne.lineage_complete ? '齐全' : '断链' }}
        <span v-if="!reconOne.lineage_complete">：{{ (reconOne.broken_tickets || []).join('、') }}</span>
      </p>
      <p>快照数 {{ reconOne.seal_count }} · 可出证明
        <span :class="reconOne.proof_ready ? 'recon-ok' : 'recon-warn'">{{ reconOne.proof_ready ? '是' : '否' }}</span>
      </p>
    </div>
    <div v-if="lineageIntegrity" class="detail-box">
      <p>
        全港血统
        <span class="badge" :class="lineageIntegrity.broken_count ? 'fail' : 'pass'">
          {{ lineageIntegrity.broken_count ? '断链 ' + lineageIntegrity.broken_count : '齐全' }}
        </span>
        · 正式边 {{ lineageIntegrity.formal_edges }} · 影子待校验 {{ lineageIntegrity.shadow_edges }}
      </p>
    </div>
    <ul v-if="reconAllItems.length">
      <li v-for="r in reconAllItems" :key="r.bl_code">
        {{ r.bl_code }} ·
        <span :class="r.balanced ? 'recon-ok' : 'recon-warn'">{{ r.balanced ? '平' : '偏' }}</span>
        ·
        <span :class="r.lineage_complete ? 'recon-ok' : 'recon-warn'">{{ r.lineage_complete ? '血统齐' : '血统断' }}</span>
      </li>
    </ul>
  </section>
  <section class="panel">
    <h2>单票证明</h2>
    <div class="form-row"><div><label>票号</label><input v-model="proofCode"/></div></div>
    <div class="actions"><button @click="loadProof">拉证明</button></div>
    <div v-if="proofOne" class="detail-box">
      <p>提单匹配 {{ proofOne.manifest_matches_bl ? '是' : '否' }}</p>
      <p>超重闭环 {{ proofOne.overload_closed ? '是' : '否' }}</p>
      <p>已放行 {{ proofOne.released ? '是' : '否' }}</p>
      <p :class="proofOne.lineage_complete ? 'recon-ok' : 'recon-warn'">
        血缘齐全 {{ proofOne.lineage_complete ? '是' : '否' }}
      </p>
      <p :class="proofOne.coherent ? 'recon-ok' : 'recon-warn'">一致 {{ proofOne.coherent ? '是' : '否' }}</p>
    </div>
  </section>
</section>
`;
