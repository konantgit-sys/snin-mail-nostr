/* Mail.main — инициализация: STATE, события, старт. */
"use strict";

Mail.STATE = Object.assign(Mail.STATE || {}, { mails: [], outbox: [], drafts: [], tab: "inbox", current: null, query: "", deleteArm: 0, owner: "", attach: [], draftId: 0 });

/* ── события ─────────────────────────────────────────── */
Mail.$("login-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const mode = Mail.STATE.loginMode === "nsec" ? "nsec" : "pass";
  let payload;
  if (mode === "nsec") {
    payload = { nsec: Mail.$("login-nsec").value.trim() };
  } else {
    payload = { address: Mail.$("login-addr").value.trim(), password: Mail.$("login-pass").value };
  }
  const r = await Mail.api("/api/login", { method: "POST", body: JSON.stringify(payload) });
  if (r.ok) {
    Mail.$("login-pass").value = "";
    Mail.$("login-nsec").value = "";
    Mail.$("login-error").hidden = true;
    if (r.token) { Mail.STATE.token = r.token; localStorage.setItem("nm_token", r.token); }
    await Mail.loadStatus();
    Mail.STATE._stagger = true;
    await Mail.loadMails();
  } else {
    Mail.$("login-error").textContent = mode === "nsec"
      ? (r.error === "нет ящика для этого ключа — сначала зарегистрируйся" ? "Для этого ключа нет ящика — зарегистрируйся" : "Неверный ключ")
      : (r.error === "unknown address" ? "Ящик с таким адресом не найден" : "Неверный пароль");
    Mail.$("login-error").hidden = false;
    (mode === "nsec" ? Mail.$("login-nsec") : Mail.$("login-pass")).select();
  }
});

/* переключатель способа входа: пароль / nsec */
function setLoginMode(mode) {
  Mail.STATE.loginMode = mode;
  const nsecMode = mode === "nsec";
  Mail.$("login-addr").hidden = nsecMode;
  Mail.$("login-pass").hidden = nsecMode;
  Mail.$("login-nsec").hidden = !nsecMode;
  Mail.$("mode-pass").classList.toggle("active", !nsecMode);
  Mail.$("mode-nsec").classList.toggle("active", nsecMode);
  Mail.$("mode-pass").setAttribute("aria-selected", String(!nsecMode));
  Mail.$("mode-nsec").setAttribute("aria-selected", String(nsecMode));
  (nsecMode ? Mail.$("login-nsec") : Mail.$("login-addr")).focus();
}
Mail.$("mode-pass").addEventListener("click", () => setLoginMode("pass"));
Mail.$("mode-nsec").addEventListener("click", () => setLoginMode("nsec"));

Mail.$("register-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const nsec = Mail.$("reg-nsec").value.trim();
  const label = Mail.$("reg-label").value.trim();
  const pass = Mail.$("reg-pass").value;
  const r = await Mail.api("/api/register", { method: "POST", body: JSON.stringify({ nsec, label, password: pass }) });
  if (r.ok) {
    Mail.$("register-error").textContent = "";
    Mail.$("register-error").hidden = true;
    Mail.$("register-view").hidden = true;
    Mail.$("login-view").hidden = false;
    Mail.$("login-addr").value = r.address;
    Mail.$("login-pass").value = "";
    Mail.$("login-pass").focus();
  } else {
    const msg = r.error === "already registered" ? "Этот ключ уже зарегистрирован — войди" :
                r.error === "invalid nsec" ? "Неверный nsec" :
                r.error === "password too short" ? "Пароль короче 6 символов" : "Ошибка регистрации";
    Mail.$("register-error").textContent = msg;
    Mail.$("register-error").hidden = false;
  }
});

Mail.$("btn-show-register").addEventListener("click", () => {
  Mail.$("login-view").hidden = true;
  Mail.$("register-view").hidden = false;
  Mail.$("reg-nsec").focus();
});
Mail.$("btn-show-reset").addEventListener("click", () => {
  Mail.$("login-view").hidden = true;
  Mail.$("reset-view").hidden = false;
});
Mail.$("btn-reset-back").addEventListener("click", () => {
  Mail.$("reset-view").hidden = true;
  Mail.$("login-view").hidden = false;
});
Mail.$("reset-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const r = await Mail.api("/api/reset-password", {
    method: "POST",
    body: JSON.stringify({
      address: Mail.$("reset-addr").value.trim(),
      nsec: Mail.$("reset-nsec").value.trim(),
      new_password: Mail.$("reset-pass").value,
    }),
  });
  if (r.ok) {
    Mail.$("reset-error").hidden = true;
    alert("Пароль сброшен. Войдите с новым паролем.");
    Mail.$("reset-view").hidden = true;
    Mail.$("login-view").hidden = false;
    Mail.$("login-addr").value = Mail.$("reset-addr").value.trim();
  } else {
    Mail.$("reset-error").textContent = r.error || "Ошибка";
    Mail.$("reset-error").hidden = false;
  }
});
Mail.$("btn-show-login").addEventListener("click", () => {
  Mail.$("register-view").hidden = true;
  Mail.$("login-view").hidden = false;
  Mail.$("login-addr").focus();
});

Mail.$("btn-logout").addEventListener("click", async () => {
  await Mail.api("/api/logout", { method: "POST" }).catch(() => {});
  Mail.STATE.token = "";
  localStorage.removeItem("nm_token");
  location.reload();
});

Mail.$("btn-compose").addEventListener("click", () => Mail.openComposer("Новое письмо"));
Mail.$("account-trigger").addEventListener("click", Mail.toggleAccountModal);
Mail.$("account-backdrop").addEventListener("click", Mail.closeAccountModal);
Mail.$("account-modal-list").addEventListener("click", (ev) => {
  const item = ev.target.closest(".account-item");
  if (!item) return;
  Mail.setAccount(item.dataset.pubkey);
  Mail.closeAccountModal();
});
document.addEventListener("keydown", (ev) => {
  if (ev.key === "Escape") Mail.closeAccountModal();
});
Mail.$("btn-close").addEventListener("click", Mail.closeComposer);
Mail.$("btn-cancel").addEventListener("click", Mail.closeComposer);
Mail.$("compose-backdrop").addEventListener("click", (ev) => {
  if (ev.target === Mail.$("compose-backdrop")) Mail.closeComposer();
});
Mail.$("compose-form").addEventListener("submit", Mail.sendMail);

Mail.$("btn-reply").addEventListener("click", Mail.replyTo);
Mail.$("btn-forward").addEventListener("click", Mail.forwardTo);
Mail.$("btn-archive").addEventListener("click", Mail.archiveMail);
Mail.$("btn-refresh").addEventListener("click", Mail.refreshList);
Mail.$("btn-back").addEventListener("click", Mail.backToList);
Mail.$("btn-delete").addEventListener("click", Mail.deleteMail);
Mail.$("btn-unread").addEventListener("click", Mail.toggleRead);
Mail.$("mail-address").addEventListener("click", Mail.copyAddress);

document.addEventListener("keydown", (ev) => {
  if (ev.key === "Escape" && !Mail.$("compose-backdrop").hidden) Mail.closeComposer();
  if (ev.key === "Escape" && !Mail.$("help-backdrop").hidden) Mail.closeHelp();
});

/* ── инструкция ── */
Mail.openHelp = function () { Mail.$("help-backdrop").hidden = false; };
Mail.closeHelp = function () { Mail.$("help-backdrop").hidden = true; };
Mail.$("btn-help").addEventListener("click", Mail.openHelp);
Mail.$("btn-help-login").addEventListener("click", Mail.openHelp);
Mail.$("btn-help-close").addEventListener("click", Mail.closeHelp);
Mail.$("btn-help-close2").addEventListener("click", Mail.closeHelp);
Mail.$("help-backdrop").addEventListener("click", (ev) => {
  if (ev.target === Mail.$("help-backdrop")) Mail.closeHelp();
});

let _searchTimer = null;
Mail.$("search").addEventListener("input", () => {
  Mail.STATE.query = Mail.$("search").value.trim();
  clearTimeout(_searchTimer);
  _searchTimer = setTimeout(() => {
    if (Mail.STATE.tab === "drafts") {
      Mail.renderList();  // черновики фильтруются по загруженным
    } else {
      Mail.loadMails();   // серверный поиск по всей БД (q передаётся в URL)
    }
  }, 250);
});

document.querySelectorAll(".tab").forEach((t) => {
  t.addEventListener("click", () => Mail.switchTab(t.dataset.tab));
});

/* ── старт ── */
(async () => {
  await Mail.loadStatus();
  if (!Mail.$("main-view").hidden) {
    Mail.STATE._stagger = true;
    await Mail.loadMails();
  }
})();

/* ── scroll-reveal (стиль SNIN Network) ── */
(function () {
  const els = document.querySelectorAll(".reveal");
  if (!els.length) return;
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (reduced || !("IntersectionObserver" in window)) {
    els.forEach((el) => el.classList.add("visible"));
    return;
  }
  const io = new IntersectionObserver((entries) => {
    entries.forEach((e) => {
      if (e.isIntersecting) { e.target.classList.add("visible"); io.unobserve(e.target); }
    });
  }, { threshold: 0.12, rootMargin: "0px 0px -40px 0px" });
  els.forEach((el) => io.observe(el));
})();
