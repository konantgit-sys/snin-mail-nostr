/* Mail.core — namespace, хелперы: DOM, время, экранирование, тосты. */
"use strict";
window.Mail = window.Mail || {};

Mail.$ = (id) => document.getElementById(id);

Mail.esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

Mail.fmtDate = (ts) => {
  if (!ts) return "";
  return new Date(ts * 1000).toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
};

Mail.fmtSize = (n) => {
  if (!n || n <= 0) return "";
  if (n < 1024) return Math.round(n) + " Б";
  if (n < 1048576) return (n / 1024).toFixed(1) + " КБ";
  return (n / 1048576).toFixed(1) + " МБ";
};

/* Иконка файла по MIME (24×24 stroke, как в композере). */
Mail.attachIcon = (mime) => {
  const pdf = '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><path d="M9 15l1.5 1.5L14 13"/>';
  const audio = '<path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/>';
  const video = '<rect x="2" y="6" width="14" height="12" rx="2"/><path d="m22 8-6 4 6 4V8z"/>';
  const zip = '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><path d="M9 13h6l-6 6h6"/>';
  const doc = '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><path d="M8 13h8M8 17h5"/>';
  const t = mime || "";
  if (t === "application/pdf" || t.includes("pdf")) return pdf;
  if (t.startsWith("audio/")) return audio;
  if (t.startsWith("video/")) return video;
  if (t.includes("zip") || t.includes("compressed") || t.includes("tar") || t.includes("rar") || t.includes("7z")) return zip;
  if (t.startsWith("text/") || t.includes("json") || t.includes("javascript") || t.includes("html") || t.includes("xml") || t.includes("document") || t.includes("sheet")) return doc;
  return doc;
};

Mail.fmtAgo = (ts) => {
  if (!ts) return "";
  const diff = Date.now() / 1000 - ts;
  if (diff < 60) return "только что";
  if (diff < 3600) return Math.floor(diff / 60) + " мин назад";
  if (diff < 86400) return Math.floor(diff / 3600) + " ч назад";
  if (diff < 172800) return "вчера";
  return new Date(ts * 1000).toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit", year: "2-digit" });
};

Mail.shortNpub = (npub) => {
  if (!npub) return "";
  return npub.length > 16 ? npub.slice(0, 10) + "…" + npub.slice(-6) : npub;
};

/* ── toast ─────────────────────────────────────────── */
let toastTimer = null;
Mail.showToast = (msg, kind = "ok") => {
  const t = Mail.$("toast");
  t.textContent = msg;
  t.dataset.kind = kind;
  t.classList.remove("toast-visible");
  void t.offsetWidth; /* рестарт анимации */
  t.classList.add("toast-visible");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove("toast-visible"), 2800);
};

/* Аватар: инициал + стабильный цвет по ключу (hash → hue). */
Mail.avatarOf = function (addr) {
  const key = (addr || "").split("@")[0] || "?";
  let h = 0;
  for (let i = 0; i < key.length; i++) h = (h * 31 + key.charCodeAt(i)) >>> 0;
  const hue = h % 360;
  const ch = (addr && addr[0] === "n") ? key[5] || "N" : (key[0] || "?").toUpperCase();
  return { ch, bg: `linear-gradient(135deg, hsl(${hue} 72% 58%), hsl(${(hue + 40) % 360} 72% 44%))` };
};

/* Плавный вход экрана: fade + slide (только transform/opacity, ease-out, <300ms).
   dist=0 — чистый fade (для больших контейнеров). */
Mail.enter = function (el, dist = 10) {
  if (!el || el.hidden) return;
  el.classList.remove("view-in", "view-in-fade");
  void el.offsetWidth;
  el.classList.add(dist ? "view-in" : "view-in-fade");
};
