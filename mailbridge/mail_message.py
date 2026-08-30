"""
Nostr Mail — формат письма kind:1301.

Content события kind:1301 = RFC 2822-совместимый текст:
    From: <адрес>
    To: <адрес>
    Subject: <тема>
    Date: <RFC 2822 date>
    Message-ID: <id@домен>
    [In-Reply-To: <id>]
    [References: <id1> <id2>]

    <тело письма>

Заголовки UTF-8 как есть (nostr-экосистема UTF-8-first; nostrmail.org рендерит
их напрямую). Message-ID генерируется уникальным — для дедупликации и threads.

Лимит: письмо должно влезать в NIP-44 plaintext (65535 байт), оставляем запас
на JSON-обёртку rumor — MAX_MAIL_SIZE = 60000.
"""

from __future__ import annotations

import datetime
import re

MAIL_KIND = 1301
MAX_MAIL_SIZE = 60000  # байт (запас под NIP-44 лимит 65535 + JSON rumor)

_HEADER_RE = re.compile(r"^([A-Za-z0-9-]+):\s?(.*)$", re.MULTILINE)


def _rfc2822_date(ts: datetime.datetime | None = None) -> str:
    dt = ts or datetime.datetime.now(datetime.timezone.utc)
    return dt.strftime("%a, %d %b %Y %H:%M:%S %z")


def build_mail(
    from_addr: str,
    to_addr: str,
    subject: str,
    body: str,
    date: datetime.datetime | None = None,
    message_id: str | None = None,
    in_reply_to: str | None = None,
    references: str | None = None,
    extra_headers: dict[str, str] | None = None,
    attachments: list[dict] | None = None,
) -> str:
    """Собирает RFC 2822 текст письма. Кидает ValueError при превышении лимита.

    attachments: [{filename, mime, url}] → внешнее вложение по ссылке
    (Blossom NIP-96, часть message/external-body по RFC 2017 + список
    ссылок в конце текста — читается в любом клиенте);
    [{filename, mime, data_base64}] → inline base64 (fallback, RFC 2046).
    """
    import base64
    import uuid

    mid = message_id or f"<{uuid.uuid4().hex}@snin-mail.v2.site>"

    lines = [
        f"From: {from_addr}",
        f"To: {to_addr}",
        f"Subject: {subject}",
        f"Date: {_rfc2822_date(date)}",
        f"Message-ID: {mid}",
    ]
    if in_reply_to:
        lines.append(f"In-Reply-To: {in_reply_to}")
    if references:
        refs = references if isinstance(references, str) else " ".join(references)
        lines.append(f"References: {refs}")

    url_atts = [a for a in (attachments or []) if a.get("url")]
    b64_atts = [a for a in (attachments or []) if not a.get("url")]

    if attachments:
        boundary = f"nb-{uuid.uuid4().hex[:12]}"
        lines.append(f"Content-Type: multipart/mixed; boundary=\"{boundary}\"")
        body_text = body or ""
        if url_atts:
            body_text += "\n\nВложения:\n" + "\n".join(
                f"- {a.get('filename') or 'file'} ({a.get('mime') or 'application/octet-stream'}): {a['url']}"
                for a in url_atts
            )
        parts = [f"--{boundary}", "Content-Type: text/plain; charset=utf-8", "", body_text]
        for att in url_atts:
            fname = (att.get("filename") or "file").replace('"', "'")
            mime = att.get("mime") or "application/octet-stream"
            sha = att.get("sha256") or ""
            parts += [
                f"--{boundary}",
                f'Content-Type: message/external-body; access-type=URL; URL="{att["url"]}"; name="{fname}"',
                f'Content-Disposition: attachment; filename="{fname}"',
                f"X-Attachment-Mime: {mime}",
                f"X-Attachment-Sha256: {sha}" if sha else "X-Attachment-Sha256:",
                "",
                f"Файл: {fname} ({mime}). Скачать: {att['url']}",
            ]
        for att in b64_atts:
            fname = (att.get("filename") or "file").replace('"', "'")
            mime = att.get("mime") or "application/octet-stream"
            data = (att.get("data_base64") or "").replace("\n", "")
            b64_lines = "\n".join(data[i:i + 76] for i in range(0, len(data), 76))
            parts += [
                f"--{boundary}",
                f'Content-Type: {mime}; name="{fname}"',
                f'Content-Disposition: attachment; filename="{fname}"',
                "Content-Transfer-Encoding: base64",
                "",
                b64_lines,
            ]
        parts.append(f"--{boundary}--")
        mail = "\n".join(lines) + "\n\n" + "\n".join(parts)
    else:
        mail = "\n".join(lines) + "\n\n" + (body or "")

    if len(mail.encode("utf-8")) > MAX_MAIL_SIZE:
        raise ValueError(
            f"письмо {len(mail.encode('utf-8'))} байт > лимита {MAX_MAIL_SIZE}"
        )
    return mail


def parse_mail(text: str) -> dict:
    """Разбирает RFC 2822 текст письма в dict. Не строгий — выживает при мусоре."""
    if "\n\n" in text:
        header_block, body = text.split("\n\n", 1)
    elif "\r\n\r\n" in text:
        header_block, body = text.split("\r\n\r\n", 1)
    else:
        header_block, body = text, ""

    headers: dict[str, str] = {}
    for match in _HEADER_RE.finditer(header_block):
        name = match.group(1).lower()
        value = match.group(2).strip()
        # продолжаем многострочные заголовки (folding) — редкий случай, ок
        headers[name] = value

    attachments: list[dict] = []
    if "multipart" in headers.get("content-type", "").lower():
        parts = _parse_parts(header_block, body)
        # тело = первая text/plain часть без Content-Disposition: attachment
        for ph, pbody in parts:
            ctype = ph.get("content-type", "").lower()
            disp = ph.get("content-disposition", "")
            if "text/plain" in ctype and "attachment" not in disp.lower():
                body = pbody
                break
        attachments = [p for ph, pbody in parts
                       if "attachment" in ph.get("content-disposition", "").lower()
                       for p in [_part_to_attachment(ph, pbody)] if p]

    return {
        "from": headers.get("from", ""),
        "to": headers.get("to", ""),
        "subject": headers.get("subject", ""),
        "date": headers.get("date", ""),
        "message_id": headers.get("message-id", ""),
        "in_reply_to": headers.get("in-reply-to", ""),
        "references": headers.get("references", ""),
        "body": body,
        "headers": headers,
        "attachments": attachments,
    }


def _parse_parts(header_block: str, body: str) -> list[tuple[dict, str]]:
    """Разбирает multipart/mixed на [(заголовки части, тело части)]."""
    import re

    m = re.search(r'Content-Type:\s*multipart/mixed;\s*boundary="?([^"\s;]+)"?', header_block, re.IGNORECASE)
    if not m:
        return []
    boundary = m.group(1)
    out: list[tuple[dict, str]] = []
    for part in body.split(f"--{boundary}"):
        part = part.strip("\r\n ")
        if not part or part in ("--",):
            continue
        if "\n\n" in part:
            phead, pbody = part.split("\n\n", 1)
        else:
            phead, pbody = part, ""
        ph = {}
        for match in _HEADER_RE.finditer(phead):
            ph[match.group(1).lower()] = match.group(2).strip()
        out.append((ph, pbody))
    return out


def _part_to_attachment(ph: dict, pbody: str) -> dict | None:
    """Часть multipart → {filename, mime, data_base64|url} или None."""
    import base64
    import re

    ctype = ph.get("content-type", "")
    disp = ph.get("content-disposition", "")
    fname = ""
    fm = re.search(r'filename="?([^"\r\n]+)"?', disp, re.IGNORECASE)
    if fm:
        fname = fm.group(1).strip()
    if not fname:
        fm2 = re.search(r'name="?([^"\r\n]+)"?', ctype, re.IGNORECASE)
        if fm2:
            fname = fm2.group(1).strip()
    mime = ctype.split(";")[0].strip() or "application/octet-stream"

    # Blossom NIP-96: вложение по ссылке (message/external-body, RFC 2017)
    if "message/external-body" in ctype.lower():
        um = re.search(r'URL="?([^"\s]+)"?', ctype, re.IGNORECASE)
        url = um.group(1) if um else None
        if url:
            return {"filename": fname or "file",
                    "mime": (ph.get("x-attachment-mime") or "application/octet-stream").strip(),
                    "url": url,
                    "sha256": (ph.get("x-attachment-sha256") or "").strip()}
        return None
    loc = ph.get("content-location", "")
    if loc.strip().startswith("http"):
        return {"filename": fname or "file", "mime": mime, "url": loc.strip()}

    raw = "".join(pbody.split())
    try:
        decoded = base64.b64decode(raw)
        data_b64 = base64.b64encode(decoded).decode()
    except Exception:
        return None
    return {"filename": fname or "file", "mime": mime, "data_base64": data_b64}


def _parse_attachments(header_block: str, body: str) -> list[dict]:
    """(обратная совместимость) — вложения из multipart."""
    out = []
    for ph, pbody in _parse_parts(header_block, body):
        if "attachment" in ph.get("content-disposition", "").lower():
            att = _part_to_attachment(ph, pbody)
            if att:
                out.append(att)
    return out


def _parse_attachments(header_block: str, body: str) -> list[dict]:
    """Разбирает multipart/mixed: возвращает [{filename, mime, data_base64}]."""
    import base64
    import re

    m = re.search(r'Content-Type:\s*multipart/mixed;\s*boundary="?([^"\s;]+)"?', header_block, re.IGNORECASE)
    if not m:
        return []
    boundary = m.group(1)
    parts = body.split(f"--{boundary}")
    out: list[dict] = []
    for part in parts:
        part = part.strip("\r\n ")
        if not part or part in ("--",):
            continue
        if part.startswith("--") and part.strip() == "--":
            continue
        if "\n\n" in part:
            phead, pbody = part.split("\n\n", 1)
        else:
            phead, pbody = part, ""
        ph = {}
        for match in _HEADER_RE.finditer(phead):
            ph[match.group(1).lower()] = match.group(2).strip()
        ctype = ph.get("content-type", "")
        disp = ph.get("content-disposition", "")
        is_attachment = "attachment" in disp.lower()
        if not is_attachment and "multipart" not in ctype.lower():
            continue  # текстовая часть — это body, не вложение
        fname = ""
        fm = re.search(r'filename="?([^"\r\n]+)"?', disp, re.IGNORECASE)
        if fm:
            fname = fm.group(1).strip()
        if not fname:
            fm2 = re.search(r'name="?([^"\r\n]+)"?', ctype, re.IGNORECASE)
            if fm2:
                fname = fm2.group(1).strip()
        mime = ctype.split(";")[0].strip() or "application/octet-stream"
        raw = "".join(pbody.split())
        try:
            decoded = base64.b64decode(raw)
            data_b64 = base64.b64encode(decoded).decode()
        except Exception:
            continue
        out.append({"filename": fname or "file", "mime": mime, "data_base64": data_b64})
    return out


def extract_addresses(text: str) -> tuple[str, str]:
    """Из RFC 2822 текста — (from_addr, to_addr) для быстрой маршрутизации."""
    parsed = parse_mail(text)
    return parsed["from"], parsed["to"]
