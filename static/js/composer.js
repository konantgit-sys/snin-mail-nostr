/* Mail.composer — композер: вложения через Blossom (чанками, до 5 МБ),
   валидация, отправка. Прокси *.v2.site режет тело запроса ~1 МБ — поэтому
   файлы грузим кусками по 400 КБ через /api/blossom/upload-chunk. */
"use strict";

Mail.STATE = Mail.STATE || {};
Mail.STATE.attach = [];  // [{name, size, mime, _file, url, sha256, pct}]

const ATTACH_MAX_BYTES = 5 * 1024 * 1024;  // лимит сервера: 5 МБ на файл
const ATTACH_MAX_COUNT = 5;
const CHUNK_BYTES = 400 * 1024;            // кусок 400 КБ raw → base64 ~533 КБ (< 1 МБ прокси)

Mail.openComposer = function (title, to = "", subject = "", replyTo = "", body = "") {
  Mail.STATE.draftId = 0;  // новое письмо / ответ / переслать — не черновик
  Mail.$("compose-title").textContent = title;
  Mail.$("compose-to").value = to;
  Mail.$("compose-subject").value = subject;
  Mail.$("compose-body").value = "";
  Mail.$("compose-form").dataset.replyTo = replyTo || "";
  Mail.$("compose-body").value = body || "";
  Mail.$("compose-error").hidden = true;
  const fromEl = Mail.$("compose-from");
  if (fromEl) {
    const addr = (Mail.$("mail-address").textContent || "").trim();
    fromEl.innerHTML = addr && addr !== "—"
      ? "От: <b>" + Mail.esc(addr) + "</b>"
      : "";
  }
  const bd = Mail.$("compose-backdrop");
  const form = Mail.$("compose-form");
  /* стек-эффект: панель растёт от кнопки «Написать» (design-rules: popover от триггера) */
  const trig = Mail.$("btn-compose");
  if (trig) {
    const r = trig.getBoundingClientRect();
    form.style.transformOrigin =
      ((r.left + r.width / 2) / window.innerWidth * 100).toFixed(1) + "% " +
      ((r.top + r.height / 2) / window.innerHeight * 100).toFixed(1) + "%";
  }
  form.classList.remove("compose-in");
  bd.classList.remove("modal-in");
  bd.hidden = false;
  void bd.offsetWidth;
  bd.classList.add("modal-in");
  form.classList.add("compose-in");
  setTimeout(() => Mail.$("compose-to").focus(), 120);
};

Mail.closeComposer = function () {
  const bd = Mail.$("compose-backdrop");
  bd.classList.remove("modal-in");
  bd.hidden = true;
  Mail.$("compose-progress").hidden = true;
  /* автосохранение черновика: если что-то заполнено — пишем в БД */
  const to = Mail.$("compose-to").value.trim();
  const subject = Mail.$("compose-subject").value.trim();
  const body = Mail.$("compose-body").value.trim();
  if ((to || subject || body) && !Mail.STATE._skipDraftSave) {
    const atts = Mail.STATE.attach
      .filter((a) => a.url)
      .map((a) => ({ filename: a.name, mime: a.mime, url: a.url, sha256: a.sha256 || "" }));
    Mail.api("/api/drafts", {
      method: "POST",
      body: JSON.stringify({ id: Mail.STATE.draftId || 0, to_addr: to, subject, body, attachments: atts }),
    }).then((r) => {
      if (r && r.ok && !r.deleted) {
        Mail.STATE.draftId = r.id;
        Mail.showToast("Черновик сохранён");
      }
    }).catch(() => {});
  }
  Mail.STATE.attach.forEach((a) => { if (a._thumb) URL.revokeObjectURL(a._thumb); });
  Mail.STATE.attach = [];
  renderAttachments();
};

/* Открыть черновик в композере (из вкладки «Черновики»). */
Mail.openDraft = async function (d) {
  let draft = d;
  try {
    const r = await Mail.api("/api/drafts/" + d.id);
    if (r && r.ok) draft = r.draft;
  } catch (_) { /* используем то, что в списке */ }
  Mail.STATE.draftId = draft.id;
  Mail.STATE.attach = (draft.attachments || []).map((a) => ({
    name: a.filename || a.name || "файл", size: 0, mime: a.mime || "application/octet-stream",
    url: a.url || "", sha256: a.sha256 || "", uploading: false, pct: 100,
  }));
  Mail.openComposer("Черновик", draft.to || "", draft.subject || "", "", draft.body || "");
  Mail.STATE.draftId = draft.id;  // openComposer сбросил — вернуть id
  renderAttachments();
  Mail.$("compose-title").textContent = "Черновик";
};

/* ── вложения ─────────────────────────────────────────── */
function fmtSize(n) {
  if (n < 1024) return n + " Б";
  if (n < 1048576) return (n / 1024).toFixed(1) + " КБ";
  return (n / 1048576).toFixed(1) + " МБ";
}

/* Миниатюра: image/* — реальное превью файла, остальное — иконка по типу. */
function attachThumb(a) {
  if (a.mime && a.mime.startsWith("image/") && a._thumb) {
    return `<span class="attach-thumb"><img src="${a._thumb}" alt="" loading="lazy"></span>`;
  }
  const pdf = '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><path d="M9 15l1.5 1.5L14 13"/>';
  const audio = '<path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/>';
  const video = '<rect x="2" y="6" width="14" height="12" rx="2"/><path d="m22 8-6 4 6 4V8z"/>';
  const zip = '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><path d="M9 13h6l-6 6h6"/>';
  const t = a.mime || "";
  let ic = pdf;
  if (t.startsWith("audio/")) ic = audio;
  else if (t.startsWith("video/")) ic = video;
  else if (t.includes("zip") || t.includes("compressed") || t.includes("tar")) ic = zip;
  return `<span class="attach-thumb"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${ic}</svg></span>`;
}

function renderAttachments() {
  const box = Mail.$("compose-attachments");
  if (!Mail.STATE.attach.length) { box.innerHTML = ""; box.hidden = true; return; }
  box.hidden = false;
  box.innerHTML = Mail.STATE.attach.map((a, i) => {
    const status = a.uploading
      ? `<span class="attach-progress"><i style="width:${a.pct || 0}%"></i></span><span class="attach-pct">${a.pct || 0}%</span>`
      : (a.url
        ? `<span class="attach-ok" title="Загружено в Blossom"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg></span>`
        : "");
    return `<span class="attach-card" data-i="${i}" style="--i:${i}">
      ${attachThumb(a)}
      <span class="attach-meta">
        <span class="attach-name" title="${Mail.esc(a.name)}">${Mail.esc(a.name)}</span>
        <span class="attach-sub">${fmtSize(a.size)}${status}</span>
      </span>
      <button type="button" class="attach-remove" data-i="${i}" aria-label="Убрать">×</button>
    </span>`;
  }).join("");
  box.querySelectorAll(".attach-remove").forEach((b) =>
    b.addEventListener("click", () => {
      const i = Number(b.dataset.i);
      const a = Mail.STATE.attach[i];
      if (!a || a.uploading) return;
      if (a._thumb) URL.revokeObjectURL(a._thumb);
      Mail.STATE.attach.splice(i, 1);
      renderAttachments();
    })
  );
}

Mail.$("btn-attach").addEventListener("click", () => Mail.$("attach-input").click());
Mail.$("attach-input").addEventListener("change", (ev) => {
  const files = [...ev.target.files];
  for (const f of files) {
    if (Mail.STATE.attach.length >= ATTACH_MAX_COUNT) {
      Mail.showToast(`Максимум ${ATTACH_MAX_COUNT} вложений`, "err"); break;
    }
    if (f.size > ATTACH_MAX_BYTES) {
      Mail.showToast(`«${f.name}» больше 5 МБ — лимит сервера`, "err"); continue;
    }
    if (f.size === 0) { Mail.showToast(`«${f.name}» пустой`, "err"); continue; }
    const att = { name: f.name, size: f.size, mime: f.type || "application/octet-stream", _file: f, uploading: false, pct: 0, url: "", sha256: "" };
    if (f.type && f.type.startsWith("image/")) att._thumb = URL.createObjectURL(f);
    Mail.STATE.attach.push(att);
  }
  ev.target.value = "";
  renderAttachments();
});

/* Загрузка одного файла в Blossom кусками по 400 КБ. */
Mail.uploadToBlossom = function (att) {
  return new Promise((resolve, reject) => {
    const file = att._file;
    const total = Math.max(1, Math.ceil(file.size / CHUNK_BYTES));
    let upId = "";
    let loaded = 0;
    const next = (i) => {
      if (i >= total) return resolve();
      const slice = file.slice(i * CHUNK_BYTES, Math.min(file.size, (i + 1) * CHUNK_BYTES));
      const fr = new FileReader();
      fr.onload = async () => {
        const b64 = String(fr.result).split(",")[1] || "";
        const payload = { filename: file.name, mime: att.mime, total, index: i, data_base64: b64 };
        if (upId) payload.upload_id = upId;
        try {
          const r = await Mail.api("/api/blossom/upload-chunk", { method: "POST", body: JSON.stringify(payload) });
          if (!r.ok) return reject(new Error(r.error || "ошибка загрузки"));
          if (r.done) return resolve({ url: r.url, sha256: r.sha256 });
          upId = upId || r.upload_id || upId;
          loaded += slice.size;
          att.pct = Math.max(1, Math.min(99, Math.round((loaded / file.size) * 100)));
          renderAttachments();
          next(i + 1);
        } catch (e) { reject(e); }
      };
      fr.onerror = () => reject(new Error("Не удалось прочитать файл"));
      fr.readAsDataURL(slice);
    };
    next(0);
  });
};

Mail.sendMail = async function (ev) {
  ev.preventDefault();
  const btn = Mail.$("btn-send");
  const to = Mail.$("compose-to").value.trim();
  const subject = Mail.$("compose-subject").value.trim();
  const body = Mail.$("compose-body").value.trim();
  const err = Mail.$("compose-error");

  /* валидация */
  if (!to) { err.textContent = "Укажите адресата (npub или npub@домен)"; err.hidden = false; Mail.$("compose-to").focus(); return; }
  if (!/^(npub1[a-z0-9]{58,62})(@[^\s@]+)?$/i.test(to)) { err.textContent = "Адрес должен быть npub1… (или npub1…@домен)"; err.hidden = false; Mail.$("compose-to").focus(); return; }
  if (!subject) { err.textContent = "Укажите тему письма"; err.hidden = false; Mail.$("compose-subject").focus(); return; }
  if (!body) { err.textContent = "Напишите текст письма"; err.hidden = false; Mail.$("compose-body").focus(); return; }

  btn.disabled = true;
  btn.textContent = "Отправка…";
  err.hidden = true;

  try {
    /* 1) загрузка вложений в Blossom (чанками) */
    const pend = Mail.STATE.attach.filter((a) => !a.url);
    const prog = Mail.$("compose-progress");
    const bar = Mail.$("compose-progress-bar");
    const txt = Mail.$("compose-progress-text");
    if (pend.length) {
      prog.hidden = false;
      bar.innerHTML = "<i></i>";
      const barI = bar.querySelector("i");
      let doneN = 0;
      for (const a of pend) {
        a.uploading = true;
        txt.textContent = `Загрузка «${a.name}»…`;
        const up = await Mail.uploadToBlossom(a);
        a.url = up.url; a.sha256 = up.sha256; a.uploading = false; a.pct = 100;
        doneN += 1;
        barI.style.width = Math.round((doneN / pend.length) * 100) + "%";
        renderAttachments();
      }
      txt.textContent = "Загрузка завершена";
      setTimeout(() => { prog.hidden = true; }, 600);
    }

    /* 2) отправка письма со ссылками на Blossom */
    const attachments = Mail.STATE.attach.map((a) => ({ filename: a.name, mime: a.mime, url: a.url, sha256: a.sha256 }));
    const r = await Mail.api("/api/send", {
      method: "POST",
      body: JSON.stringify({ to_npub: to, subject, body, in_reply_to: Mail.$("compose-form").dataset.replyTo || "", owner: Mail.STATE.owner || "", attachments }),
    });
    if (!r.ok) throw new Error(r.error || "ошибка");
    const sentDraftId = Mail.STATE.draftId || 0;
    Mail.STATE._skipDraftSave = true;  // отправлено — не пересохранять черновик
    Mail.closeComposer();
    Mail.STATE._skipDraftSave = false;
    if (sentDraftId) {
      Mail.api("/api/drafts/" + sentDraftId, { method: "DELETE" }).catch(() => {});
    }
    Mail.showToast(r.published ? `Отправлено на ${r.published} релеев ✅` : "Письмо записано в исходящие ✅");
    await Mail.switchTab("outbox");
  } catch (e) {
    err.textContent = e.message;
    err.hidden = false;
  } finally {
    btn.disabled = false;
    btn.textContent = "Отправить";
  }
};

Mail.replyTo = function () {
  if (!Mail.STATE.current) return;
  const m = Mail.STATE.current;
  const to = m.isOutbox ? "" : m.from;
  const subject = (m.subject || "").startsWith("Re:") ? m.subject : "Re: " + (m.subject || "");
  // цитата оригинала в тело ответа
  const bodyLines = (m.body || "").split("\n").map((l) => "> " + l).join("\n");
  const quote = bodyLines ? "\n\n" + bodyLines + "\n" : "";
  Mail.openComposer("Ответ", to || "", subject, m.isOutbox ? "" : m.message_id, quote);
};

/* Переслать: тело + заголовок оригинала + список вложений (ссылки на Blossom). */
Mail.forwardTo = function () {
  if (!Mail.STATE.current) return;
  const m = Mail.STATE.current;
  const subject = (m.subject || "").startsWith("Fwd:") ? m.subject : "Fwd: " + (m.subject || "(без темы)");
  const from = m.isOutbox ? (m.to || "—") : (m.from || "—");
  const when = m.isOutbox ? Mail.fmtDate(m.sent_at) : Mail.fmtDate(m.received_at);
  let body = "Пересылаемое письмо — от " + from + ", " + when + ":\n\n" + (m.body || "");
  if (m.attachments && m.attachments.length) {
    body += "\n\nВложения:\n";
    body += m.attachments.map((a) => {
      const name = a.filename || a.name || a.sha || "файл";
      const url = a.url || (a.sha ? "/media/" + a.sha : "");
      return url ? name + " — " + url : name;
    }).join("\n");
  }
  Mail.openComposer("Переслать", "", subject, "", body);
};
