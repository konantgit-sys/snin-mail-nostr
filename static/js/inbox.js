/* Mail.inbox — список (входящие/исходящие): пагинация, поиск, фильтры,
   массовое удаление прочитанных. */
"use strict";

Mail.STATE = Mail.STATE || {};
Mail.STATE.filter = "all";   // all | unread | attach
Mail.STATE.offset = 0;
Mail.STATE.hasMore = false;

/* ── загрузка ─────────────────────────────────────────── */
Mail.loadMails = async function (append = false) {
  try {
    const off = append ? Mail.STATE.offset : 0;
    if (!append && !Mail.STATE._loaded?.inbox) Mail.renderSkeleton();
    const q = (Mail.STATE.query || "").trim();
    const folder = Mail.STATE.tab === "archive" ? "archive" : "";
    const params = new URLSearchParams({
      owner: Mail.STATE.owner || "", offset: off, limit: 100,
    });
    if (q) params.set("q", q);
    if (folder) params.set("folder", folder);
    const d = await Mail.api("/api/mails?" + params.toString());
    if (!append) Mail.STATE.mails = [];
    Mail.STATE.mails = Mail.STATE.mails.concat(d.mails || []);
    Mail.STATE.total = d.total || 0;
    Mail.STATE.unread = d.unread || 0;
    Mail.STATE.offset = Mail.STATE.mails.length;
    Mail.STATE.hasMore = !!d.has_more;
    Mail.STATE._loaded = Mail.STATE._loaded || {};
    Mail.STATE._loaded.inbox = true;
  } catch (_) { return; }
  if (Mail.STATE.tab === "inbox" || Mail.STATE.tab === "archive") Mail.renderList();
  Mail.renderHero();
};

Mail.loadDrafts = async function () {
  try {
    if (!Mail.STATE._loaded?.drafts) Mail.renderSkeleton();
    const params = new URLSearchParams({ owner: Mail.STATE.owner || "", offset: 0, limit: 100 });
    const d = await Mail.api("/api/drafts?" + params.toString());
    Mail.STATE.drafts = d.drafts || [];
    Mail.STATE.draftsTotal = d.total || 0;
    Mail.STATE._loaded = Mail.STATE._loaded || {};
    Mail.STATE._loaded.drafts = true;
  } catch (_) { return; }
  if (Mail.STATE.tab === "drafts") Mail.renderList();
  Mail.renderHero();
};

Mail.loadOutbox = async function (append = false) {
  try {
    const off = append ? Mail.STATE.offset : 0;
    if (!append && !Mail.STATE._loaded?.outbox) Mail.renderSkeleton();
    const d = await Mail.api("/api/outbox?owner=" + encodeURIComponent(Mail.STATE.owner || "") + "&offset=" + off + "&limit=100");
    if (!append) Mail.STATE.outbox = [];
    Mail.STATE.outbox = Mail.STATE.outbox.concat(d.outbox || []);
    Mail.STATE.outboxTotal = d.total || 0;
    Mail.STATE.offset = Mail.STATE.outbox.length;
    Mail.STATE.hasMore = !!d.has_more;
    Mail.STATE._loaded = Mail.STATE._loaded || {};
    Mail.STATE._loaded.outbox = true;
  } catch (_) { return; }
  if (Mail.STATE.tab === "outbox") Mail.renderList();
  Mail.renderHero();
};

Mail.visibleItems = function () {
  const isOutbox = Mail.STATE.tab === "outbox";
  if (Mail.STATE.tab === "drafts") {
    const items = Mail.STATE.drafts || [];
    if (!Mail.STATE.query) return items;
    const q = Mail.STATE.query.toLowerCase();
    return items.filter((m) => ((m.subject || "") + " " + (m.body || "") + " " + (m.to || "")).toLowerCase().includes(q));
  }
  const items = isOutbox ? Mail.STATE.outbox : Mail.STATE.mails;
  return items.filter((m) => {
    if (Mail.STATE.filter === "unread" && isOutbox) return false;
    if (Mail.STATE.filter === "unread" && m.is_read) return false;
    if (Mail.STATE.filter === "attach" && !(m.attachments && m.attachments.length)) return false;
    if (!Mail.STATE.query) return true;
    const q = Mail.STATE.query.toLowerCase();
    const hay = isOutbox
      ? (m.subject || "") + " " + (m.body || "") + " " + (m.to || "")
      : (m.subject || "") + " " + (m.body || "") + " " + (m.from || "");
    return hay.toLowerCase().includes(q);
  });
};

Mail.renderList = function () {
  const list = Mail.$("mail-list");
  const emptyInbox = Mail.$("empty-inbox");
  const emptySearch = Mail.$("empty-search");
  const btnMore = Mail.$("btn-more");
  const isOutbox = Mail.STATE.tab === "outbox";
  const isDrafts = Mail.STATE.tab === "drafts";
  const isArchive = Mail.STATE.tab === "archive";

  /* hero виден только на вкладке «Входящие»; анимируем только при появлении */
  const hero = Mail.$("hero-view");
  const heroWasHidden = hero.hidden;
  hero.hidden = isOutbox || Mail.STATE.tab !== "inbox";
  if (!hero.hidden) {
    Mail.renderHero();
    if (heroWasHidden) Mail.enter(hero);
  }

  const unread = Mail.STATE.mails.filter((m) => !m.is_read).length;
  Mail.$("inbox-count").textContent = unread;
  Mail.$("inbox-count").hidden = !unread;

  /* панель фильтров — только для входящих */
  Mail.$("list-filters").hidden = isOutbox || isDrafts || isArchive;
  Mail.$("btn-clean").classList.remove("armed");
  Mail.$("btn-clean").textContent = "Очистить прочитанные";
  const hasRead = Mail.STATE.mails.some((m) => m.is_read);
  Mail.$("btn-clean").hidden = !hasRead;

  const items = Mail.visibleItems();
  emptySearch.hidden = true;
  emptyInbox.hidden = true;
  if (!items.length) {
    list.innerHTML = "";
    if (Mail.STATE.query || (Mail.STATE.filter !== "all" && !isDrafts && !isArchive)) emptySearch.hidden = false;
    else if (isDrafts) {
      emptyInbox.querySelector("p").innerHTML = "Черновиков пока нет.<br>Начните письмо — оно сохранится само при закрытии.";
      emptyInbox.hidden = false;
    } else if (isArchive) {
      emptyInbox.querySelector("p").innerHTML = "Архив пуст.<br>Письма из архива попадают сюда.";
      emptyInbox.hidden = false;
    } else if (!isOutbox) {
      emptyInbox.querySelector("p").innerHTML = "Писем пока нет. Напишите Крайтеру на<br><code id=\"empty-addr\">—</code>";
      emptyInbox.hidden = false;
      const addr = Mail.$("mail-address").textContent;
      const ea = emptyInbox.querySelector("#empty-addr");
      if (ea) ea.textContent = addr;
    } else {
      emptyInbox.querySelector("p").innerHTML = "Пока ничего не отправлено.<br>Напишите первое письмо!";
      emptyInbox.hidden = false;
    }
    btnMore.hidden = true;
    return;
  }

  list.innerHTML = "";
  const frag = document.createDocumentFragment();
  const useStagger = !!Mail.STATE._stagger;
  Mail.STATE._stagger = false;

  /* черновики — отдельная карточка: перо, «Черновик», updated_at */
  if (isDrafts) {
    items.forEach((d, i) => {
      const el = document.createElement("div");
      el.className = "mail-item draft";
      if (useStagger) el.style.setProperty("--i", i);
      const snip = d.body ? Mail.esc(String(d.body).replace(/\s+/g, " ").trim()).slice(0, 110) : "";
      el.innerHTML =
        `<div class="avatar draft-avatar" aria-hidden="true"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg></div>
         <div class="mail-item-main">
           <div class="mail-item-top">
             <span class="mail-item-from">${Mail.esc(Mail.shortNpub(d.to || "—"))}<span class="draft-tag">Черновик</span></span>
             <span class="mail-item-date">${Mail.fmtAgo(d.updated_at)}</span>
           </div>
           <div class="mail-item-subject">${Mail.esc(d.subject || "(без темы)")}</div>
           ${snip ? `<div class="mail-item-snippet">${snip}</div>` : ""}
         </div>`;
      el.addEventListener("click", () => Mail.openDraft(d));
      frag.appendChild(el);
    });
    list.appendChild(frag);
    if (useStagger) {
      list.classList.add("enter");
      setTimeout(() => list.classList.remove("enter"), 700);
    }
    btnMore.hidden = true;
    return;
  }

  items.forEach((m, i) => {
    const el = document.createElement("div");
    el.className = "mail-item" + (m.is_read ? "" : " unread") + (isArchive ? " archived" : "");
    if (useStagger) el.style.setProperty("--i", i);
    const isUnread = !m.is_read && !isOutbox;
    const who = isOutbox ? (m.to || "—") : (m.from || "—");
    const when = isOutbox ? m.sent_at : m.received_at;
    const av = Mail.avatarOf(who);
    const attIcon = (m.attachments && m.attachments.length)
      ? `<svg class="mail-item-att" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m21.44 11.05-9.19 9.19a6 6 0 0 1-8.49-8.49l8.57-8.57A4 4 0 1 1 18 8.84l-8.59 8.57a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg>`
      : "";
    const snip = m.body ? Mail.esc(String(m.body).replace(/\s+/g, " ").trim()).slice(0, 110) : "";
    el.innerHTML =
      `<div class="avatar" style="background:${av.bg}" aria-hidden="true">${av.ch}</div>
       <div class="mail-item-main">
         <div class="mail-item-top">
           <span class="mail-item-from">${Mail.esc(Mail.shortNpub(who))}${isUnread ? '<span class="unread-dot" aria-hidden="true"></span>' : ""}${isArchive ? '<span class="draft-tag">Архив</span>' : ""}</span>
           <span class="mail-item-date">${Mail.fmtAgo(when)}</span>
         </div>
         <div class="mail-item-subject">${Mail.esc(m.subject || "(без темы)")} ${attIcon}</div>
         ${snip ? `<div class="mail-item-snippet">${snip}</div>` : ""}
       </div>`;
    el.addEventListener("click", () => Mail.openMail(m.id, isOutbox));
    if (!isOutbox) {
      const wrap = document.createElement("div");
      wrap.className = "mail-item-wrap";
      wrap.appendChild(Mail.renderMailActions(m));
      wrap.appendChild(el);
      frag.appendChild(wrap);
    } else {
      frag.appendChild(el);
    }
  });
  list.appendChild(frag);
  if (useStagger) {
    list.classList.add("enter");
    setTimeout(() => list.classList.remove("enter"), 700);
  }
  btnMore.hidden = !(Mail.STATE.hasMore && !Mail.STATE.query);
};

/* ── «Загрузить ещё» ──────────────────────────────────── */
Mail.$("btn-more").addEventListener("click", () => {
  if (Mail.STATE.tab === "outbox") Mail.loadOutbox(true);
  else Mail.loadInboxMore();
});

Mail.loadInboxMore = async function () {
  try {
    const folder = Mail.STATE.tab === "archive" ? "archive" : "";
    const params = new URLSearchParams({
      owner: Mail.STATE.owner || "", offset: Mail.STATE.offset, limit: 100,
    });
    if (folder) params.set("folder", folder);
    const d = await Mail.api("/api/mails?" + params.toString());
    Mail.STATE.mails = Mail.STATE.mails.concat(d.mails || []);
    Mail.STATE.offset = Mail.STATE.mails.length;
    Mail.STATE.hasMore = !!d.has_more;
  } catch (_) { return; }
  Mail.renderList();
};

/* ── фильтры ──────────────────────────────────────────── */
document.querySelectorAll(".seg-btn").forEach((b) =>
  b.addEventListener("click", () => {
    Mail.STATE.filter = b.dataset.filter;
    document.querySelectorAll(".seg-btn").forEach((x) => {
      const on = x === b;
      x.classList.toggle("active", on);
      x.setAttribute("aria-selected", String(on));
    });
    Mail.renderList();
  })
);

/* ── «Очистить прочитанные» (2 клика) ─────────────────── */
let cleanArm = 0;
Mail.$("btn-clean").addEventListener("click", async () => {
  const btn = Mail.$("btn-clean");
  const now = Date.now();
  if (now - cleanArm > 3000) {
    cleanArm = now;
    btn.classList.add("armed");
    btn.textContent = "Точно удалить?";
    Mail.showToast("Нажмите ещё раз для подтверждения", "warn");
    setTimeout(() => { btn.classList.remove("armed"); btn.textContent = "Очистить прочитанные"; }, 3000);
    return;
  }
  cleanArm = 0;
  btn.classList.remove("armed");
  btn.textContent = "Очистить прочитанные";
  const r = await Mail.api("/api/mails?filter=read", { method: "DELETE" });
  if (!r.ok) { Mail.showToast(r.error || "Ошибка", "err"); return; }
  Mail.showToast(`Удалено прочитанных: ${r.deleted || 0}`);
  Mail.STATE.mails = Mail.STATE.mails.filter((m) => m.is_read ? false : true);
  Mail.renderList();
});

/* ── hero (Фаза 2): приветствие + статистика ящика + последние письма ── */
Mail.renderHero = function () {
  const hero = Mail.$("hero-view");
  if (!hero) return;
  const s = Mail.STATE.status || {};
  const accs = s.accounts || [];
  const cur = accs.find((a) => a.pubkey === Mail.STATE.owner) || {};
  const label = String(cur.label || (s.me || {}).label || "").trim();
  Mail.$("hero-title").textContent = label ? "Добро пожаловать, " + label : "Добро пожаловать в SNIN Mail";
  Mail.$("hero-addr").textContent = cur.address || Mail.$("mail-address").textContent || "—";
  Mail.$("stat-unread").textContent = Mail.STATE.unread != null ? Mail.STATE.unread : "—";
  Mail.$("stat-total").textContent = Mail.STATE.total != null ? Mail.STATE.total : "—";
  Mail.$("stat-relays").textContent = ((s.relays || []).length) || "—";
  if (Mail.STATE.outboxTotal == null && !Mail.STATE._sentAsked) {
    Mail.STATE._sentAsked = true;
    Mail.api("/api/outbox?owner=" + encodeURIComponent(Mail.STATE.owner || "") + "&limit=1")
      .then((d) => { Mail.STATE.outboxTotal = (d && d.total) || 0; Mail.$("stat-sent").textContent = Mail.STATE.outboxTotal; })
      .catch(() => {});
  }
  Mail.$("stat-sent").textContent = Mail.STATE.outboxTotal != null ? Mail.STATE.outboxTotal : "—";

  const recent = (Mail.STATE.mails || []).slice(0, 3);
  const wrap = Mail.$("hero-recent-list");
  if (!recent.length) { Mail.$("hero-recent").hidden = true; return; }
  Mail.$("hero-recent").hidden = false;
  wrap.innerHTML = "";
  const frag = document.createDocumentFragment();
  recent.forEach((m) => {
    const el = document.createElement("button");
    el.type = "button";
    el.className = "hero-mail" + (m.is_read ? "" : " unread");
    const av = Mail.avatarOf(m.from || "?");
    el.innerHTML =
      '<span class="avatar" style="background:' + av.bg + '" aria-hidden="true">' + av.ch + '</span>' +
      '<span class="hero-mail-main">' +
        '<span class="hero-mail-top">' +
          '<span class="hero-mail-from">' + Mail.esc(Mail.shortNpub(m.from || "—")) + '</span>' +
          '<span class="hero-mail-date">' + Mail.fmtAgo(m.received_at) + '</span>' +
        '</span>' +
        '<span class="hero-mail-subject">' + Mail.esc(m.subject || "(без темы)") + '</span>' +
      '</span>';
    el.addEventListener("click", () => Mail.openMail(m.id, false));
    frag.appendChild(el);
  });
  wrap.appendChild(frag);
};

/* ── скелетон загрузки (пульсирующие карточки) ── */
Mail.renderSkeleton = function (count = 7) {
  const list = Mail.$("mail-list");
  if (!list) return;
  Mail.$("empty-inbox").hidden = true;
  Mail.$("empty-search").hidden = true;
  Mail.$("btn-more").hidden = true;
  list.innerHTML = Array.from({ length: count }, (_, i) =>
    `<div class="skeleton-item" style="--i:${i}">
       <div class="skeleton-avatar shimmer"></div>
       <div class="skeleton-lines">
         <div class="skeleton-line shimmer" style="width:62%"></div>
         <div class="skeleton-line shimmer" style="width:38%"></div>
       </div>
     </div>`).join("");
};

/* ── переключение вкладок (Входящие/Отправленные/Черновики/Архив/IMAP) ── */
Mail.switchTab = async function (tabName) {
  document.querySelectorAll(".tab").forEach((x) => x.classList.toggle("active", x.dataset.tab === tabName));
  Mail.STATE.tab = tabName;
  Mail.$("search").value = "";
  Mail.STATE.query = "";
  Mail.STATE._stagger = true;
  Mail.renderSkeleton();
  if (tabName === "outbox") await Mail.loadOutbox();
  else if (tabName === "drafts") await Mail.loadDrafts();
  else await Mail.loadMails();
  Mail.enter(Mail.$("list-view"));
};

/* ── свайп-действия на мобиле (прочитано / удалить) ── */
Mail.renderMailActions = function (m) {
  const div = document.createElement("div");
  div.className = "mail-actions";
  div.innerHTML =
    `<button type="button" class="act act-read" data-id="${Mail.esc(m.id)}" title="${m.is_read ? "Не прочитано" : "Прочитано"}" aria-label="${m.is_read ? "Отметить непрочитанным" : "Отметить прочитанным"}">
       <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>
     </button>
     <button type="button" class="act act-del" data-id="${Mail.esc(m.id)}" title="Удалить" aria-label="Удалить">
       <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
     </button>`;
  return div;
};

Mail.closeSwipes = function () {
  document.querySelectorAll(".mail-item-wrap.swiped").forEach((w) => w.classList.remove("swiped"));
};

Mail.toggleReadById = async function (id) {
  const m = Mail.STATE.mails.find((x) => String(x.id) === String(id));
  if (!m) return;
  const target = !m.is_read;
  const r = await Mail.api(`/api/mails/${id}/read`, { method: "POST", body: JSON.stringify({ read: target }) });
  if (!r.ok) { Mail.showToast(r.error || "Ошибка", "err"); return; }
  m.is_read = target;
  Mail.showToast(target ? "Отмечено прочитанным" : "Отмечено непрочитанным");
  Mail.closeSwipes();
  Mail.renderList();
};

Mail.deleteById = async function (id, btn) {
  const armed = btn && btn.classList.contains("armed");
  if (!armed) {
    if (btn) {
      btn.classList.add("armed");
      setTimeout(() => btn.classList.remove("armed"), 2500);
    }
    Mail.showToast("Нажмите ещё раз для удаления", "warn");
    return;
  }
  const r = await Mail.api("/api/mails/" + id, { method: "DELETE" });
  if (!r.ok) { Mail.showToast(r.error || "Ошибка удаления", "err"); return; }
  Mail.STATE.mails = Mail.STATE.mails.filter((m) => String(m.id) !== String(id));
  Mail.showToast("Письмо удалено");
  Mail.closeSwipes();
  Mail.renderList();
};

/* свайп по карточке: влево — действия, вправо/тап мимо — закрыть */
(function () {
  if (!window.PointerEvent) return;
  let startX = 0, startY = 0, dragging = null, moved = false, baseX = 0;
  document.addEventListener("pointerdown", (ev) => {
    const item = ev.target.closest(".mail-item");
    if (!item || ev.pointerType !== "touch") return;
    const wrap = item.closest(".mail-item-wrap");
    if (!wrap) return;
    Mail.closeSwipes();
    dragging = wrap;
    moved = false;
    baseX = wrap.classList.contains("swiped") ? -128 : 0;
    startX = ev.clientX; startY = ev.clientY;
    try { item.setPointerCapture && item.setPointerCapture(ev.pointerId); } catch (_) {}
    item.dataset.touch = "1";
  }, { passive: true });
  document.addEventListener("pointermove", (ev) => {
    if (!dragging) return;
    const item = dragging.querySelector(".mail-item");
    const dx = ev.clientX - startX;
    const dy = ev.clientY - startY;
    if (!moved && Math.abs(dx) < 8 && Math.abs(dy) < 8) return;
    if (!moved && Math.abs(dy) > Math.abs(dx)) { dragging = null; return; } /* вертикальный скролл */
    moved = true;
    let t = Math.max(-128, Math.min(0, baseX + dx));
    item.style.transform = "translateX(" + t + "px)";
    item.style.transition = "none";
  }, { passive: true });
  document.addEventListener("pointerup", (ev) => {
    if (!dragging) return;
    const wrap = dragging; dragging = null;
    const item = wrap.querySelector(".mail-item");
    if (!moved) { delete item.dataset.touch; return; }
    item.style.transition = "";
    item.style.transform = "";
    const swiped = wrap.classList.contains("swiped");
    wrap.classList.toggle("swiped", !swiped);
    delete item.dataset.touch;
  }, { passive: true });
  /* кнопки действий */
  document.addEventListener("click", (ev) => {
    const read = ev.target.closest(".act-read");
    if (read) { ev.stopPropagation(); Mail.toggleReadById(read.dataset.id); return; }
    const del = ev.target.closest(".act-del");
    if (del) { ev.stopPropagation(); Mail.deleteById(del.dataset.id, del); return; }
    if (!ev.target.closest(".mail-item-wrap")) Mail.closeSwipes();
  });
})();

/* ── Обновление списка (кнопка + pull-to-refresh) ─────── */
Mail.refreshList = async function () {
  const btn = Mail.$("btn-refresh");
  if (btn) btn.classList.add("spinning");
  try {
    if (Mail.STATE.tab === "outbox") await Mail.loadOutbox();
    else if (Mail.STATE.tab === "drafts") await Mail.loadDrafts();
    else await Mail.loadMails();
  } catch (_) { /* список не обновился — остаётся старый */ }
  if (btn) setTimeout(() => btn.classList.remove("spinning"), 420);
};

(function () {
  if (!("ontouchstart" in window)) return;
  const THRESHOLD = 72;
  let startY = 0, startX = 0, active = false, dist = 0;
  const ind = document.createElement("div");
  ind.className = "ptr-indicator";
  ind.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" aria-hidden="true"><path d="M21 12a9 9 0 1 1-6.2-8.56"/></svg>';
  document.body.appendChild(ind);

  const canPull = () =>
    !Mail.$("list-view").hidden &&
    (window.scrollY || document.documentElement.scrollTop || 0) <= 0;

  document.addEventListener("touchstart", (ev) => {
    if (!canPull()) return;
    const t = ev.target;
    if (t && t.closest && t.closest("button, a, input, textarea, select, .mail-actions")) return;
    if (!ev.touches || !ev.touches[0]) return;
    startY = ev.touches[0].clientY;
    startX = ev.touches[0].clientX;
    active = true; dist = 0;
  }, { passive: true });

  document.addEventListener("touchmove", (ev) => {
    if (!active) return;
    const dy = ev.touches[0].clientY - startY;
    const dx = ev.touches[0].clientX - startX;
    if (Math.abs(dx) > Math.abs(dy) || dy <= 0) { if (dist) ind.style.setProperty("--pull", 0); dist = 0; return; }
    dist = Math.min(dy * 0.5, THRESHOLD + 40);
    ind.style.setProperty("--pull", dist);
    if (ev.cancelable) ev.preventDefault();
  }, { passive: false });

  document.addEventListener("touchend", () => {
    if (!active) return;
    active = false;
    if (dist >= THRESHOLD) {
      ind.classList.add("loading");
      Mail.refreshList().finally(() => {
        setTimeout(() => { ind.classList.remove("loading"); ind.style.setProperty("--pull", 0); }, 500);
      });
    } else {
      ind.style.setProperty("--pull", 0);
    }
    dist = 0;
  }, { passive: true });
})();
