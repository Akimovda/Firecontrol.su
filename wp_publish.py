#!/usr/bin/env python3
"""
wp_publish.py — публикация статьи в WordPress как ЧЕРНОВИК через REST API,
с поддержкой FAQ-блока (видимый + JSON-LD schema) и SEO-полей Yoast / Rank Math.

Как работает:
  - читает статью из markdown-файла или из stdin
  - конвертирует markdown -> HTML
  - опционально добавляет видимый FAQ-блок И FAQPage JSON-LD (Schema.org)
  - опционально заполняет SEO title / description / focus keyword под Yoast или Rank Math
  - создаёт пост со статусом "draft" (черновик, не публикуется сразу)
  - печатает ссылку на редактирование

ВАЖНО про SEO-поля:
  SEO-плагины держат данные в защищённых мета-полях. Чтобы REST API мог их писать,
  на сайт нужно один раз положить mu-плагин wp-seo-rest-meta.php (идёт рядом).
  Без него сам пост и FAQ создадутся, а SEO-поля молча не запишутся.

Настройка (переменные окружения):
  WP_URL          например https://example.com
  WP_USER         логин пользователя WordPress
  WP_APP_PASSWORD пароль приложения (Application Password), НЕ основной пароль

Примеры:
  # статья + SEO + FAQ из stdin
  cat article.md | python3 wp_publish.py \
    --title "Требования к СОУЭ в БЦ" \
    --seo-title "Требования к СОУЭ в бизнес-центрах — нормы и проект" \
    --seo-desc "Разбираем требования к СОУЭ 3 типа для БЦ: нормы СП, проект, монтаж." \
    --focus-keyword "СОУЭ в бизнес-центре" \
    --category "Статьи" --tag "СОУЭ" \
    --faq "Какой тип СОУЭ нужен для БЦ?::Обычно 3–4 тип по СП 3.13130." \
    --faq "Нужен ли проект?::Да, монтаж без проекта незаконен."

  # FAQ из JSON-файла [{"q":"...","a":"..."}, ...]
  python3 wp_publish.py --file article.md --faq-file faq.json

  # с обложкой из локального файла
  python3 wp_publish.py --file article.md \
    --image cover.jpg --image-alt "Монтаж СОУЭ в бизнес-центре"

  # с обложкой по URL (скрипт скачает и зальёт сам)
  python3 wp_publish.py --file article.md \
    --image-url "https://example.com/cover.jpg" --image-alt "Пожарная сигнализация"
"""

import argparse
import json
import mimetypes
import os
import re
import sys

import requests
from dotenv import load_dotenv
from markdown import markdown

load_dotenv()

TIMEOUT = 30

SEO_META_KEYS = {
    "yoast": {
        "title": "_yoast_wpseo_title",
        "desc": "_yoast_wpseo_metadesc",
        "kw": "_yoast_wpseo_focuskw",
    },
    "rankmath": {
        "title": "rank_math_title",
        "desc": "rank_math_description",
        "kw": "rank_math_focus_keyword",
    },
}

# "Автор публикации" (ACF-поле avtory, см. template-parts/content-single.php) —
# по умолчанию Михайлов Владислав (ID 686, "Ведущий специалист"). _avtory — служебная
# ссылка ACF на ключ поля, без неё ACF не отрисует блок автора. Оба поля зарегистрированы
# для REST в wp-seo-rest-meta.php.
AVTORY_FIELD_KEY = "field_651fe0a99da65"
DEFAULT_SPECIALIST_ID = "686"


def env(name):
    val = os.environ.get(name)
    if not val:
        sys.exit(f"[ошибка] не задана переменная окружения {name}")
    return val


def read_content(path):
    if path:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    if not sys.stdin.isatty():
        return sys.stdin.read()
    sys.exit("[ошибка] нет входных данных: укажите --file или передайте текст через stdin")


def extract_title(md_text):
    for line in md_text.splitlines():
        m = re.match(r"^#\s+(.+)", line.strip())
        if m:
            return m.group(1).strip()
    return None


def strip_first_h1(md_text, title):
    lines = md_text.splitlines()
    out, removed = [], False
    for line in lines:
        if not removed and re.match(r"^#\s+" + re.escape(title) + r"\s*$", line.strip()):
            removed = True
            continue
        out.append(line)
    return "\n".join(out).strip()


def load_faq(faq_pairs, faq_file):
    """Собираем список {'q':..,'a':..} из --faq "Q::A" и/или --faq-file JSON."""
    items = []
    for pair in faq_pairs:
        if "::" not in pair:
            sys.exit(f"[ошибка] --faq должен быть в формате 'Вопрос::Ответ', получено: {pair}")
        q, a = pair.split("::", 1)
        items.append({"q": q.strip(), "a": a.strip()})
    if faq_file:
        with open(faq_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        for it in data:
            items.append({"q": it["q"].strip(), "a": it["a"].strip()})
    return items


def build_faq(items, heading="Часто задаваемые вопросы"):
    """Возвращает (видимый_html, jsonld_словарь). Google требует, чтобы FAQ
    был виден на странице, поэтому отдаём и видимый блок, и JSON-LD."""
    if not items:
        return "", None

    parts = [f"<h2>{heading}</h2>"]
    for it in items:
        parts.append(f"<h3>{it['q']}</h3>")
        parts.append(f"<p>{it['a']}</p>")
    visible = "\n".join(parts)

    jsonld = {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": [
            {
                "@type": "Question",
                "name": it["q"],
                "acceptedAnswer": {"@type": "Answer", "text": it["a"]},
            }
            for it in items
        ],
    }
    return visible, jsonld


def detect_seo_plugin(session, base):
    """Определяем плагин по зарегистрированным REST-неймспейсам."""
    try:
        r = session.get(f"{base}/wp-json/", timeout=TIMEOUT)
        r.raise_for_status()
        namespaces = r.json().get("namespaces", [])
    except Exception:
        return None
    if any(ns.startswith("yoast") for ns in namespaces):
        return "yoast"
    if any(ns.startswith("rankmath") for ns in namespaces):
        return "rankmath"
    return None


def seo_meta_payload(plugin, seo_title, seo_desc, focus_kw):
    keys = SEO_META_KEYS[plugin]
    meta = {}
    if seo_title:
        meta[keys["title"]] = seo_title
    if seo_desc:
        meta[keys["desc"]] = seo_desc
    if focus_kw:
        meta[keys["kw"]] = focus_kw
    return meta


def fetch_image_url(url):
    """Скачивает картинку по URL. Возвращает (bytes, filename, mime).
    Использовать только доверенные источники."""
    r = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": "wp_publish.py"})
    r.raise_for_status()
    mime = r.headers.get("Content-Type", "").split(";")[0].strip()
    if not mime.startswith("image/"):
        sys.exit(f"[ошибка] по URL пришёл не image, а '{mime or 'неизвестно'}': {url}")
    filename = os.path.basename(url.split("?")[0]) or "cover"
    if "." not in filename:
        ext = mimetypes.guess_extension(mime) or ".jpg"
        filename += ext
    return r.content, filename, mime


def read_image_file(path):
    """Читает локальный файл. Возвращает (bytes, filename, mime)."""
    if not os.path.isfile(path):
        sys.exit(f"[ошибка] файл картинки не найден: {path}")
    mime, _ = mimetypes.guess_type(path)
    if not mime or not mime.startswith("image/"):
        sys.exit(f"[ошибка] это не похоже на картинку: {path}")
    with open(path, "rb") as f:
        return f.read(), os.path.basename(path), mime


def upload_media(session, base, image_bytes, filename, mime, alt=None, caption=None):
    """Заливает картинку в медиатеку и возвращает её id.
    При наличии alt/caption проставляет их вторым запросом."""
    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Content-Type": mime,
    }
    r = session.post(f"{base}/wp-json/wp/v2/media", data=image_bytes,
                     headers=headers, timeout=TIMEOUT)
    if r.status_code >= 400:
        sys.exit(f"[ошибка] загрузка картинки: {r.status_code}: {r.text[:400]}")
    media_id = r.json()["id"]

    meta = {}
    if alt:
        meta["alt_text"] = alt
    if caption:
        meta["caption"] = caption
    if meta:
        session.post(f"{base}/wp-json/wp/v2/media/{media_id}", json=meta, timeout=TIMEOUT)
    return media_id


def resolve_terms(session, base, taxonomy, names):
    ids = []
    endpoint = f"{base}/wp-json/wp/v2/{taxonomy}"
    for name in names:
        r = session.get(endpoint, params={"search": name}, timeout=TIMEOUT)
        r.raise_for_status()
        match = next((t for t in r.json() if t["name"].lower() == name.lower()), None)
        if match:
            ids.append(match["id"])
        else:
            cr = session.post(endpoint, json={"name": name}, timeout=TIMEOUT)
            cr.raise_for_status()
            ids.append(cr.json()["id"])
            print(f"[инфо] создан новый элемент в {taxonomy}: {name}")
    return ids


def main():
    p = argparse.ArgumentParser(description="Публикация статьи в WordPress как черновик + SEO + FAQ.")
    p.add_argument("--file", help="путь к markdown-файлу (иначе читается stdin)")
    p.add_argument("--title", help="заголовок (иначе берётся из первого H1)")
    p.add_argument("--category", action="append", default=[], help="категория (можно повторять)")
    p.add_argument("--tag", action="append", default=[], help="тег (можно повторять)")
    p.add_argument("--excerpt", help="краткое описание (excerpt)")
    p.add_argument("--status", default="draft",
                   choices=["draft", "pending", "publish", "private"],
                   help="статус поста (по умолчанию draft — черновик)")
    # SEO
    p.add_argument("--seo-title", help="SEO title (тег <title> в выдаче)")
    p.add_argument("--seo-desc", help="SEO meta description")
    p.add_argument("--focus-keyword", help="фокусный ключ")
    p.add_argument("--seo-plugin", default="auto",
                   choices=["auto", "yoast", "rankmath", "both", "none"],
                   help="какой SEO-плагин заполнять (auto = определить сам)")
    # FAQ
    p.add_argument("--faq", action="append", default=[],
                   help="пара 'Вопрос::Ответ' (можно повторять)")
    p.add_argument("--faq-file", help="JSON-файл вида [{\"q\":..,\"a\":..}]")
    p.add_argument("--faq-heading", default="Часто задаваемые вопросы",
                   help="заголовок FAQ-блока")
    # Обложка (featured image)
    p.add_argument("--image", help="локальный файл картинки для обложки")
    p.add_argument("--image-url", help="URL картинки (скрипт скачает и зальёт)")
    p.add_argument("--image-alt", help="alt-текст обложки (важно для SEO)")
    p.add_argument("--image-caption", help="подпись к обложке")
    # Автор публикации
    p.add_argument("--specialist-id", default=DEFAULT_SPECIALIST_ID,
                   help=f"ID специалиста для блока «Автор публикации» (по умолчанию {DEFAULT_SPECIALIST_ID} — Михайлов Владислав)")
    p.add_argument("--no-author", action="store_true",
                   help="не проставлять «Автор публикации» этому посту")
    args = p.parse_args()

    if args.image and args.image_url:
        sys.exit("[ошибка] укажите либо --image, либо --image-url, но не оба")

    base = env("WP_URL").rstrip("/")
    user = env("WP_USER")
    app_pw = env("WP_APP_PASSWORD")

    md_text = read_content(args.file)
    title = args.title or extract_title(md_text)
    if not title:
        sys.exit("[ошибка] заголовок не задан и не найден H1 — укажите --title")
    if not args.title:
        md_text = strip_first_h1(md_text, title)

    html = markdown(md_text, extensions=["extra", "sane_lists"])

    # FAQ: видимый блок + JSON-LD
    faq_items = load_faq(args.faq, args.faq_file)
    faq_html, faq_jsonld = build_faq(faq_items, args.faq_heading)
    if faq_html:
        html += "\n\n" + faq_html
    if faq_jsonld:
        html += (
            '\n\n<script type="application/ld+json">'
            + json.dumps(faq_jsonld, ensure_ascii=False)
            + "</script>"
        )

    session = requests.Session()
    session.auth = (user, app_pw)
    session.headers.update({"User-Agent": "wp_publish.py"})

    # SEO-мета
    meta = {}
    want_seo = args.seo_title or args.seo_desc or args.focus_keyword
    if want_seo and args.seo_plugin != "none":
        if args.seo_plugin == "auto":
            plugin = detect_seo_plugin(session, base)
            if not plugin:
                print("[внимание] SEO-плагин не определён по REST — SEO-поля пропущены. "
                      "Укажите --seo-plugin yoast|rankmath явно.")
            targets = [plugin] if plugin else []
        elif args.seo_plugin == "both":
            targets = ["yoast", "rankmath"]
        else:
            targets = [args.seo_plugin]
        for plug in targets:
            meta.update(seo_meta_payload(plug, args.seo_title, args.seo_desc, args.focus_keyword))

    # Автор публикации (по умолчанию — Михайлов Владислав)
    if not args.no_author:
        meta["avtory"] = [args.specialist_id]
        meta["_avtory"] = AVTORY_FIELD_KEY

    # Обложка: заливаем в медиатеку и получаем id
    featured_id = None
    if args.image or args.image_url:
        if args.image_url:
            img_bytes, fname, img_mime = fetch_image_url(args.image_url)
        else:
            img_bytes, fname, img_mime = read_image_file(args.image)
        featured_id = upload_media(session, base, img_bytes, fname, img_mime,
                                   alt=args.image_alt, caption=args.image_caption)

    payload = {"title": title, "content": html, "status": args.status}
    if args.excerpt:
        payload["excerpt"] = args.excerpt
    if meta:
        payload["meta"] = meta
    if featured_id:
        payload["featured_media"] = featured_id
    if args.category:
        payload["categories"] = resolve_terms(session, base, "categories", args.category)
    if args.tag:
        payload["tags"] = resolve_terms(session, base, "tags", args.tag)

    r = session.post(f"{base}/wp-json/wp/v2/posts", json=payload, timeout=TIMEOUT)
    if r.status_code >= 400:
        sys.exit(f"[ошибка] {r.status_code}: {r.text[:600]}")

    data = r.json()
    post_id = data["id"]
    edit_url = f"{base}/wp-admin/post.php?post={post_id}&action=edit"
    print(f"[готово] статус: {data['status']}")
    print(f"[готово] заголовок: {title}")
    if featured_id:
        alt_note = " с alt" if args.image_alt else " (без alt — задайте --image-alt для SEO)"
        print(f"[готово] обложка установлена, media id: {featured_id}{alt_note}")
    if faq_items:
        print(f"[готово] FAQ добавлен: {len(faq_items)} вопрос(ов) + JSON-LD schema")
    if meta:
        # проверим, что SEO-мета реально записалась (mu-плагин установлен)
        returned = data.get("meta", {})
        written = [k for k in meta if returned.get(k)]
        if written:
            print(f"[готово] SEO-поля записаны: {', '.join(written)}")
        else:
            print("[внимание] SEO-поля НЕ записались — вероятно не установлен mu-плагин "
                  "wp-seo-rest-meta.php. Пост и FAQ при этом созданы.")
    print(f"[готово] редактировать: {edit_url}")


if __name__ == "__main__":
    main()
