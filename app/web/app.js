/* 营养指标平台前端：无框架单页应用，全部数据来自 /api。 */
const $ = (id) => document.getElementById(id);

async function api(path, body) {
  const opt = body ? { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body) } : {};
  const resp = await fetch(path, opt);
  const data = await resp.json();
  if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
  return data;
}

function esc(v) {
  return String(v ?? "").replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function fillSelect(sel, items, valueKey, labelFn) {
  const current = sel.value;
  sel.innerHTML = sel.id.startsWith("q-") ? '<option value="">全部</option>' : "";
  for (const item of items) {
    const opt = document.createElement("option");
    opt.value = valueKey ? item[valueKey] : item;
    opt.textContent = labelFn ? labelFn(item) : item;
    sel.appendChild(opt);
  }
  if ([...sel.options].some((o) => o.value === current)) sel.value = current;
}

// ---------------------------------------------------------- 标签页
document.querySelectorAll(".tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    $(`tab-${btn.dataset.tab}`).classList.add("active");
    if (btn.dataset.tab === "catalog") renderCatalog();
    if (btn.dataset.tab === "subs") renderSubs();
    if (btn.dataset.tab === "inbox") renderInbox();
  });
});

// ---------------------------------------------------------- 初始化下拉
async function init() {
  const [catalog, options] = await Promise.all([
    api("/api/catalog"), api("/api/options"),
  ]);
  const metrics = catalog.metrics;
  for (const id of ["q-metric", "r-metric", "b-metric"]) {
    const sel = $(id);
    sel.innerHTML = "";
    for (const m of metrics) {
      const opt = document.createElement("option");
      opt.value = m.code;
      opt.textContent = `${m.name} (${m.code} v${m.version})`;
      sel.appendChild(opt);
    }
  }
  fillSelect($("q-region"), options.regions);
  fillSelect($("q-family"), options.families);
  fillSelect($("q-spec"), options.specs);
  const topicSel = $("q-topic");
  topicSel.innerHTML = '<option value="">不按专题</option>';
  for (const t of options.topics) {
    const opt = document.createElement("option");
    opt.value = t; opt.textContent = t;
    topicSel.appendChild(opt);
  }
}

// ---------------------------------------------------------- 查询
$("q-run").addEventListener("click", async () => {
  const body = {
    metric_code: $("q-metric").value,
    cutoff_mode: $("q-mode").value,
    cutoff: $("q-cutoff").value,
    period_start: $("q-start").value,
    period_end: $("q-end").value,
    region: $("q-region").value || null,
    family: $("q-family").value || null,
    spec_id: $("q-spec").value || null,
    topic: $("q-topic").value || null,
    attribution_version: $("q-attr").value || null,
    group_by: $("q-group").value.split(",").map((s) => s.trim()).filter(Boolean),
  };
  $("q-blocked").classList.add("hidden");
  try {
    const data = await api("/api/query", body);
    if (data.blocked_reason) {
      $("q-blocked").textContent = `不可计算：${data.blocked_reason}`;
      $("q-blocked").classList.remove("hidden");
      $("q-result").innerHTML = "";
    } else {
      renderRows(data.rows, data.audit);
    }
    renderAudit(data.audit);
  } catch (err) {
    $("q-blocked").textContent = err.message;
    $("q-blocked").classList.remove("hidden");
  }
});

function renderRows(rows, audit) {
  if (!rows.length) { $("q-result").innerHTML = "<p>无结果分组。</p>"; return; }
  const dims = ["region", "family", "spec", "topic"];
  const cols = [...dims.filter((d) => rows.some((r) => r[d] !== null)),
    "value", "uncertain_value"];
  const head = cols.map((c) => `<th>${{
    value: audit.unit === "ratio" ? "值（比率）" :
      audit.unit === "minor_amount" ? "值（分）" : "值",
    uncertain_value: "不确定区（缺项不当零）",
  }[c] || c)}</th>`).join("");
  const body = rows.map((r) => "<tr>" + cols.map((c) => {
    if (c === "uncertain_value") {
      return `<td class="${r[c] != null ? "uncertain" : ""}">${
        r[c] == null ? "—" : typeof r[c] === "object" ? JSON.stringify(r[c]) : r[c]}</td>`;
    }
    if (c === "value") return `<td><b>${r[c] == null ? "不可计算" : r[c]}</b></td>`;
    return `<td>${esc(r[c])}</td>`;
  }).join("") + "</tr>").join("");
  $("q-result").innerHTML = `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}

function renderAudit(audit) {
  const kv = (k, v) => `<div><b>${esc(k)}</b><span>${esc(typeof v === "object" ? JSON.stringify(v) : v)}</span></div>`;
  const fc = audit.fact_context;
  $("q-audit-body").innerHTML = `
    <div class="kv">
      ${kv("指标 / 版本", `${audit.metric_name} v${audit.version}（${audit.status}）`)}
      ${kv("负责人", audit.owner)}
      ${kv("单位", audit.unit)}
      ${kv("时间归属", audit.time_basis === "operating" ? "经营期（按原成交窗口）" : "回款期（按实际发生日）")}
      ${kv("去重对象", audit.dedup_object)}
      ${kv("分子", audit.numerator)}
      ${kv("分母", audit.denominator)}
      ${kv("可加性", audit.additivity)}
      ${kv("表达式", audit.expression || "（基础度量）")}
      ${kv("事实截止", `${fc.mode} @ ${fc.cutoff}`)}
      ${kv("经营期", `${fc.period_start} 至 ${fc.period_end}（不含）`)}
      ${kv("歧义成交行", fc.ambiguous_sale_count)}
      ${kv("歧义规格", fc.ambiguous_spec_count)}
      ${kv("剔除重复报送行", fc.excluded_duplicate_count)}
    </div>
    <h4>计算血缘</h4><ul>${audit.lineage.map((l) => `<li>${esc(l)}</li>`).join("")}</ul>
    <h4>截止规则说明</h4><ul>${(fc.cutoff_notes || []).map((l) => `<li>${esc(l)}</li>`).join("") || "<li>无</li>"}</ul>`;
}

// ---------------------------------------------------------- 登记册
async function renderCatalog() {
  const data = await api("/api/catalog");
  $("catalog-list").innerHTML = data.metrics.map((m) => `
    <div class="metric-card">
      <h4>${esc(m.name)} <span class="pill">${esc(m.code)}</span>
        <span class="badge ${m.status}">v${m.version} ${m.status}</span></h4>
      <div class="small">
        负责人 ${esc(m.owner)} ｜ 时间归属 ${m.time_basis} ｜ 单位 ${m.unit} ｜
        去重 ${esc(m.dedup_object)} ｜ 可加性 ${m.additivity}<br>
        分子：${esc(m.numerator)}<br>分母：${esc(m.denominator)}<br>
        可展开维度：${esc(m.eligible_dims.join(", "))}
        ${m.expression ? `<br>表达式：<code>${esc(m.expression)}</code>` : ""}
        ${m.window_days ? `<br>观察窗：${m.window_days} 天` : ""}
      </div>
    </div>`).join("");
  const drafts = data.metrics.filter((m) => m.status === "draft");
  fillSelect($("draft-picker"), drafts, "code", (m) => `${m.code} v${m.version}`);
}

$("btn-register").addEventListener("click", async () => {
  try {
    const payload = JSON.parse($("new-metric").value);
    const data = await api("/api/metrics/register", payload);
    $("catalog-msg").textContent = `已登记草稿：${data.code}（尚未确认，业务端暂不可查询）`;
    renderCatalog();
  } catch (err) { $("catalog-msg").textContent = err.message; }
});

$("btn-confirm-selected").addEventListener("click", async () => {
  const code = $("draft-picker").value;
  if (!code) return;
  try {
    await api("/api/metrics/confirm", { metric_code: code, actor: "analyst" });
    $("catalog-msg").textContent = `${code} 校核通过并已确认`;
    renderCatalog(); init();
  } catch (err) { $("catalog-msg").textContent = err.message; }
});

// ---------------------------------------------------------- 换版
$("btn-preview").addEventListener("click", async () => {
  try {
    const data = await api("/api/revise/preview", {
      metric_code: $("r-metric").value,
      changes: JSON.parse($("r-changes").value || "{}"),
      note: $("r-note").value, actor: $("r-actor").value,
      preview_context: {
        cutoff: $("r-cutoff").value,
        period_start: $("r-start").value, period_end: $("r-end").value,
      },
    });
    $("revise-msg").textContent = JSON.stringify(data, null, 2);
  } catch (err) { $("revise-msg").textContent = err.message; }
});

$("btn-confirm-rev").addEventListener("click", async () => {
  try {
    const data = await api("/api/revise/confirm", {
      metric_code: $("r-metric").value,
      changes: JSON.parse($("r-changes").value || "{}"),
      note: $("r-note").value, actor: $("r-actor").value,
    });
    $("revise-msg").textContent = JSON.stringify(data, null, 2);
    init(); renderSubs();
  } catch (err) { $("revise-msg").textContent = err.message; }
});

// ---------------------------------------------------------- 差异桥
$("btn-bridge").addEventListener("click", async () => {
  try {
    const data = await api("/api/bridge", {
      metric_code: $("b-metric").value,
      baseline_version: Number($("b-base-ver").value),
      prior_cutoff: $("b-prior").value, current_cutoff: $("b-current").value,
      prior_cutoff_mode: "replay", current_cutoff_mode: "restate",
      period_start: $("b-start").value, period_end: $("b-end").value,
    });
    $("bridge-msg").textContent = JSON.stringify(data, null, 2);
  } catch (err) { $("bridge-msg").textContent = err.message; }
});

// ---------------------------------------------------------- 订阅
async function renderSubs() {
  const data = await api("/api/subscriptions");
  $("sub-list").innerHTML = data.subscriptions.map((s) => `
    <div class="notice">
      <h4>${esc(s.id)} <span class="badge ${s.status}">${s.status}</span></h4>
      <div class="small">
        收件方 ${esc(s.recipient)} ｜ ${esc(s.metric_name)} 锁定 v${s.metric_version} ｜
        归属 ${esc(s.attribution_version || "无")} ｜ ${s.cutoff_mode} @ ${esc(s.cutoff)}<br>
        期间 ${s.period_start} ~ ${s.period_end} ｜ 到期 ${esc(s.scheduled_at)} ｜
        delivery_key ${esc(s.delivery_key)}
        ${s.renewal_reason ? `<br><b style="color:var(--warn)">待重新确认：${esc(s.renewal_reason)}</b>` : ""}
        ${s.data_revision_notice ? `<br><b style="color:var(--bad)">资料修订提示：${esc(s.data_revision_notice)}</b>` : ""}
      </div>
    </div>`).join("");
  fillSelect($("sub-picker"), data.subscriptions, "id", (s) => `${s.id} (${s.status})`);
}

$("btn-sub-create").addEventListener("click", async () => {
  try {
    const data = await api("/api/subscriptions/create", JSON.parse($("new-sub").value));
    $("sub-msg").textContent = `订阅已建立：${data.id}`;
    renderSubs();
  } catch (err) { $("sub-msg").textContent = err.message; }
});

$("btn-sub-renew").addEventListener("click", async () => {
  try {
    const data = await api("/api/subscriptions/renew", {
      subscription_id: $("sub-picker").value, actor: "recipient",
    });
    $("sub-msg").textContent = `续约已确认：${data.id} -> v${data.metric_version}`;
    renderSubs();
  } catch (err) { $("sub-msg").textContent = err.message; }
});

// ---------------------------------------------------------- 收件箱
$("btn-due").addEventListener("click", async () => {
  try {
    const data = await api("/api/reports/run-due", { now: $("now-at").value });
    $("due-msg").textContent = JSON.stringify(data, null, 2);
    renderInbox();
  } catch (err) { $("due-msg").textContent = err.message; }
});

async function renderInbox() {
  const data = await api("/api/inbox");
  $("inbox-list").innerHTML = data.notices.map((n) => `
    <div class="notice">
      <h4>${esc(n.notice_key)} ${n.status === "renewal_required"
        ? '<span class="badge renewal_required">口径待确认</span>' : ""}</h4>
      <div class="small">
        收件方 ${esc(n.recipient)} ｜ 生成于 ${esc(n.generated_at)}<br>
        事实截面 ${esc(n.report.fact_cross_section.mode)} @ ${esc(n.report.fact_cross_section.cutoff)}
        ｜ 归属 ${esc(n.report.attribution_version || "无")}<br>
        指标章节：${n.report.sections.map((s) => s.metric_code
          ? `${esc(s.metric_code)} v${s.metric_version}` : `阻断:${esc(s.blocked_reason)}`).join("；")}
        ${n.renewal_reason ? `<br><b style="color:var(--warn)">${esc(n.renewal_reason)}</b>` : ""}
      </div>
      <details><summary>报告全文</summary><pre class="msg">${esc(JSON.stringify(n.report, null, 2))}</pre></details>
    </div>`).join("") || "<p>暂无通知。</p>";
}

init().catch((err) => {
  document.querySelector("main").insertAdjacentHTML(
    "afterbegin", `<div class="blocked">初始化失败：${esc(err.message)}</div>`);
});
