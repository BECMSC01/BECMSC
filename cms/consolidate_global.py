#!/usr/bin/env python3
"""
Вторая фаза разметки: объединяет ПОЛНОСТЬЮ идентичные поля (одинаковый
тег, одинаковые атрибуты кроме data-cms, одинаковое значение),
повторяющиеся на нескольких страницах (шапка, футер, меню, логотип,
cookie-баннер и т.п.), в ОДИН общий ключ "global-...". Так правка
одного такого поля в админке сразу применяется ко всем страницам.

Безопасно: текстовые/html-поля объединяются только если у них ПОЛНОСТЬЮ
совпадают тег + все остальные атрибуты + текущее значение, и такое
совпадение встречается не меньше MIN_SHARED раз - случайное совпадение
короткого текста с разными ссылками/контекстом не объединяется.

Картинки - особый случай: объединяются по ОДНОМУ ТОЛЬКО пути к файлу
(+ webp-паре), независимо от тега/атрибутов и при ЛЮБОМ количестве
повторов от двух и выше. Это не эвристика, а необходимость: замена
картинки в build.py работает подменой ФАЙЛА по пути на диске (см.
apply_image_overlays) - если два РАЗНЫХ data-cms ключа указывают на
один и тот же файл (например, одна и та же фотография использована
и как размытый фон, и как чёткий передний план - частый приём на
карточках услуг), то правка одного из них физически меняет файл,
который показывает и другой, даже если их не объединить в seed.
Поэтому для картинок два ключа с одинаковым путём ВСЕГДА должны стать
одним общим полем - иначе админка будет лгать, показывая их как два
независимых поля, хотя правка одного молча меняет и второе.

Запуск: python3 cms/consolidate_global.py            (применяет)
        python3 cms/consolidate_global.py --dry-run   (только отчёт)
"""
import json
import pathlib
import re
import sys
from collections import defaultdict

from bs4 import BeautifulSoup

# Значения, которые ПОХОЖИ НА ДАННЫЕ (цена, длительность, процент, голое
# число) - НИКОГДА не объединяем в общий ключ, даже если они встречаются
# на нескольких страницах одинаково: это, скорее всего, СЛУЧАЙНОЕ совпадение
# у РАЗНЫХ услуг (например, две разные процедуры по 45 000 ₽), и объединение
# привело бы к тому, что правка цены одной услуги меняла бы цену другой.
DATA_LIKE_PATTERNS = [
    re.compile(r"^от\s+\d"),                       # "от 1 500 ₽"
    re.compile(r"\d[\d\s]*\s*₽"),                  # "35 000 ₽"
    re.compile(r"\d+\s*%$"),                        # "100%"
    re.compile(r"\d+\s*(мин|минут|час|ч\b)", re.I), # "45 мин", "45-60 мин"
    re.compile(r"^\d+[+]?$"),                       # голое число "36", "10+"
]


def is_data_like(value: str) -> bool:
    v = value.strip()
    return any(p.search(v) for p in DATA_LIKE_PATTERNS)

ROOT = pathlib.Path(__file__).parent.parent
SEED_PATH = ROOT / "cms" / "content.seed.json"
MIN_SHARED = 3


def slugify(text, maxlen=24):
    text = text.lower()
    translit = {
        "а":"a","б":"b","в":"v","г":"g","д":"d","е":"e","ё":"e","ж":"zh","з":"z",
        "и":"i","й":"y","к":"k","л":"l","м":"m","н":"n","о":"o","п":"p","р":"r",
        "с":"s","т":"t","у":"u","ф":"f","х":"h","ц":"c","ч":"ch","ш":"sh","щ":"sch",
        "ъ":"","ы":"y","ь":"","э":"e","ю":"yu","я":"ya",
    }
    text = "".join(translit.get(ch, ch) for ch in text)
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text[:maxlen] or "field"


def attrs_signature(tag):
    sig = []
    for k, v in tag.attrs.items():
        if k == "data-cms":
            continue
        if isinstance(v, list):
            v = tuple(v)
        sig.append((k, v))
    return tuple(sorted(sig))


def main():
    dry_run = "--dry-run" in sys.argv
    seed = json.loads(SEED_PATH.read_text(encoding="utf-8"))

    groups = defaultdict(list)  # signature -> [(file, key)]
    file_soups = {}

    for path in sorted(ROOT.glob("*.html")):
        raw = path.read_text(encoding="utf-8")
        soup = BeautifulSoup(raw, "html.parser")
        file_soups[path.name] = raw
        for tag in soup.find_all(attrs={"data-cms": True}):
            key = tag["data-cms"]
            if key not in seed:
                continue
            entry = seed[key]
            if entry["type"] == "text" and is_data_like(entry["value"]):
                continue  # цена/длительность/число - всегда отдельное поле
            if entry["type"] == "image":
                # Картинки группируем СТРОГО по физическому файлу (путь +
                # webp-пара), тег/атрибуты не важны - см. пояснение в шапке
                # модуля. tag_name/attrs_sig здесь не используются.
                sig = ("__image__", None, "image", (entry["value"], entry.get("webp")))
            else:
                sig = (tag.name, attrs_signature(tag), entry["type"], entry["value"])
            groups[sig].append((path.name, key))

    merges = 0
    renamed_total = 0
    for sig, occurrences in groups.items():
        tag_name, attrs_sig, ftype, value = sig
        webp = None
        if ftype == "image":
            value, webp = value  # распаковываем (путь, webp) из sig

        min_required = 2 if ftype == "image" else MIN_SHARED
        if len(occurrences) < min_required:
            continue

        if ftype == "image":
            # для картинок используем имя файла без расширения, а не весь
            # путь - иначе slug получается длинным и малочитаемым
            base_slug = slugify(pathlib.Path(value).stem)
        else:
            base_slug = slugify(value)
        canonical_key = f"global-{base_slug}"
        suffix = 1
        while canonical_key in seed and seed[canonical_key].get("value") != value:
            suffix += 1
            canonical_key = f"global-{base_slug}-{suffix}"

        extra = {}
        if ftype == "image" and webp:
            extra["webp"] = webp

        seed[canonical_key] = {
            "type": ftype, "value": value, "file": "global",
            "shared_count": len(occurrences), **extra,
        }

        by_file = defaultdict(list)
        for fname, key in occurrences:
            by_file[fname].append(key)
            if key != canonical_key and key in seed and seed[key].get("file") != "global":
                del seed[key]

        for fname, keys in by_file.items():
            text = file_soups[fname]
            for old_key in keys:
                if old_key == canonical_key:
                    continue
                old_attr = f'data-cms="{old_key}"'
                new_attr = f'data-cms="{canonical_key}"'
                count = text.count(old_attr)
                if count != 1:
                    print(f"WARNING: {fname}: {old_attr} встречается {count} раз (ожидался 1)")
                text = text.replace(old_attr, new_attr)
                renamed_total += 1
            file_soups[fname] = text

        merges += 1

    if not dry_run:
        for fname, text in file_soups.items():
            (ROOT / fname).write_text(text, encoding="utf-8")
        SEED_PATH.write_text(json.dumps(seed, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"{'[DRY RUN] ' if dry_run else ''}Групп объединено: {merges}")
    print(f"Переименований data-cms атрибутов: {renamed_total}")
    print(f"Итого полей в seed после объединения: {len(seed)}")


if __name__ == "__main__":
    main()
