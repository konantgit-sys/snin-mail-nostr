#!/usr/bin/env python3
"""Сборка фронта (Фаза 3): один бандл app.<hash>.js (esbuild) + app.<hash>.css.

index.html — только разметка + ссылки на бандлы (fingerprint в имени).
Старые app.*.js/css удаляются. Использование: python3 build.py (из каталога сайта).
"""
import hashlib
import os
import pathlib
import re
import shutil
import subprocess
import sys

BASE = pathlib.Path(__file__).resolve().parent / "static"
JS_DIR = BASE / "js"


def _find_esbuild() -> pathlib.Path:
    """esbuild: локальный бинарник → node_modules → PATH (для публичного репо)."""
    candidates = [
        pathlib.Path(os.path.expanduser("~/data/tools/esbuild/node_modules/.bin/esbuild")),
        pathlib.Path(__file__).resolve().parent / "node_modules/.bin/esbuild",
    ]
    for c in candidates:
        if c.exists():
            return c
    w = shutil.which("esbuild")
    if w:
        return pathlib.Path(w)
    return candidates[0]


ESBUILD = _find_esbuild()
ORDER = ["core.js", "api.js", "inbox.js", "detail.js", "composer.js", "main.js"]


def build_js_bundle() -> pathlib.Path:
    """Собирает один minified-бандл, возвращает путь к app.<hash>.js."""
    if not ESBUILD.exists():
        sys.exit("esbuild не найден: установите через 'npm install esbuild' или положите бинарник в node_modules/.bin")
    entry = JS_DIR / "entry.js"
    entry.write_text(
        "/* точка входа Фазы 3: порядок = зависимости (core → api → экраны → main) */\n"
        + "".join(f'import "./{f}";\n' for f in ORDER),
        encoding="utf-8",
    )
    tmp = BASE / "app.tmp.js"
    subprocess.run(
        [str(ESBUILD), str(entry), "--bundle", "--minify", "--format=iife",
         "--target=es2020", "--charset=utf8", "--legal-comments=none",
         "--outfile=" + str(tmp)],
        check=True, capture_output=True,
    )
    h = hashlib.sha256(tmp.read_bytes()).hexdigest()[:12]
    out = BASE / f"app.{h}.js"
    if not out.exists():
        tmp.rename(out)
    else:
        tmp.unlink()
    return out


def copy_css() -> pathlib.Path:
    """CSS: копия style.css → app.<hash>.css (без minify — ноль риска)."""
    css = (BASE / "style.css").read_bytes()
    h = hashlib.sha256(css).hexdigest()[:12]
    out = BASE / f"app.{h}.css"
    out.write_bytes(css)
    return out


def write_index(js_name: str, css_name: str) -> None:
    tpl = (BASE / "templates" / "index.src.html").read_text(encoding="utf-8")
    link = f'<link rel="stylesheet" href="/static/{css_name}">'
    script = f'<script src="/static/{js_name}" defer></script>'
    out = tpl.replace("__CSS__", link).replace("__JS__", script)
    (BASE / "index.html").write_text(out, encoding="utf-8")


def clean_old(current: list[pathlib.Path]) -> None:
    """Удалить старые app.*.js/css, не входящие в текущий набор."""
    for pat in ("app.*.js", "app.*.css", "app.tmp.js"):
        for f in BASE.glob(pat):
            if f not in current:
                f.unlink()


if __name__ == "__main__":
    js = build_js_bundle()
    css = copy_css()
    write_index(js.name, css.name)
    clean_old([js, css])
    gz = len(__import__("gzip").compress(js.read_bytes()))
    print(f"index.html: {(BASE/'index.html').stat().st_size} байт")
    print(f"бандл: {js.name} ({js.stat().st_size/1024:.1f} КБ, gzip {gz/1024:.1f} КБ)")
    print(f"css:   {css.name} ({css.stat().st_size/1024:.1f} КБ)")
