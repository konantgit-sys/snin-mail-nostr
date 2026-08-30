/* Mail.api — транспорт: fetch с авторизацией, views, статус, авто-обновление. */
"use strict";

Mail.STATE = Mail.STATE || {};
Mail.STATE.token = localStorage.getItem("nm_token") || "";

Mail.api = async function (path, opts = {}) {
  const headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
  if (Mail.STATE.token) headers["Authorization"] = "Bearer " + Mail.STATE.token;
  const res = await fetch(path, { ...opts, headers });
  let data = {};
  try { data = await res.json(); } catch (_) { /* 204/empty */ }
  if (res.status === 401 || data.error === "auth") {
    Mail.STATE.token = "";
    localStorage.removeItem("nm_token");
    Mail.showLogin();
    throw new Error("auth");
  }
  return data;
};

Mail.showLogin = function () {
  Mail.$("login-view").hidden = false;
  Mail.$("register-view").hidden = true;
  Mail.$("main-view").hidden = true;
  Mail.stopRefresh();
};

Mail.showMain = function () {
  Mail.$("login-view").hidden = true;
  Mail.$("main-view").hidden = false;
  Mail.startRefresh();
};

Mail.loadStatus = async function () {
  // БЕЗ введённого пароля (localStorage-токена) не заходим в main —
  // прокси v2.site подмешивает свою cookie, и без этой проверки
  // браузер «входит без пароля» в чужой ящик (проверено 2026-08-26).
  if (!Mail.STATE.token) {
    Mail.showLogin();
    return;
  }
  const s = await Mail.api("/api/status");
  Mail.STATE.status = s;
  const accs = s.accounts || [];
  const isAdmin = (s.me || {}).role === "admin";
  if (isAdmin && accs.length > 1) {
    const saved = localStorage.getItem("mail_owner");
    Mail.STATE.owner = accs.some((a) => a.pubkey === saved) ? saved : (s.default_owner || accs[0].pubkey);
    Mail.renderAccountUI(accs, Mail.STATE.owner);
  } else if (accs.length === 1) {
    Mail.STATE.owner = accs[0].pubkey;
  } else {
    Mail.STATE.owner = "";
  }
  const cur = accs.find((a) => a.pubkey === Mail.STATE.owner) || accs[0] || {};
  Mail.$("mail-address").textContent = cur.address || s.address || "—";
  Mail.$("ln-addr").textContent = s.lightning || "—";
  Mail.$("empty-addr").textContent = cur.address || "";
  Mail.$("btn-logout").hidden = !s.ok;
  if (s.ok) Mail.showMain(); else Mail.showLogin();
  return s;
};

Mail.setAccount = async function (owner) {
  Mail.STATE.owner = owner;
  Mail.STATE.outboxTotal = null;
  Mail.STATE._sentAsked = false;
  localStorage.setItem("mail_owner", owner);
  const s = await Mail.api("/api/status");
  Mail.STATE.status = s;
  const accs = s.accounts || [];
  if ((s.me || {}).role === "admin" && accs.length > 1) Mail.renderAccountUI(accs, owner);
  const cur = accs.find((a) => a.pubkey === owner) || {};
  Mail.$("mail-address").textContent = cur.address || s.address || "—";
  Mail.$("empty-addr").textContent = cur.address || "";
  await Mail.loadMails();
};

Mail.copyAddress = async function () {
  const addr = Mail.$("mail-address").textContent;
  if (!addr || addr === "—") return;
  try {
    await navigator.clipboard.writeText(addr);
    Mail.showToast("Адрес скопирован 📋");
  } catch (_) {
    Mail.showToast(addr, "ok");
  }
};

/* ── авто-обновление (30с, только входящие на виду) ── */
let refreshTimer = null;
Mail.startRefresh = function () {
  Mail.stopRefresh();
  refreshTimer = setInterval(() => {
    if (document.hidden) return;
    if (Mail.STATE.tab === "inbox" && !Mail.$("list-view").hidden) Mail.loadMails();
  }, 30000);
};
Mail.stopRefresh = function () {
  if (refreshTimer) { clearInterval(refreshTimer); refreshTimer = null; }
};

/* ── переключатель ящиков: триггер + модалка ── */
Mail.initials = function (label) {
  const parts = String(label || "?").trim().split(/\s+/).filter(Boolean);
  const ch = (parts[0] || "?")[0] || "?";
  return ch.toUpperCase();
};
Mail.avatarBg = function (pubkey) {
  let h = 0; const s = String(pubkey || "");
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
  const hue = h % 360;
  return `linear-gradient(135deg, hsl(${hue} 72% 52%), hsl(${(hue + 45) % 360} 68% 34%))`;
};
Mail.renderAccountUI = function (accs, owner) {
  const trig = Mail.$("account-trigger");
  const cur = accs.find((a) => a.pubkey === owner) || accs[0] || {};
  trig.hidden = false;
  Mail.$("account-trigger-avatar").textContent = Mail.initials(cur.label);
  Mail.$("account-trigger-avatar").style.background = Mail.avatarBg(cur.pubkey);
  Mail.$("account-trigger-name").textContent = cur.label || "Ящик";
  const list = Mail.$("account-modal-list");
  list.innerHTML = accs.map((a, i) => {
    const active = a.pubkey === owner;
    return `<button class="account-item${active ? " active" : ""}" data-pubkey="${Mail.esc(a.pubkey)}" style="--i:${i}">
      <span class="account-avatar" style="background:${Mail.avatarBg(a.pubkey)}">${Mail.initials(a.label)}</span>
      <span class="account-item-main">
        <span class="account-item-name">${Mail.esc(a.label || "Ящик")}</span>
        <span class="account-item-addr" title="${Mail.esc(a.address || "")}">${Mail.esc(a.address || "")}</span>
      </span>
      ${active ? '<svg class="account-check" width="16" height="16" viewBox="0 0 24 24" aria-hidden="true"><path d="M5 13l4 4L19 7" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg>' : ""}
    </button>`;
  }).join("");
  Mail.$("account-modal-count").textContent = accs.length;
};
Mail.toggleAccountModal = function () {
  const modal = Mail.$("account-modal"), back = Mail.$("account-backdrop"), trig = Mail.$("account-trigger");
  if (!modal.hidden) { Mail.closeAccountModal(); return; }
  modal.hidden = false; back.hidden = false;
  requestAnimationFrame(() => {
    modal.classList.add("open"); back.classList.add("open"); trig.classList.add("open");
  });
};
Mail.closeAccountModal = function () {
  const modal = Mail.$("account-modal"), back = Mail.$("account-backdrop"), trig = Mail.$("account-trigger");
  modal.classList.remove("open"); back.classList.remove("open"); trig.classList.remove("open");
  setTimeout(() => { modal.hidden = true; back.hidden = true; }, 220);
};
