(() => {
  const { api, el, $ } = window.weave;
  let me = null;
  const STATE = { succeeded: ["ready", "ok"], running: ["building", "busy"], queued: ["queued", "busy"],
    failed: ["failed", "fail"], cancelled: ["cancelled", "fail"] };

  async function load() {
    try {
      me = await api("/api/account/me");
    } catch (e) {
      if (e.status === 401) location.replace("/start");
      return;
    }
    $("#main").hidden = false;
    $("#who").textContent = me.username;
    $("#hello").textContent = `Hi ${me.call_sign}`;
    $("#address").textContent = me.address.replace("https://", "");
    $("#address").href = me.address;
    $("#call-sign").value = me.call_sign;
    const waBase = me.whatsapp ? "https://wa.me/" + me.whatsapp.replace(/\D/g, "") : null;
    $("#connect").hidden = !!me.phone;
    $("#linked").hidden = !me.phone;
    if (me.phone) {
      $("#phone").textContent = me.phone;
      if (waBase) $("#wa-open").href = waBase; else $("#wa-open").hidden = true;
    }
    if (me.invite) { $("#invite-line").hidden = false; $("#invite").textContent = me.invite; }
    renderProjects();
    renderStats();
  }

  function project(p, deleted) {
    const [label, cls] = STATE[p.status] || [p.ready ? "ready" : "—", p.ready ? "ok" : ""];
    const files = el("div", { class: "files", hidden: true });
    const actions = deleted
      ? [el("button", { class: "btn ghost small", type: "button", onclick: () => act(p, "restore") }, "Restore")]
      : [
        p.ready ? el("button", { class: "btn ghost small", type: "button", onclick: () => showFiles(p, files) }, "Files") : null,
        el("button", { class: "btn ghost small", type: "button", onclick: () => rename(p) }, "Rename"),
        el("button", { class: "btn danger small", type: "button", onclick: () => remove(p) }, "Delete"),
      ];
    return el("li", { class: "project" },
      el("div", { class: "top" },
        el("span", { class: "name" }, `${p.number}. ${p.name}`),
        el("span", { class: "tag" }, p.kind === "website" ? "website" : "Android app"),
        deleted ? null : el("span", { class: "tag " + cls }, label)),
      p.url && !deleted ? el("a", { class: "url", href: p.url, target: "_blank", rel: "noopener" }, p.url) : null,
      p.prompt ? el("div", { class: "small muted" }, p.prompt.length > 160 ? p.prompt.slice(0, 160) + "…" : p.prompt) : null,
      el("div", { class: "row actions" }, actions), files);
  }

  function renderProjects() {
    const list = $("#projects");
    list.replaceChildren(...me.projects.map((p) => project(p, false)));
    if (!me.projects.length) {
      list.append(el("li", { class: "muted" }, me.phone ? "Nothing yet. Describe something above or text Weave on WhatsApp."
        : "Connect WhatsApp first, then your projects show up here."));
    }
    $("#trash-box").hidden = !me.trash.length;
    $("#trash-note").textContent = `Deleted projects can be restored for ${me.limits.trash_days} days.`;
    $("#trash").replaceChildren(...me.trash.map((p) => project(p, true)));
  }

  function renderStats() {
    const u = me.usage, l = me.limits;
    const stat = (value, label) => el("div", { class: "stat" }, el("b", {}, String(value)), el("span", {}, label));
    $("#stats").replaceChildren(
      stat(`${u.today.builds} / ${l.builds_per_day}`, "builds today"),
      stat(u.month.builds, "builds this month"),
      stat(`${me.projects.length} / ${l.projects}`, "projects"),
      stat(`${u.storage_mb} / ${l.storage_mb} MB`, "storage"),
    );
  }

  async function showFiles(p, box) {
    if (!box.hidden) { box.hidden = true; return; }
    box.hidden = false;
    box.replaceChildren("Loading…");
    try {
      const r = await api(`/api/account/projects/${p.id}/files`);
      const items = [];
      if (r.preview) items.push(el("div", {}, el("a", { href: r.preview, target: "_blank", rel: "noopener" }, "Open private preview")));
      if (r.apk) items.push(el("div", {}, el("a", { href: r.apk }, "Download the Android app (APK)")));
      for (const f of r.files) {
        items.push(el("div", {}, el("a", { href: `/api/account/projects/${p.id}/files/${encodeURIComponent(f.name)}` }, f.name),
          ` · ${(f.bytes / 1024).toFixed(1)} KB`));
      }
      box.replaceChildren(...(items.length ? items : ["No files yet."]));
    } catch (e) {
      box.replaceChildren(e.message);
    }
  }

  async function act(p, verb) {
    try { await api(`/api/account/projects/${p.id}/${verb}`, {}); await load(); } catch (e) { alert(e.message); }
  }

  async function rename(p) {
    const name = prompt("New name", p.name);
    if (!name || name === p.name) return;
    try { await api(`/api/account/projects/${p.id}/rename`, { name }); await load(); } catch (e) { alert(e.message); }
  }

  function remove(p) {
    const live = p.url ? " Its website goes offline." : "";
    if (confirm(`Delete "${p.name}"?${live} You can restore it for ${me.limits.trash_days} days.`)) act(p, "delete");
  }

  async function getCode() {
    try {
      const r = await api("/api/account/phone-code", {});
      $("#code-box").hidden = false;
      $("#code").textContent = r.code;
      $("#code-msg").textContent = "LINK " + r.code;
      if (r.whatsapp_link) { $("#wa").hidden = false; $("#wa").href = r.whatsapp_link; }
      $("#get-code").textContent = "New code";
      $("#recheck").hidden = false;
      return r;
    } catch (e) { alert(e.message); }
  }

  $("#get-code").addEventListener("click", getCode);
  $("#recheck").addEventListener("click", load);
  $("#change-number").addEventListener("click", async () => {
    $("#connect").hidden = false;
    $("#connect-text").textContent = "Send this from your new WhatsApp number. Your projects move with you.";
    await getCode();
  });
  $("#new-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const idea = $("#idea").value.trim();
    if (!idea) return;
    $("#new-hint").textContent = "Sending…";
    try {
      const r = await api("/api/account/projects", { idea });
      $("#new-hint").textContent = r.ok ? "Got it. Weave is messaging you on WhatsApp." :
        "Weave replied on WhatsApp (you may be at a limit or a build is still running).";
      $("#idea").value = "";
      setTimeout(load, 1500);
    } catch (err) { $("#new-hint").textContent = err.message; }
  });
  $("#call-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      const r = await api("/api/account/call-sign", { call_sign: $("#call-sign").value });
      $("#call-hint").className = "hint good";
      $("#call-hint").textContent = `Saved. Weave will call you ${r.call_sign}.`;
      $("#hello").textContent = `Hi ${r.call_sign}`;
    } catch (err) { $("#call-hint").className = "hint bad"; $("#call-hint").textContent = err.message; }
  });
  $("#logout").addEventListener("click", async () => {
    await api("/api/account/logout", {}).catch(() => {});
    location.replace("/start");
  });
  $("#delete-account").addEventListener("click", async () => {
    const typed = prompt(`This deletes your account and takes all your sites offline. Your username stays retired.\n` +
      `Type your username (${me.username}) to confirm.`);
    if (typed === null) return;
    try { await api("/api/account/delete", { confirm: typed }); location.replace("/start"); } catch (e) { alert(e.message); }
  });

  load();
  setInterval(() => { if (!document.hidden) load(); }, 20000);
})();
