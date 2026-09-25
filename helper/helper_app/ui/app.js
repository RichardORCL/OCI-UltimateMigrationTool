/* OCI Ultimate Migration Tool - web UI (no framework, hash routing). */
(function () {
  "use strict";

  const app = document.getElementById("app");
  const nav = document.getElementById("nav");
  const userBox = document.getElementById("user");
  const state = { me: null, config: null, jobsByVm: {}, region: "", jobsFilter: { q: "", phase: "", page: 0 } };
  let activePoll = null;

  // ---------------------------------------------------------------------- api
  class ApiError extends Error {
    constructor(message, status, detail) { super(message); this.status = status; this.detail = detail; }
  }

  async function api(method, path, body) {
    const headers = { "Accept": "application/json" };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    const resp = await fetch("../api" + path, {
      method, headers, credentials: "same-origin",
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const text = await resp.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch (_) { data = { detail: text }; }
    if (!resp.ok) {
      const detail = data && data.detail ? (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail)) : resp.statusText;
      if (resp.status === 401 && !path.startsWith("/auth/")) { state.me = null; showStart(); }
      throw new ApiError(detail, resp.status, data && data.detail);
    }
    return data;
  }

  // -------------------------------------------------------------------- utils
  const fmtBytes = (n) => {
    if (n === null || n === undefined) return "-";
    const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0; let v = n;
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
    return (i === 0 ? v : v.toFixed(v >= 100 ? 0 : 1)) + " " + u[i];
  };
  const fmtRate = (bps) => (bps === null || bps === undefined) ? "-" : `${fmtBytes(bps)}/s (${(bps * 8 / 1e6).toFixed(bps * 8 >= 1e8 ? 0 : 1)} Mbit/s)`;
  const fmtDuration = (s) => {
    if (s === null || s === undefined) return "-";
    s = Math.round(s);
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
    return h ? `${h}h ${m}m ${sec}s` : m ? `${m}m ${sec}s` : `${sec}s`;
  };
  const el = (tag, attrs, ...children) => {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined) continue;
      if (k === "class") e.className = v; else if (k.startsWith("on")) e.addEventListener(k.slice(2), v); else e.setAttribute(k, v);
    }
    for (const c of children) if (c !== null && c !== undefined) e.append(c.nodeType ? c : document.createTextNode(String(c)));
    return e;
  };
  // "encrypted" badge with an (i) button opening the how-to-decrypt dialog; vm is a VmSummary (encrypted only)
  // or a VmSpec (encrypted, has_vtpm, encrypted_disks), the steps are tailored to what is known
  const encryptedBadge = (vm) => {
    const disksOnly = !vm.encrypted && (vm.encrypted_disks || []).length > 0;
    return el("span", { class: "nowrap" },
      el("span", { class: "badge warn", title: "Encrypted VM: vSphere does not allow exporting it until it is decrypted" },
        disksOnly ? "encrypted disks" : "encrypted"),
      el("button", { class: "info", type: "button", title: "What to do to export this VM", "aria-label": "How to export an encrypted VM",
        onclick: (ev) => { ev.preventDefault(); ev.stopPropagation(); showEncryptedHelp(vm); } }, "i"));
  };
  // Azure Disk Encryption: the export would copy ciphertext; no vSphere-style help dialog, the badge says it all
  const azureEncryptedBadge = () => el("span", { class: "badge warn",
    title: "Azure Disk Encryption (BitLocker / dm-crypt with keys in Key Vault): the exported disks would be unreadable. Disable ADE on the VM in Azure first" }, "ADE encrypted");
  function showEncryptedHelp(vm) {
    const dlg = document.getElementById("encrypted-help");
    const known = "has_vtpm" in vm;  // VmSpec from the export page; the list only knows the flag
    const windows = /windows/i.test(vm.guest_id || "") || /windows/i.test(vm.guest_full_name || "");
    const disksOnly = known && !vm.encrypted && (vm.encrypted_disks || []).length > 0;
    document.getElementById("encrypted-help-name").textContent = vm.name;
    document.getElementById("encrypted-help-bitlocker").hidden = known && !windows;
    document.getElementById("encrypted-help-vtpm").hidden = known && !vm.has_vtpm;
    document.getElementById("encrypted-help-vm").hidden = disksOnly;
    document.getElementById("encrypted-help-disks").hidden = known && !(vm.encrypted_disks || []).length;
    if (!dlg.dataset.wired) {
      dlg.dataset.wired = "1";
      document.getElementById("encrypted-help-close").addEventListener("click", () => dlg.close());
      dlg.addEventListener("click", (ev) => { if (ev.target === dlg) dlg.close(); });  // click on the backdrop
      window.addEventListener("hashchange", () => { if (dlg.open) dlg.close(); });
    }
    dlg.showModal();
  }
  function showAzureAuthHelp() {
    const dlg = document.getElementById("azure-auth-help");
    if (!dlg.dataset.wired) {
      dlg.dataset.wired = "1";
      document.getElementById("azure-auth-help-close").addEventListener("click", () => dlg.close());
      const copyBtn = document.getElementById("azure-auth-help-copy");
      const copyState = document.getElementById("azure-auth-help-copy-state");
      const cliArea = document.getElementById("azure-auth-help-cli");
      copyBtn.addEventListener("click", async () => {
        copyBtn.disabled = true;
        const text = cliArea.value;
        try {
          if (!navigator.clipboard) throw new Error("clipboard API not available");
          await navigator.clipboard.writeText(text);
          copyState.textContent = `Copied ${text.split("\n").length} lines to the clipboard.`;
        } catch (_) {
          cliArea.focus();
          cliArea.select();
          copyState.textContent = "Clipboard not available; the commands are selected — press Ctrl+C.";
        } finally {
          copyBtn.disabled = false;
          setTimeout(() => {
            if (copyState.textContent.startsWith("Copied")) copyState.textContent = "";
          }, 6000);
        }
      });
      dlg.addEventListener("click", (ev) => { if (ev.target === dlg) dlg.close(); });
      window.addEventListener("hashchange", () => { if (dlg.open) dlg.close(); });
    }
    dlg.showModal();
  }
  function wireAzureAuthHelpButton(id) {
    const btn = document.getElementById(id);
    if (btn && !btn.dataset.wired) {
      btn.dataset.wired = "1";
      btn.addEventListener("click", (ev) => { ev.preventDefault(); ev.stopPropagation(); showAzureAuthHelp(); });
    }
  }
  function showGcpAuthHelp() {
    const dlg = document.getElementById("gcp-auth-help");
    if (!dlg.dataset.wired) {
      dlg.dataset.wired = "1";
      document.getElementById("gcp-auth-help-close").addEventListener("click", () => dlg.close());
      const copyBtn = document.getElementById("gcp-auth-help-copy");
      const copyState = document.getElementById("gcp-auth-help-copy-state");
      const cliArea = document.getElementById("gcp-auth-help-cli");
      copyBtn.addEventListener("click", async () => {
        copyBtn.disabled = true;
        const text = cliArea.value;
        try {
          if (!navigator.clipboard) throw new Error("clipboard API not available");
          await navigator.clipboard.writeText(text);
          copyState.textContent = `Copied ${text.split("\n").length} lines to the clipboard.`;
        } catch (_) {
          cliArea.focus();
          cliArea.select();
          copyState.textContent = "Clipboard not available; the commands are selected — press Ctrl+C.";
        } finally {
          copyBtn.disabled = false;
          setTimeout(() => { if (copyState.textContent.startsWith("Copied")) copyState.textContent = ""; }, 6000);
        }
      });
      dlg.addEventListener("click", (ev) => { if (ev.target === dlg) dlg.close(); });
      window.addEventListener("hashchange", () => { if (dlg.open) dlg.close(); });
    }
    dlg.showModal();
  }
  function wireGcpAuthHelpButton(id) {
    const btn = document.getElementById(id);
    if (btn && !btn.dataset.wired) {
      btn.dataset.wired = "1";
      btn.addEventListener("click", (ev) => { ev.preventDefault(); ev.stopPropagation(); showGcpAuthHelp(); });
    }
  }
  function showAwsAuthHelp() {
    const dlg = document.getElementById("aws-auth-help");
    if (!dlg.dataset.wired) {
      dlg.dataset.wired = "1";
      document.getElementById("aws-auth-help-close").addEventListener("click", () => dlg.close());
      const copyBtn = document.getElementById("aws-auth-help-copy");
      const copyState = document.getElementById("aws-auth-help-copy-state");
      const cliArea = document.getElementById("aws-auth-help-cli");
      copyBtn.addEventListener("click", async () => {
        copyBtn.disabled = true;
        const text = cliArea.value;
        try {
          if (!navigator.clipboard) throw new Error("clipboard API not available");
          await navigator.clipboard.writeText(text);
          copyState.textContent = "Copied.";
        } catch (e) {
          copyState.textContent = "Copy failed: " + e.message;
        } finally {
          copyBtn.disabled = false;
          setTimeout(() => { if (copyState.textContent.startsWith("Copied")) copyState.textContent = ""; }, 6000);
        }
      });
      dlg.addEventListener("click", (ev) => { if (ev.target === dlg) dlg.close(); });
      window.addEventListener("hashchange", () => { if (dlg.open) dlg.close(); });
    }
    dlg.showModal();
  }
  function wireAwsAuthHelpButton(id) {
    const btn = document.getElementById(id);
    if (btn && !btn.dataset.wired) {
      btn.dataset.wired = "1";
      btn.addEventListener("click", (ev) => { ev.preventDefault(); ev.stopPropagation(); showAwsAuthHelp(); });
    }
  }
  const kv = (container, pairs) => {
    container.innerHTML = "";
    for (const [k, v] of pairs) { container.append(el("span", { class: "k" }, k), el("span", { class: "v" }, v)); }
  };
  const showError = (msg) => { app.innerHTML = ""; app.append(el("div", { class: "card" }, el("span", { class: "error" }, msg))); };
  const tpl = (id) => document.getElementById(id).content.cloneNode(true);
  const stopPolling = () => { if (activePoll) { activePoll(); activePoll = null; } };
  const isWindows = (vm) => /windows/i.test((vm.guest_id || "") + " " + (vm.guest_full_name || ""));
  // aws.inventory maps AL2/AL2023 AMIs to amazonlinux<ver>_64Guest / "Amazon Linux <ver>"
  const isAmazonLinux = (vm) => /^amazonlinux/i.test(vm.guest_id || "") || /amazon linux/i.test(vm.guest_full_name || "");
  // copy button next to a read-only command box: clipboard when available, otherwise select the text
  function wireCopyCommands(btnId, areaId, stateId) {
    const btn = document.getElementById(btnId);
    if (!btn || btn.dataset.wired) return;
    btn.dataset.wired = "1";
    const area = document.getElementById(areaId), state = document.getElementById(stateId);
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      const text = area.value;
      try {
        if (!navigator.clipboard) throw new Error("clipboard API not available");
        await navigator.clipboard.writeText(text);
        state.textContent = `Copied ${text.split("\n").length} lines to the clipboard.`;
      } catch (_) {
        area.focus(); area.select();
        state.textContent = "Clipboard not available here: the commands are selected, press Ctrl+C.";
      } finally {
        btn.disabled = false;
        setTimeout(() => { if (state.textContent.startsWith("Copied")) state.textContent = ""; }, 6000);
      }
    });
  }
  // client editions (mirrors mapping.map_guest_os): "Microsoft Windows 10/11 (64-bit)", or windows9/11/12_64Guest
  // without a "Server" release in the display name
  const isWindowsClient = (vm) => /windows\s+(10|11)\b/i.test(vm.guest_full_name || "")
    || (!/server/i.test(vm.guest_full_name || "") && /^windows(9|1[12])_64/i.test(vm.guest_id || ""));
  const TERMINAL = ["COMPLETED", "FAILED", "CANCELLED"];
  // remote console: completed migrations and running ISO installations with an OCI instance (mirrors
  // routes_console._console_job)
  const hasConsole = (job) => (job.phase === "COMPLETED" || job.phase === "INSTALLING") && !!job.instance_id;
  const isIso = (job) => job.kind === "iso";
  const isOva = (job) => job.kind === "ova";
  const isOvaExport = (job) => job.kind === "ovaexport";
  const isAzure = (job) => job.kind === "azure";
  const isGcp = (job) => job.kind === "gcp";
  const isAws = (job) => job.kind === "aws";
  const isOlvm = (job) => job.kind === "olvm";
  const sourceTypeLabel = (job) => {
    if (isOvaExport(job)) return "OCI export";
    if (isOva(job)) return "OVA";
    if (isIso(job)) return "ISO";
    if (isAzure(job)) return "Azure";
    if (isGcp(job)) return "Google Cloud";
    if (isAws(job)) return "AWS";
    if (isOlvm(job)) return "OLVM";
    return "VMware";
  };
  const SVG_NS = "http://www.w3.org/2000/svg";
  const jobEndpointIcon = (role) => {
    // Material-style logout (arrow out of box) / login (arrow into box), same family as the top-bar log off icon
    const d = role === "source"
      ? "M17 7l-1.41 1.41L18.17 11H8v2h10.17l-2.58 2.58L17 17l5-5zM4 5h8V3H4c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h8v-2H4V5z"
      : "M11 7L9.6 8.4l2.6 2.6H2v2h10.2l-2.6 2.6L11 17l5-5-5-5zm9 12h-8v2h8c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2h-8v2h8v14z";
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("class", `job-source-icon job-source-icon-${role}`);
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("width", "14");
    svg.setAttribute("height", "14");
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("focusable", "false");
    svg.setAttribute("title", role === "source" ? "Source (outgoing)" : "Target (incoming)");
    const path = document.createElementNS(SVG_NS, "path");
    path.setAttribute("fill", "currentColor");
    path.setAttribute("d", d);
    svg.append(path);
    return svg;
  };
  // what the job was made from, for lists and titles: the VM's name, or the ISO's file name
  const sourceName = (job) => job.vm ? job.vm.name
    : job.iso ? job.iso.object_name.split("/").pop()
      : job.ova ? job.ova.object_name.split("/").pop()
        : job.ova_export ? (job.ova_export.instance_name || job.ova_export.instance_id)
          : "-";
  // a session with an Azure service-principal login (the vCenter login is "!anonymous && !azure")
  const hasAzure = (me) => !!(me && me.azure_tenant_id);
  const hasGcp = (me) => !!(me && me.gcp_client_email);
  const hasAws = (me) => !!(me && me.aws_account_id);
  const hasOlvm = (me) => !!(me && me.olvm_engine);
  const hasVcenter = (me) => !!(me && !me.anonymous && !me.azure_tenant_id && !me.gcp_client_email && !me.aws_account_id && !me.olvm_engine);
  const gcpZone = (id) => { const m = /\/zones\/([^/]+)\/instances\//i.exec(id || ""); return m ? m[1] : ""; };
  // Azure resource IDs are lower-cased by the API; the resource group is the fourth path element
  const azureResourceGroup = (id) => { const m = /\/resourcegroups\/([^/]+)/i.exec(id || ""); return m ? m[1] : ""; };
  const STEP_LABELS = { seed_image: "Seed image import", iso_image: "ISO image import", launch_instance: "Instance launch" };
  // OCI console deep link for an instance OCID; the region query parameter makes the console switch to
  // the helper's region instead of the user's last one
  const consoleUrl = (kind, ocid) => `https://cloud.oracle.com/compute/${kind}/${encodeURIComponent(ocid)}${state.region ? `?region=${encodeURIComponent(state.region)}` : ""}`;
  const ocidLink = (kind, ocid, cls) => ocid
    ? el("a", { href: consoleUrl(kind, ocid), target: "_blank", rel: "noopener", class: cls || null, title: "Open in the OCI console" }, ocid)
    : "-";

  // -------------------------------------------------------------------- auth
  // Top bar: home (always); Jobs, Setup, and Log off when there is a session (including anonymous).
  const inventoryHashFor = (me) => {
    if (!me || me.anonymous) return null;
    if (hasGcp(me)) return "#/gcp/vms";
    if (hasAzure(me)) return "#/azure/vms";
    if (hasAws(me)) return "#/aws/vms";
    if (hasOlvm(me)) return "#/olvm/vms";
    if (hasVcenter(me)) return "#/vms";
    return null;
  };

  const goInventory = (hash) => (ev) => {
    ev.preventDefault();
    if (location.hash !== hash) location.hash = hash;
    else route().catch((e) => showError(e.message));
  };

  function paintUserBar() {
    const me = state.me;
    if (!me) return;
    const anonymous = !!me.anonymous;
    const azure = hasAzure(me);
    const gcp = hasGcp(me);
    const aws = hasAws(me);
    const olvm = hasOlvm(me);
    const subs = azure ? (me.azure_subscriptions || []).map((s) => s.name || s.id) : [];
    const label = anonymous ? "not logged in"
      : gcp ? `GCP: ${me.gcp_export_bucket || me.gcp_project_id}`
        : azure ? `Azure: ${subs.length ? subs.slice(0, 2).join(", ") + (subs.length > 2 ? ` +${subs.length - 2}` : "") : me.azure_tenant_id}`
          : aws ? `AWS: ${me.aws_account_id} (${me.aws_region})`
            : olvm ? `${me.username} @ ${me.olvm_engine}`
            : `${me.username} @ ${me.vcenter_host}${me.vcenter_port && me.vcenter_port !== 443 ? ":" + me.vcenter_port : ""}`;
    const detailTitle = gcp ? `service account ${me.gcp_client_email}\nexport bucket: ${me.gcp_export_bucket}`
      : azure ? `service principal ${me.azure_client_id} in tenant ${me.azure_tenant_id}${subs.length ? `\nsubscriptions: ${subs.join(", ")}` : ""}`
        : aws ? `IAM ${me.aws_access_key_id}\naccount ${me.aws_account_id} / ${me.aws_region}` : "";
    const inventoryHash = inventoryHashFor(me);
    let who = userBox.querySelector("[data-username]");
    if (!who) {
      who = el("span", { "data-username": "" });
      userBox.append(who);
    }
    const next = inventoryHash
      ? el("a", {
        href: inventoryHash, "data-username": "",
        class: "user-inventory",
        title: detailTitle ? `${detailTitle}\nOpen VM inventory` : "Open VM inventory",
        onclick: goInventory(inventoryHash),
      }, label)
      : el("span", { "data-username": "", title: detailTitle || null }, label);
    who.replaceWith(next);
  }

  function setUser(me) {
    state.me = me;
    nav.hidden = !me;
    userBox.hidden = !me;
    document.getElementById("logout-btn").hidden = !me;
    if (!me) return;
    paintUserBar();
  }

  // the session is gone (401 from the API: expired, or the service restarted): back to the start page, where
  // route() obtains a fresh anonymous session; the login form only when it was asked for explicitly
  function showStart() {
    stopPolling();
    setUser(null);
    if (location.hash !== "#/start" && location.hash !== "#/unlock" && location.hash !== "#/first-use") { location.hash = "#/start"; return; }
    return route().catch((e) => showError(e.message));
  }

  async function showUnlock() {
    stopPolling();
    setUser(null);
    app.innerHTML = "";
    app.append(tpl("tpl-unlock"));
    const form = document.getElementById("unlock-form");
    const err = document.getElementById("unlock-error");
    form.elements.password.focus();
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      err.textContent = "";
      const btn = document.getElementById("unlock-btn");
      btn.disabled = true;
      try {
        setUser(await api("POST", "/auth/unlock", { password: form.elements.password.value }));
        if (location.hash === "#/unlock" || !location.hash) location.hash = "#/start";
        await route();
      } catch (e) { err.textContent = e.message; }
      finally { btn.disabled = false; }
    });
  }

  async function showFirstUse() {
    stopPolling();
    setUser(null);
    app.innerHTML = "";
    app.append(tpl("tpl-first-use"));
    const form = document.getElementById("first-use-form");
    const err = document.getElementById("first-use-error");
    form.elements.password.focus();
    const finish = async (password) => {
      err.textContent = "";
      setUser(await api("POST", "/auth/first-use", { password }));
      if (location.hash === "#/first-use" || !location.hash) location.hash = "#/start";
      await route();
    };
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const pw = form.elements.password.value;
      const pw2 = form.elements.password2.value;
      if (pw.length < 8) { err.textContent = "Password must be at least 8 characters."; return; }
      if (pw !== pw2) { err.textContent = "Password and confirmation do not match."; return; }
      const btn = document.getElementById("first-use-save");
      btn.disabled = true;
      try { await finish(pw); } catch (e) { err.textContent = e.message; }
      finally { btn.disabled = false; }
    });
    document.getElementById("first-use-skip").addEventListener("click", async () => {
      const btn = document.getElementById("first-use-skip");
      btn.disabled = true;
      try { await finish(""); } catch (e) { err.textContent = e.message; }
      finally { btn.disabled = false; }
    });
  }

  // the three boxes: VMware goes to the vCenter login (or straight to the VM list when logged in already), Azure
  // to the service-principal login (or the Azure VM list); the ISO flow is a plain link, route() has made sure
  // a session exists
  function startView() {
    app.innerHTML = "";
    app.append(tpl("tpl-start"));
    if (hasVcenter(state.me)) document.getElementById("start-vmware").href = "#/vms";
    if (hasAzure(state.me)) document.getElementById("start-azure").href = "#/azure/vms";
    if (hasGcp(state.me)) document.getElementById("start-gcp").href = "#/gcp/vms";
    if (hasAws(state.me)) document.getElementById("start-aws").href = "#/aws/vms";
    if (hasOlvm(state.me)) document.getElementById("start-olvm").href = "#/olvm/vms";
    wireAzureAuthHelpButton("start-azure-info");
    wireGcpAuthHelpButton("start-gcp-info");
    wireAwsAuthHelpButton("start-aws-info");
  }

  // Azure service principal login: tenant + client ID are remembered in this browser (never the secret)
  async function showAzureLogin() {
    stopPolling();
    setUser(null);
    app.innerHTML = "";
    app.append(tpl("tpl-azure-login"));
    app.querySelector(".login").prepend(el("p", {}, el("a", { href: "#/start", class: "muted" }, "\u2190 back to start")));
    const form = document.getElementById("azure-login-form");
    const err = document.getElementById("azure-login-error");
    const btn = document.getElementById("azure-login-btn");
    let last = {};
    try { last = JSON.parse(localStorage.getItem("vcoci.azureLogin") || "{}") || {}; } catch (_) { /* ignore */ }
    form.elements.tenant_id.value = last.tenant_id || "";
    form.elements.client_id.value = last.client_id || "";
    (form.elements.tenant_id.value && form.elements.client_id.value ? form.elements.client_secret : form.elements.tenant_id).focus();
    wireAzureAuthHelpButton("azure-login-info");
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      err.textContent = ""; btn.disabled = true;
      const tenant_id = form.elements.tenant_id.value.trim();
      const client_id = form.elements.client_id.value.trim();
      try {
        const me = await api("POST", "/auth/azure/login", { tenant_id, client_id, client_secret: form.elements.client_secret.value });
        try { localStorage.setItem("vcoci.azureLogin", JSON.stringify({ tenant_id, client_id })); } catch (_) { /* private mode */ }
        setUser(me);
        if (["#/azure/login", "#/login", "#/start", "#/iso"].includes(location.hash)) location.hash = "#/azure/vms";
        else route();
      } catch (e) { err.textContent = e.message; }
      finally { btn.disabled = false; }
    });
  }

  async function showGcpLogin() {
    stopPolling();
    setUser(null);
    app.innerHTML = "";
    app.append(tpl("tpl-gcp-login"));
    app.querySelector(".login").prepend(el("p", {}, el("a", { href: "#/start", class: "muted" }, "\u2190 back to start")));
    const form = document.getElementById("gcp-login-form");
    const err = document.getElementById("gcp-login-error");
    const btn = document.getElementById("gcp-login-btn");
    let last = {};
    try { last = JSON.parse(localStorage.getItem("vcoci.gcpLogin") || "{}") || {}; } catch (_) { /* ignore */ }
    form.elements.export_bucket.value = last.export_bucket || "";
    wireGcpAuthHelpButton("gcp-login-info");
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      err.textContent = ""; btn.disabled = true;
      const export_bucket = form.elements.export_bucket.value.trim();
      try {
        const me = await api("POST", "/auth/gcp/login", {
          export_bucket,
          service_account_json: form.elements.service_account_json.value,
        });
        try { localStorage.setItem("vcoci.gcpLogin", JSON.stringify({ export_bucket })); } catch (_) { /* ignore */ }
        setUser(me);
        if (["#/gcp/login", "#/login", "#/start", "#/iso"].includes(location.hash)) location.hash = "#/gcp/vms";
        else route();
      } catch (e) { err.textContent = e.message; }
      finally { btn.disabled = false; }
    });
  }

  async function showAwsLogin() {
    stopPolling();
    setUser(null);
    app.innerHTML = "";
    app.append(tpl("tpl-aws-login"));
    app.querySelector(".login").prepend(el("p", {}, el("a", { href: "#/start", class: "muted" }, "\u2190 back to start")));
    const form = document.getElementById("aws-login-form");
    const err = document.getElementById("aws-login-error");
    const btn = document.getElementById("aws-login-btn");
    let last = {};
    try { last = JSON.parse(localStorage.getItem("vcoci.awsLogin") || "{}") || {}; } catch (_) { /* ignore */ }
    form.elements.access_key_id.value = last.access_key_id || "";
    if (last.region) form.elements.region.value = last.region;
    (form.elements.access_key_id.value ? form.elements.secret_access_key : form.elements.access_key_id).focus();
    wireAwsAuthHelpButton("aws-login-info");
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      err.textContent = ""; btn.disabled = true;
      const access_key_id = form.elements.access_key_id.value.trim();
      const region = form.elements.region.value.trim();
      try {
        const me = await api("POST", "/auth/aws/login", {
          access_key_id, secret_access_key: form.elements.secret_access_key.value, region,
        });
        try { localStorage.setItem("vcoci.awsLogin", JSON.stringify({ access_key_id, region })); } catch (_) { /* ignore */ }
        setUser(me);
        if (["#/aws/login", "#/login", "#/start", "#/iso"].includes(location.hash)) location.hash = "#/aws/vms";
        else route();
      } catch (e) { err.textContent = e.message; }
      finally { btn.disabled = false; }
    });
  }

  async function showOlvmLogin() {
    stopPolling();
    setUser(null);
    app.innerHTML = "";
    app.append(tpl("tpl-olvm-login"));
    app.querySelector(".login").prepend(el("p", {}, el("a", { href: "#/start", class: "muted" }, "\u2190 back to start")));
    const form = document.getElementById("olvm-login-form");
    const err = document.getElementById("olvm-login-error");
    const btn = document.getElementById("olvm-login-btn");
    let last = {};
    try { last = JSON.parse(localStorage.getItem("vcoci.olvmLogin") || "{}") || {}; } catch (_) { /* ignore */ }
    const recent = Array.isArray(last.recent) ? last.recent : [];
    const datalist = document.getElementById("olvm-recent");
    for (const h of recent) datalist.append(el("option", { value: h }));
    form.elements.engine.value = recent[0] || last.engine || "";
    form.elements.username.value = last.username || "";
    form.elements.verify_ssl.checked = !!last.verify_ssl;
    (form.elements.engine.value ? form.elements.password : form.elements.engine).focus();
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      err.textContent = ""; btn.disabled = true;
      const engine_url = form.elements.engine.value.trim();
      const username = form.elements.username.value.trim();
      const verify_ssl = form.elements.verify_ssl.checked;
      try {
        const me = await api("POST", "/auth/olvm/login", { engine_url, username, password: form.elements.password.value, verify_ssl });
        const remembered = [engine_url, ...recent.filter((h) => h !== engine_url)].slice(0, 8);
        try { localStorage.setItem("vcoci.olvmLogin", JSON.stringify({ engine: engine_url, username, verify_ssl, recent: remembered })); } catch (_) { /* private mode */ }
        setUser(me);
        if (["#/olvm/login", "#/login", "#/start", "#/iso"].includes(location.hash)) location.hash = "#/olvm/vms";
        else route();
      } catch (e) { err.textContent = e.message; }
      finally { btn.disabled = false; }
    });
  }

  async function showLogin() {
    stopPolling();
    setUser(null);
    app.innerHTML = "";
    app.append(tpl("tpl-login"));
    app.querySelector(".login").prepend(el("p", {}, el("a", { href: "#/start", class: "muted" }, "\u2190 back to start")));
    const form = document.getElementById("login-form");
    const err = document.getElementById("login-error");
    const btn = document.getElementById("login-btn");
    // recently used vCenters live in this browser only; the configured one is the default
    const recent = recentVcenters();
    const datalist = document.getElementById("vcenter-recent");
    for (const h of recent) datalist.append(el("option", { value: h }));
    try {
      state.config = state.config || await api("GET", "/auth/config");
      const configured = state.config.vcenter_host ? state.config.vcenter_host + (state.config.vcenter_port !== 443 ? ":" + state.config.vcenter_port : "") : "";
      if (configured && !recent.includes(configured)) datalist.append(el("option", { value: configured }));
      form.elements.vcenter.value = recent[0] || configured;
    } catch (e) { err.textContent = e.message; }
    // the last user name and TLS choice used with the selected vCenter are remembered in this browser
    const prefillUser = () => {
      const host = form.elements.vcenter.value.trim();
      const u = lastUsername(host);
      if (u && !form.elements.username.value) form.elements.username.value = u;
      const v = lastVerifySsl(host);
      form.elements.verify_ssl.checked = v === null ? Boolean(state.config && state.config.verify_ssl) : v;
    };
    prefillUser();
    form.elements.vcenter.addEventListener("change", () => { form.elements.username.value = ""; prefillUser(); });
    if (form.elements.vcenter.value) (form.elements.username.value ? form.elements.password : form.elements.username).focus();
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      err.textContent = ""; btn.disabled = true;
      const vcenter = form.elements.vcenter.value.trim();
      const username = form.elements.username.value.trim();
      const verifySsl = form.elements.verify_ssl.checked;
      try {
        const me = await api("POST", "/auth/login", { username, password: form.elements.password.value, vcenter_host: vcenter, verify_ssl: verifySsl });
        rememberVcenter(vcenter);
        rememberUsername(vcenter, username);
        rememberVerifySsl(vcenter, verifySsl);
        setUser(me);
        if (location.hash === "#/login" || location.hash === "#/start" || location.hash === "#/iso") location.hash = "#/vms";
        else route();
      } catch (e) { err.textContent = e.message; }
      finally { btn.disabled = false; }
    });
  }

  function recentVcenters() {
    try { return JSON.parse(localStorage.getItem("vcoci.recentVcenters") || "[]"); } catch (_) { return []; }
  }
  function rememberVcenter(host) {
    if (!host) return;
    const list = [host, ...recentVcenters().filter((h) => h !== host)].slice(0, 8);
    try { localStorage.setItem("vcoci.recentVcenters", JSON.stringify(list)); } catch (_) { /* private mode */ }
  }
  // user names only (never passwords), keyed by vCenter host; "" holds the last one used anywhere
  function lastUsernames() {
    try { return JSON.parse(localStorage.getItem("vcoci.lastUsernames") || "{}") || {}; } catch (_) { return {}; }
  }
  function lastUsername(host) {
    const map = lastUsernames();
    return map[host] || map[""] || "";
  }
  function rememberUsername(host, username) {
    if (!username) return;
    const map = lastUsernames();
    map[host] = username; map[""] = username;
    try { localStorage.setItem("vcoci.lastUsernames", JSON.stringify(map)); } catch (_) { /* private mode */ }
  }
  // "verify the server certificate" choice per vCenter host; null when the host was never used here
  function lastVerifySsl(host) {
    try {
      const map = JSON.parse(localStorage.getItem("vcoci.verifySsl") || "{}") || {};
      return typeof map[host] === "boolean" ? map[host] : null;
    } catch (_) { return null; }
  }
  function rememberVerifySsl(host, value) {
    if (!host) return;
    let map = {};
    try { map = JSON.parse(localStorage.getItem("vcoci.verifySsl") || "{}") || {}; } catch (_) { /* ignore */ }
    map[host] = Boolean(value);
    try { localStorage.setItem("vcoci.verifySsl", JSON.stringify(map)); } catch (_) { /* private mode */ }
  }

  document.getElementById("logout-btn").addEventListener("click", async () => {
    try { await api("POST", "/auth/logout"); } catch (_) { /* ignore */ }
    state.me = null;
    setUser(null);
    if (location.hash === "#/start") route().catch((e) => showError(e.message));
    else location.hash = "#/start";
  });

  // ------------------------------------------------------------- job rendering
  async function fetchJobDiagnosticsText(root, jobId) {
    const resp = await fetch(`../api/jobs/${encodeURIComponent(jobId)}/diagnostics`, { credentials: "same-origin", cache: "no-store" });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const text = await resp.text();
    const area = root.querySelector("[data-diag] textarea");
    area.value = text;
    return text;
  }
  function jobDiagnosticsFilename(job) {
    const label = (job.instance_display_name || job.target.display_name || job.id).replace(/[^\w.-]+/g, "_").slice(0, 80);
    return `oci-migration-diagnostics-${label}-${job.id.slice(0, 8)}.txt`;
  }
  function saveTextAsFile(filename, text) {
    const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    link.click();
    URL.revokeObjectURL(url);
  }

  function renderJob(container, job, opts) {
    opts = opts || {};
    let root = container.querySelector(".job");
    if (!root) { root = tpl("tpl-job").firstElementChild; container.innerHTML = ""; container.append(root); }
    const phase = root.querySelector("[data-phase]");
    phase.textContent = job.phase; phase.className = "phase " + job.phase;
    root.querySelector("[data-message]").textContent = job.message || "";
    root.querySelector("[data-error]").textContent = job.error || "";

    // export-phase line: the percentage vCenter shows on its "Export OVF template" task + the last-minute speed
    const tr = job.transfer || {};
    const transfer = root.querySelector("[data-transfer]");
    transfer.innerHTML = "";
    if (job.phase === "EXPORTING" && tr.started_at) {
      transfer.hidden = false;
      transfer.textContent = `${isOvaExport(job) ? "Export disks to Object Storage" : isOva(job) ? "Copy data disks from Object Storage" : isAzure(job) ? "Disk export from Azure" : isOlvm(job) ? "Disk download from OLVM" : "Export OVF template"}: ${tr.percent || 0}% - ${fmtBytes(tr.bytes_received)} received` +
        (tr.throughput_bps ? ` at ${fmtRate(tr.throughput_bps)} (last minute)` : "") +
        ` - running ${fmtDuration((Date.now() - new Date(tr.started_at)) / 1000)}`;
    } else if (!TERMINAL.includes(job.phase) && job.step_percent !== null && job.step_percent !== undefined) {
      // an OCI work request (e.g. the seed image import) reports how far the current step is
      transfer.hidden = false;
      transfer.append(el("div", { class: "meta" }, el("span", {}, `${STEP_LABELS[job.step] || job.step}: ${job.step_percent}%`)),
        el("div", { class: "bar" }, el("div", { style: `width:${job.step_percent}%` })));
    } else transfer.hidden = true;

    const disks = root.querySelector("[data-disks]");
    disks.innerHTML = "";
    for (const d of job.disks) {
      // percent of this disk's stream: exact when the lease reported the stream size, else bounded by capacity
      const pct = d.status === "COPIED" ? 100 : d.percent || Math.min(99, Math.round(100 * (d.bytes_received || 0) / Math.max(1, d.stream_bytes || d.capacity_bytes)));
      const barClass = "bar" + (d.status === "COPIED" ? " done" : d.status === "FAILED" ? " failed" : "");
      const of = d.stream_bytes ? ` of ${fmtBytes(d.stream_bytes)}` : "";
      const detail = d.status === "COPIED" ? `copied, ${fmtBytes(d.bytes_received)} received, ${fmtBytes(d.bytes_written)} written` :
        d.status === "COPYING" ? `${pct}% - ${fmtBytes(d.bytes_received)}${of} received` +
          (d.throughput_bps ? ` at ${fmtRate(d.throughput_bps)}` : "") + (d.attempts > 1 ? ` (attempt ${d.attempts})` : "") :
        d.status === "FAILED" ? (d.error || "failed") : d.status.toLowerCase();
      disks.append(el("div", { class: "disk" },
        el("div", { class: "meta" },
          el("span", {}, `${d.label || "disk " + d.index} (${fmtBytes(d.capacity_bytes)})${d.device ? " -> " + d.device : ""}`),
          el("span", {}, detail)),
        el("div", { class: barClass }, el("div", { style: `width:${pct}%` }))));
    }

    const terminal = TERMINAL.includes(job.phase);
    const iso = isIso(job);
    const ova = isOva(job);
    const ovaExport = isOvaExport(job);
    const azure = isAzure(job);
    const gcpJob = isGcp(job);
    const awsJob = isAws(job);
    const olvmJob = isOlvm(job);
    const az = job.azure || {};
    const gc = job.gcp || {};
    const aw = job.aws || {};
    const sm = job.summary || {};
    const name = sourceName(job);
    // left panel: the instance that is (being) created in OCI
    const xe = job.ova_export || {};
    const sourceRows = ovaExport ? [
      ["Source instance", el("span", { title: xe.instance_id || job.instance_id || "" }, `${xe.instance_name || job.instance_display_name || job.instance_id || "-"}`)],
      ["Destination", `${xe.bucket || "-"}/${xe.prefix || ""}`],
      ["OS / shape", `${xe.operating_system || "-"} ${xe.operating_system_version || ""} · ${xe.shape || job.target.shape || "-"}`],
      ["Disks", `${job.disks.length} (${job.disks.filter((d) => d.is_boot).length} boot)`],
      ...(xe.objects && xe.objects.length ? [["Objects", xe.objects.join(", ")]] : []),
    ] : ova ? [
      ["Source OVA", `${job.ova.bucket}/${job.ova.object_name}${job.ova.size_bytes ? ` (${fmtBytes(job.ova.size_bytes)})` : ""}`],
      ["Operating system", `${job.ova.operating_system} ${job.ova.operating_system_version}`],
      ["Disks", `${job.disks.length} (${job.disks.filter((d) => d.is_boot).length} boot via image)`],
    ] : iso ? [
      ["Source ISO", `${job.iso.bucket}/${job.iso.object_name}${job.iso.size_bytes ? ` (${fmtBytes(job.iso.size_bytes)})` : ""}`],
      ["Operating system", `${job.iso.operating_system} ${job.iso.operating_system_version}`],
      ["Boot volume", `${job.iso.boot_disk_gb} GB (blank; the OS is installed onto it), ${job.target.volume_vpus_per_gb} VPU/GB`],
    ] : azure ? [
      ["Source VM", el("span", { title: job.vm.moid }, `${job.vm.name} in Azure${az.location ? ` (${az.location})` : ""} - ${az.vm_size || "size unknown"}, ${job.vm.num_cpu} vCPU, ${fmtBytes(job.vm.memory_mb * 1024 * 1024)} RAM, ${job.vm.disks.length} disk(s)`)],
      ["Subscription / resource group", `${az.subscription_name || az.subscription_id || "-"} / ${az.resource_group || "-"}`],
      ["Guest OS", `${job.vm.guest_full_name || job.vm.guest_id}${job.target.operating_system_version ? ` - release ${job.target.operating_system_version} (selected)` : ""}`],
    ] : gcpJob ? [
      ["Source VM", el("span", { title: job.vm.moid }, `${job.vm.name} in GCP (${gc.zone || "-"}) - ${gc.machine_type || "size unknown"}, ${job.vm.num_cpu} vCPU, ${fmtBytes(job.vm.memory_mb * 1024 * 1024)} RAM, ${job.vm.disks.length} disk(s)`)],
      ["Project / export bucket", `${gc.project_id || "-"} / ${gc.export_bucket || "-"}`],
      ["Guest OS", `${job.vm.guest_full_name || job.vm.guest_id}${job.target.operating_system_version ? ` - release ${job.target.operating_system_version} (selected)` : ""}`],
    ] : olvmJob ? [
      ["Source VM", `${job.vm.name} on ${((job.olvm || {}).engine_host) || "OLVM"}${job.olvm && job.olvm.cluster ? ` (${job.olvm.cluster})` : ""} - ${job.vm.num_cpu} vCPU, ${fmtBytes(job.vm.memory_mb * 1024 * 1024)} RAM, ${job.vm.disks.length} disk(s)`],
      ["Guest OS", `${job.vm.guest_full_name || job.vm.guest_id}${job.target.operating_system_version ? ` - release ${job.target.operating_system_version} (selected)` : ""}`],
    ] : awsJob ? [
      ["Source VM", el("span", { title: job.vm.moid }, `${job.vm.name} in EC2 (${aw.region || "-"}) - ${aw.instance_type || "size unknown"}, ${job.vm.num_cpu} vCPU, ${fmtBytes(job.vm.memory_mb * 1024 * 1024)} RAM, ${job.vm.disks.length} disk(s)`)],
      ["Account / region", `${aw.account_id || "-"} / ${aw.region || "-"}`],
      ["Guest OS", `${job.vm.guest_full_name || job.vm.guest_id}${job.target.operating_system_version ? ` - release ${job.target.operating_system_version} (selected)` : ""}`],
    ] : [
      ["Source VM", `${job.vm.name} (${job.vm.moid})${job.vcenter_host ? " on " + job.vcenter_host : ""} - ${job.vm.num_cpu} vCPU, ${fmtBytes(job.vm.memory_mb * 1024 * 1024)} RAM, ${job.vm.disks.length} disk(s)`],
      ["Guest OS", `${job.vm.guest_full_name || job.vm.guest_id}${job.target.operating_system_version ? ` - release ${job.target.operating_system_version} (selected)` : ""}`],
    ];
    kv(root.querySelector("[data-target]"), [
      ["Name", job.instance_id ? el("strong", {}, job.instance_display_name || job.target.display_name || name)
        : `${job.target.display_name || name} (not launched yet)`],
      ["Instance", ocidLink("instances", job.instance_id)],
      ["State in OCI", ociStateEl(root, job)],
      ...sourceRows,
      ["Shape", `${job.target.shape || "(migration tool default)"}${job.target.ocpus || job.target.memory_gb ? ` - ${job.target.ocpus ?? "auto"} OCPU / ${job.target.memory_gb ?? "auto"} GB${iso ? "" : " (custom)"}` : /^BM\./i.test(job.target.shape || "") ? " - bare metal, fixed cores and memory" : " - sized from the source VM"}`],
      ["IP addresses", ociIpsEl(root, job)],
      ["Launch options", job.launch_options ? `${job.launch_options.firmware}${job.launch_options.secure_boot ? " + Secure Boot (shielded instance, with Measured Boot + vTPM on VM shapes)" : ""}, boot ${job.launch_options.boot_volume_type}, nic ${job.launch_options.network_type}` : "-"],
      ...(job.target.windows_license_type ? [["Windows license", job.target.windows_license_type === "OCI_PROVIDED"
        ? "OCI provided (change it in the OCI console if needed)" : "Bring your own license (change it in the OCI console if needed)"]] : []),
      ova ? ["OVA image", job.ova_image_id ? ocidLink("images", job.ova_image_id) : "-"]
        : iso ? ["ISO image", job.iso_image_id ? ocidLink("images", job.iso_image_id) : "-"] : ["Seed image", job.seed_image_id || "-"],
    ]);
    // right panel: the job itself (ISO: the installation the user runs through the console)
    root.querySelector("[data-job-title]").textContent = iso ? "Installation" : ovaExport ? "OVA/OVF export" : ova ? "OVA import" : "Migration";
    root.querySelector("[data-install-note]").hidden = !(iso && job.phase === "INSTALLING");
    const rows = ovaExport ? [
      ["Step", job.step || "-"],
      ["Result", job.phase === "COMPLETED" ? "Boot volume reattached; data volumes stayed on the instance; instance left STOPPED" : "The instance is stopped for the export and stays stopped. Data volumes remain attached to it."],
      ...(xe.ovf_object ? [["OVF", xe.ovf_object]] : []),
      ...(xe.manifest_object ? [["Manifest", xe.manifest_object]] : []),
      ["Started by", `${job.created_by || "-"} at ${new Date(job.created_at).toLocaleString()}`],
    ] : ova ? [
      ["Step", job.step || "-"],
      ["Disk import", job.phase === "EXPORTING" || job.disks.some((d) => d.status === "COPYING")
        ? "Copying data disks from Object Storage" : job.disks.length > 1 ? "Data disks in OVA" : "Single-disk OVA"],
      ...(job.guest_fixup ? [["Initramfs fix-up", fixupEl(job.guest_fixup,
        "The instance may stop in the dracut emergency shell; enable initramfs rebuild on the OVA import page or run dracut inside the guest.")]] : []),
      ...(job.network_fixup ? [["Network fix-up", fixupEl(job.network_fixup,
        "The instance may come up without network; enable network fix-up on the OVA import page or configure DHCP on the new interface manually.")]] : []),
      ["Started by", `${job.created_by || "-"} at ${new Date(job.created_at).toLocaleString()}`],
    ] : iso ? [
      ["Step", job.step || "-"],
      ["Started by", `${job.created_by || "-"} at ${new Date(job.created_at).toLocaleString()}`],
    ] : azure ? [
      ["Step", job.step || "-"],
      ["Capture", az.capture_mode === "snapshot"
        ? "snapshots of the disks while the VM keeps running (crash-consistent)" + (job.power_off_result === "snapshotted" ? " - taken" : "")
        : "deallocate the VM and export its disks" + ({ already_off: " - the VM was already deallocated when the export started",
          deallocated: " - deallocated right before the export (it stays deallocated in Azure)" }[job.power_off_result]
          || (job.power_off_source ? " - the VM was running when the job was created; it is deallocated right before the export" : ""))],
      ...(az.snapshot_ids && az.snapshot_ids.length ? [["Snapshots", el("span", {}, ...az.snapshot_ids.map((id) => el("div", { class: "ocid", title: id }, id.split("/").pop())),
        el("span", { class: "muted" }, terminal ? "deleted when the job ended (check Azure if the cleanup was reported as failed)" : "deleted when the job ends"))]] : []),
      ...(az.sas_expires_at && !terminal ? [["Export access", `read SAS granted on ${(az.sas_granted || []).length} disk(s)/snapshot(s), valid until ${new Date(az.sas_expires_at).toLocaleString()} (renewed automatically)`]] : []),
      ...(job.guest_fixup ? [["Initramfs fix-up", fixupEl(job.guest_fixup,
        "The instance may stop in the dracut emergency shell; rebuild the initramfs with virtio drivers inside the guest (dracut -f --add-drivers \"virtio_blk virtio_scsi virtio_pci virtio_net\") and migrate again, or check Copy diagnostics for the details.")]] : []),
      ...(job.network_fixup ? [["Network fix-up", fixupEl(job.network_fixup,
        "The instance may come up without network. Open the Remote console, log in and configure DHCP on the new interface (NetworkManager: nmcli con add type ethernet con-name oci ifname \"*\" ipv4.method auto; network-scripts: create /etc/sysconfig/network-scripts/ifcfg-<nic> with BOOTPROTO=dhcp ONBOOT=yes), or check Copy diagnostics for the details.")]] : []),
      ...(job.azure_fixup ? [["Azure cleanup", fixupEl(job.azure_fixup,
        "First boot may hang ~90s on Azure metadata or show /dev/sr0 errors. Enable Azure cleanup under Advanced: firmware and device model on the export page, or apply the same steps manually (cloud-init, waagent, fstab sr0, serial-getty@ttyS0).")]] : []),
      ["Disk download", `Azure page blobs (allocated ranges only)${job.target.volume_vpus_per_gb ? `, ${job.target.volume_vpus_per_gb} VPU/GB volumes` : ""}`],
      ["Started by", `${job.created_by || "-"} at ${new Date(job.created_at).toLocaleString()}`],
    ] : gcpJob ? [
      ["Step", job.step || "-"],
      ["Capture", gc.capture_mode === "snapshot"
        ? "snapshots exported to GCS while the VM keeps running (crash-consistent)"
        : "stop the VM and export its disks via GCS" + ({ already_off: " - already stopped",
          stopped: " - stopped before export" }[job.power_off_result] || "")],
      ...(gc.gcs_objects && gc.gcs_objects.length ? [["GCS objects", gc.gcs_objects.join(", ")]] : []),
      ...(job.gcp_fixup ? [["GCP cleanup", fixupEl(job.gcp_fixup, "Enable GCP cleanup on the export page if first boot waits on GCE metadata.")]] : []),
      ...(job.guest_fixup ? [["Initramfs fix-up", fixupEl(job.guest_fixup, "See diagnostics.")]] : []),
      ...(job.network_fixup ? [["Network fix-up", fixupEl(job.network_fixup, "See diagnostics.")]] : []),
      ["Disk download", `GCS raw export${job.target.volume_vpus_per_gb ? `, ${job.target.volume_vpus_per_gb} VPU/GB volumes` : ""}`],
      ["Started by", `${job.created_by || "-"} at ${new Date(job.created_at).toLocaleString()}`],
    ] : awsJob ? [
      ["Step", job.step || "-"],
      ["Capture", aw.capture_mode === "snapshot"
        ? "snapshots of the EBS volumes while the instance keeps running (crash-consistent)" + (job.power_off_result === "snapshotted" ? " - taken" : "")
        : "stop the instance and snapshot its volumes" + ({ already_off: " - already stopped",
          stopped: " - stopped right before the export (it stays stopped in AWS)" }[job.power_off_result]
          || (job.power_off_source ? " - the instance was running when the job was created; it is stopped right before the export" : ""))],
      ...(aw.snapshot_ids && aw.snapshot_ids.length ? [["Snapshots", aw.snapshot_ids.join(", ")]] : []),
      ...(job.guest_fixup ? [["Initramfs fix-up", fixupEl(job.guest_fixup, "See diagnostics.")]] : []),
      ...(job.network_fixup ? [["Network fix-up", fixupEl(job.network_fixup, "See diagnostics.")]] : []),
      ...(job.aws_fixup ? [["AWS cleanup", fixupEl(job.aws_fixup, "Enable AWS cleanup on the export page if first boot waits on EC2 metadata.")]] : []),
      ["Disk download", `EBS snapshot blocks${job.target.volume_vpus_per_gb ? `, ${job.target.volume_vpus_per_gb} VPU/GB volumes` : ""}`],
      ["Started by", `${job.created_by || "-"} at ${new Date(job.created_at).toLocaleString()}`],
    ] : olvmJob ? [
      ["Step", job.step || "-"],
      ...(job.power_off_source || job.power_off_result ? [["Source power-off", { already_off: "was already powered off when the export started",
        guest_shutdown: "shut down in OLVM before the export (it stays powered off)",
        powered_off: "stopped hard in OLVM before the export (the guest did not shut down in time)" }[job.power_off_result]
        || "the VM was powered on when the job was created; it is shut down right before the export"]] : []),
      ...(job.guest_fixup ? [["Initramfs fix-up", fixupEl(job.guest_fixup, "See diagnostics.")]] : []),
      ...(job.network_fixup ? [["Network fix-up", fixupEl(job.network_fixup, "See diagnostics.")]] : []),
      ["Disk download", `OLVM image transfer (allocated extents only)${job.target.volume_vpus_per_gb ? `, ${job.target.volume_vpus_per_gb} VPU/GB volumes` : ""}`],
      ["Started by", `${job.created_by || "-"} at ${new Date(job.created_at).toLocaleString()}`],
    ] : [
      ["Step", job.step || "-"],
      ...(job.power_off_source ? [["Source power-off", { already_off: "was already powered off when the export started",
        guest_shutdown: "shut down cleanly through VMware Tools before the export",
        powered_off: "powered off hard before the export (VMware Tools not running or guest did not stop in time)" }[job.power_off_result]
        || "the VM was powered on when the job was created; it is shut down right before the export"]] : []),
      ...(job.guest_fixup ? [["Initramfs fix-up", fixupEl(job.guest_fixup,
        "The instance may stop in the dracut emergency shell; rebuild the initramfs with virtio drivers inside the guest (dracut -f --add-drivers \"virtio_blk virtio_scsi virtio_pci virtio_net\") and migrate again, or check Copy diagnostics for the details.")]] : []),
      ...(job.network_fixup ? [["Network fix-up", fixupEl(job.network_fixup,
        "The instance may come up without network. Open the Remote console, log in and configure DHCP on the new interface (NetworkManager: nmcli con add type ethernet con-name oci ifname \"*\" ipv4.method auto; network-scripts: create /etc/sysconfig/network-scripts/ifcfg-<nic> with BOOTPROTO=dhcp ONBOOT=yes), or check Copy diagnostics for the details.")]] : []),
      ["Disk download", job.nfc_host ? `${job.nfc_host}${job.target.nfc_direct_to_esxi ? " (ESXi host, direct)" : ""}${job.target.pipelined_decode ? ", pipelined decode/write" : ""}` : "-"],
      ["Started by", `${job.created_by || "-"} at ${new Date(job.created_at).toLocaleString()}`],
    ];
    if (terminal) {
      rows.push(["Finished", job.finished_at ? new Date(job.finished_at).toLocaleString() : "-"]);
      rows.push(["Duration", fmtDuration(sm.duration_s) + (sm.transfer_duration_s ? ` (export ${fmtDuration(sm.transfer_duration_s)})` : "")]);
      if (tr.started_at && !iso) {
        rows.push(["Data transferred", ovaExport
          ? `${fmtBytes(sm.bytes_received)} read from volumes, ${fmtBytes(sm.bytes_written)} written to Object Storage`
          : `${fmtBytes(sm.bytes_received)} received from ${gcpJob ? "Google Cloud" : awsJob ? "Amazon EC2" : azure ? "Azure" : "vCenter"}, ${fmtBytes(sm.bytes_written)} written to OCI volumes`]);
        rows.push(["Average bandwidth", sm.average_bps ? fmtRate(sm.average_bps) : "-"]);
      }
    }
    rows.push(["Job id", job.id]);
    kv(root.querySelector("[data-migration]"), rows);

    const cancelBtn = root.querySelector("[data-cancel]");
    cancelBtn.hidden = job.phase === "COMPLETED";
    cancelBtn.textContent = job.phase === "CANCELLED" ? "Retry cleanup" : job.phase === "FAILED" ? "Clean up OCI resources" : job.phase === "INSTALLING" ? "Cancel and terminate instance" : "Cancel";
    cancelBtn.onclick = async () => {
      const what = ovaExport ? "Cancel this export? The boot volume is reattached; data volumes stay on the source instance, which is left stopped. Objects already written to the bucket are kept."
        : iso ? "Cancel this installation? The OCI instance and its boot volume will be terminated (the imported ISO image is kept)."
        : azure ? "Cancel this migration? The OCI instance and volumes created so far will be deleted; the disk export access is revoked and snapshots created by the job are deleted in Azure. A deallocated VM is not started again."
          : gcpJob ? "Cancel this migration? OCI resources are deleted and GCS export objects / GCP snapshots from this job are removed. The VM is not started again."
          : awsJob ? "Cancel this migration? OCI resources are deleted and EBS snapshots created by this job are removed. The instance is not started again."
          : "Cancel this migration? The OCI instance and volumes created so far will be deleted.";
      if (!confirm(what)) return;
      cancelBtn.disabled = true;
      try { await api("POST", `/jobs/${job.id}/cancel`); } catch (e) { alert(e.message); }
      finally { cancelBtn.disabled = false; }
    };
    // ISO installation: the user says when the OS is installed; the job completes, the instance stays
    const finishBtn = root.querySelector("[data-finish]");
    finishBtn.hidden = !(iso && job.phase === "INSTALLING");
    finishBtn.onclick = async () => {
      if (!confirm("Mark the installation as finished? The job completes; the instance keeps running from its boot volume.")) return;
      finishBtn.disabled = true;
      try { renderJob(container, await api("POST", `/jobs/${job.id}/finish`), opts); } catch (e) { alert(e.message); }
      finally { finishBtn.disabled = false; }
    };
    // a job that failed after all disks were copied (attach / start rejected by OCI) can resume finalizing
    const resumeBtn = root.querySelector("[data-resume]");
    resumeBtn.hidden = ovaExport || !(job.phase === "FAILED" && job.instance_id && job.disks.length && job.disks.every((d) => d.status === "COPIED"));
    resumeBtn.onclick = async () => {
      resumeBtn.disabled = true;
      try { await api("POST", `/jobs/${job.id}/finalize`); route(); }  // polling stopped at FAILED; restart the view
      catch (e) { alert(e.message); }
      finally { resumeBtn.disabled = false; }
    };
    const copyBtn = root.querySelector("[data-copy]");
    const saveDiagBtn = root.querySelector("[data-save-diagnostics]");
    const copyState = root.querySelector("[data-copy-state]");
    const clearDiagState = () => {
      setTimeout(() => {
        if (/^(Copied|Saved)/.test(copyState.textContent)) copyState.textContent = "";
      }, 6000);
    };
    if (!copyBtn.onclick) copyBtn.onclick = async () => {
      copyBtn.disabled = true; copyState.textContent = "Collecting...";
      try {
        const text = await fetchJobDiagnosticsText(root, job.id);
        const diag = root.querySelector("[data-diag]"); const area = diag.querySelector("textarea");
        try {
          if (!navigator.clipboard) throw new Error("clipboard API not available");
          await navigator.clipboard.writeText(text);
          copyState.textContent = `Copied ${text.split("\n").length} lines to the clipboard.`;
        } catch (_) {
          // insecure context or permission denied: show the text for manual copying
          diag.hidden = false; diag.open = true; area.focus(); area.select();
          copyState.textContent = "Clipboard not available; the text is selected below, press Ctrl+C.";
        }
      } catch (e) { copyState.textContent = "Cannot collect diagnostics: " + e.message; }
      finally { copyBtn.disabled = false; clearDiagState(); }
    };
    if (saveDiagBtn && !saveDiagBtn.onclick) saveDiagBtn.onclick = async () => {
      saveDiagBtn.disabled = true; copyState.textContent = "Collecting...";
      try {
        const text = await fetchJobDiagnosticsText(root, job.id);
        const filename = jobDiagnosticsFilename(job);
        saveTextAsFile(filename, text);
        copyState.textContent = `Saved ${text.split("\n").length} lines to ${filename}.`;
      } catch (e) { copyState.textContent = "Cannot collect diagnostics: " + e.message; }
      finally { saveDiagBtn.disabled = false; clearDiagState(); }
    };
    // the VNC console of the migrated instance (OCI console connection through the helper)
    const consoleBtn = root.querySelector("[data-console]");
    consoleBtn.hidden = !hasConsole(job);
    consoleBtn.href = `#/jobs/${job.id}/console`;
    if (iso && job.phase === "INSTALLING") consoleBtn.classList.replace("secondary", "primary"); else consoleBtn.classList.replace("primary", "secondary");
    if (opts.onTerminal && terminal) opts.onTerminal(job);
    // stop polling on the final states; INSTALLING keeps polling (slowly) so a cancel/finish from elsewhere shows up
    return job.phase === "COMPLETED" || job.phase === "CANCELLED";
  }

  // one post-copy fix-up step (initramfs / network): badge, detail and, when it did not happen, what to do by hand
  function fixupEl(fx, manualHint) {
    const needsHint = fx.status === "failed" || (fx.status === "skipped" && !/disabled|Windows/.test(fx.detail));
    return el("span", {},
      el("span", { class: "badge " + ({ done: "ok", not_needed: "ok", failed: "warn" }[fx.status] || "") },
        { done: "done", not_needed: "not needed", skipped: "skipped", failed: "failed" }[fx.status] || fx.status),
      " ", fx.detail,
      needsHint ? el("div", { class: "muted" }, manualHint) : null);
  }

  // Live state of the target instance (lifecycle state + addresses of its primary VNIC), asked from OCI
  // through the helper.  While the job runs its record is re-rendered every few seconds and the state is
  // refreshed at most every OCI_STATE_TTL ms; once the job is finished (job polling stops) a timer keeps
  // the state refreshing on its own for as long as the page is open.
  const OCI_STATE_TTL = 15000;
  const OCI_STATE_CLASS = { RUNNING: "ok", PROVISIONING: "", STARTING: "", STOPPING: "warn", STOPPED: "warn",
    CREATING_IMAGE: "", MOVING: "", TERMINATING: "bad", TERMINATED: "bad", NOT_FOUND: "bad" };
  function ociStateEl(root, job) {
    const span = el("span", { "data-oci-state": "" });
    if (!job.instance_id) { span.textContent = "-"; return span; }
    const c = ociCached(root, job);
    if (c) fillOciState(span, c); else span.textContent = "checking...";
    ensureOciRefresh(root, job);
    return span;
  }
  function ociIpsEl(root, job) {
    const span = el("span", { "data-oci-ips": "" });
    fillOciIps(span, job, ociCached(root, job));
    return span;
  }
  const ociCached = (root, job) => (root._ociState && root._ociState.id === job.instance_id) ? root._ociState : null;
  function ensureOciRefresh(root, job) {
    const c = ociCached(root, job);
    const age = c ? Date.now() - c.at : Infinity;
    if (age > OCI_STATE_TTL) refreshOciState(root, job);
    else if (TERMINAL.includes(job.phase)) scheduleOciRefresh(root, job, OCI_STATE_TTL - age);
  }
  function scheduleOciRefresh(root, job, delayMs) {
    clearTimeout(root._ociTimer);
    root._ociTimer = setTimeout(() => { if (root.isConnected) refreshOciState(root, job); }, Math.max(1000, delayMs));
  }
  function fillOciState(span, c) {
    span.innerHTML = "";
    if (c.error) { span.append(el("span", { class: "badge bad" }, "unknown"), ` ${c.error}`); return; }
    const cls = OCI_STATE_CLASS[c.state];
    span.append(el("span", { class: "badge" + (cls ? " " + cls : "") }, c.state),
      el("span", { class: "muted" }, ` checked ${new Date(c.at).toLocaleTimeString()}`));
  }
  function fillOciIps(span, job, c) {
    // what was configured, then what OCI actually assigned once the VNIC exists
    const wanted = `${job.target.private_ip ? "private IP " + job.target.private_ip + " (fixed)" : "private IP assigned by OCI (DHCP)"}${job.target.assign_public_ip ? ", public IP" : ""}`;
    span.innerHTML = "";
    if (c && !c.error && c.private_ip) {
      span.append(el("strong", {}, `private ${c.private_ip}`), c.public_ip ? el("strong", {}, `, public ${c.public_ip}`) : "",
        el("div", { class: "muted" }, `requested: ${wanted}`));
    } else if (job.instance_id && (!c || c.error || !["TERMINATING", "TERMINATED", "NOT_FOUND"].includes(c.state))) {
      span.append(wanted, el("span", { class: "muted" }, c && !c.error ? " - address not assigned yet" : " - checking..."));
    } else span.textContent = wanted;
  }
  async function refreshOciState(root, job) {
    root._ociJob = job;  // newest record: the phase may turn terminal while a request is in flight
    if (root._ociPending) return;
    root._ociPending = true;
    const cache = { id: job.instance_id, at: Date.now() };
    try {
      const st = await api("GET", `/jobs/${job.id}/instance`);
      cache.state = st.lifecycle_state; cache.at = new Date(st.checked_at).getTime() || cache.at;
      cache.private_ip = st.private_ip || null; cache.public_ip = st.public_ip || null;
    } catch (e) { if (e.status === 401) return; cache.error = e.message; }
    finally { root._ociPending = false; }
    root._ociState = cache;
    job = root._ociJob;
    root.querySelectorAll("[data-oci-state]").forEach((s) => fillOciState(s, cache));
    root.querySelectorAll("[data-oci-ips]").forEach((s) => fillOciIps(s, job, cache));
    if (TERMINAL.includes(job.phase)) scheduleOciRefresh(root, job, OCI_STATE_TTL);  // job polling has stopped
  }

  function pollJob(jobId, container, opts) {
    let timer = null; let stopped = false;
    const tick = async () => {
      if (stopped) return;
      try {
        const job = await api("GET", `/jobs/${jobId}`);
        if (renderJob(container, job, opts)) return;
      } catch (e) { if (e.status === 401) return; console.warn(e); }
      timer = setTimeout(tick, 3000);
    };
    tick();
    return () => { stopped = true; clearTimeout(timer); };
  }

  // ------------------------------------------------------------------ VM list
  async function vmsView(opts) {
    const olvm = !!(opts && opts.olvm);
    app.innerHTML = "";
    app.append(tpl("tpl-vms"));
    if (olvm) {
      const clusterHead = app.querySelector("table.vms thead th:nth-child(2)");
      if (clusterHead) clusterHead.textContent = "Cluster";
      const filterBox = document.getElementById("vm-filter");
      if (filterBox) filterBox.placeholder = "Filter by name, cluster or guest OS";
    }
    const rows = document.getElementById("vm-rows");
    const filter = document.getElementById("vm-filter");
    const offOnly = document.getElementById("vm-off-only");
    const folderSel = document.getElementById("vm-folder");
    const osSel = document.getElementById("vm-os");
    const count = document.getElementById("vm-count");
    const err = document.getElementById("vm-error");
    const pager = document.getElementById("vm-pager");
    const pageInfo = document.getElementById("vm-page-info");
    const prevBtn = document.getElementById("vm-prev"), nextBtn = document.getElementById("vm-next");
    const pageSizeSel = document.getElementById("vm-page-size");
    let vms = [];
    let page = 0;
    const pageSize = () => Number(pageSizeSel.value) || 50;
    const osOf = (vm) => vm.guest_full_name || vm.guest_id || "(unknown)";

    // folder / guest OS dropdowns are built from the inventory; the current choice survives a refresh
    const fillFilters = () => {
      const fill = fillFilterSelect;
      const uniq = (list) => [...new Set(list)].sort((a, b) => a.localeCompare(b, undefined, { sensitivity: "base" }));
      fill(folderSel, uniq(vms.map((vm) => vm.folder || "(no folder)")), `All folders (${new Set(vms.map((vm) => vm.folder || "(no folder)")).size})`);
      fill(osSel, uniq(vms.map(osOf)), `All guest OSes (${new Set(vms.map(osOf)).size})`);
    };

    const matches = (vm) => {
      if (offOnly.checked && vm.power_state !== "poweredOff") return false;
      if (folderSel.value && (vm.folder || "(no folder)") !== folderSel.value) return false;
      if (osSel.value && osOf(vm) !== osSel.value) return false;
      const q = filter.value.trim().toLowerCase();
      return !q || `${vm.name} ${vm.folder} ${vm.guest_full_name}`.toLowerCase().includes(q);
    };

    const render = () => {
      const filtered = vms.filter(matches);
      const size = pageSize(), pages = Math.max(1, Math.ceil(filtered.length / size));
      page = Math.min(page, pages - 1);
      const start = page * size, visible = filtered.slice(start, start + size);
      rows.innerHTML = "";
      for (const vm of visible) {
        const job = state.jobsByVm[vm.moid];
        const off = vm.power_state === "poweredOff";
        const on = vm.power_state === "poweredOn";  // migratable: the helper shuts it down before the export
        const active = job && !TERMINAL.includes(job.phase);
        const exportable = (off || on) && !vm.encrypted;
        const why = vm.encrypted ? "The VM is encrypted (VM encryption or a virtual TPM): vSphere does not allow exporting it. Decrypt it in vCenter first"
          : off ? "" : on ? (olvm ? "The VM is powered on: it will be shut down in OLVM just before the disk export"
            : "The VM is powered on: it will be shut down just before the disk export")
            : "Resume and shut down, or power off the VM first";
        rows.append(el("tr", {},
          el("td", { class: "name" }, vm.name, vm.encrypted ? " " : null, vm.encrypted ? encryptedBadge(vm) : null),
          el("td", { class: "muted" }, vm.folder || "-"),
          el("td", {}, el("span", { class: "power " + vm.power_state }, vm.power_state.replace("powered", "").toLowerCase())),
          el("td", {}, vm.guest_full_name || vm.guest_id || "-"),
          el("td", {}, `${vm.num_cpu} / ${fmtBytes(vm.memory_mb * 1024 * 1024)}`),
          el("td", {}, `${vm.num_disks} (${fmtBytes(vm.disk_capacity_bytes)})`),
          el("td", {}, job ? el("a", { href: `#/jobs/${job.id}`, class: "phase " + job.phase }, job.phase) : el("span", { class: "muted" }, "-")),
          el("td", {}, active
            ? el("a", { href: `#/jobs/${job.id}`, class: "button secondary small" }, "View job")
            : el("a", { href: olvm ? `#/olvm/export/${encodeURIComponent(vm.moid)}` : `#/export/${vm.moid}`, class: "button primary small" + (exportable ? "" : " disabled"), title: why }, "Migrate"))));
      }
      if (!visible.length) rows.append(el("tr", {}, el("td", { colspan: 8, class: "muted" }, vms.length ? "No virtual machines match the filters." : "No virtual machines found in this inventory.")));
      count.textContent = filtered.length === vms.length ? `${vms.length} virtual machines` : `${filtered.length} of ${vms.length} virtual machines`;
      // pagination: only when the filtered list does not fit on one page
      pager.hidden = filtered.length <= size && page === 0;
      pageInfo.textContent = filtered.length ? `${start + 1}-${Math.min(start + size, filtered.length)} of ${filtered.length} (page ${page + 1} of ${pages})` : "";
      prevBtn.disabled = page === 0;
      nextBtn.disabled = page >= pages - 1;
    };
    const resetPage = () => { page = 0; render(); };

    const load = async (refresh) => {
      err.textContent = ""; count.textContent = "Loading inventory...";
      try {
        const [list, jobs] = await Promise.all([api("GET", (olvm ? "/olvm/vms" : "/vms") + (refresh ? "?refresh=true" : "")), api("GET", "/jobs")]);
        vms = list;
        state.jobsByVm = {};
        for (const j of jobs) if (j.vm && !state.jobsByVm[j.vm.moid]) state.jobsByVm[j.vm.moid] = j; // jobs are newest first
        fillFilters();
        render();
      } catch (e) { if (e.status !== 401) err.textContent = e.message; count.textContent = ""; }
    };
    filter.addEventListener("input", resetPage);
    offOnly.addEventListener("change", resetPage);
    folderSel.addEventListener("change", resetPage);
    osSel.addEventListener("change", resetPage);
    pageSizeSel.addEventListener("change", resetPage);
    prevBtn.addEventListener("click", () => { page = Math.max(0, page - 1); render(); rows.closest("table").scrollIntoView({ block: "start" }); });
    nextBtn.addEventListener("click", () => { page += 1; render(); rows.closest("table").scrollIntoView({ block: "start" }); });
    document.getElementById("vm-refresh").addEventListener("click", () => load(true));
    await load(false);
  }

  // ------------------------------------------------------------ Azure VM list
  // same shape as vmsView: the rows come from GET /api/azure/vms (all subscriptions the principal can read),
  // grouped by subscription / resource group ("folder" in the summary) instead of vCenter folders
  function fillFilterSelect(sel, values, all) {
    const previous = sel.value;
    sel.replaceChildren(el("option", { value: "" }, all));
    for (const value of values) sel.append(el("option", { value }, value));
    sel.value = values.includes(previous) ? previous : "";
  }

  async function azureVmsView() {
    app.innerHTML = "";
    app.append(tpl("tpl-azure-vms"));
    const rows = document.getElementById("azvm-rows");
    const filter = document.getElementById("azvm-filter");
    const groupSel = document.getElementById("azvm-group");
    const osSel = document.getElementById("azvm-os");
    const count = document.getElementById("azvm-count");
    const err = document.getElementById("azvm-error");
    let vms = [];
    const osOf = (vm) => vm.guest_full_name || vm.guest_id || "(unknown)";
    const groupOf = (vm) => vm.folder || "(unknown)";

    const fillFilters = () => {
      const fill = fillFilterSelect;
      const uniq = (list) => [...new Set(list)].sort((a, b) => a.localeCompare(b, undefined, { sensitivity: "base" }));
      fill(groupSel, uniq(vms.map(groupOf)), `All resource groups (${new Set(vms.map(groupOf)).size})`);
      fill(osSel, uniq(vms.map(osOf)), `All guest OSes (${new Set(vms.map(osOf)).size})`);
    };
    const matches = (vm) => {
      if (groupSel.value && groupOf(vm) !== groupSel.value) return false;
      if (osSel.value && osOf(vm) !== osSel.value) return false;
      const q = filter.value.trim().toLowerCase();
      return !q || `${vm.name} ${vm.folder} ${vm.guest_full_name} ${vm.vm_size} ${vm.location}`.toLowerCase().includes(q);
    };
    const render = () => {
      const filtered = vms.filter(matches);
      rows.innerHTML = "";
      for (const vm of filtered) {
        const job = state.jobsByVm[vm.moid];
        const active = job && !TERMINAL.includes(job.phase);
        // Block only ADE and transitional power states; unknown power still opens the migration page (it re-checks)
        const azurePowerBlock = ["starting", "stopping", "deallocating"];
        const exportable = !vm.encrypted && !azurePowerBlock.includes(vm.power_state);
        const why = vm.encrypted ? "The disks use Azure Disk Encryption: the export would copy ciphertext. Decrypt the VM in Azure first"
          : azurePowerBlock.includes(vm.power_state) ? `The VM is ${vm.power_state}: wait until it is running or deallocated`
            : vm.power_state === "poweredOn" ? "The VM is running: it is deallocated right before the disk export, or its disks are snapshotted while it runs"
              : vm.power_state === "stopped" ? "The VM is stopped but still allocated: it is deallocated right before the disk export"
                : vm.power_state === "unknown" ? "Azure did not report power state in the list; the migration page verifies the VM before starting"
                  : "";
        rows.append(el("tr", {},
          el("td", { class: "name", title: vm.moid }, vm.name, vm.encrypted ? " " : null, vm.encrypted ? azureEncryptedBadge() : null),
          el("td", { class: "muted" }, vm.folder || "-"),
          el("td", {}, el("span", { class: "power " + vm.power_state }, vm.power_state.replace("powered", "").toLowerCase())),
          el("td", {}, vm.guest_full_name || vm.guest_id || "-"),
          el("td", {}, vm.vm_size || "-"),
          el("td", { class: "muted" }, vm.location || "-"),
          el("td", {}, `${vm.num_disks} (${fmtBytes(vm.disk_capacity_bytes)})`),
          el("td", {}, job ? el("a", { href: `#/jobs/${job.id}`, class: "phase " + job.phase }, job.phase) : el("span", { class: "muted" }, "-")),
          el("td", {}, active
            ? el("a", { href: `#/jobs/${job.id}`, class: "button secondary small" }, "View job")
            : el("a", { href: `#/azure/export/${encodeURIComponent(vm.moid)}`, class: "button primary small" + (exportable ? "" : " disabled"), title: why }, "Migrate"))));
      }
      if (!filtered.length) rows.append(el("tr", {}, el("td", { colspan: 9, class: "muted" }, vms.length ? "No virtual machines match the filters." : "No virtual machines found in the subscriptions this service principal can read.")));
      count.textContent = filtered.length === vms.length ? `${vms.length} virtual machines` : `${filtered.length} of ${vms.length} virtual machines`;
    };
    const load = async (refresh) => {
      err.textContent = ""; count.textContent = "Loading Azure inventory...";
      try {
        const [list, jobs] = await Promise.all([api("GET", "/azure/vms" + (refresh ? "?refresh=true" : "")), api("GET", "/jobs")]);
        vms = list;
        state.jobsByVm = {};
        for (const j of jobs) if (j.vm && !state.jobsByVm[j.vm.moid]) state.jobsByVm[j.vm.moid] = j; // jobs are newest first
        fillFilters();
        render();
      } catch (e) { if (e.status !== 401) err.textContent = e.message; count.textContent = ""; }
    };
    filter.addEventListener("input", render);
    groupSel.addEventListener("change", render);
    osSel.addEventListener("change", render);
    document.getElementById("azvm-refresh").addEventListener("click", () => load(true));
    await load(false);
  }

  const CLOUD_INVENTORY = {
    gcp: { label: "Google Cloud", noun: "VM", plural: "virtual machines", blocked: ["starting", "stopping"],
      empty: "No virtual machines found in the projects this service account can read." },
    aws: { label: "EC2", noun: "instance", plural: "instances", blocked: ["pending", "stopping", "shutting-down"],
      empty: "No instances found in this region." },
  };
  const gcpVmsView = () => cloudVmsView("gcp");
  const awsVmsView = () => cloudVmsView("aws");

  async function cloudVmsView(provider) {
    const config = CLOUD_INVENTORY[provider];
    app.innerHTML = "";
    app.append(tpl(`tpl-${provider}-vms`));
    const rows = document.getElementById(`${provider}vm-rows`);
    const filter = document.getElementById(`${provider}vm-filter`);
    const groupSel = document.getElementById(`${provider}vm-group`);
    const osSel = document.getElementById(`${provider}vm-os`);
    const count = document.getElementById(`${provider}vm-count`);
    const err = document.getElementById(`${provider}vm-error`);
    let vms = [];
    const osOf = (vm) => vm.guest_full_name || vm.guest_id || "(unknown)";
    const groupOf = (vm) => vm.folder || "(unknown)";
    const fillFilters = () => {
      const fill = fillFilterSelect;
      const uniq = (list) => [...new Set(list)].sort((a, b) => a.localeCompare(b, undefined, { sensitivity: "base" }));
      fill(groupSel, uniq(vms.map(groupOf)), `All locations (${new Set(vms.map(groupOf)).size})`);
      fill(osSel, uniq(vms.map(osOf)), `All guest OSes (${new Set(vms.map(osOf)).size})`);
    };
    const matches = (vm) => {
      if (groupSel.value && groupOf(vm) !== groupSel.value) return false;
      if (osSel.value && osOf(vm) !== osSel.value) return false;
      const q = filter.value.trim().toLowerCase();
      return !q || `${vm.name} ${vm.folder} ${vm.guest_full_name} ${vm.vm_size} ${vm.location}`.toLowerCase().includes(q);
    };
    const render = () => {
      const filtered = vms.filter(matches);
      rows.innerHTML = "";
      for (const vm of filtered) {
        const job = state.jobsByVm[vm.moid];
        const active = job && !TERMINAL.includes(job.phase);
        const block = config.blocked;
        const exportable = !block.includes(vm.power_state) && vm.power_state !== "terminated";
        const why = block.includes(vm.power_state) ? `The ${config.noun} is ${vm.power_state}: wait until it is running or stopped`
          : vm.power_state === "poweredOn" ? `The ${config.noun} is running: it is stopped before export, or use snapshot mode`
            : "";
        rows.append(el("tr", {},
          el("td", { class: "name", title: vm.moid }, vm.name),
          el("td", { class: "muted" }, vm.folder || "-"),
          el("td", {}, el("span", { class: "power " + vm.power_state }, vm.power_state.replace("powered", "").toLowerCase())),
          el("td", {}, vm.guest_full_name || vm.guest_id || "-"),
          el("td", {}, vm.vm_size || "-"),
          el("td", { class: "muted" }, vm.location || "-"),
          el("td", {}, `${vm.num_disks}`),
          el("td", {}, job ? el("a", { href: `#/jobs/${job.id}`, class: "phase " + job.phase }, job.phase) : el("span", { class: "muted" }, "-")),
          el("td", {}, active
            ? el("a", { href: `#/jobs/${job.id}`, class: "button secondary small" }, "View job")
            : el("a", { href: `#/${provider}/export/${encodeURIComponent(vm.moid)}`, class: "button primary small" + (exportable ? "" : " disabled"), title: why }, "Migrate"))));
      }
      if (!filtered.length) rows.append(el("tr", {}, el("td", { colspan: 9, class: "muted" }, vms.length ? `No ${config.plural} match the filters.` : config.empty)));
      count.textContent = filtered.length === vms.length ? `${vms.length} ${config.plural}` : `${filtered.length} of ${vms.length} ${config.plural}`;
    };
    const load = async (refresh) => {
      err.textContent = ""; count.textContent = `Loading ${config.label} inventory...`;
      try {
        const [list, jobs] = await Promise.all([api("GET", `/${provider}/vms` + (refresh ? "?refresh=true" : "")), api("GET", "/jobs")]);
        vms = list;
        state.jobsByVm = {};
        for (const j of jobs) if (j.vm && !state.jobsByVm[j.vm.moid]) state.jobsByVm[j.vm.moid] = j;
        fillFilters();
        render();
      } catch (e) { if (e.status !== 401) err.textContent = e.message; count.textContent = ""; }
    };
    filter.addEventListener("input", render);
    groupSel.addEventListener("change", render);
    osSel.addEventListener("change", render);
    document.getElementById(`${provider}vm-refresh`).addEventListener("click", () => load(true));
    await load(false);
  }

  // -------------------------------------------------------------- export view
  // one form for both sources: ``src.azure`` switches the inspection endpoint, the source details, the
  // capture choice (deallocate vs snapshot instead of the vSphere power-off pop-up) and the job endpoint
  async function exportView(moid, src) {
    const azure = !!(src && src.azure);
    const gcp = !!(src && src.gcp);
    const aws = !!(src && src.aws);
    const olvm = !!(src && src.olvm);
    app.innerHTML = "";
    app.append(tpl("tpl-export"));
    const form = document.getElementById("target-form");
    const formError = document.getElementById("form-error");
    const submit = document.getElementById("submit-btn");
    const captureBox = document.getElementById("azure-capture");
    const gcpCaptureBox = document.getElementById("gcp-capture");
    const awsCaptureBox = document.getElementById("aws-capture");
    const captureMode = () => {
      if (gcp) return (document.querySelector('#gcp-capture input[name="gcp_capture_mode"]:checked') || {}).value || "stop";
      if (aws) return (document.querySelector('#aws-capture input[name="aws_capture_mode"]:checked') || {}).value || "stop";
      if (azure) return (document.querySelector('#azure-capture input[name="capture_mode"]:checked') || {}).value || "deallocate";
      return "deallocate";
    };
    const inspectUrl = () => gcp ? `/gcp/vm?id=${encodeURIComponent(moid)}&capture_mode=${captureMode()}`
      : aws ? `/aws/vm?id=${encodeURIComponent(moid)}&capture_mode=${captureMode()}`
        : azure ? `/azure/vm?id=${encodeURIComponent(moid)}&capture_mode=${captureMode()}`
          : olvm ? `/olvm/vm?id=${encodeURIComponent(moid)}`
          : `/vms/${encodeURIComponent(moid)}`;
    if (gcp) document.querySelector("#vm-card .toolbar a").href = "#/gcp/vms";
    else if (aws) document.querySelector("#vm-card .toolbar a").href = "#/aws/vms";
    else if (azure) document.querySelector("#vm-card .toolbar a").href = "#/azure/vms";
    else if (olvm) document.querySelector("#vm-card .toolbar a").href = "#/olvm/vms";
    if (olvm) {
      document.getElementById("power-off-note").innerHTML =
        "<strong>This VM is powered on.</strong> Starting the migration <strong>shuts it down</strong> in OLVM right before the disk export (after the OCI instance and volumes are prepared): a guest shutdown, then a hard stop if it does not power off in time. The VM stays powered off. You will be asked to confirm.";
    }

    let inspection, options;
    try {
      [inspection, options] = await Promise.all([api("GET", inspectUrl()), api("GET", "/oci/options")]);
    } catch (e) { if (e.status !== 401) showError("Cannot load VM or OCI information: " + e.message); return; }
    const vm = inspection.vm;

    kv(document.getElementById("vm-details"), [
      ["Name", azure ? el("span", { title: vm.moid }, vm.name) : vm.name], ["Guest OS", vm.guest_full_name || vm.guest_id],
      ["Power state", vm.power_state],
      azure ? ["Resource group", azureResourceGroup(vm.moid) || "-"]
        : gcp ? ["Zone", gcpZone(vm.moid) || "-"]
          : aws ? ["AZ / instance", vm.host_name || vm.moid]
            : olvm ? ["Cluster", vm.host_name || "-"]
            : ["ESXi host", vm.host_name || "-"],
      ["CPU / memory", `${vm.num_cpu} vCPU / ${fmtBytes(vm.memory_mb * 1024 * 1024)}`],
      ["Firmware", vm.firmware.toUpperCase() + (vm.secure_boot ? " (secure boot)" : "") + (vm.has_vtpm ? " + vTPM" : "")],
      ...(vm.encrypted || vm.encrypted_disks.length ? [["Encryption", el("span", {}, azure ? azureEncryptedBadge() : encryptedBadge(vm),
        " ", vm.encrypted ? (azure ? "Azure Disk Encryption on the OS disk" : "VM encryption" + (vm.has_vtpm ? " with a Virtual TPM" : "")) : vm.encrypted_disks.join(", "),
        azure ? null : el("span", { class: "muted" }, " - click (i) for the steps to decrypt it in vCenter"))]] : []),
      ["Disks", vm.disks.map((d) => `${d.label}: ${fmtBytes(d.capacity_bytes)} on ${d.controller_type}`).join("; ")],
      // one line per adapter: type, port group and the last addresses VMware Tools reported (when vCenter knows them)
      ["Network", vm.nics.length ? el("span", {}, ...vm.nics.map((n) => el("div", {},
        azure ? `${n.label}: Azure network interface` : aws ? `${n.label}: EC2 network interface`
          : `${n.label}: ${n.adapter_type}${n.network ? " on " + n.network : ""}`,
        n.ip_addresses && n.ip_addresses.length ? el("span", {}, " - ", el("strong", {}, n.ip_addresses.join(", ")))
          : el("span", { class: "muted" }, azure ? " - the OCI instance gets a new address from its subnet" : " - IP address unknown")))) : "-"],
    ]);
    const problems = document.getElementById("vm-problems");
    const warnings = document.getElementById("vm-warnings");
    const revokeBox = document.getElementById("azure-revoke-export");
    const revokeBtn = document.getElementById("azure-revoke-btn");
    const revokeStatus = document.getElementById("azure-revoke-status");
    // Azure: problems, warnings and the "needs power off" flag depend on the capture mode, so they are
    // re-fetched when the radio changes (vSphere: filled once)
    const fillChecks = () => {
      problems.innerHTML = ""; warnings.innerHTML = "";
      for (const p of inspection.problems) problems.append(el("li", {}, p));
      for (const w of inspection.warnings) warnings.append(el("li", {}, w));
      document.getElementById("power-off-note").hidden = azure || gcp || aws || !inspection.needs_power_off;
      if (azure) {
        document.getElementById("azure-capture-hint").textContent = captureMode() === "snapshot"
          ? "Snapshots are created right before the export and deleted when the job ends (Azure bills their storage in between). The VM is not touched."
          : inspection.needs_power_off ? `"${vm.name}" is running: it is deallocated right before the disk export (after the OCI instance and volumes are prepared) and stays deallocated in Azure. You will be asked to confirm.`
            : `"${vm.name}" is already deallocated: its disks are exported as they are.`;
      }
      if (aws) {
        document.getElementById("aws-capture-hint").textContent = captureMode() === "snapshot"
          ? "Snapshots are created right before the export and deleted when the job ends (AWS bills their storage in between). The instance is not touched."
          : inspection.needs_power_off ? `"${vm.name}" is running: it is stopped right before the disk export (after the OCI instance and volumes are prepared) and stays stopped in EC2. You will be asked to confirm.`
            : `"${vm.name}" is already stopped: its volumes are snapshotted as they are.`;
      }
      if (gcp) {
        document.getElementById("gcp-capture-hint").textContent = captureMode() === "snapshot"
          ? "Snapshots are exported to your GCS bucket and deleted when the job ends. The VM keeps running."
          : inspection.needs_power_off ? `"${vm.name}" is running: it is stopped before the disk export. You will be asked to confirm.`
            : `"${vm.name}" is already stopped: its disks are exported as they are.`;
      }
      submit.disabled = !inspection.can_export;
      const revocable = inspection.azure_revoke_export_disks || [];
      if (revokeBox) {
        revokeBox.hidden = !azure || !revocable.length;
        if (revocable.length) {
          document.getElementById("azure-revoke-text").textContent =
            revocable.length === 1
              ? `Disk ${revocable[0].name} still has export read access granted (ActiveSAS). Revoke it before starting a new migration (same as az disk revoke-access).`
              : `${revocable.length} disks still have export read access granted (ActiveSAS). Revoke them before starting a new migration.`;
        }
      }
    };
    fillChecks();
    if (azure && revokeBtn && !revokeBtn.dataset.wired) {
      revokeBtn.dataset.wired = "1";
      revokeBtn.addEventListener("click", async () => {
        revokeBtn.disabled = true;
        if (revokeStatus) revokeStatus.textContent = "Revoking export access…";
        formError.textContent = "";
        try {
          const ids = (inspection.azure_revoke_export_disks || []).map((d) => d.disk_id);
          const res = await api("POST", "/azure/revoke-export-access", { disk_ids: ids });
          const failed = (res.results || []).filter((r) => !r.ok);
          if (revokeStatus) {
            revokeStatus.textContent = failed.length
              ? failed.map((r) => r.message).join("; ")
              : (res.results || []).map((r) => r.message).join("; ");
          }
          inspection = await api("GET", inspectUrl());
          fillChecks();
          if (failed.length) revokeBtn.disabled = false;
        } catch (e) {
          if (e.status !== 401) formError.textContent = e.message;
          if (revokeStatus) revokeStatus.textContent = "";
          revokeBtn.disabled = false;
        }
      });
    }
    captureBox.hidden = !azure;
    gcpCaptureBox.hidden = !gcp;
    awsCaptureBox.hidden = !aws;
    const wireCapture = (box) => {
      if (!box || box.hidden) return;
      for (const radio of box.querySelectorAll('input[type="radio"]')) radio.addEventListener("change", async () => {
        submit.disabled = true;
        try { inspection = await api("GET", inspectUrl()); fillChecks(); }
        catch (e) { if (e.status !== 401) formError.textContent = e.message; }
      });
    };
    wireCapture(captureBox);
    wireCapture(gcpCaptureBox);
    wireCapture(awsCaptureBox);

    // populate the target form (compartments, networks, private IP check, shapes, device model preview)
    const { sel, renderSizing } = wireTargetForm(form, options, {
      formError,
      sizing: { autoOcpus: Math.max(1, Math.ceil(vm.num_cpu / 2)), autoMemoryGb: Math.max(1, Math.ceil(vm.memory_mb / 1024)),
        source: `Source VM: ${vm.num_cpu} vCPU / ${(vm.memory_mb / 1024).toFixed(vm.memory_mb % 1024 ? 1 : 0)} GB.` },
      firmwareText: () => (vm.firmware === "efi" ? "UEFI_64" : "BIOS") + (vm.secure_boot ? " + Secure Boot" : ""),
    });
    sel("display_name").value = vm.name;
    // guest OS release recorded on the OCI image: vSphere encodes it for most guests, but not for e.g.
    // ubuntu64Guest ("Ubuntu Linux (64-bit)"), where the user has to pick it from OCI's list
    const osInfo = inspection.os;
    const osLabel = document.getElementById("os-version-label"), osSel = sel("operating_system_version");
    if (osInfo && osInfo.version_choices.length) {
      osLabel.hidden = false;
      osSel.innerHTML = "";
      if (!osInfo.version_detected) osSel.append(el("option", { value: "" }, `Select the ${osInfo.operating_system} release...`));
      for (const v of osInfo.version_choices) osSel.append(el("option", { value: v }, `${osInfo.operating_system} ${v}`));
      osSel.value = osInfo.version_detected ? osInfo.operating_system_version : "";
      osSel.required = !osInfo.version_detected;
      osLabel.classList.toggle("attention", !osInfo.version_detected);
      osSel.addEventListener("change", () => osLabel.classList.toggle("attention", !osSel.value));
      const from = gcp ? "Google Cloud" : aws ? "Amazon EC2" : azure ? "Azure" : olvm ? "OLVM" : "vCenter";
      document.getElementById("os-version-hint").textContent = osInfo.version_detected
        ? `Detected from ${from} (${vm.guest_full_name || vm.guest_id}); change it if the guest runs another release.`
        : `${from} only reports "${vm.guest_full_name || vm.guest_id}" without the release. Select the one installed in the guest; OCI records it on the image and uses it for OS-specific defaults.`;
    } else {
      osLabel.hidden = true; osSel.required = false;
    }
    const isWin = isWindows(vm);
    document.getElementById("windows-fieldset").hidden = !isWin;
    document.getElementById("windows-driver-note").hidden = !isWin;
    // Amazon Linux from EC2: the kernel-modules-extra package has to be installed inside the instance before
    // the copy (the initramfs rebuild cannot add modules the guest does not have)
    document.getElementById("amazon-linux-note").hidden = !(aws && !isWin && isAmazonLinux(vm));
    wireCopyCommands("amazon-linux-copy", "amazon-linux-cmds", "amazon-linux-copy-state");
    // the initramfs fix-up is a Linux thing (Windows gets its VirtIO drivers installed inside the guest)
    for (const id of ["rebuild-initramfs-label", "rebuild-initramfs-hint", "fix-network-label", "fix-network-hint"]) {
      document.getElementById(id).hidden = isWin;  // Linux-only post-copy fix-ups
    }
    const showAzureCleanup = azure && !isWin;
    for (const id of ["azure-cleanup-label", "azure-cleanup-hint"]) {
      document.getElementById(id).hidden = !showAzureCleanup;
    }
    const showGcpCleanup = gcp && !isWin;
    for (const id of ["gcp-cleanup-label", "gcp-cleanup-hint"]) {
      document.getElementById(id).hidden = !showGcpCleanup;
    }
    const showAwsCleanup = aws && !isWin;
    for (const id of ["aws-cleanup-label", "aws-cleanup-hint"]) {
      document.getElementById(id).hidden = !showAwsCleanup;
    }
    document.getElementById("gcp-transfer-note").hidden = !gcp;
    document.getElementById("azure-transfer-note").hidden = !azure;
    document.getElementById("aws-transfer-note").hidden = !aws;
    document.getElementById("nfc-options").hidden = azure || gcp || aws || olvm;
    if (isWin && isWindowsClient(vm)) {
      // OCI has no licenses for client editions; the API refuses OCI_PROVIDED for them
      const ociLic = form.querySelector('input[name="windows_license_type"][value="OCI_PROVIDED"]');
      ociLic.disabled = true; ociLic.checked = false;
      form.querySelector('input[name="windows_license_type"][value="BRING_YOUR_OWN_LICENSE"]').checked = true;
      document.getElementById("windows-license-hint").textContent = "Windows 10/11: OCI does not provide licenses for client editions, so the instance is registered as BYOL (check your Microsoft license terms for running the desktop OS in a cloud).";
    }
    document.getElementById("esxi-host-hint").textContent = vm.host_name ? `(${vm.host_name})` : "";
    sel("nfc_direct_to_esxi").disabled = !vm.host_name;
    // the NFC options are vCenter-only; Azure downloads page ranges instead
    document.getElementById("nfc-options").hidden = azure || gcp || aws || olvm;
    document.getElementById("azure-transfer-note").hidden = !azure;
    renderSizing();

    // a migration of this VM is already running: nothing to configure here, show the job instead
    try {
      const jobs = await api("GET", `/jobs?vm_moid=${encodeURIComponent(moid)}`);
      const active = jobs.find((j) => !TERMINAL.includes(j.phase));
      if (active) { location.hash = `#/jobs/${active.id}`; return; }
    } catch (_) { /* ignore */ }

    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      formError.textContent = "";
      const fd = new FormData(form);
      const target = {
        compartment_id: fd.get("compartment_id"),
        availability_domain: options.helper_availability_domain,
        subnet_id: fd.get("subnet_id"),
        private_ip: (fd.get("private_ip") || "").trim() || null,
        shape: fd.get("shape") || null,
        ocpus: fd.get("ocpus") ? Number(fd.get("ocpus")) : null,
        memory_gb: fd.get("memory_gb") ? Number(fd.get("memory_gb")) : null,
        display_name: fd.get("display_name") || null,
        operating_system_version: osLabel.hidden ? null : (fd.get("operating_system_version") || null),
        assign_public_ip: fd.get("assign_public_ip") === "on",
        start_after_migration: fd.get("start_after_migration") === "on",
        windows_license_type: isWin ? fd.get("windows_license_type") : null,
        compatibility_mode: fd.get("compatibility_mode") === "on",
        boot_volume_type_override: fd.get("boot_volume_type_override") || null,
        network_type_override: fd.get("network_type_override") || null,
        nfc_direct_to_esxi: !azure && !gcp && !aws && !olvm && fd.get("nfc_direct_to_esxi") === "on",
        pipelined_decode: !azure && !gcp && !aws && !olvm && fd.get("pipelined_decode") === "on",
        rebuild_initramfs: !isWin && fd.get("rebuild_initramfs") === "on",
        fix_network: !isWin && fd.get("fix_network") === "on",
        azure_cleanup: azure && !isWin && fd.get("azure_cleanup") === "on",
        gcp_cleanup: gcp && !isWin && fd.get("gcp_cleanup") === "on",
        aws_cleanup: aws && !isWin && fd.get("aws_cleanup") === "on",
        volume_vpus_per_gb: Number(fd.get("volume_vpus_per_gb") || 10),
      };
      // a running VM is shut down (vSphere) or deallocated (Azure, deallocate mode) by the migration: make the
      // operator confirm it, naming the VM; Azure snapshot mode leaves the VM alone and needs no confirmation
      if (inspection.needs_power_off) {
        const ok = gcp
          ? confirm(`WARNING: "${vm.name}" is running in Google Cloud.\n\n` +
            `Starting this migration will STOP the VM "${vm.name}" right before the disk export.\n\n` +
            `Stop "${vm.name}" and migrate it?`)
          : aws
          ? confirm(`WARNING: "${vm.name}" is running in Amazon EC2.\n\n` +
            `Starting this migration will STOP the instance "${vm.name}" right before the disk export ` +
            "(after the OCI instance and volumes are prepared).\n\n" +
            "The instance stays stopped in AWS afterwards.\n\n" +
            `Stop "${vm.name}" and migrate it?`)
          : olvm
          ? confirm(`WARNING: "${vm.name}" is powered on in OLVM.\n\n` +
            `Starting this migration will POWER OFF the VM "${vm.name}" right before the disk export ` +
            "(after the OCI instance and volumes are prepared). The engine asks the guest to shut down; " +
            "if it does not stop in time the VM is stopped hard.\n\n" +
            "The VM stays powered off in OLVM afterwards.\n\n" +
            `Power off "${vm.name}" and migrate it?`)
          : azure
          ? confirm(`WARNING: "${vm.name}" is running in Azure.\n\n` +
            `Starting this migration will DEALLOCATE (stop) the VM "${vm.name}" right before the disk export ` +
            "(after the OCI instance and volumes are prepared). Azure shuts the guest OS down first; if it does not stop in time the VM is stopped hard.\n\n" +
            "The VM stays deallocated in Azure afterwards (it is not billed for compute, its disks are kept).\n\n" +
            `Deallocate "${vm.name}" and migrate it?`)
          : confirm(`WARNING: "${vm.name}" is powered on.\n\n` +
            `Starting this migration will POWER OFF the VM "${vm.name}" right before the disk export ` +
            `(after the OCI instance and volumes are prepared). ${inspection.tools_running
              ? "It will be shut down through VMware Tools (guest OS shutdown); if it does not stop in time it is powered off hard."
              : "VMware Tools is NOT running, so it will be POWERED OFF HARD (like pulling the plug)."}\n\n` +
            "The VM stays powered off in vSphere afterwards.\n\n" +
            `Power off "${vm.name}" and migrate it?`);
        if (!ok) return;
      }
      submit.disabled = true;
      try {
        const job = gcp
          ? await api("POST", "/jobs/gcp", { vm_id: moid, target, capture_mode: captureMode(), power_off_source: inspection.needs_power_off })
          : aws
          ? await api("POST", "/jobs/aws", { vm_id: moid, target, capture_mode: captureMode(), power_off_source: inspection.needs_power_off })
          : azure
          ? await api("POST", "/jobs/azure", { vm_id: moid, target, capture_mode: captureMode(), power_off_source: inspection.needs_power_off })
          : olvm
          ? await api("POST", "/jobs/olvm", { vm_id: moid, target, power_off_source: inspection.needs_power_off })
          : await api("POST", "/jobs", { vm_moid: moid, target, power_off_source: inspection.needs_power_off });
        location.hash = `#/jobs/${job.id}`;  // follow the migration on its own page
      } catch (e) { formError.textContent = e.message; submit.disabled = false; }
    });
  }

  // The OCI target part shared by the migration form and the ISO form: compartment pickers (instance and
  // network), VCN -> subnet, fixed private IP with the OCI check, x86 flex shapes with sizing bounds, and
  // the device model preview.  ``cfg.sizing`` (auto values derived from a source VM) is optional: without
  // it the OCPU / memory fields are plain required inputs.
  function wireTargetForm(form, options, cfg) {
    const formError = cfg.formError;
    const sel = (name) => form.elements[name];
    for (const c of options.compartments) {
      sel("compartment_id").append(el("option", { value: c.id }, c.path || c.name));
      sel("network_compartment_id").append(el("option", { value: c.id }, c.path || c.name));
    }
    // not a choice: the helper writes the volumes itself and boot volumes are AD-local, so the target
    // always lands in the helper's AD (the API enforces it as well)
    sel("availability_domain").value = options.helper_availability_domain;
    // VCN -> subnet: the subnet list is filtered by the selected VCN
    let netOptions = options;
    const fillSubnets = () => {
      const vcnId = sel("vcn_id").value;
      const subnets = netOptions.subnets.filter((s) => s.vcn_id === vcnId);
      sel("subnet_id").innerHTML = "";
      for (const s of subnets) sel("subnet_id").append(el("option", { value: s.id }, `${s.name} (${s.cidr_block})${s.prohibit_public_ip ? ", private" : ""}${s.availability_domain ? ", " + s.availability_domain : ""}`));
      document.getElementById("subnet-hint").textContent = subnets.length ? "" : (vcnId ? "No subnets in this VCN within the network compartment." : "Select a VCN first.");
      checkPrivateIp();
    };
    // fixed private IP: instant feedback that it fits the selected subnet's CIDR (the API additionally asks
    // OCI whether the address is free; the first two and the last address of a CIDR are reserved by OCI)
    const ipInput = sel("private_ip"); const ipHint = document.getElementById("private-ip-hint");
    const ipCheckBtn = document.getElementById("private-ip-check");
    const ipHintDefault = ipHint.textContent;
    const ipToInt = (ip) => { const m = /^\s*(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})\s*$/.exec(ip); if (!m) return null; const p = m.slice(1).map(Number); return p.some((x) => x > 255) ? null : ((p[0] << 24) | (p[1] << 16) | (p[2] << 8) | p[3]) >>> 0; };
    const setIpHint = (text, cls) => { ipHint.textContent = text; ipHint.classList.remove("error", "ok"); if (cls) ipHint.classList.add(cls); };
    // local (instant) part: syntax and CIDR fit; returns the problem text or "" when OCI may be asked
    const localIpProblem = () => {
      const raw = ipInput.value.trim(); const subnet = netOptions.subnets.find((s) => s.id === sel("subnet_id").value);
      const ip = ipToInt(raw); const [base, bits] = (subnet && subnet.cidr_block || "").split("/");
      if (ip === null) return "Enter an IPv4 address such as 10.0.1.25.";
      if (subnet && bits !== undefined) {
        const mask = bits === "0" ? 0 : (~0 << (32 - Number(bits))) >>> 0; const net = (ipToInt(base) & mask) >>> 0; const bcast = (net | (~mask >>> 0)) >>> 0;
        if ((ip & mask) >>> 0 !== net) return `${raw} is outside the subnet ${subnet.name} (${subnet.cidr_block}).`;
        if (ip === net || ip === net + 1 || ip === bcast) return `${raw} is reserved by OCI in ${subnet.cidr_block} (network address, gateway or broadcast).`;
      }
      return "";
    };
    // remote part: ask OCI (GET /api/oci/private-ip-check) whether the address is allocated; the answer is
    // tied to the exact ip+subnet it was given for, so any later edit invalidates it
    let ipChecked = null;  // { key, available }
    let ipCheckTimer = null; let ipCheckSeq = 0;
    const ipKey = () => `${ipInput.value.trim()}@${sel("subnet_id").value}`;
    const remoteIpCheck = async () => {
      const key = ipKey(); const [ip, subnetId] = key.split("@");
      if (!ip || !subnetId || localIpProblem()) return;
      const seq = ++ipCheckSeq;
      ipCheckBtn.disabled = true; setIpHint(`Checking with OCI whether ${ip} is free...`);
      try {
        const res = await api("GET", `/oci/private-ip-check?subnet_id=${encodeURIComponent(subnetId)}&ip=${encodeURIComponent(ip)}`);
        if (seq !== ipCheckSeq || key !== ipKey()) return;  // user typed on meanwhile
        ipChecked = { key, available: res.available };
        ipInput.setCustomValidity(res.available ? "" : res.message);
        setIpHint(res.message, res.available ? "ok" : "error");
      } catch (e) {
        if (seq !== ipCheckSeq || key !== ipKey()) return;
        ipChecked = null;
        setIpHint(`Could not check ${ip} with OCI: ${e.message}. You can retry with Check; the migration verifies it again.`, "error");
      } finally { if (key === ipKey()) ipCheckBtn.disabled = false; }
    };
    const checkPrivateIp = () => {
      clearTimeout(ipCheckTimer); ipCheckSeq++;
      const raw = ipInput.value.trim();
      ipInput.setCustomValidity("");
      if (!raw) { ipChecked = null; ipCheckBtn.disabled = true; setIpHint(ipHintDefault); return; }
      const problem = localIpProblem();
      if (problem) { ipChecked = null; ipCheckBtn.disabled = true; ipInput.setCustomValidity(problem); setIpHint(problem, "error"); return; }
      ipCheckBtn.disabled = false;
      if (ipChecked && ipChecked.key === ipKey()) {  // unchanged since the last answer
        if (!ipChecked.available) ipInput.setCustomValidity(ipHint.textContent);
        return;
      }
      const subnet = netOptions.subnets.find((s) => s.id === sel("subnet_id").value);
      setIpHint(`${raw} lies in ${subnet ? subnet.cidr_block : "the subnet"}; checking with OCI whether it is free...`);
      ipCheckTimer = setTimeout(remoteIpCheck, 700);  // a complete address usually means a typing pause
    };
    ipInput.addEventListener("input", checkPrivateIp);
    ipInput.addEventListener("blur", () => { if (ipCheckTimer && !localIpProblem() && ipInput.value.trim()) { clearTimeout(ipCheckTimer); remoteIpCheck(); } });
    ipCheckBtn.addEventListener("click", () => { clearTimeout(ipCheckTimer); ipChecked = null; remoteIpCheck(); });
    sel("subnet_id").addEventListener("change", checkPrivateIp);
    const fillNetworks = (o) => {
      netOptions = o;
      const vcnSel = sel("vcn_id");
      const previous = vcnSel.value;
      vcnSel.innerHTML = "";
      for (const v of o.vcns) vcnSel.append(el("option", { value: v.id }, `${v.name}${v.cidr_blocks.length ? " (" + v.cidr_blocks.join(", ") + ")" : ""}`));
      // VCNs that only show up through their subnets (VCN in another compartment)
      for (const s of o.subnets) if (!o.vcns.some((v) => v.id === s.vcn_id) && ![...vcnSel.options].some((op) => op.value === s.vcn_id)) vcnSel.append(el("option", { value: s.vcn_id }, s.vcn_name || s.vcn_id));
      if ([...vcnSel.options].some((op) => op.value === previous)) vcnSel.value = previous;
      fillSubnets();
    };
    sel("vcn_id").addEventListener("change", fillSubnets);
    fillNetworks(options);
    // shapes: the API lists flex VM shapes and bare metal shapes, x86 and Ampere (arch x86_64 / aarch64).
    // The migration form only offers x86 VM shapes (a vSphere guest is x86); the ISO form has an
    // instance_kind switch (VM / BM) and shows the Ampere shapes in their own group (aarch64 ISO needed).
    let shapes = options.shapes;
    let shapeOptions = options;
    const kindInput = () => form.elements.instance_kind;  // RadioNodeList, or undefined on the migration form
    const isoForm = !!kindInput();
    const currentKind = () => (kindInput() && kindInput().value) || "VM";
    const isArm = (s) => s && s.arch === "aarch64";
    const fillShapes = (o) => {
      shapeOptions = o;
      const kind = currentKind();
      shapes = o.shapes.filter((s) => (s.kind || "VM") === kind && (isoForm || !isArm(s)));
      const shapeSel = sel("shape"); const previous = shapeSel.value;
      shapeSel.innerHTML = "";
      const label = (s) => `${s.name}${!s.is_flex && s.max_ocpus != null ? ` - ${s.max_ocpus} OCPU / ${s.max_memory_gb} GB` : ""}`;
      const x86 = shapes.filter((s) => !isArm(s)), arm = shapes.filter(isArm);
      const x86Group = isoForm && arm.length ? el("optgroup", { label: kind === "BM" ? "x86 bare metal" : "x86" }) : shapeSel;
      if (kind === "VM") x86Group.append(el("option", { value: "" }, `${o.default_shape} (default)`));
      for (const s of x86) if (s.name !== o.default_shape) x86Group.append(el("option", { value: s.name }, label(s)));
      if (x86Group !== shapeSel) shapeSel.append(x86Group);
      if (arm.length) {
        const armGroup = el("optgroup", { label: kind === "BM" ? "Ampere Arm bare metal (aarch64 ISO)" : "Ampere Arm (aarch64 ISO)" });
        for (const s of arm) armGroup.append(el("option", { value: s.name }, label(s)));
        shapeSel.append(armGroup);
      }
      if ([...shapeSel.options].some((op) => op.value === previous)) shapeSel.value = previous;
      document.getElementById("shape-hint").textContent = "";
      if (kind === "BM" && !shapes.length) document.getElementById("shape-hint").textContent = "No bare metal shapes are available in this compartment and availability domain.";
      renderSizing();
    };
    // sizing mirrors mapping.map_shape: 2 vCPU = 1 OCPU, RAM rounded up to whole GB; both can be overridden.
    // Without a source VM (ISO form) the fields are required and only the shape bounds are applied.
    // Bare metal shapes have fixed cores and memory: the sizing inputs are hidden and not submitted.
    const sizing = cfg.sizing || null;
    const currentShape = () => shapes.find((s) => s.name === (sel("shape").value || shapeOptions.default_shape));
    const sizingRow = document.getElementById("sizing-row");
    const renderSizing = () => {
      const shape = currentShape();
      const ocpusIn = sel("ocpus"), memIn = sel("memory_gb");
      const fixed = currentKind() === "BM";
      if (sizingRow) {
        sizingRow.hidden = fixed;
        ocpusIn.required = memIn.required = !fixed && !sizing;
      }
      const armNote = isArm(shape) ? ` ${shape.name} is an Ampere (Arm) shape: the ISO must be an aarch64 build, UEFI firmware, no Secure Boot.` : "";
      if (fixed) {
        document.getElementById("sizing-hint").textContent = shape ? `Bare metal instance: ${shape.name} comes with ${shape.max_ocpus} OCPU / ${shape.max_memory_gb} GB, fixed.${armNote}` : "";
        return;
      }
      if (sizing) { ocpusIn.placeholder = `${sizing.autoOcpus} (auto)`; memIn.placeholder = `${sizing.autoMemoryGb} (auto)`; }
      if (shape && shape.is_flex) {
        if (shape.min_ocpus != null) ocpusIn.min = shape.min_ocpus;
        if (shape.max_ocpus != null) ocpusIn.max = shape.max_ocpus;
        if (shape.min_memory_gb != null) memIn.min = shape.min_memory_gb;
        if (shape.max_memory_gb != null) memIn.max = shape.max_memory_gb;
      } else { ocpusIn.removeAttribute("max"); memIn.removeAttribute("max"); }
      const range = shape && shape.is_flex && shape.max_ocpus != null
        ? ` ${shape.name} allows ${shape.min_ocpus ?? 1}-${shape.max_ocpus} OCPU and ${shape.min_memory_gb ?? 1}-${shape.max_memory_gb} GB.` : "";
      if (!sizing) {
        document.getElementById("shape-hint").textContent = (range + armNote).trim();
        document.getElementById("sizing-hint").textContent = `Instance will be launched with ${Number(ocpusIn.value) || "?"} OCPU / ${Number(memIn.value) || "?"} GB.`;
        return;
      }
      const ocpus = Number(ocpusIn.value) || sizing.autoOcpus, mem = Number(memIn.value) || sizing.autoMemoryGb;
      const overridden = ocpusIn.value !== "" || memIn.value !== "";
      document.getElementById("shape-hint").textContent = `${sizing.source}${range}`;
      document.getElementById("sizing-hint").textContent = overridden
        ? `Instance will be launched with ${ocpus} OCPU / ${mem} GB (custom). Leave both fields empty to size from the source VM.`
        : `Instance will be launched with ${sizing.autoOcpus} OCPU / ${sizing.autoMemoryGb} GB, derived from the source VM. Enter values to override.`;
    };
    sel("shape").addEventListener("change", renderSizing);
    sel("ocpus").addEventListener("input", renderSizing);
    sel("memory_gb").addEventListener("input", renderSizing);
    if (kindInput()) for (const r of kindInput()) r.addEventListener("change", () => fillShapes(shapeOptions));
    fillShapes(options);
    // both compartment pickers start at the helper's compartment; the instance compartment drives the shape
    // list, the network compartment the VCN/subnet list
    for (const name of ["compartment_id", "network_compartment_id"]) {
      if (options.compartments.some((c) => c.id === options.helper_compartment_id)) sel(name).value = options.helper_compartment_id;
      else if (options.compartments.length) sel(name).selectedIndex = 0;
    }
    const reloadOptions = async () => {
      formError.textContent = "";
      const q = new URLSearchParams({ compartment_id: sel("compartment_id").value, network_compartment_id: sel("network_compartment_id").value });
      try {
        const o = await api("GET", `/oci/options?${q}`);
        fillNetworks(o); fillShapes(o);
      } catch (e) { formError.textContent = e.message; }
    };
    sel("compartment_id").addEventListener("change", reloadOptions);
    sel("network_compartment_id").addEventListener("change", reloadOptions);

    // mirrors mapping.map_launch_options: paravirtualized unless "Maximum compatibility" or an override is chosen
    const renderPreview = () => {
      const compat = form.elements.compatibility_mode.checked;
      const bootOverride = form.elements.boot_volume_type_override.value, netOverride = form.elements.network_type_override.value;
      kv(document.getElementById("launch-preview"), [
        ["Firmware", cfg.firmwareText()],
        ["Boot volume type", bootOverride || (compat ? "IDE" : "PARAVIRTUALIZED")],
        ["Network type", netOverride || (compat ? "E1000" : "PARAVIRTUALIZED")],
      ]);
    };
    renderPreview();
    for (const name of ["compatibility_mode", "boot_volume_type_override", "network_type_override"]) {
      form.elements[name].addEventListener("change", renderPreview);
    }
    return { sel, renderSizing, renderPreview, currentShape };
  }

  // ----------------------------------------------------------- ISO instance view
  // Create an OCI instance that boots an installer ISO from Object Storage: bucket + ISO, OS metadata,
  // firmware and boot disk on the left; the usual OCI target form on the right.
  async function isoView() {
    app.innerHTML = "";
    app.append(tpl("tpl-iso"));
    const isoForm = document.getElementById("iso-form");
    const form = document.getElementById("target-form");
    const formError = document.getElementById("form-error");
    const submit = document.getElementById("submit-btn");

    let options, catalog;
    try {
      [options, catalog] = await Promise.all([api("GET", "/oci/options"), api("GET", "/oci/os-catalog")]);
    } catch (e) { if (e.status !== 401) showError("Cannot load OCI information: " + e.message); return; }

    const isel = (name) => isoForm.elements[name];
    const firmwareText = () => `${isel("firmware").value}${isel("secure_boot").checked ? " + Secure Boot" : ""}`;
    const { sel, renderSizing, renderPreview, currentShape } = wireTargetForm(form, options, { formError, sizing: null, firmwareText });
    renderSizing();

    // bucket compartment -> bucket -> ISO objects; the compartment list is the one of the options call
    const bucketHint = document.getElementById("bucket-hint"), isoHint = document.getElementById("iso-hint");
    const isoHintDefault = isoHint.textContent;
    let objects = []; let namespace = "";  // Object Storage namespace of the tenancy (comes with the bucket list)
    for (const c of options.compartments) isel("bucket_compartment_id").append(el("option", { value: c.id }, c.path || c.name));
    if (options.compartments.some((c) => c.id === options.helper_compartment_id)) isel("bucket_compartment_id").value = options.helper_compartment_id;
    const loadObjects = async () => {
      const bucket = isel("bucket").value; const objSel = isel("object_name");
      objSel.innerHTML = ""; objects = [];
      if (!bucket) { isoHint.textContent = "Select a bucket first."; return; }
      isoHint.textContent = "Listing the bucket...";
      try {
        objects = await api("GET", `/oci/objects?bucket=${encodeURIComponent(bucket)}`);
        for (const o of objects) objSel.append(el("option", { value: o.name }, `${o.name} (${fmtBytes(o.size_bytes)})`));
        isoHint.textContent = objects.length ? isoHintDefault : `No .iso objects in bucket ${bucket}. Upload the installer ISO to this bucket (OCI console > Object Storage) and click Refresh.`;
      } catch (e) { isoHint.textContent = e.message; }
      suggestName();
    };
    const loadBuckets = async () => {
      const comp = isel("bucket_compartment_id").value; const bSel = isel("bucket");
      bSel.innerHTML = ""; isel("object_name").innerHTML = "";
      bucketHint.textContent = "Listing buckets...";
      try {
        const buckets = await api("GET", `/oci/buckets?compartment_id=${encodeURIComponent(comp)}`);
        for (const b of buckets) bSel.append(el("option", { value: b.name }, b.name));
        if (buckets.length) namespace = buckets[0].namespace;
        bucketHint.textContent = buckets.length ? "" : "No buckets in this compartment.";
      } catch (e) { bucketHint.textContent = e.message; }
      await loadObjects();
    };
    isel("bucket_compartment_id").addEventListener("change", loadBuckets);
    isel("bucket").addEventListener("change", loadObjects);
    document.getElementById("iso-refresh").addEventListener("click", loadObjects);

    // OS family / version from the catalog; Windows shows the licensing fieldset and defaults to
    // "Maximum compatibility" (Windows Setup has no virtio drivers)
    const osSel = isel("operating_system"), verSel = isel("operating_system_version");
    const verLabel = document.getElementById("iso-os-version-label"), verHint = document.getElementById("iso-os-version-hint");
    for (const entry of catalog) osSel.append(el("option", { value: entry.operating_system }, entry.operating_system));
    const currentOs = () => catalog.find((e) => e.operating_system === osSel.value) || catalog[0];
    const isWin = () => currentOs().family === "windows";
    let compatTouched = false;
    const renderOs = () => {
      const entry = currentOs();
      verSel.innerHTML = "";
      if (entry.versions.length) {
        verLabel.hidden = false;
        for (const v of entry.versions) verSel.append(el("option", { value: v }, `${entry.operating_system} ${v}`));
        verSel.value = entry.versions[entry.versions.length - 1];
        verSel.required = true;
        verHint.textContent = "Release recorded on the OCI image; OCI uses it for OS-specific defaults.";
      } else {
        verLabel.hidden = true; verSel.required = false;
        verHint.textContent = "";
      }
      const win = isWin();
      document.getElementById("windows-fieldset").hidden = !win;
      document.getElementById("iso-windows-note").hidden = !win;
      if (!compatTouched) { form.elements.compatibility_mode.checked = win; renderPreview(); }
      renderLicense();
    };
    const renderLicense = () => {
      // OCI has no licenses for Windows 10/11; the API refuses OCI_PROVIDED for them
      const client = isWin() && /^Windows1[01]$/.test(verSel.value);
      const ociLic = form.querySelector('input[name="windows_license_type"][value="OCI_PROVIDED"]');
      ociLic.disabled = client;
      if (client) { ociLic.checked = false; form.querySelector('input[name="windows_license_type"][value="BRING_YOUR_OWN_LICENSE"]').checked = true; }
      document.getElementById("windows-license-hint").textContent = client
        ? "Windows 10/11: OCI does not provide licenses for client editions, so the instance is registered as BYOL."
        : "The instance is registered as Windows in OCI; the license type can be changed later in the OCI console.";
    };
    osSel.addEventListener("change", renderOs);
    verSel.addEventListener("change", renderLicense);
    form.elements.compatibility_mode.addEventListener("change", () => { compatTouched = true; });
    // Ubuntu is a sensible default for a Linux ISO
    if (catalog.some((e) => e.operating_system === "Ubuntu")) osSel.value = "Ubuntu";
    renderOs();

    // firmware / Secure Boot: Secure Boot only with UEFI; both feed the device model preview.  Ampere (Arm)
    // shapes boot with UEFI only and have no shielded instances: BIOS and Secure Boot are locked out then.
    const secure = isel("secure_boot"), secureLabel = document.getElementById("secure-boot-label");
    const firmwareRadios = [...isoForm.querySelectorAll('input[name="firmware"]')];
    const renderFirmware = () => {
      const shape = currentShape();
      const arm = !!(shape && shape.arch === "aarch64");
      if (arm) for (const r of firmwareRadios) { r.checked = r.value === "UEFI_64"; }
      for (const r of firmwareRadios) { r.disabled = arm && r.value !== "UEFI_64"; r.parentElement.classList.toggle("muted", r.disabled); }
      const uefi = isel("firmware").value === "UEFI_64";
      secure.disabled = !uefi || arm; if (secure.disabled) secure.checked = false;
      secureLabel.classList.toggle("muted", secure.disabled);
      renderPreview();
    };
    for (const r of firmwareRadios) r.addEventListener("change", renderFirmware);
    secure.addEventListener("change", renderPreview);
    sel("shape").addEventListener("change", renderFirmware);
    for (const r of form.querySelectorAll('input[name="instance_kind"]')) r.addEventListener("change", renderFirmware);
    renderFirmware();

    // instance name: proposed from the ISO file name until the user types one
    let nameTouched = false;
    const suggestName = () => {
      if (nameTouched) return;
      const file = (isel("object_name").value || "").split("/").pop().replace(/\.iso$/i, "");
      sel("display_name").value = file ? file.toLowerCase().replace(/[^a-z0-9-]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 60) : "";
    };
    isel("object_name").addEventListener("change", suggestName);
    sel("display_name").addEventListener("input", () => { nameTouched = sel("display_name").value !== ""; });
    await loadBuckets();

    // one submit for both forms: the ISO form is validated first (the submit button sits in the target form)
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      formError.textContent = "";
      if (!isoForm.reportValidity()) return;
      const fd = new FormData(form);
      const obj = objects.find((o) => o.name === isel("object_name").value);
      if (!obj) { formError.textContent = "Select the ISO file."; return; }
      const bucket = isel("bucket").value;
      const body = {
        iso: {
          namespace,
          bucket,
          object_name: obj.name,
          size_bytes: obj.size_bytes || 0,
          etag: obj.etag || "",
          operating_system: osSel.value,
          operating_system_version: verLabel.hidden ? "unknown" : verSel.value,
          firmware: isel("firmware").value,
          secure_boot: secure.checked,
          boot_disk_gb: Number(isel("boot_disk_gb").value),
        },
        target: {
          compartment_id: fd.get("compartment_id"),
          availability_domain: options.helper_availability_domain,
          subnet_id: fd.get("subnet_id"),
          private_ip: (fd.get("private_ip") || "").trim() || null,
          shape: fd.get("shape") || null,
          // bare metal shapes have fixed cores and memory; the sizing inputs are hidden then
          ocpus: fd.get("instance_kind") === "BM" ? null : Number(fd.get("ocpus")),
          memory_gb: fd.get("instance_kind") === "BM" ? null : Number(fd.get("memory_gb")),
          display_name: (fd.get("display_name") || "").trim(),
          assign_public_ip: fd.get("assign_public_ip") === "on",
          windows_license_type: isWin() ? fd.get("windows_license_type") : null,
          compatibility_mode: fd.get("compatibility_mode") === "on",
          boot_volume_type_override: fd.get("boot_volume_type_override") || null,
          network_type_override: fd.get("network_type_override") || null,
          volume_vpus_per_gb: Number(fd.get("volume_vpus_per_gb") || 10),
          rebuild_initramfs: false,
          fix_network: false,
        },
      };
      submit.disabled = true;
      try {
        const job = await api("POST", "/jobs/iso", body);
        location.hash = `#/jobs/${job.id}`;  // follow the image import and launch on the job page
      } catch (e) { formError.textContent = e.message; submit.disabled = false; }
    });
  }

  // ----------------------------------------------------------- OVA import view
  async function ovaView() {
    app.innerHTML = "";
    app.append(tpl("tpl-ova"));
    const ovaForm = document.getElementById("ova-form");
    const form = document.getElementById("ova-target-form");
    const formError = document.getElementById("ova-form-error");
    const submit = document.getElementById("ova-submit-btn");

    let options, catalog;
    try {
      [options, catalog] = await Promise.all([api("GET", "/oci/options"), api("GET", "/oci/os-catalog")]);
    } catch (e) { if (e.status !== 401) showError("Cannot load OCI information: " + e.message); return; }

    const osel = (name) => ovaForm.elements[name];
    const firmwareText = () => `${osel("firmware").value}${osel("secure_boot").checked ? " + Secure Boot" : ""}`;
    const { sel, renderSizing, renderPreview } = wireTargetForm(form, options, { formError, sizing: null, firmwareText, isoForm: false });
    renderSizing();

    const bucketHint = document.getElementById("ova-bucket-hint");
    const ovaHint = document.getElementById("ova-hint");
    const loadState = document.getElementById("ova-load-state");
    const uploadState = document.getElementById("ova-upload-state");
    let objects = []; let namespace = "";

    for (const c of options.compartments) osel("bucket_compartment_id").append(el("option", { value: c.id }, c.path || c.name));
    if (options.compartments.some((c) => c.id === options.helper_compartment_id)) {
      osel("bucket_compartment_id").value = options.helper_compartment_id;
    }

    const loadObjects = async () => {
      const bucket = osel("bucket").value;
      const objSel = osel("object_name");
      objSel.innerHTML = ""; objects = [];
      if (!bucket) { ovaHint.textContent = "Select a bucket first."; return; }
      ovaHint.textContent = "Listing the bucket...";
      try {
        objects = await api("GET", `/oci/ova-objects?bucket=${encodeURIComponent(bucket)}`);
        for (const o of objects) objSel.append(el("option", { value: o.name }, `${o.name} (${fmtBytes(o.size_bytes)})`));
        ovaHint.textContent = objects.length
          ? "Select one or more OVA/VMDK files (Ctrl+click)."
          : `No .ova, .ovf, or .vmdk objects in ${bucket}. Upload files or use the OCI console.`;
      } catch (e) { ovaHint.textContent = e.message; }
    };

    const loadBuckets = async () => {
      const comp = osel("bucket_compartment_id").value;
      const bSel = osel("bucket");
      bSel.innerHTML = ""; osel("object_name").innerHTML = "";
      bucketHint.textContent = "Listing buckets...";
      try {
        const buckets = await api("GET", `/oci/buckets?compartment_id=${encodeURIComponent(comp)}`);
        for (const b of buckets) bSel.append(el("option", { value: b.name }, b.name));
        if (buckets.length) namespace = buckets[0].namespace;
        bucketHint.textContent = buckets.length ? "" : "No buckets in this compartment.";
      } catch (e) { bucketHint.textContent = e.message; }
      await loadObjects();
    };

    osel("bucket_compartment_id").addEventListener("change", loadBuckets);
    osel("bucket").addEventListener("change", loadObjects);
    document.getElementById("ova-refresh").addEventListener("click", loadObjects);

    const osSel = osel("operating_system"), verSel = osel("operating_system_version");
    const verLabel = document.getElementById("ova-os-version-label");
    const verHint = document.getElementById("ova-os-version-hint");
    for (const entry of catalog) osSel.append(el("option", { value: entry.operating_system }, entry.operating_system));
    const renderOs = (preferredVersion) => {
      const entry = catalog.find((e) => e.operating_system === osSel.value) || catalog[0];
      const prev = preferredVersion || verSel.value;
      verSel.innerHTML = "";
      if (entry.versions.length) {
        verLabel.hidden = false;
        for (const v of entry.versions) verSel.append(el("option", { value: v }, v));
        verSel.required = true;
        if (prev && entry.versions.includes(prev)) verSel.value = prev;
        else verSel.value = entry.versions[entry.versions.length - 1];
        verHint.textContent = "Release recorded on the OCI image; pick the one installed in the guest if the OVF suggestion is wrong.";
        verLabel.classList.remove("attention");
      } else {
        verLabel.hidden = true; verSel.required = false;
        verHint.textContent = "";
      }
    };
    const currentOvaOs = () => catalog.find((e) => e.operating_system === osSel.value) || catalog[0];
    const ovaIsWin = () => currentOvaOs().family === "windows";
    const renderOvaLicense = () => {
      const client = ovaIsWin() && !verLabel.hidden && /^Windows1[01]$/.test(verSel.value);
      const ociLic = form.querySelector('input[name="windows_license_type"][value="OCI_PROVIDED"]');
      ociLic.disabled = client;
      if (client) {
        ociLic.checked = false;
        form.querySelector('input[name="windows_license_type"][value="BRING_YOUR_OWN_LICENSE"]').checked = true;
      }
      document.getElementById("ova-windows-license-hint").textContent = client
        ? "Windows 10/11: OCI does not provide licenses for client editions, so the instance is registered as BYOL."
        : "The instance is registered as Windows in OCI; the license type can be changed later in the OCI console.";
    };
    const syncOvaWindowsUi = () => {
      const isWin = ovaIsWin();
      document.getElementById("ova-windows-note").hidden = !isWin;
      document.getElementById("ova-windows-fieldset").hidden = !isWin;
      for (const id of ["ova-rebuild-initramfs-label", "ova-rebuild-initramfs-hint", "ova-fix-network-label", "ova-fix-network-hint"]) {
        document.getElementById(id).hidden = isWin;
      }
      if (isWin) renderOvaLicense();
    };
    osSel.addEventListener("change", () => { renderOs(); syncOvaWindowsUi(); });
    verSel.addEventListener("change", () => { verLabel.classList.toggle("attention", !verSel.value); renderOvaLicense(); });
    if (catalog.some((e) => e.operating_system === "Ubuntu")) osSel.value = "Ubuntu";
    renderOs();
    syncOvaWindowsUi();

    const applyOvaInspect = (info) => {
      const osEntry = catalog.find((e) => e.operating_system === info.operating_system);
      if (osEntry) {
        osSel.value = info.operating_system;
        renderOs(info.operating_system_version);
        if (!info.version_detected && !verLabel.hidden) {
          verSel.value = "";
          verLabel.classList.add("attention");
          verHint.textContent = "Could not determine the release from the OVF; select the version installed in the guest.";
        } else if (info.operating_system_version && osEntry.versions.includes(info.operating_system_version)) {
          verSel.value = info.operating_system_version;
        }
      }
      const fw = osel("firmware");
      if (info.firmware === "BIOS") fw.value = "BIOS";
      else fw.value = "UEFI_64";
      osel("secure_boot").checked = !!info.secure_boot;
      if (info.suggested_ocpus != null) sel("ocpus").value = String(info.suggested_ocpus);
      if (info.suggested_memory_gb != null) sel("memory_gb").value = String(info.suggested_memory_gb);
      renderSizing();
      renderPreview();
      const base = (info.object_name || "").replace(/\.(ova|ovf)$/i, "").replace(/[^\w.-]+/g, "-").slice(0, 200);
      if (base && !sel("display_name").value.trim()) sel("display_name").value = base;
      syncOvaWindowsUi();
      const parts = [];
      if (info.num_vcpu != null && info.memory_mb != null) {
        parts.push(`${info.num_vcpu} vCPU, ${Math.round(info.memory_mb / 1024 * 10) / 10} GB RAM`);
      }
      if (info.boot_disk_gb != null) parts.push(`boot disk ~${info.boot_disk_gb} GB`);
      if (info.disk_count > 1) parts.push(`${info.disk_count} disks`);
      const summary = parts.length ? `Suggested: ${parts.join("; ")}.` : "Form updated from OVF.";
      loadState.textContent = info.notes?.length ? `${summary} ${info.notes.join(" ")}` : summary;
      loadState.classList.remove("error");
    };

    document.getElementById("ova-load").addEventListener("click", async () => {
      const bucket = osel("bucket").value;
      const selected = [...osel("object_name").selectedOptions].map((o) => o.value);
      if (!bucket) { loadState.textContent = "Select a bucket first."; loadState.classList.add("error"); return; }
      const descriptor = selected.filter((n) => /\.(ova|ovf)$/i.test(n));
      if (descriptor.length !== 1) {
        loadState.textContent = "Select exactly one .ova or .ovf file to load its descriptor.";
        loadState.classList.add("error");
        return;
      }
      loadState.textContent = "Reading OVF from Object Storage...";
      loadState.classList.remove("error");
      try {
        const info = await api("GET", `/oci/ova-inspect?bucket=${encodeURIComponent(bucket)}&object_name=${encodeURIComponent(descriptor[0])}`);
        applyOvaInspect(info);
      } catch (e) {
        loadState.textContent = e.message;
        loadState.classList.add("error");
      }
    });

    document.getElementById("ova-upload").addEventListener("change", async (ev) => {
      const files = [...ev.target.files || []];
      if (!files.length) return;
      const bucket = osel("bucket").value;
      if (!bucket) { uploadState.textContent = "Select a bucket first."; return; }
      uploadState.textContent = "Uploading...";
      try {
        for (const file of files) {
          const par = await api("POST", "/oci/objects/upload-request", { bucket, object_name: file.name });
          namespace = par.namespace;
          const resp = await fetch(par.upload_url, { method: "PUT", body: file, headers: { "Content-Type": "application/octet-stream" } });
          if (!resp.ok) throw new Error(`upload ${file.name}: HTTP ${resp.status}`);
        }
        uploadState.textContent = `Uploaded ${files.length} file(s).`;
        await loadObjects();
      } catch (e) { uploadState.textContent = e.message; }
      ev.target.value = "";
    });

    await loadBuckets();

    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      formError.textContent = "";
      if (!ovaForm.reportValidity() || !form.reportValidity()) return;
      const entry = currentOvaOs();
      if (entry.versions.length && !verSel.value) {
        formError.textContent = `Select the ${entry.operating_system} release installed in the guest.`;
        verLabel.classList.add("attention");
        return;
      }
      if (entry.versions.length && verSel.value && !entry.versions.includes(verSel.value)) {
        formError.textContent = `Select a ${entry.operating_system} release from the list (OCI catalog).`;
        return;
      }
      const selected = [...osel("object_name").selectedOptions].map((o) => o.value);
      if (!selected.length) { formError.textContent = "Select at least one OVA file."; return; }
      const fd = new FormData(form);
      const bucket = osel("bucket").value;
      const baseTarget = {
        compartment_id: fd.get("compartment_id"),
        availability_domain: options.helper_availability_domain,
        subnet_id: fd.get("subnet_id"),
        private_ip: (fd.get("private_ip") || "").trim() || null,
        shape: fd.get("shape") || null,
        ocpus: fd.get("instance_kind") === "BM" ? null : Number(fd.get("ocpus")),
        memory_gb: fd.get("instance_kind") === "BM" ? null : Number(fd.get("memory_gb")),
        assign_public_ip: fd.get("assign_public_ip") === "on",
        compatibility_mode: fd.get("compatibility_mode") === "on",
        boot_volume_type_override: fd.get("boot_volume_type_override") || null,
        network_type_override: fd.get("network_type_override") || null,
        volume_vpus_per_gb: Number(fd.get("volume_vpus_per_gb") || 10),
        rebuild_initramfs: !ovaIsWin() && fd.get("rebuild_initramfs") === "on",
        fix_network: !ovaIsWin() && fd.get("fix_network") === "on",
        windows_license_type: ovaIsWin() ? fd.get("windows_license_type") : null,
        start_after_migration: fd.get("start_after_migration") === "on",
      };
      submit.disabled = true;
      try {
        let firstId = null;
        for (let i = 0; i < selected.length; i++) {
          const obj = objects.find((o) => o.name === selected[i]);
          const body = {
            ova: {
              namespace,
              bucket,
              object_name: selected[i],
              size_bytes: obj ? obj.size_bytes : 0,
              etag: obj ? obj.etag : "",
              operating_system: osSel.value,
              operating_system_version: verLabel.hidden ? "unknown" : verSel.value,
              firmware: osel("firmware").value,
              secure_boot: osel("secure_boot").checked,
            },
            target: {
              ...baseTarget,
              display_name: selected.length > 1
                ? `${(fd.get("display_name") || "").trim()}-${i + 1}`
                : (fd.get("display_name") || "").trim(),
            },
          };
          const job = await api("POST", "/jobs/ova", body);
          if (!firstId) firstId = job.id;
        }
        location.hash = `#/jobs/${firstId}`;
      } catch (e) { formError.textContent = e.message; submit.disabled = false; }
    });
  }

  // ---------------------------------------------------------------- jobs view
  const JOBS_PAGE_SIZE = 25;
  async function jobsView() {
    app.innerHTML = "";
    app.append(tpl("tpl-jobs"));
    const search = document.getElementById("jobs-filter"), phaseSel = document.getElementById("jobs-phase");
    const count = document.getElementById("jobs-count"), err = document.getElementById("jobs-error");
    const body = document.getElementById("jobs-body");
    const pager = document.getElementById("jobs-pager"), pageInfo = document.getElementById("jobs-page-info");
    const prevBtn = document.getElementById("jobs-prev"), nextBtn = document.getElementById("jobs-next");
    // the filters survive the periodic refresh and a return to this page
    search.value = state.jobsFilter.q; phaseSel.value = state.jobsFilter.phase;
    let jobs = [];
    let page = state.jobsFilter.page || 0;

    const matches = (j) => {
      const p = phaseSel.value;
      if (p === "ACTIVE" ? TERMINAL.includes(j.phase) : p && j.phase !== p) return false;
      const q = search.value.trim().toLowerCase();
      const target = j.instance_display_name || j.target.display_name || "";
      const extra = j.iso ? j.iso.bucket : j.ova ? j.ova.bucket : j.ova_export ? `${j.ova_export.bucket} ${j.ova_export.prefix}` : j.azure ? `${j.azure.resource_group} ${j.azure.subscription_name}` : j.olvm ? `${j.olvm.engine_host} ${j.olvm.cluster}` : "";
      return !q || `${sourceTypeLabel(j)} ${sourceName(j)} ${extra} ${target}`.toLowerCase().includes(q);
    };

    // fixed layout (see style.css): the message column takes what the others leave
    const columns = [["Source", "17%"], ["Phase", "112px"], ["Message", null], ["OCI instance", "14%"], ["Job", "19%"], ["By", "11%", "by"], ["", "84px"]];
    // source -> target name; the target is the launched instance's name, else what the form asked for.
    // ISO jobs show bucket/file.iso as the source, Azure jobs the VM name with its resource group
    const vmCell = (j) => {
      const sourceVm = j.iso ? `${j.iso.bucket}/${j.iso.object_name}`
        : j.ova ? `${j.ova.bucket}/${j.ova.object_name}`
          : j.ova_export ? (j.ova_export.instance_name || sourceName(j))
            : sourceName(j);
      const targetVm = j.ova_export
        ? (j.ova_export.ovf_object || `${j.ova_export.bucket}/${j.ova_export.prefix || ""}`)
        : (j.instance_display_name || j.target.display_name || sourceName(j));
      const where = j.azure ? el("span", { class: "muted" }, ` (${j.azure.resource_group})`)
        : j.olvm && j.olvm.cluster ? el("span", { class: "muted" }, ` (${j.olvm.cluster})`) : null;
      const type = sourceTypeLabel(j);
      return el("td", { class: "name", title: j.azure ? `${type}\n${sourceVm}\n${targetVm}\n${j.azure.subscription_name || j.azure.subscription_id}/${j.azure.resource_group}` : `${type}\n${sourceVm}\n${targetVm}` },
        el("div", { class: "job-source" },
          el("div", { class: "job-source-line job-source-type" }, el("span", { class: "source-type-box", title: "Source platform" }, type)),
          el("div", { class: "job-source-line job-source-vm" }, jobEndpointIcon("source"), el("span", { class: "job-source-text" }, sourceVm, where)),
          el("div", { class: "job-source-line job-source-target muted" }, jobEndpointIcon("target"), el("span", { class: "job-source-text" }, targetVm))));
    };
    // start / end / duration / average transfer speed of the migration
    const migrationCell = (j) => {
      const sm = j.summary || {}; const done = TERMINAL.includes(j.phase);
      const when = (iso) => new Date(iso).toLocaleString(undefined, { dateStyle: "short", timeStyle: "short" });
      const lines = [el("div", {}, el("span", { class: "muted" }, "Started "), when(j.created_at))];
      if (done && j.finished_at) lines.push(el("div", {}, el("span", { class: "muted" }, "Finished "), when(j.finished_at)));
      const figures = [(done ? "" : "running ") + fmtDuration(sm.duration_s)];
      const bps = sm.average_bps || (!done && j.transfer ? j.transfer.throughput_bps : null);
      if (bps) figures.push(`${fmtBytes(bps)}/s${sm.average_bps ? " avg" : ""}`);
      lines.push(el("div", { class: "muted" }, figures.join(" \u00b7 ")));
      return el("td", { class: "migration" }, ...lines);
    };
    const row = (j) => el("tr", {},
      vmCell(j), el("td", {}, el("span", { class: "phase " + j.phase }, j.phase)),
      el("td", {}, (j.message || "") + (j.phase === "EXPORTING" && j.transfer && j.transfer.started_at
        ? ` - ${j.transfer.percent || 0}%${j.transfer.throughput_bps ? ", " + fmtRate(j.transfer.throughput_bps) : ""}`
        : !TERMINAL.includes(j.phase) && j.step_percent !== null && j.step_percent !== undefined && !/\d+%/.test(j.message || "")
          ? ` - ${j.step_percent}%` : "")),
      el("td", { class: "ocid", title: j.instance_id || "" }, ocidLink("instances", j.instance_id)),
      migrationCell(j), el("td", { class: "by" }, j.created_by || "-"),
      el("td", { class: "row-actions" }, el("a", { class: "button secondary small", href: `#/jobs/${j.id}` }, "Details")));

    const render = () => {
      const filtered = jobs.filter(matches);
      const pages = Math.max(1, Math.ceil(filtered.length / JOBS_PAGE_SIZE));
      page = Math.min(page, pages - 1);
      const start = page * JOBS_PAGE_SIZE, visible = filtered.slice(start, start + JOBS_PAGE_SIZE);
      state.jobsFilter = { q: search.value, phase: phaseSel.value, page };
      body.innerHTML = "";
      if (!jobs.length) {
        body.append(el("div", { class: "muted" }, state.me && state.me.anonymous
          ? "No jobs yet. Use New ISO instance to start one."
          : hasAzure(state.me) ? "No jobs yet. Pick a VM under Azure VMs to start one."
            : hasGcp(state.me) ? "No jobs yet. Pick a VM under Google Cloud VMs to start one."
              : hasAws(state.me) ? "No jobs yet. Pick an instance under AWS VMs to start one."
                : "No jobs yet. Pick a VM under Source VMs, or create an instance from an ISO, to start one."));
      } else {
        body.append(el("table", { class: "jobs" },
          el("colgroup", {}, ...columns.map(([, w, cls]) => el("col", { style: w ? `width:${w}` : null, class: cls || null }))),
          el("thead", {}, el("tr", {}, ...columns.map(([h, , cls]) => el("th", { class: cls || null }, h)))),
          el("tbody", {}, ...visible.map(row),
            visible.length ? null : el("tr", {}, el("td", { colspan: columns.length, class: "muted" }, "No jobs match the filters.")))));
      }
      count.textContent = filtered.length === jobs.length ? `${jobs.length} job${jobs.length === 1 ? "" : "s"}` : `${filtered.length} of ${jobs.length} jobs`;
      pager.hidden = filtered.length <= JOBS_PAGE_SIZE && page === 0;
      pageInfo.textContent = filtered.length ? `${start + 1}-${Math.min(start + JOBS_PAGE_SIZE, filtered.length)} of ${filtered.length} (page ${page + 1} of ${pages})` : "";
      prevBtn.disabled = page === 0;
      nextBtn.disabled = page >= pages - 1;
    };
    const resetPage = () => { page = 0; render(); };

    let timer = null;
    const load = async () => {
      try { jobs = await api("GET", "/jobs"); err.textContent = ""; }
      catch (e) { if (e.status === 401) return; err.textContent = e.message; }
      render();
      // keep the table live while jobs are active (the filters and the page are kept)
      if (jobs.some((j) => !TERMINAL.includes(j.phase))) timer = setTimeout(() => { if (location.hash === "#/jobs") load(); }, 5000);
    };
    search.addEventListener("input", resetPage);
    phaseSel.addEventListener("change", resetPage);
    prevBtn.addEventListener("click", () => { page = Math.max(0, page - 1); render(); });
    nextBtn.addEventListener("click", () => { page += 1; render(); });
    activePoll = () => clearTimeout(timer);
    await load();
  }

  async function jobDetailView(jobId) {
    app.innerHTML = "";
    const c = el("div", { class: "card" });
    app.append(el("div", { class: "toolbar" }, el("a", { href: "#/jobs", class: "muted" }, "\u2190 all jobs")), c);
    activePoll = pollJob(jobId, c);
  }

  // ------------------------------------------------------------ remote console
  // OCI instance console connection (created by the helper with a temporary key) -> SSH tunnel on the helper
  // -> WebSocket on this origin -> noVNC in this page.  The connection is deleted on Close or when idle.
  // ``target`` is {kind: "job", id} (console of a migration / ISO job) or {kind: "instance", id} (any instance,
  // from the OCI Remote Console page); both map onto the same API shape under /jobs/{id} or /instances/{id}.
  async function consoleView(target) {
    app.innerHTML = "";
    app.append(tpl("tpl-console"));
    const status = document.getElementById("console-status"), errBox = document.getElementById("console-error");
    const screen = document.getElementById("vnc-screen");
    const cadBtn = document.getElementById("console-cad"), reconnectBtn = document.getElementById("console-reconnect");
    const closeBtn = document.getElementById("console-close"), back = document.getElementById("console-back");
    const fkeySel = document.getElementById("console-fkey"), enterBtn = document.getElementById("console-enter");
    // F1..F12: X11 keysyms XK_F1 (0xFFBE) .. XK_F12 (0xFFC9), with the matching DOM key codes
    for (let n = 1; n <= 12; n++) fkeySel.append(el("option", { value: String(0xFFBD + n), "data-code": `F${n}` }, `F${n}`));
    const isJob = target.kind === "job";
    const base = `/${isJob ? "jobs" : "instances"}/${encodeURIComponent(target.id)}`;
    const backHash = isJob ? `#/jobs/${target.id}` : "#/instances";
    back.href = backHash; back.textContent = isJob ? "\u2190 job" : "\u2190 instances";
    let rfb = null; let stopped = false; let closing = false;
    const setStatus = (text, ok) => { status.textContent = text; status.className = "console-status" + (ok ? " ok" : " muted"); };
    const setError = (text) => { errBox.textContent = text || ""; };

    if (isJob) {
      let job;
      try { job = await api("GET", base); }
      catch (e) { if (e.status !== 401) showError(e.message); return; }
      document.getElementById("console-title").textContent = `- ${job.instance_display_name || sourceName(job)}`;
      if (!hasConsole(job)) { setStatus("", false); setError("The remote console is available for completed migrations and running ISO installations with an OCI instance."); closeBtn.hidden = true; return; }
    } else {
      let inst;
      try { inst = await api("GET", base); }
      catch (e) { if (e.status === 401) return; setStatus("", false); setError(e.message); closeBtn.hidden = true; return; }
      document.getElementById("console-title").textContent = `- ${inst.name} (${inst.lifecycle_state}${inst.shape ? ", " + inst.shape : ""})`;
    }

    // 1. console connection on the OCI side (idempotent while one is active)
    const openConnection = async () => {
      setStatus("Creating the OCI console connection...", false);
      let st;
      try { st = await api("POST", `${base}/console`); }
      catch (e) {
        if (e.status === 409 && e.detail && e.detail.code === "foreign_connection") {
          if (!confirm(`${e.detail.message}\n\nReplace the existing console connection ${e.detail.connection_id}?`)) throw new Error("An existing console connection is in the way; nothing was changed.");
          st = await api("POST", `${base}/console?replace=true`);
        } else throw e;
      }
      while (st.state === "CREATING" && !stopped) {
        await new Promise((r) => setTimeout(r, 2000));
        st = await api("GET", `${base}/console`);
      }
      if (st.state !== "ACTIVE") throw new Error(st.error || `console connection is ${st.state}`);
      return st;
    };

    // 2. noVNC over the helper's WebSocket bridge
    const connectVnc = async () => {
      const { default: RFB } = await import("./vendor/novnc/core/rfb.js");
      const url = `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api${base}/console/vnc`;
      setStatus("Connecting to the instance console...", false);
      screen.innerHTML = "";
      rfb = new RFB(screen, url, {});
      rfb.scaleViewport = true;
      rfb.resizeSession = false;
      rfb.background = "#000";
      rfb.addEventListener("connect", () => { setStatus("Connected", true); setError(""); cadBtn.disabled = enterBtn.disabled = fkeySel.disabled = false; reconnectBtn.disabled = true; rfb.focus(); });
      rfb.addEventListener("disconnect", (ev) => {
        cadBtn.disabled = enterBtn.disabled = fkeySel.disabled = true; reconnectBtn.disabled = stopped;
        if (closing) return;
        setStatus(ev.detail.clean ? "Disconnected" : "Connection lost", false);
        if (!ev.detail.clean) setError("The console connection dropped (the migration tool logs the reason; the instance may be rebooting or the tunnel was refused). Use Reconnect to try again.");
      });
      rfb.addEventListener("securityfailure", (ev) => setError(`VNC security failure: ${ev.detail.reason || ev.detail.status}`));
      rfb.addEventListener("credentialsrequired", () => setError("The VNC server asked for credentials; the OCI console does not normally do this."));
    };

    const start = async () => {
      setError(""); reconnectBtn.disabled = true;
      try { await openConnection(); if (!stopped) await connectVnc(); }
      catch (e) { if (e.status === 401) return; setStatus("Not connected", false); setError(e.message); reconnectBtn.disabled = false; }
    };
    cadBtn.onclick = () => { if (rfb) rfb.sendCtrlAltDel(); };
    // Enter: XK_Return (0xFF0D) press + release, focus back to the screen
    enterBtn.onclick = () => { if (rfb) { rfb.sendKey(0xFF0D, "Enter"); rfb.focus(); } };
    // press + release of the chosen function key, then back to the placeholder and focus to the screen
    fkeySel.onchange = () => {
      const opt = fkeySel.selectedOptions[0];
      if (rfb && opt && opt.value) rfb.sendKey(Number(opt.value), opt.dataset.code);
      fkeySel.value = "";
      if (rfb) rfb.focus();
    };
    reconnectBtn.onclick = () => { if (rfb) { try { rfb.disconnect(); } catch (_) { /* already gone */ } rfb = null; } start(); };
    closeBtn.onclick = async () => {
      if (!confirm("Close the remote console and delete the OCI console connection?")) return;
      closing = true; closeBtn.disabled = true; setStatus("Closing...", false);
      if (rfb) { try { rfb.disconnect(); } catch (_) { /* ignore */ } rfb = null; }
      try { await api("DELETE", `${base}/console`); } catch (e) { alert(e.message); }
      location.hash = backHash;
    };
    // leaving the view (hash change) disconnects the VNC session; the console connection stays for a quick
    // return and is removed by the helper's idle timeout
    activePoll = () => { stopped = true; if (rfb) { try { rfb.disconnect(); } catch (_) { /* ignore */ } rfb = null; } };
    await start();
  }

  // ------------------------------------------------------- OCI Remote Console: instance picker
  // Instances of a compartment (compute ListInstances) or found by name (Resource Search); "Console" opens the
  // same noVNC view as a job's console, keyed by the instance OCID.  The chosen compartment and the last search
  // are kept in ``state`` so coming back from a console shows the same list.
  async function ovaExportView() {
    app.innerHTML = "";
    app.append(tpl("tpl-ova-export"));
    const form = document.getElementById("ova-export-form");
    const formError = document.getElementById("ova-export-form-error");
    const submit = document.getElementById("ova-export-submit");
    const compSel = form.elements.instance_compartment_id;
    const bucketCompSel = form.elements.bucket_compartment_id;
    const bucketSel = form.elements.bucket;
    const search = document.getElementById("ova-export-search");
    const statusEl = document.getElementById("ova-export-inst-status");
    const rows = document.getElementById("ova-export-inst-rows");
    const bucketHint = document.getElementById("ova-export-bucket-hint");
    let selectedId = "";
    let seq = 0;

    let options;
    try { options = await api("GET", "/oci/options"); }
    catch (e) { if (e.status !== 401) showError("Cannot load OCI information: " + e.message); return; }

    for (const c of options.compartments) {
      compSel.append(el("option", { value: c.id }, c.path || c.name));
      bucketCompSel.append(el("option", { value: c.id }, c.path || c.name));
    }
    if (options.compartments.some((c) => c.id === options.helper_compartment_id)) {
      compSel.value = options.helper_compartment_id;
      bucketCompSel.value = options.helper_compartment_id;
    }

    const exportable = (i) => i.lifecycle_state === "RUNNING" || i.lifecycle_state === "STOPPED";
    const render = (list, what) => {
      rows.innerHTML = "";
      if (!list.length) { statusEl.textContent = `No instances ${what}.`; return; }
      statusEl.textContent = `${list.length} instance${list.length === 1 ? "" : "s"} ${what}.`;
      for (const i of list) {
        const can = exportable(i) && i.id !== options.helper_instance_id;
        const radio = el("input", { type: "radio", name: "instance_id", value: i.id, disabled: can ? null : "disabled" });
        if (i.id === selectedId && can) radio.checked = true;
        radio.addEventListener("change", () => { selectedId = i.id; if (!form.elements.prefix.value) form.elements.prefix.placeholder = i.name; });
        rows.append(el("tr", {},
          el("td", {}, radio),
          el("td", { class: "name", title: i.id }, i.name),
          el("td", {}, el("span", { class: `power ${i.lifecycle_state}` }, i.lifecycle_state)),
          el("td", {}, i.shape || "\u2013"),
          el("td", { title: i.compartment_id }, i.compartment_path || i.compartment_id)));
      }
    };
    const load = async (fn, what) => {
      const my = ++seq;
      statusEl.textContent = "Loading...";
      try {
        const list = await fn();
        if (my === seq) render(list, what);
      } catch (e) {
        if (my !== seq) return;
        if (e.status === 401) return;
        rows.innerHTML = "";
        statusEl.textContent = "";
        formError.textContent = e.message;
      }
    };
    const listCompartment = () => load(
      () => api("GET", `/instances?compartment_id=${encodeURIComponent(compSel.value)}`),
      "in the selected compartment",
    );
    const doSearch = () => {
      const q = search.value.trim();
      if (!q) return listCompartment();
      return load(() => api("GET", `/instances/search?q=${encodeURIComponent(q)}`), `named like "${q}"`);
    };
    const loadBuckets = async () => {
      const comp = bucketCompSel.value;
      bucketSel.innerHTML = "";
      if (!comp) return;
      bucketHint.textContent = "Listing buckets...";
      try {
        const buckets = await api("GET", `/oci/buckets?compartment_id=${encodeURIComponent(comp)}`);
        for (const b of buckets) bucketSel.append(el("option", { value: b.name || b }, b.name || b));
        bucketHint.textContent = buckets.length ? "OVF, VMDK and manifest objects are written here." : "No buckets in this compartment.";
      } catch (e) {
        if (e.status !== 401) bucketHint.textContent = e.message;
      }
    };
    compSel.addEventListener("change", () => { search.value = ""; listCompartment(); });
    document.getElementById("ova-export-refresh").addEventListener("click", () => { search.value = ""; listCompartment(); });
    document.getElementById("ova-export-search-btn").addEventListener("click", (ev) => { ev.preventDefault(); doSearch(); });
    bucketCompSel.addEventListener("change", loadBuckets);
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      formError.textContent = "";
      const picked = form.querySelector("input[name=instance_id]:checked");
      if (!picked) { formError.textContent = "Select an instance to export."; return; }
      if (!bucketSel.value) { formError.textContent = "Select a destination bucket."; return; }
      submit.disabled = true;
      try {
        const job = await api("POST", "/jobs/ova-export", {
          instance_id: picked.value,
          bucket: bucketSel.value,
          prefix: form.elements.prefix.value.trim() || null,
          include_data_volumes: form.elements.include_data_volumes.checked,
        });
        location.hash = `#/jobs/${job.id}`;
      } catch (e) {
        formError.textContent = e.message;
        submit.disabled = false;
      }
    });
    await Promise.all([listCompartment(), loadBuckets()]);
  }

  async function instancesView() {
    app.innerHTML = "";
    app.append(tpl("tpl-instances"));
    const compSel = document.getElementById("inst-compartment"), search = document.getElementById("inst-search");
    const form = document.getElementById("instances-form"), refreshBtn = document.getElementById("inst-refresh");
    const searchBtn = document.getElementById("inst-search-btn");
    const statusEl = document.getElementById("inst-status"), errBox = document.getElementById("inst-error");
    const rows = document.getElementById("inst-rows");
    const setStatus = (t) => { statusEl.textContent = t || ""; };
    const setError = (t) => { errBox.textContent = t || ""; };
    const busy = (on) => { searchBtn.disabled = refreshBtn.disabled = compSel.disabled = on; };
    let seq = 0;  // ignore late responses of a superseded listing / search

    const render = (list, what) => {
      rows.innerHTML = "";
      if (!list.length) { setStatus(`No instances ${what}.`); return; }
      setStatus(`${list.length} instance${list.length === 1 ? "" : "s"} ${what}.`);
      for (const i of list) {
        const name = el("td", { class: "name" }, el("a", { href: consoleUrl("instances", i.id), target: "_blank", rel: "noopener", title: `${i.id}\nOpen in the OCI console` }, i.name));
        if (i.job_id) name.append(" ", el("a", { href: `#/jobs/${i.job_id}`, class: "muted", title: "Created by this migration tool job" }, "(job)"));
        rows.append(el("tr", {},
          name,
          el("td", {}, el("span", { class: `power ${i.lifecycle_state}` }, i.lifecycle_state)),
          el("td", {}, i.shape || "\u2013"),
          el("td", { title: i.compartment_id }, i.compartment_path || i.compartment_id),
          el("td", {}, i.availability_domain || "\u2013"),
          el("td", { class: "row-actions" },
            el("a", { class: "button primary small", href: `#/instances/${encodeURIComponent(i.id)}/console`,
              title: "Create an instance console connection and open the VNC console" }, "Console"))));
      }
    };

    const load = async (fn, what) => {
      const my = ++seq; busy(true); setError(""); setStatus("Loading...");
      try { const list = await fn(); if (my === seq) render(list, what); }
      catch (e) { if (my !== seq) return; if (e.status === 401) return; rows.innerHTML = ""; setStatus(""); setError(e.message); }
      finally { if (my === seq) busy(false); }
    };
    const listCompartment = () => {
      state.instancesCompartment = compSel.value; state.instancesQuery = "";
      const label = compSel.selectedOptions[0] ? compSel.selectedOptions[0].textContent : "";
      return load(() => api("GET", `/instances?compartment_id=${encodeURIComponent(compSel.value)}`), `in ${label}`);
    };
    const doSearch = () => {
      const q = search.value.trim();
      if (!q) return listCompartment();
      state.instancesQuery = q;
      return load(() => api("GET", `/instances/search?q=${encodeURIComponent(q)}`), `named like "${q}" (all compartments)`);
    };

    // compartments first; the migration tool's own compartment is preselected (or the one chosen last time)
    let comps;
    try { comps = await api("GET", "/oci/compartments"); }
    catch (e) { if (e.status !== 401) setError(e.message); return; }
    for (const c of comps) compSel.append(el("option", { value: c.id }, c.path || c.name));
    const preferred = state.instancesCompartment || state.ownCompartment;
    if (preferred && comps.some((c) => c.id === preferred)) compSel.value = preferred;

    compSel.onchange = () => { search.value = ""; listCompartment(); };
    refreshBtn.onclick = () => { search.value = ""; listCompartment(); };
    form.onsubmit = (ev) => { ev.preventDefault(); doSearch(); };
    if (state.instancesQuery) { search.value = state.instancesQuery; await doSearch(); }
    else await listCompartment();
  }

  // --------------------------------------------------------------- setup view
  async function setupView() {
    app.innerHTML = "";
    app.append(tpl("tpl-setup"));
    const swKv = document.getElementById("sw-kv"); const swState = document.getElementById("sw-state");
    const swErr = document.getElementById("sw-error"); const swBtn = document.getElementById("sw-update");
    const swLog = document.getElementById("sw-log"); const swLogDetails = document.getElementById("sw-log-details");
    const short = (sha) => (sha || "").slice(0, 10);
    const when = (iso) => (iso ? new Date(iso).toLocaleString() : "");
    let timer = null;
    let watching = false; // update triggered: keep the page locked until the helper comes back
    let lastRemote = {};  // latest_* fields survive refreshes done with check=false

    const renderSoftware = (sw) => {
      if (sw.latest_commit) lastRemote = { latest_commit: sw.latest_commit, latest_date: sw.latest_date, latest_subject: sw.latest_subject, update_available: sw.update_available };
      else if (!sw.check_error) sw = { ...lastRemote, ...sw, latest_commit: lastRemote.latest_commit || "", latest_date: lastRemote.latest_date || "", latest_subject: lastRemote.latest_subject || "", update_available: lastRemote.update_available ?? null };
      if (watching) sw = { ...sw, update_running: true, can_update: false, reason: "" };
      const badge = sw.update_running ? el("span", { class: "badge warn" }, "update running")
        : sw.update_available === true ? el("span", { class: "badge warn" }, "update available")
        : sw.update_available === false ? el("span", { class: "badge ok" }, "up to date")
        : el("span", { class: "badge" }, sw.install_method === "source" ? "unknown" : sw.install_method);
      const rows = [["Installed version", `${sw.version}`], ["Status", badge]];
      if (sw.install_method === "source") {
        rows.push(["Installed commit", `${short(sw.commit)}${sw.commit_date ? " (" + when(sw.commit_date) + ")" : ""}${sw.commit_subject ? " - " + sw.commit_subject : ""}`]);
        rows.push(["Latest on " + (sw.branch || "remote"), sw.latest_commit ? `${short(sw.latest_commit)}${sw.latest_date ? " (" + when(sw.latest_date) + ")" : ""}${sw.latest_subject ? " - " + sw.latest_subject : ""}` : (sw.check_error || "-")]);
        rows.push(["Repository", sw.repo_url ? el("a", { href: sw.repo_url, target: "_blank", rel: "noopener" }, sw.repo_url) : sw.source_dir]);
      }
      kv(swKv, rows);
      swErr.textContent = sw.can_update ? "" : (sw.reason || "");
      swBtn.hidden = sw.install_method !== "source";
      const forceOk = Boolean(sw.can_force) || (sw.active_jobs > 0 && sw.install_method === "source" && !sw.update_running);
      swBtn.disabled = watching || !(sw.can_update || forceOk);
      swBtn.textContent = forceOk && !sw.can_update
        ? "Update anyway"
        : (sw.update_available === false ? "Reinstall current version" : "Update now");
      swLogDetails.hidden = !sw.log;
      swLog.textContent = sw.log || "";
      if (sw.update_running) { swLogDetails.open = true; swLog.scrollTop = swLog.scrollHeight; }
      return sw;
    };

    const loadSoftware = async (check) => {
      swState.textContent = check ? "Checking GitHub..." : "";
      try { const sw = renderSoftware(await api("GET", "/setup/software?check=" + (check ? "true" : "false"))); swState.textContent = ""; return sw; }
      catch (e) { if (e.status !== 401) swErr.textContent = e.message; swState.textContent = ""; return null; }
    };

    // after the update is triggered the service restarts: watch /api/health until the commit changes,
    // then send the user back to the login page (sessions do not survive a restart)
    const watchRestart = (oldCommit) => {
      const started = Date.now();
      const tick = async () => {
        try {
          const r = await fetch("../api/health", { cache: "no-store" });
          if (r.ok) {
            const h = await r.json();
            if (h.commit && h.commit !== oldCommit) { swState.textContent = `Updated to ${short(h.commit)}; please log in again.`; setTimeout(showStart, 1500); return; }
          }
        } catch (_) { swState.textContent = "The migration tool is restarting..."; }
        if (Date.now() - started > 15 * 60 * 1000) { swState.textContent = "The update is taking unusually long; check the update log or the service journal."; return; }
        const sw = await loadSoftware(false).catch(() => null);
        if (sw) swState.textContent = "Update running; waiting for the migration tool to restart...";
        if (sw && sw.log && /UPDATE FAILED/.test(sw.log.split("update started").pop())) { watching = false; renderSoftware(sw); swState.textContent = "The update failed; see the log."; return; }
        timer = setTimeout(tick, 3000);
      };
      timer = setTimeout(tick, 3000);
    };

    swBtn.addEventListener("click", async () => {
      const sw = await loadSoftware(false);
      if (!sw) return;
      const force = sw.active_jobs > 0;
      const msg = force
        ? `${sw.active_jobs} migration(s) are still running. Updating restarts the service and those jobs will fail. After login, open each failed job and cancel it to clean up OCI resources.\n\nUpdate anyway?`
        : ((sw.update_available ? "Update the migration tool to the latest version from GitHub and restart the service?" : "Reinstall the current version and restart the service?")
          + "\n\nAll users will have to log in again.");
      if (!confirm(msg)) return;
      swBtn.disabled = true; swErr.textContent = "";
      try { const r = await api("POST", "/setup/software/update", force ? { force: true } : {}); watching = true; renderSoftware(r); swState.textContent = "Update started..."; watchRestart(sw.commit); }
      catch (e) { swErr.textContent = e.message; swBtn.disabled = false; }
    });
    document.getElementById("sw-check").addEventListener("click", () => loadSoftware(true));

    // logging: applies immediately, persisted on the helper
    const logForm = document.getElementById("log-form"); const logResult = document.getElementById("log-result");
    const renderLogging = (lg) => {
      const sel = logForm.elements.log_level;
      sel.innerHTML = "";
      for (const l of lg.levels) sel.append(el("option", { value: l }, l));
      sel.value = lg.log_level;
      logForm.elements.oci_log_requests.checked = lg.oci_log_requests;
      logResult.textContent = lg.warning || (lg.persisted ? "" : "Defaults from the environment; not changed yet.");
      logResult.className = lg.warning ? "error" : "muted";
    };
    logForm.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const btn = document.getElementById("log-save"); btn.disabled = true;
      try {
        const lg = await api("PUT", "/setup/logging", { log_level: logForm.elements.log_level.value, oci_log_requests: logForm.elements.oci_log_requests.checked });
        renderLogging(lg);
        if (!lg.warning) logResult.textContent = "Saved and applied.";
      } catch (e) { logResult.textContent = e.message; logResult.className = "error"; }
      finally { btn.disabled = false; }
    });

    // helper identity; the operation values are taken from the form so a save is reflected at once
    let info = null;
    const fmtTtl = (s) => { const h = s / 3600; return h >= 1 ? `${Math.round(h * 10) / 10} h` : `${Math.round(s / 60)} min`; };
    const renderHelperInfo = () => {
      if (!info) return;
      const f = document.getElementById("op-form").elements;
      state.region = state.region || info.region;
      kv(document.getElementById("setup-kv"), [
        ["Version", info.version + (info.commit ? ` (${short(info.commit)})` : "")],
        ["Region / AD", `${info.region} / ${info.availability_domain}`],
        ["Instance", ocidLink("instances", info.instance_id)], ["Compartment", info.compartment_id],
        ...(info.default_vcenter ? [["Default vCenter", info.default_vcenter]] : []),
        ["Seed image bucket", info.seed_bucket], ["Default shape", info.default_shape],
        ["Concurrent migrations", f.max_concurrent_jobs.value || String(info.max_concurrent_jobs)],
        ["Session idle timeout", fmtTtl(f.session_ttl_h.value ? Number(f.session_ttl_h.value) * 3600 : info.session_ttl_s)],
        ["Logged-in sessions", String(info.sessions)], ["Running migrations", String(info.active_jobs)],
      ]);
    };

    // operation limits: concurrency and session idle timeout; applied immediately, persisted on the helper
    const opForm = document.getElementById("op-form"); const opResult = document.getElementById("op-result");
    const renderOperation = (op) => {
      const conc = opForm.elements.max_concurrent_jobs, ttl = opForm.elements.session_ttl_h;
      conc.max = op.max_concurrent_jobs_limit; conc.value = op.max_concurrent_jobs;
      ttl.min = (op.session_ttl_min_s / 3600).toFixed(1); ttl.max = Math.round(op.session_ttl_max_s / 3600);
      ttl.value = String(Math.round(op.session_ttl_s / 360) / 10);
      document.getElementById("op-concurrency-hint").textContent = `Migrations copying disks at the same time (1-${op.max_concurrent_jobs_limit}); further jobs wait in the queue. Lowering it never interrupts a running migration.`;
      opResult.textContent = op.warning || (op.persisted ? "" : "Defaults from the environment; not changed yet.");
      opResult.className = op.warning ? "error" : "muted";
      renderHelperInfo();
    };
    opForm.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const btn = document.getElementById("op-save"); btn.disabled = true;
      try {
        const op = await api("PUT", "/setup/operation", {
          max_concurrent_jobs: Number(opForm.elements.max_concurrent_jobs.value),
          session_ttl_s: Math.round(Number(opForm.elements.session_ttl_h.value) * 3600),
        });
        renderOperation(op);
        if (!op.warning) opResult.textContent = "Saved and applied.";
      } catch (e) { opResult.textContent = e.message; opResult.className = "error"; }
      finally { btn.disabled = false; }
    });

    const pwForm = document.getElementById("pw-form"); const pwResult = document.getElementById("pw-result");
    const renderUiPassword = (pw) => {
      document.getElementById("pw-status").textContent = pw.required
        ? "The web UI and API require this password before use."
        : "Not set. Anyone who can reach this VM can use the tool; set a password to lock it.";
      document.getElementById("pw-current-wrap").hidden = !pw.required;
      pwForm.elements.current_password.required = pw.required;
      pwForm.elements.current_password.value = "";
      pwForm.elements.new_password.value = "";
      pwForm.elements.new_password2.value = "";
      pwResult.textContent = "";
      pwResult.className = "muted";
    };
    pwForm.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const neu = pwForm.elements.new_password.value;
      const neu2 = pwForm.elements.new_password2.value;
      pwResult.className = "error";
      if (neu !== neu2) { pwResult.textContent = "New password and confirmation do not match."; return; }
      if (neu && neu.length < 8) { pwResult.textContent = "Password must be at least 8 characters."; return; }
      const btn = document.getElementById("pw-save"); btn.disabled = true;
      try {
        const pw = await api("PUT", "/setup/ui-password", {
          current_password: pwForm.elements.current_password.value,
          new_password: neu,
        });
        renderUiPassword(pw);
        pwResult.textContent = pw.required ? "Password saved." : "Password removed.";
        pwResult.className = "muted";
      } catch (e) { pwResult.textContent = e.message; pwResult.className = "error"; }
      finally { btn.disabled = false; }
    });

    document.getElementById("seed-cleanup").addEventListener("click", async (ev) => {
      const out = document.getElementById("seed-result");
      if (!confirm("Delete all seed images and their staging objects?")) return;
      ev.target.disabled = true; out.textContent = "Deleting...";
      try { const r = await api("DELETE", "/seed-images"); out.textContent = `Deleted ${r.deleted.length} object(s).`; }
      catch (e) { out.textContent = e.message; } finally { ev.target.disabled = false; }
    });

    document.getElementById("iso-images-cleanup").addEventListener("click", async (ev) => {
      const out = document.getElementById("iso-images-result");
      if (!confirm("Delete all custom images imported from ISOs? The next instance from the same ISO imports it again.")) return;
      ev.target.disabled = true; out.textContent = "Deleting...";
      try { const r = await api("DELETE", "/iso-images"); out.textContent = `Deleted ${r.deleted.length} image(s).`; }
      catch (e) { out.textContent = e.message; } finally { ev.target.disabled = false; }
    });

    // job history: delete the records of failed (FAILED + CANCELLED) or of all finished jobs
    const purgeBtns = ["jobs-purge-failed", "jobs-purge-all"].map((id) => document.getElementById(id));
    const purge = async (scope) => {
      const out = document.getElementById("jobs-purge-result");
      const what = scope === "failed" ? "all FAILED and CANCELLED jobs" : "ALL finished jobs (completed, failed and cancelled)";
      if (!confirm(`Delete the records of ${what} from the migration tool?\n\n` +
        "Running or queued jobs are kept. This only removes the job history: OCI resources a failed job may have " +
        "left behind (instance, volumes) are NOT cleaned up - use 'Clean up OCI resources' on such jobs first if needed.\n\n" +
        "This cannot be undone.")) return;
      purgeBtns.forEach((b) => { b.disabled = true; }); out.textContent = "Deleting...";
      try {
        const r = await api("DELETE", `/setup/jobs?scope=${scope}`);
        out.textContent = `Deleted ${r.deleted} job record(s)${r.kept_active ? `; ${r.kept_active} active job(s) kept` : ""}.`;
      } catch (e) { out.textContent = e.message; } finally { purgeBtns.forEach((b) => { b.disabled = false; }); }
    };
    purgeBtns[0].addEventListener("click", () => purge("failed"));
    purgeBtns[1].addEventListener("click", () => purge("all"));

    // live resource usage of the helper VM: numbers with meters, plus a canvas chart of CPU % and network
    // throughput over the sampler's history (10 minutes); polled every 3 s while the page is open
    const statsKv = document.getElementById("stats-kv"); const statsNote = document.getElementById("stats-note");
    const chart = document.getElementById("stats-chart"); const chartRange = document.getElementById("stats-chart-range");
    let statsTimer = null; let lastStats = null;
    const meter = (frac) => {
      const pct = Math.max(0, Math.min(100, Math.round((frac || 0) * 100)));
      return el("span", { class: "meter" + (pct >= 90 ? " bad" : pct >= 75 ? " warn" : "") }, el("span", { style: `width:${pct}%` }));
    };
    const renderStats = (s) => {
      lastStats = s;
      statsNote.textContent = s.available ? (s.sampled_at ? `sampled ${new Date(s.sampled_at).toLocaleTimeString()}, every ${s.interval_s} s` : "") : s.note;
      const rows = [];
      if (s.available) {
        rows.push(["CPU", el("span", {}, meter((s.cpu_pct || 0) / 100),
          `${s.cpu_pct === null ? "-" : s.cpu_pct.toFixed(0) + " %"} of ${s.cpu_count} vCPU${s.cpu_count === 1 ? "" : "s"}`,
          s.load_1m !== null ? el("span", { class: "muted" }, ` - load ${s.load_1m} / ${s.load_5m} / ${s.load_15m}`) : null)]);
        if (s.mem_total_bytes) {
          rows.push(["Memory", el("span", {}, meter(s.mem_used_bytes / s.mem_total_bytes),
            `${fmtBytes(s.mem_used_bytes)} of ${fmtBytes(s.mem_total_bytes)} used (${Math.round(100 * s.mem_used_bytes / s.mem_total_bytes)} %)`,
            s.swap_total_bytes ? el("span", { class: "muted" }, ` - swap ${fmtBytes(s.swap_used_bytes)} of ${fmtBytes(s.swap_total_bytes)}`) : null)]);
        }
      }
      for (const d of s.disks) {
        rows.push([`Disk ${d.mount}`, el("span", {}, meter(d.used_bytes / d.total_bytes),
          `${fmtBytes(d.free_bytes)} free of ${fmtBytes(d.total_bytes)} (${Math.round(100 * d.used_bytes / d.total_bytes)} % used)`)]);
      }
      if (s.available) {
        rows.push(["Network", el("span", {}, `received ${fmtRate(s.net_rx_bps)}, sent ${fmtRate(s.net_tx_bps)}`,
          el("span", { class: "muted" }, ` - since boot ${fmtBytes(s.net_rx_total_bytes)} in / ${fmtBytes(s.net_tx_total_bytes)} out`))]);
      }
      if (s.uptime_s !== null && s.uptime_s !== undefined) rows.push(["Uptime", fmtDuration(s.uptime_s)]);
      kv(statsKv, rows);
      chart.parentElement.hidden = !s.available;
      if (s.available) drawChart(s);
    };
    // two panels on one canvas: CPU % (fixed 0-100 scale, filled) and bytes/s in and out (auto scale, lines)
    const drawChart = (s) => {
      const dpr = window.devicePixelRatio || 1;
      const cssW = chart.clientWidth || 600, cssH = 220;
      if (chart.width !== Math.round(cssW * dpr) || chart.height !== Math.round(cssH * dpr)) { chart.width = Math.round(cssW * dpr); chart.height = Math.round(cssH * dpr); }
      const ctx = chart.getContext("2d");
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, cssW, cssH);
      const left = 56, right = 8, gap = 18, top = 6, bottom = 18;
      const panelH = (cssH - top - bottom - gap) / 2;
      const plotW = cssW - left - right;
      const now = Date.now() / 1000, span = s.history_s;
      const x = (t) => left + plotW * (1 - Math.min(1, Math.max(0, (now - t) / span)));
      const pts = s.history.filter((p) => now - p.t <= span);
      ctx.font = "11px system-ui, sans-serif"; ctx.textBaseline = "middle";
      const frame = (y0, h, ticks, fmt) => {
        ctx.strokeStyle = "#d5dde2"; ctx.fillStyle = "#5f6b73"; ctx.lineWidth = 1;
        for (const [frac, label] of ticks) {
          const y = y0 + h * (1 - frac);
          ctx.beginPath(); ctx.moveTo(left, y); ctx.lineTo(left + plotW, y); ctx.stroke();
          ctx.textAlign = "right"; ctx.fillText(fmt(label), left - 6, y);
        }
      };
      // CPU panel
      const cpuY = top;
      frame(cpuY, panelH, [[0, 0], [0.5, 50], [1, 100]], (v) => v + " %");
      ctx.beginPath(); let started = false;
      for (const p of pts) { if (p.cpu_pct === null) continue; const px = x(p.t), py = cpuY + panelH * (1 - p.cpu_pct / 100); if (!started) { ctx.moveTo(px, cpuY + panelH); ctx.lineTo(px, py); started = true; } else ctx.lineTo(px, py); }
      if (started) {
        const lastX = x(pts[pts.length - 1].t);
        ctx.lineTo(lastX, cpuY + panelH); ctx.closePath();
        ctx.fillStyle = "rgba(0, 114, 163, .18)"; ctx.fill();
        ctx.beginPath(); started = false;
        for (const p of pts) { if (p.cpu_pct === null) continue; const px = x(p.t), py = cpuY + panelH * (1 - p.cpu_pct / 100); if (!started) { ctx.moveTo(px, py); started = true; } else ctx.lineTo(px, py); }
        ctx.strokeStyle = "#0072a3"; ctx.lineWidth = 1.5; ctx.stroke();
      }
      // network panel: scale to the peak (at least 1 MB/s so an idle helper does not look noisy)
      const netY = top + panelH + gap;
      const MiB = 1024 * 1024;
      const peak = Math.max(MiB, ...pts.map((p) => Math.max(p.rx_bps || 0, p.tx_bps || 0)));
      const nice = [1, 2, 5, 10, 20, 50, 100, 200, 500, 1024, 2048, 5120, 10240].map((v) => v * MiB).find((v) => v >= peak) || peak;
      frame(netY, panelH, [[0, 0], [0.5, nice / 2], [1, nice]], (v) => v ? fmtBytes(v) + "/s" : "0");
      const line = (key, color) => {
        ctx.beginPath(); let on = false;
        for (const p of pts) { if (p[key] === null) { on = false; continue; } const px = x(p.t), py = netY + panelH * (1 - Math.min(1, p[key] / nice)); if (!on) { ctx.moveTo(px, py); on = true; } else ctx.lineTo(px, py); }
        ctx.strokeStyle = color; ctx.lineWidth = 1.5; ctx.stroke();
      };
      line("rx_bps", "#2f8b3a"); line("tx_bps", "#d9822b");
      // time axis: "-10 min" ... "now"
      ctx.fillStyle = "#5f6b73"; ctx.textAlign = "left"; ctx.fillText(`-${Math.round(span / 60)} min`, left, cssH - bottom / 2);
      ctx.textAlign = "right"; ctx.fillText("now", left + plotW, cssH - bottom / 2);
      ctx.textAlign = "center"; ctx.fillText(`-${Math.round(span / 120)} min`, left + plotW / 2, cssH - bottom / 2);
      chartRange.textContent = pts.length ? `${pts.length} samples` : "collecting samples...";
    };
    const pollStats = async () => {
      try { renderStats(await api("GET", "/setup/stats")); }
      catch (e) { if (e.status === 401) return; statsNote.textContent = e.message; }
      statsTimer = setTimeout(pollStats, 3000);
    };
    const onResize = () => { if (lastStats && lastStats.available) drawChart(lastStats); };
    window.addEventListener("resize", onResize);

    try {
      const [inf, lg, op, pw] = await Promise.all([
        api("GET", "/setup/info"), api("GET", "/setup/logging"), api("GET", "/setup/operation"),
        api("GET", "/setup/ui-password"),
      ]);
      info = inf;
      renderLogging(lg);
      renderOperation(op);
      renderUiPassword(pw);
    } catch (e) { if (e.status !== 401) showError(e.message); return; }
    activePoll = () => { clearTimeout(timer); clearTimeout(statsTimer); window.removeEventListener("resize", onResize); };
    pollStats();
    await loadSoftware(true);
  }

  // ------------------------------------------------------------------- routing
  async function route() {
    stopPolling();
    const hash = location.hash || "#/start";
    if (!state.me) {
      // api() leaves /auth/* 401s alone (a failed login must not navigate), so the "no session" case is handled
      // here: everything but the VMware inventory works without vCenter, so an anonymous session is started
      // right away (after a restart or an expired session the page simply comes back)
      try { setUser(await api("GET", "/auth/me")); }
      catch (e) {
        if (e.status !== 401) { showError(e.message); return; }
        let cfg = {};
        try { cfg = await api("GET", "/auth/config"); } catch (_) { /* ignore */ }
        if (cfg.ui_password_required) return showUnlock();
        if (cfg.ui_password_setup_pending) return showFirstUse();
        if (hash === "#/login") return showLogin();
        if (hash === "#/azure/login") return showAzureLogin();
        if (hash === "#/gcp/login") return showGcpLogin();
        if (hash === "#/aws/login") return showAwsLogin();
        if (hash === "#/olvm/login") return showOlvmLogin();
        try { setUser(await api("POST", "/auth/anonymous")); }
        catch (e2) { showError(e2.message); return; }
      }
    }
    paintUserBar();
    if (!state.region) {
      try {
        const h = await api("GET", "/health");
        state.region = h.region || ""; state.ownCompartment = h.compartment_id || "";
      } catch (_) { /* links work without it */ }
    }
    // the export forms light up their list entry (#/azure/export/... -> Azure VMs, #/export/... -> Source VMs)
    const navHash = hash.startsWith("#/azure/export/") ? "#/azure/vms"
      : hash.startsWith("#/gcp/export/") ? "#/gcp/vms"
        : hash.startsWith("#/aws/export/") ? "#/aws/vms"
          : hash.startsWith("#/olvm/export/") ? "#/olvm/vms"
          : hash.startsWith("#/export/") ? "#/vms" : hash;
    const homeLink = document.getElementById("home-link");
    if (homeLink) homeLink.classList.toggle("active", hash === "#/start");
    for (const a of nav.querySelectorAll("a")) {
      const href = a.getAttribute("href");
      a.classList.toggle("active", href === "#/setup" ? navHash === "#/setup" : navHash.startsWith(href));
    }
    const anonymous = !!state.me.anonymous;
    const azure = hasAzure(state.me);
    const gcp = hasGcp(state.me);
    const aws = hasAws(state.me);
    const olvm = hasOlvm(state.me);
    if (hash === "#/start") return startView();
    if (hash === "#/unlock" || hash === "#/first-use") { location.hash = "#/start"; return; }
    if (hash === "#/login") { if (!hasVcenter(state.me)) return showLogin(); location.hash = "#/vms"; return; }
    if (hash === "#/azure/login") { if (!azure) return showAzureLogin(); location.hash = "#/azure/vms"; return; }
    if (hash === "#/gcp/login") { if (!gcp) return showGcpLogin(); location.hash = "#/gcp/vms"; return; }
    if (hash === "#/aws/login") { if (!aws) return showAwsLogin(); location.hash = "#/aws/vms"; return; }
    if (hash === "#/olvm/login") { if (!olvm) return showOlvmLogin(); location.hash = "#/olvm/vms"; return; }
    if (hash === "#/iso") return isoView();
    if (hash === "#/ova") return ovaView();
    if (hash === "#/ova-export") return ovaExportView();
    let m;
    if (hash === "#/instances") return instancesView();
    if ((m = /^#\/instances\/([^/]+)\/console$/.exec(hash))) return consoleView({ kind: "instance", id: decodeURIComponent(m[1]) });
    if ((m = /^#\/jobs\/([^/]+)\/console$/.exec(hash))) return consoleView({ kind: "job", id: decodeURIComponent(m[1]) });
    if ((m = /^#\/jobs\/(.+)$/.exec(hash))) return jobDetailView(decodeURIComponent(m[1]));
    if (hash === "#/jobs") return jobsView();
    if (hash === "#/vms") { if (anonymous || azure || gcp || aws || olvm) { location.hash = "#/login"; return; } return vmsView(); }
    if (hash === "#/setup") return setupView();
    // the Azure inventory and export form need an Azure login, the vSphere ones a vCenter login; the login
    // pages replace whatever session the browser has (a vCenter login cannot list Azure VMs and vice versa)
    if (hash === "#/azure/vms" || hash.startsWith("#/azure/export/")) {
      if (!azure) { location.hash = "#/azure/login"; return; }
      if ((m = /^#\/azure\/export\/(.+)$/.exec(hash))) return exportView(decodeURIComponent(m[1]), { azure: true });
      return azureVmsView();
    }
    if (hash === "#/gcp/vms" || hash.startsWith("#/gcp/export/")) {
      if (!gcp) { location.hash = "#/gcp/login"; return; }
      if ((m = /^#\/gcp\/export\/(.+)$/.exec(hash))) return exportView(decodeURIComponent(m[1]), { gcp: true });
      return gcpVmsView();
    }
    if (hash === "#/aws/vms" || hash.startsWith("#/aws/export/")) {
      if (!aws) { location.hash = "#/aws/login"; return; }
      if ((m = /^#\/aws\/export\/(.+)$/.exec(hash))) return exportView(decodeURIComponent(m[1]), { aws: true });
      return awsVmsView();
    }
    if (hash === "#/olvm/vms" || hash.startsWith("#/olvm/export/")) {
      if (!olvm) { location.hash = "#/olvm/login"; return; }
      if ((m = /^#\/olvm\/export\/(.+)$/.exec(hash))) return exportView(decodeURIComponent(m[1]), { olvm: true });
      return vmsView({ olvm: true });
    }
    if (anonymous || azure || gcp || aws || olvm) { location.hash = "#/login"; return; }
    if ((m = /^#\/export\/(.+)$/.exec(hash))) return exportView(decodeURIComponent(m[1]));
    return vmsView();
  }

  window.addEventListener("hashchange", () => { route().catch((e) => showError(e.message)); });
  route().catch((e) => showError(e.message));
})();
