/* Mail.detail — открытие письма, прочитано/нет, удаление (2 клика), назад. */
"use strict";

Mail.openMail = async function (id, isOutbox = false) {
  if (isOutbox) {
    const m = Mail.STATE.outbox.find((x) => x.id === id);
    if (!m) return;
    Mail.STATE.current = { ...m, isOutbox: true, is_read: true, to: m.to };
    Mail.$("list-view").hidden = true;
    Mail.$("hero-view").hidden = true;
    Mail.$("detail-view").hidden = false;
    Mail.enter(Mail.$("detail-view"));
    Mail.renderDetail();
    return;
  }
  Mail.renderDetailSkeleton();
  Mail.$("list-view").hidden = true;
  Mail.$("hero-view").hidden = true;
  Mail.$("detail-view").hidden = false;
  Mail.enter(Mail.$("detail-view"));
  const d = await Mail.api("/api/mails/" + id);
  if (!d.ok) return;
  Mail.STATE.current = d.mail;
  Mail.renderDetail();
  const item = Mail.STATE.mails.find((m) => m.id === id);
  if (item) item.is_read = true;
  const unread = Mail.STATE.mails.filter((m) => !m.is_read).length;
  Mail.$("inbox-count").textContent = unread;
  Mail.$("inbox-count").hidden = !unread;
};

Mail.renderDetailSkeleton = function () {
  const card = Mail.$("mail-card");
  if (!card) return;
  card.innerHTML =
    `<div class="sk-head">
       <div class="skeleton-avatar shimmer"></div>
       <div class="skeleton-lines">
         <div class="skeleton-line shimmer" style="width:64%"></div>
         <div class="skeleton-line shimmer" style="width:40%"></div>
       </div>
     </div>
     <div class="skeleton-lines sk-body">
       <div class="skeleton-line shimmer" style="width:100%"></div>
       <div class="skeleton-line shimmer" style="width:92%"></div>
       <div class="skeleton-line shimmer" style="width:97%"></div>
       <div class="skeleton-line shimmer" style="width:88%"></div>
       <div class="skeleton-line shimmer" style="width:94%"></div>
     </div>`;
};

Mail.renderDetail = function () {
  const m = Mail.STATE.current;
  const card = Mail.$("mail-card");
  const who = m.isOutbox ? (m.to || "—") : (m.from || "—");
  const when = m.isOutbox ? Mail.fmtDate(m.sent_at) : Mail.fmtDate(m.received_at);
  card.innerHTML =
    `<div class="mail-card-head">
       <div class="mail-avatar">${Mail.esc((m.isOutbox ? "→" : (m.from || "?"))[0].toUpperCase())}</div>
       <div class="mail-card-meta">
         <h2>${Mail.esc(m.subject || "(без темы)")}</h2>
         <div class="mail-meta">
           <div><b>${m.isOutbox ? "Кому:" : "От:"}</b> <span class="mono">${Mail.esc(who)}</span></div>
           ${m.isOutbox ? "" : `<div><b>Кому:</b> <span class="mono">${Mail.esc(m.to || "—")}</span></div>`}
           <div><b>Когда:</b> ${when}</div>
         </div>
       </div>
     </div>
     <div class="mail-body">${Mail.renderBody(m.body)}</div>
     ${(m.attachments && m.attachments.length) ? Mail.renderAttachList(m.attachments) : ""}`;
  Mail.$("btn-unread").textContent = m.isOutbox ? "" : (m.is_read ? "Не прочитано" : "Прочитано");
  Mail.$("btn-unread").hidden = !!m.isOutbox;
  Mail.$("btn-archive").textContent = m.archived ? "Из архива" : "В архив";
  Mail.$("btn-archive").hidden = !!m.isOutbox;
};

Mail.archiveMail = async function () {
  const m = Mail.STATE.current;
  if (!m || m.isOutbox) return;
  const target = !m.archived;
  const r = await Mail.api(`/api/mails/${m.id}/archive`, {
    method: "POST",
    body: JSON.stringify({ archived: target }),
  });
  if (!r.ok) { Mail.showToast(r.error || "Ошибка", "err"); return; }
  m.archived = target;
  Mail.$("btn-archive").textContent = target ? "Из архива" : "В архив";
  Mail.showToast(target ? "Письмо в архиве" : "Письмо возвращено во входящие");
  const item = Mail.STATE.mails.find((x) => String(x.id) === String(m.id));
  if (item) item.archived = target;
  if (target) {
    // убрать из текущего списка входящих сразу (без ожидания refresh)
    Mail.STATE.mails = Mail.STATE.mails.filter((x) => String(x.id) !== String(m.id));
    if (Mail.STATE.tab === "inbox") Mail.renderList();
  }
};

/* Типографика тела письма: цитаты (>), разделители (---/***), ссылки.
   Порядок: esc → linkify (иначе теги <a> сломаются экранированием). */
Mail.renderBody = function (text) {
  const lines = String(text || "").split("\n");
  const linkify = (s) => s.replace(/(https?:\/\/[^\s<]+)/g, (m) =>
    `<a class="body-link" href="${m}" target="_blank" rel="noopener noreferrer">${m}</a>`);
  const out = [];
  let inQuote = false;
  const close = () => { if (inQuote) { out.push("</blockquote>"); inQuote = false; } };
  for (const raw of lines) {
    const line = raw.replace(/\s+$/, "");
    const q = line.match(/^>{1,3}\s?(.*)$/);
    if (q) {
      if (!inQuote) { out.push("<blockquote>"); inQuote = true; }
      out.push(linkify(Mail.esc(q[1])) + "\n");
      continue;
    }
    close();
    if (/^([-*_=])\1{2,}$/.test(line.replace(/\s/g, ""))) { out.push("<hr>"); continue; }
    out.push(linkify(Mail.esc(line)) + "\n");
  }
  close();
  return out.join("");
};

Mail.toggleRead = async function () {
  const m = Mail.STATE.current;
  if (!m || m.isOutbox) return;
  const target = !m.is_read;
  const r = await Mail.api(`/api/mails/${m.id}/read`, { method: "POST", body: JSON.stringify({ read: target }) });
  if (!r.ok) { Mail.showToast(r.error || "Ошибка", "err"); return; }
  m.is_read = target;
  Mail.$("btn-unread").textContent = target ? "Не прочитано" : "Прочитано";
  const item = Mail.STATE.mails.find((x) => x.id === m.id);
  if (item) item.is_read = target;
  Mail.showToast(target ? "Отмечено прочитанным" : "Отмечено непрочитанным");
  Mail.renderList();
};

Mail.deleteMail = async function () {
  const btn = Mail.$("btn-delete");
  const now = Date.now();
  if (now - Mail.STATE.deleteArm > 3000) {
    Mail.STATE.deleteArm = now;
    btn.textContent = "Точно удалить?";
    btn.classList.add("armed");
    Mail.showToast("Нажмите ещё раз для подтверждения", "warn");
    setTimeout(() => { btn.textContent = "Удалить"; btn.classList.remove("armed"); }, 3000);
    return;
  }
  const r = await Mail.api("/api/mails/" + Mail.STATE.current.id, { method: "DELETE" });
  Mail.STATE.deleteArm = 0;
  btn.textContent = "Удалить";
  btn.classList.remove("armed");
  if (!r.ok) { Mail.showToast(r.error || "Ошибка удаления", "err"); return; }
  Mail.STATE.mails = Mail.STATE.mails.filter((m) => m.id !== Mail.STATE.current.id);
  Mail.showToast("Письмо удалено");
  Mail.backToList();
};

Mail.backToList = function () {
  Mail.$("detail-view").hidden = true;
  Mail.$("list-view").hidden = false;
  if (Mail.STATE.tab === "inbox") Mail.$("hero-view").hidden = false;
  Mail.enter(Mail.$("list-view"));
  if (Mail.STATE.tab === "inbox") Mail.enter(Mail.$("hero-view"));
  Mail.STATE.current = null;
  Mail.STATE.deleteArm = 0;
  const btn = Mail.$("btn-delete");
  btn.textContent = "Удалить";
  btn.classList.remove("armed");
  Mail.renderList();
};


/* ── вложения в письме ────────────────────────────────── */
Mail.renderAttachList = function (atts) {
  const rows = atts.map((a, i) => {
    const isImg = (a.mime || "").startsWith("image/");
    const src = a.data_base64 ? `data:${a.mime};base64,${a.data_base64}` : (a.url || "");
    const preview = isImg && src
      ? `<img class="attach-preview-img" src="${Mail.esc(src)}" alt="${Mail.esc(a.filename)}" data-i="${i}" title="Открыть ${Mail.esc(a.filename)}">`
      : "";
    const size = a.data_base64 ? (a.data_base64.length * 3) / 4 : 0;
    return `${preview}
      <button type="button" class="attach-download" data-i="${i}" title="Скачать ${Mail.esc(a.filename)}">
        <span class="att-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${Mail.attachIcon(a.mime)}</svg></span>
        <span class="att-name">${Mail.esc(a.filename)}</span>
        <span class="attach-size">${size ? Mail.fmtSize(size) : "Blossom"}</span>
        <svg class="att-go" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 10 5 5 5-5"/><path d="M12 15V3"/></svg>
      </button>`;
  }).join("");
  return `<div class="mail-attachments"><div class="attachments-title">Вложения</div><div class="attachments-list">${rows}</div></div>`;
};

Mail.downloadAttachment = function (idx) {
  const m = Mail.STATE.current;
  if (!m || !m.attachments || !m.attachments[idx]) return;
  const a = m.attachments[idx];
  if (a.url) {
    const link = document.createElement("a");
    link.href = a.url;
    link.target = "_blank";
    link.rel = "noopener";
    document.body.appendChild(link);
    link.click();
    link.remove();
    return;
  }
  try {
    const bytes = atob(a.data_base64);
    const arr = new Uint8Array(bytes.length);
    for (let i = 0; i < bytes.length; i++) arr[i] = bytes.charCodeAt(i);
    const blob = new Blob([arr], { type: a.mime || "application/octet-stream" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = a.filename || "file";
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 4000);
  } catch (e) {
    Mail.showToast("Не удалось открыть вложение: " + e.message);
  }
};

document.addEventListener("click", (ev) => {
  const img = ev.target.closest(".attach-preview-img");
  if (img) {
    const m = Mail.STATE.current;
    const a = m && m.attachments && m.attachments[Number(img.dataset.i)];
    if (!a) return;
    if (a.url) { window.open(a.url, "_blank", "noopener"); return; }
    if (a.data_base64) {
      try {
        const bytes = atob(a.data_base64);
        const arr = new Uint8Array(bytes.length);
        for (let i = 0; i < bytes.length; i++) arr[i] = bytes.charCodeAt(i);
        const url = URL.createObjectURL(new Blob([arr], { type: a.mime || "image/png" }));
        window.open(url, "_blank");
        setTimeout(() => URL.revokeObjectURL(url), 5000);
      } catch (e) { Mail.showToast("Не удалось открыть: " + e.message); }
    }
    return;
  }
  const btn = ev.target.closest(".attach-download");
  if (btn) Mail.downloadAttachment(Number(btn.dataset.i));
});
