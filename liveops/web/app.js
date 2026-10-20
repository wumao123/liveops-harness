const LIVE_ID = "LIVE-DEMO-001";
const $ = (selector) => document.querySelector(selector);
let sending = false;
let historyLoaded = false;
let campaignBusy = false;
let pauseCampaign = false;
let currentCampaign = null;
let finalMaterials = null;
let runtimeState = null;
let originalArtifact = null;
let catalog = [];
const shownCommands = new Set();
const taskLabels = { merchandiser: "选品", copywriter: "手卡", compliance: "事实核验", director: "汇总" };
const statusLabels = { pending: "待执行", running: "执行中", paused: "已暂停", blocked: "任务受阻", succeeded: "已完成", review_required: "材料待修改", failed: "失败", unknown: "待对账", manual_review: "需人工核对", ready: "已准备", sent: "已发送" };

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

async function request(path, options = {}) {
  const response = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
  const body = await response.json();
  if (!response.ok) throw new Error(typeof body.detail === "string" ? body.detail : body.message || body.code || "请求失败");
  return body;
}

function amount(cents) { return (cents / 100).toFixed(2); }

function addMessage(text, role, result) {
  const message = document.createElement("div");
  message.className = `message ${role}`;
  message.textContent = text;
  if (result) {
    const source = result.interpretation?.source;
    const generation = result.generation || {};
    const label = source === "model" ? "模型理解" : source === "offline_fallback" ? "规则降级" : "离线理解";
    const details = [label];
    if (generation.source === "model") details.push("模型建议草稿");
    if (generation.source === "offline_fallback") details.push("生成降级，保留事实结果");
    if (generation.reported_total_tokens != null) details.push(`生成 ${generation.reported_total_tokens} tokens`);
    if (result.status === "clarification") details.push("等待补充信息，未执行");
    message.append(element("small", details.join(" · "), "message-meta"));
    if (result.status === "ok" && ["advise", "plan", "brief"].includes(result.action?.type)) {
      const feedback = element("div", undefined, "advice-feedback");
      const reasons = {
        modify: [["措辞", "wording"], ["顺序", "order"], ["太长", "too_long"], ["缺信息", "missing_info"], ["其他", "other"]],
        reject: [["商品不对", "wrong_product"], ["顺序不对", "wrong_order"], ["事实错误", "factual_error"], ["答非所问", "off_topic"], ["夸大功效", "unsafe_claim"], ["其他", "other"]],
      };
      const send = async (label, choice, reason) => {
        for (const sibling of feedback.querySelectorAll("button")) sibling.disabled = true;
        try {
          await request(`/api/feedback/${LIVE_ID}/${encodeURIComponent(result.command_id)}`, { method: "POST", body: JSON.stringify({ event_id: crypto.randomUUID(), choice, ...(reason ? { reason } : {}) }) });
          feedback.replaceChildren(element("small", `已记录：${label}；未执行业务操作`));
          await refreshRuntime();
        } catch (error) {
          feedback.append(element("small", error.message));
          for (const sibling of feedback.querySelectorAll("button")) sibling.disabled = false;
        }
      };
      for (const [label, choice] of [["采纳建议", "accept"], ["需要修改", "modify"], ["不采纳", "reject"]]) {
        const button = element("button", label, "table-action");
        button.title = "只记录反馈，不执行切品、改价或发券";
        button.onclick = () => {
          if (choice === "accept") return send(label, choice);
          // A reason is optional; skipping it is counted as unreasoned feedback.
          feedback.replaceChildren(element("small", `${label}的原因：`));
          for (const [reasonLabel, reason] of [...reasons[choice], ["跳过", null]]) {
            const pick = element("button", reasonLabel, "table-action");
            pick.onclick = () => send(reason ? `${label}（${reasonLabel}）` : label, choice, reason);
            feedback.append(pick);
          }
        };
        feedback.append(button);
      }
      message.append(feedback);
    }
    if (generation.evidence_ids?.length && generation.evidence) {
      const sources = element("details", undefined, "message-sources");
      sources.append(element("summary", `商品依据（${generation.evidence_ids.length}）`));
      for (const id of generation.evidence_ids) {
        const fact = generation.evidence[id];
        if (fact) sources.append(element("p", `[${id}] ${fact.title} · ${fact.sku}@${fact.version}\n生成时售价 ${amount(fact.price_cents)} 元，库存 ${fact.stock} 件\n${fact.features}`));
      }
      message.append(sources);
    }
  }
  $("#conversation").append(message);
  $("#conversation").scrollTop = $("#conversation").scrollHeight;
}

function textCell(row, value, className = "") {
  const cell = document.createElement("td");
  if (className) cell.className = className;
  cell.textContent = value;
  row.append(cell);
  return cell;
}

function renderState(data) {
  const { live, products, coupons } = data;
  catalog = products;
  const current = products.find((product) => product.sku === live.current_sku);
  $("#goal").textContent = live.goal;
  $("#current-product").textContent = current?.title || "暂无";
  $("#current-sku").textContent = live.current_sku || "--";
  $("#current-price").textContent = current ? amount(current.price_cents) : "--";
  $("#current-stock").textContent = current?.stock ?? "--";
  $("#coupon-count").textContent = coupons.length;
  $("#product-count").textContent = `${products.length} 件商品`;
  if (!$("#campaign-products").children.length) {
    for (const [index, product] of products.entries()) {
      const label = element("label");
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.value = product.sku;
      checkbox.checked = index < 3 && product.stock > 0;
      label.append(checkbox, element("span", product.title));
      $("#campaign-products").append(label);
    }
  }

  const body = $("#products");
  body.replaceChildren();
  const select = $("#sim-sku");
  const oldSelection = select.value;
  select.replaceChildren();
  for (const product of products) {
    const row = document.createElement("tr");
    const name = document.createElement("td");
    const strong = document.createElement("span");
    strong.className = "product-name";
    strong.textContent = product.title;
    const sku = document.createElement("span");
    sku.className = "sku";
    sku.textContent = product.sku;
    name.append(strong, sku);
    row.append(name);
    textCell(row, `¥ ${amount(product.price_cents)}`);
    textCell(row, `${product.stock} 件`, product.stock === 0 ? "stock-out" : product.stock < 50 ? "stock-low" : "");
    const status = document.createElement("td");
    const tag = document.createElement("span");
    tag.className = `tag ${product.stock === 0 ? "out" : live.current_sku === product.sku ? "current" : ""}`;
    tag.textContent = product.stock === 0 ? "已售罄" : live.current_sku === product.sku ? "当前讲解" : "可销售";
    status.append(tag);
    row.append(status);
    const action = document.createElement("td");
    const button = document.createElement("button");
    button.type = "button";
    button.className = "table-action";
    button.textContent = "切换讲解";
    button.disabled = product.stock === 0 || product.sku === live.current_sku;
    button.addEventListener("click", () => sendCommand(`切到 ${product.sku}`));
    action.append(button);
    row.append(action);
    body.append(row);
    const option = document.createElement("option");
    option.value = product.sku;
    option.textContent = `${product.title} (${product.sku})`;
    select.append(option);
  }
  select.value = products.some((p) => p.sku === oldSelection) ? oldSelection : "SK-102";
}

function describeAction(action) {
  const name = { update_price: "修改售价", issue_coupon: "创建优惠券", switch_product: "切换讲解商品" }[action.type] || action.type;
  const amountText = action.amount_cents ? ` / ¥ ${amount(action.amount_cents)}` : "";
  const quantityText = action.quantity ? ` / ${action.quantity} 张` : "";
  return `${name} · ${action.sku || ""}${amountText}${quantityText}`;
}

function renderApprovals(commands) {
  const pending = commands.filter((command) => command.status === "pending_approval");
  $("#approval-count").textContent = pending.length;
  const container = $("#approvals");
  container.replaceChildren();
  if (!pending.length) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = "目前没有待审批操作";
    container.append(empty);
    return;
  }
  for (const command of pending) {
    const item = document.createElement("div");
    item.className = "approval-item";
    const title = document.createElement("strong");
    title.textContent = describeAction(command.proposal || {});
    const detail = document.createElement("p");
    detail.textContent = command.instruction;
    const controls = document.createElement("div");
    controls.className = "approval-actions";
    for (const [label, approved, className] of [["确认执行", true, "approve"], ["拒绝", false, "reject"]]) {
      const button = document.createElement("button");
      button.textContent = label;
      button.className = className;
      button.addEventListener("click", async () => {
        controls.querySelectorAll("button").forEach((control) => control.disabled = true);
        try {
          const result = await request(`/api/commands/${command.command_id}/approval`, { method: "POST", body: JSON.stringify({ approved }) });
          addMessage(result.message, result.status === "ok" ? "assistant" : "error");
          await refresh();
        } catch (error) {
          addMessage(error.message, "error");
          await refresh();
        }
      });
      controls.append(button);
    }
    item.append(title, detail, controls);
    container.append(item);
  }
}

function renderAudit(events) {
  const container = $("#audit");
  container.replaceChildren();
  if (!events.length) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = "暂无记录";
    container.append(empty);
    return;
  }
  const labels = { command_received: "收到指令", approval_requested: "等待审批", approval_resolved: "审批完成", approval_expired: "审批已过期", model_fallback: "模型降级", action_committed: "操作已提交", action_blocked: "操作被拦截", stock_changed: "仿真库存变化", preference_forgotten: "偏好已删除", remote_query: "查询远端回执", remote_queued: "已登记远端请求" };
  for (const event of events.slice(0, 12)) {
    const row = document.createElement("div");
    row.className = "event";
    const time = document.createElement("time");
    time.textContent = new Date(event.created_at * 1000).toLocaleTimeString("zh-CN", { hour12: false });
    const body = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = labels[event.event_type] || event.event_type;
    const detail = document.createElement("p");
    detail.textContent = event.payload?.result?.message || event.payload?.instruction ||
      (event.event_type === "stock_changed" ? `${event.payload.sku} 库存 ${event.payload.stock}` :
        event.payload?.type ? describeAction(event.payload) : event.payload?.code || "");
    body.append(title, detail);
    row.append(time, body);
    container.append(row);
  }
}

async function refresh() {
  const [state, commands, audit, health] = await Promise.all([
    request(`/api/state/${LIVE_ID}`),
    request(`/api/commands/${LIVE_ID}`),
    request(`/api/audit/${LIVE_ID}`),
    request("/health"),
  ]);
  renderState(state);
  renderApprovals(commands);
  renderAudit(audit);
  $("#mode").textContent = health.mode === "model" ? `${health.intent_model || "模型模式"} · ${health.advice_generation ? "对话已启用" : "仅解析"}` : "离线演示";
  if (!historyLoaded) {
    for (const command of commands.slice(0, 8).reverse()) {
      if (!command.response || shownCommands.has(command.command_id)) continue;
      shownCommands.add(command.command_id);
      addMessage(command.instruction, "user");
      addMessage(command.response.message, "assistant", command.response);
    }
    historyLoaded = true;
  }
  await Promise.all([refreshMemory(), refreshOperations(), refreshCampaignHistory(), refreshRemoteFault(), refreshRuntime()]);
}

async function refreshRuntime() {
  const [runtime, metrics] = await Promise.all([request(`/api/runtime/${LIVE_ID}`), request(`/api/metrics/${LIVE_ID}`)]);
  runtimeState = runtime.state;
  $("#runtime-scene").value = runtimeState.scene;
  $("#active-scene").textContent = { pre_live: "播前", on_live: "播中", post_live: "播后" }[runtimeState.scene];
  const provider = metrics.provider;
  const audience = runtime.context.audience;
  const fields = [["阶段版本", `R${runtimeState.revision} / 控制版本 ${runtimeState.control_revision}`],
    ["人数观测", audience?.stale ? "已过期，当前未知" : audience ? `${audience.viewers} 人（仿真）` : "尚无观测"],
    ["模型预算", `${provider.calls}/${provider.limits.max_calls} 次；预估占用 ${provider.charged_tokens} tokens`],
    ["用量缺口", `${provider.unknown_usage_calls} 次未知；未知用量不释放预算`],
    ["采纳反馈", metrics.feedback_count ? `${metrics.accepted_count}/${metrics.feedback_count} 条明确反馈` : "尚无反馈"],
    ["未采纳原因", ["modify", "reject"].flatMap(c => Object.entries(metrics.reason_counts?.[c] || {}).map(([k, n]) => `${c === "modify" ? "改" : "拒"}:${k}×${n}`)).join("，") || "无"],
    ["统计边界", "本地会话累计；不代表成交提升或线上成功率"]];
  $("#runtime-metrics").replaceChildren();
  for (const [label, value] of fields) $("#runtime-metrics").append(element("dt", label), element("dd", value));
}

async function runtimeEvent(kind, payload) {
  try {
    if (!runtimeState) await refreshRuntime();
    await request(`/api/runtime/${LIVE_ID}/events`, { method: "POST", body: JSON.stringify({ event_id: crypto.randomUUID(), expected_revision: runtimeState.revision, kind, ...payload }) });
    $("#runtime-feedback").textContent = kind === "scene_changed" ? "工作阶段已更新；旧阶段的未执行审批不再有效。" : "已更新仿真观测，60秒后不再当作实时人数。";
    await refresh();
  } catch (error) {
    $("#runtime-feedback").textContent = error.message;
    await refreshRuntime();
  }
}
$("#runtime-scene").onchange = event => runtimeEvent("scene_changed", { scene: event.target.value });
$("#observe-viewers").onclick = () => runtimeEvent("audience_observed", { viewers: Number($("#viewers").value) });

async function refreshRemoteFault() {
  const config = await request("/api/simulator/remote-fault");
  $("#remote-fault").value = config.mode;
  $("#remote-fault").disabled = !config.enabled;
  $("#set-remote-fault").disabled = !config.enabled;
}

async function sendCommand(instruction) {
  const normalized = instruction.trim();
  if (!normalized || sending) return;
  sending = true;
  const button = $("#send");
  button.disabled = true;
  addMessage(normalized, "user");
  $("#request-status").textContent = "正在理解指令、查询事实与组织回答...";
  const stored = sessionStorage.getItem("liveops.pending");
  let previous = null;
  try { previous = stored ? JSON.parse(stored) : null; } catch (_) { /* Ignore malformed browser state. */ }
  const payload = previous?.instruction === normalized ? previous : { live_id: LIVE_ID, instruction: normalized, command_id: crypto.randomUUID() };
  sessionStorage.setItem("liveops.pending", JSON.stringify(payload));
  shownCommands.add(payload.command_id);
  try {
    const result = await request("/api/commands", { method: "POST", body: JSON.stringify(payload) });
    sessionStorage.removeItem("liveops.pending");
    if (result.interpretation?.source === "offline_fallback") {
      addMessage("模型解析暂不可用，已使用离线规则继续处理。", "system");
    }
    addMessage(result.message, result.status === "blocked" ? "error" : "assistant", result);
    if (result.result?.run_id) {
      await loadCampaign(result.result.run_id);
      $("#campaign-heading").scrollIntoView({ behavior: "smooth", block: "start" });
    }
    await refresh();
    $("#request-status").textContent = result.status === "pending_approval" ? "等待主播确认" : "";
  } catch (error) {
    addMessage(error.message, "error");
    $("#request-status").textContent = "请求失败；重新发送同一句将复用请求 ID，避免重复执行";
  } finally {
    button.disabled = false;
    sending = false;
  }
}

$("#command-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const input = $("#instruction");
  sendCommand(input.value);
  input.value = "";
});
document.querySelectorAll("[data-prompt]").forEach((button) => button.addEventListener("click", () => sendCommand(button.dataset.prompt)));
$("#refresh").addEventListener("click", () => refresh().catch((error) => addMessage(error.message, "error")));
$("#change-stock").addEventListener("click", async () => {
  try {
    await request("/api/simulator/stock", { method: "POST", body: JSON.stringify({ live_id: LIVE_ID, sku: $("#sim-sku").value, stock: Number($("#sim-stock").value) }) });
    addMessage("已注入库存变化。待审批的旧操作在确认时会重新校验状态。", "system");
    await refresh();
  } catch (error) {
    addMessage(error.message, "error");
  }
});

async function refreshMemory() {
  const memory = await request(`/api/memory/${LIVE_ID}?scene=${$("#memory-scene").value}`);
  const container = $("#memory-content");
  container.replaceChildren();
  const fields = [["明确偏好", memory.L1_statement.value || "尚未保存"], ["当前商品", memory.L2_facts.current_sku || "无"],
    ["上一款", memory.L2_facts.previous_sku || "无"], ["行为证据", `${memory.L3_behavior.sample_size} 条已提交操作`],
    ["讨论焦点", memory.conversation?.focus_ambiguous ? "多款商品，需澄清" : memory.conversation?.focus_sku || "未指定"],
    ["推荐列表", memory.conversation?.listed_skus?.join(" → ") || "无"],
    ["本场排除", memory.conversation?.excluded_skus?.join("、") || "无"],
    ["列表来源", (memory.conversation?.lists || []).map(l => `${l.deliverable === "ranked_list" ? "推荐" : "备播"}[${{active: "当前", alternative: "并存", superseded: "已失效"}[l.status] || l.status}${l.plan_replanned ? "·已重规划" : ""}] ${l.skus.join("/")}`).join("；") || "无"],
    ["事实来源", "实时业务库；历史回答不替代库存和价格"]];
  for (const [label, value] of fields) container.append(element("dt", label), element("dd", value));
  for (const conflict of memory.conflicts) container.append(element("dt", "待确认"), element("dd", `声明偏好 ${conflict.declared_sku} 与近期切品不同，需要本人确认，不自动覆盖。`));
  $("#forget-preference").disabled = !memory.L1_statement.value;
  $("#context-preview").textContent = JSON.stringify(memory.runtime_context, null, 2);
  originalArtifact = memory.conversation?.last_artifact_id || null;
  $("#context-original").disabled = !originalArtifact;
  $("#context-artifact").textContent = "";
}
$("#context-original").onclick = async () => {
  try { $("#context-artifact").textContent = JSON.stringify(await request(`/api/runtime/${LIVE_ID}/artifacts/${originalArtifact}`), null, 2); }
  catch (error) { $("#memory-feedback").textContent = error.message; }
};

async function refreshOperations() {
  const operations = await request(`/api/operations/${LIVE_ID}`);
  $("#operation-count").textContent = operations.length;
  const container = $("#operations");
  container.replaceChildren();
  if (!operations.length) container.append(element("p", "暂无远端操作", "empty"));
  for (const operation of operations.slice(0, 8)) {
    const item = element("div", undefined, "operation-item");
    item.append(element("strong", `${statusLabels[operation.status] || operation.status} · ${operation.action.sku}`));
    item.append(element("p", `${amount(operation.action.amount_cents)} 元券 × ${operation.action.quantity} 张`));
    if (["unknown", "manual_review", "sent"].includes(operation.status)) {
      const button = element("button", "查询回执，不重复发券", "outline");
      button.onclick = async () => {
        button.disabled = true;
        try {
          const result = await request(`/api/commands/${operation.command_id}/reconcile`, { method: "POST" });
          addMessage(result.message, "assistant");
          await refresh();
        } catch (error) { addMessage(error.message, "error"); button.disabled = false; }
      };
      item.append(button);
    }
    container.append(item);
  }
}

async function refreshCampaignHistory() {
  const runs = await request(`/api/campaigns/${LIVE_ID}`);
  const select = $("#campaign-history");
  select.replaceChildren(element("option", "选择计划"));
  select.firstChild.value = "";
  for (const run of runs) {
    const option = element("option", `${statusLabels[run.status] || run.status} · ${run.instruction.slice(0, 35)}`);
    option.value = run.run_id;
    select.append(option);
  }
  select.value = currentCampaign?.run_id || "";
}

function campaignControls() {
  $("#campaign-start").disabled = campaignBusy;
  $("#campaign-history").disabled = campaignBusy;
  $("#campaign-pause").disabled = !campaignBusy || pauseCampaign;
  $("#campaign-resume").disabled = campaignBusy || !currentCampaign || !["pending", "paused"].includes(currentCampaign.status);
  $("#campaign-retry").disabled = campaignBusy || currentCampaign?.status !== "blocked" || !Object.values(currentCampaign.tasks).some(t => t.status === "failed" && t.error?.retryable && t.attempts < 2);
  $("#campaign-export").disabled = !finalMaterials;
  $("#campaign-replan").disabled = campaignBusy || !currentCampaign || (currentCampaign.replan_count || 0) >= 2;
}

function renderCampaign(run) {
  currentCampaign = run;
  sessionStorage.setItem("liveops.campaign", run.run_id);
  const order = ["merchandiser", "copywriter", "compliance", "director"];
  const tasks = [...run.plan.tasks].sort((a, b) => order.indexOf(a.role) - order.indexOf(b.role)).map(spec => [spec.task_id, run.tasks[spec.task_id]]);
  $("#campaign-status").textContent = statusLabels[run.status] || run.status;
  if (run.freshness?.requires_rebase) $("#campaign-status").textContent += " · 状态变化，需重规划";
  $("#campaign-progress").max = tasks.length;
  $("#campaign-progress").value = tasks.filter(([, task]) => task.status === "succeeded").length;
  const container = $("#campaign-tasks");
  container.replaceChildren();
  const specs = new Map(run.plan.tasks.map(task => [task.task_id, task]));
  for (const [id, task] of tasks) {
    const spec = specs.get(id);
    const item = element("div", undefined, `task-item ${task.status}`);
    item.append(element("strong", `${taskLabels[spec.role]} ${spec.sku || ""}`), element("small", `${statusLabels[task.status] || task.status} · 尝试 ${task.attempts} 次`));
    if (task.error) item.append(element("small", `${task.error.code} · ${task.error.retryable ? "可有限恢复" : "需修改条件后重建"}`));
    container.append(item);
  }
  $("#campaign-trace").textContent = JSON.stringify(run.trace, null, 2);
  $("#replan-old").replaceChildren(...run.selected_skus.map(sku => { const option = element("option", sku); option.value = sku; return option; }));
  $("#replan-new").replaceChildren(element("option", "仅刷新当前商品版本"));
  $("#replan-new").firstChild.value = "";
  for (const product of catalog) { const option = element("option", `${product.title} / ${product.sku}`); option.value = product.sku; $("#replan-new").append(option); }
  const lastRevision = run.plan_revisions?.at(-1);
  $("#replan-history").textContent = lastRevision ? `R${run.revision}：复用 ${lastRevision.preserved_tasks.length} 个有效任务，重做 ${lastRevision.invalidated_tasks.length} 个任务；总步数和模型预算不重置。` : `计划 R${run.revision}，最多允许两次显式重规划。`;
  campaignControls();
}

async function loadCampaign(id) {
  const run = await request(`/api/campaigns/${LIVE_ID}/${encodeURIComponent(id)}`);
  renderCampaign(run);
  finalMaterials = null;
  const container = $("#campaign-materials");
  container.replaceChildren();
  if (run.output_artifact) {
    finalMaterials = await request(`/api/campaigns/${LIVE_ID}/${id}/artifacts/${run.output_artifact}`);
    if (run.freshness?.requires_rebase) container.append(element("p", "商品、偏好或直播状态已变化。以下为历史材料，请重规划后再使用。", "stock-out"));
    container.append(element("p", finalMaterials.execution_boundary, "muted small"));
    for (const brief of finalMaterials.approved_briefs) {
      const item = element("article", undefined, "material-item");
      item.append(element("h3", brief.title), element("p", brief.claims.join("；")),
        element("p", `生成时售价 ${amount(brief.price_cents)} 元 · 库存 ${brief.stock} · ${brief.source}`, "muted"));
      const button = element("button", "基于这款写口播", "table-action");
      button.onclick = () => sendCommand(`给 ${brief.sku} 写一段自然的直播口播，带一个互动提问`);
      item.append(button);
      container.append(item);
    }
    if (finalMaterials.rejected_skus.length) container.append(element("p", `未准入材料：${finalMaterials.rejected_skus.join("、")}，不能直接发布。`, "stock-out"));
  }
  const metrics = await request(`/api/campaigns/${LIVE_ID}/${id}/metrics`);
  const ledger = metrics.provider_ledger;
  $("#campaign-metrics").textContent = `${metrics.completed_tasks}/${Object.keys(run.tasks).length} 任务完成 · 持久调用账本 ${ledger.calls} 次 / 已报告 ${ledger.reported_tokens} tokens / ${ledger.unknown_usage_calls} 次用量未知 · 计划总步数 ${run.steps}/24 · 非实际货币账单`;
  campaignControls();
}

async function advanceCampaign(retry = false) {
  if (!currentCampaign || campaignBusy) return;
  const id = currentCampaign.run_id;
  campaignBusy = true;
  pauseCampaign = false;
  campaignControls();
  let polling = false;
  const timer = setInterval(async () => {
    if (polling) return;
    polling = true;
    try { const run = await request(`/api/campaigns/${LIVE_ID}/${id}`); if (campaignBusy && currentCampaign?.run_id === id) renderCampaign(run); }
    catch (_) { /* The advancing request reports failures in the status line. */ }
    finally { polling = false; }
  }, 900);
  try {
    for (let wave = 0; wave < 12; wave++) {
      $("#campaign-feedback").textContent = `正在推进阶段 ${wave + 1}；可以暂停后续阶段`;
      const run = await request(`/api/campaigns/${LIVE_ID}/${id}/advance`, { method: "POST", body: JSON.stringify({ max_waves: 1, retry_failed: retry && wave === 0 }) });
      renderCampaign(run);
      if (pauseCampaign || ["succeeded", "blocked", "review_required"].includes(run.status) || run.budget_stop) break;
    }
    $("#campaign-feedback").textContent = pauseCampaign ? "已暂停后续阶段，当前请求完成后保留断点。" : "本次推进结束；完整材料和任务状态见下方。";
  } catch (error) { $("#campaign-feedback").textContent = `${error.message}。计划已保存，可重新读取状态。`; }
  finally {
    clearInterval(timer);
    campaignBusy = false;
    await loadCampaign(id).catch(error => { $("#campaign-feedback").textContent = error.message; });
    await refreshCampaignHistory().catch(() => {});
    campaignControls();
  }
}

$("#campaign-form").onsubmit = async event => {
  event.preventDefault();
  if (campaignBusy) return;
  const skus = Array.from(document.querySelectorAll("#campaign-products input:checked"), input => input.value);
  if (!skus.length) { $("#campaign-feedback").textContent = "请至少选一款商品。"; return; }
  $("#campaign-start").disabled = true;
  try {
    const run = await request("/api/campaigns", { method: "POST", body: JSON.stringify({ instruction: $("#campaign-goal").value, skus, run_id: crypto.randomUUID() }) });
    renderCampaign(run);
    $("#campaign-materials").replaceChildren();
    finalMaterials = null;
    await advanceCampaign();
  } catch (error) { $("#campaign-feedback").textContent = error.message; campaignControls(); }
};
$("#campaign-pause").onclick = () => { pauseCampaign = true; campaignControls(); $("#campaign-feedback").textContent = "将等待当前阶段返回后暂停，不中断已经发出的模型请求。"; };
$("#campaign-resume").onclick = () => advanceCampaign();
$("#campaign-retry").onclick = () => advanceCampaign(true);
$("#campaign-replan").onclick = async () => {
  if (!currentCampaign || campaignBusy) return;
  const run = currentCampaign;
  const target = $("#replan-new").value;
  const replacements = target ? { [$("#replan-old").value]: target } : {};
  if (!window.confirm("按最新商品状态重规划？仅重做受影响任务，不会执行切品、改价或发券。")) return;
  campaignBusy = true;
  campaignControls();
  try {
    await request(`/api/campaigns/${LIVE_ID}/${run.run_id}/replan`, { method: "POST", body: JSON.stringify({ replacements, expected_revision: run.revision }) });
    await loadCampaign(run.run_id);
    $("#campaign-feedback").textContent = "重规划已保存。点继续推进新版本，旧版本材料仍可回查。";
  } catch (error) { $("#campaign-feedback").textContent = error.message; await loadCampaign(run.run_id).catch(() => {}); }
  finally { campaignBusy = false; campaignControls(); }
};
$("#campaign-history").onchange = event => { if (event.target.value) loadCampaign(event.target.value).catch(error => { $("#campaign-feedback").textContent = error.message; }); };
$("#campaign-export").onclick = () => {
  if (!finalMaterials) return;
  const url = URL.createObjectURL(new Blob([JSON.stringify(finalMaterials, null, 2)], { type: "application/json" }));
  const link = element("a");
  link.href = url;
  link.download = `liveops-${currentCampaign.run_id}.json`;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
};
$("#memory-scene").onchange = () => refreshMemory().catch(error => { $("#memory-feedback").textContent = error.message; });
$("#forget-preference").onclick = async () => {
  if (!confirm("删除当前明确偏好？历史审计与检查点仍保留。")) return;
  try {
    await request(`/api/memory/${LIVE_ID}/preference`, { method: "DELETE" });
    $("#memory-feedback").textContent = "明确偏好已删除，历史审计仍保留。";
    await refresh();
  } catch (error) { $("#memory-feedback").textContent = error.message; }
};
$("#set-remote-fault").onclick = async () => {
  try { await request("/api/simulator/remote-fault", { method: "POST", body: JSON.stringify({ mode: $("#remote-fault").value }) }); addMessage("已设置仿真发券模式，仅影响沙箱。", "system"); }
  catch (error) { addMessage(error.message, "error"); }
};

refresh().then(async () => {
  const id = sessionStorage.getItem("liveops.campaign");
  if (id) await loadCampaign(id);
}).catch(error => addMessage(`无法加载直播状态：${error.message}`, "error"));
