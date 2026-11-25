#!/usr/bin/env python3
"""
Одноразовый (но безопасно переповторяемый) скрипт разметки.

НЕ пересериализует HTML через BeautifulSoup (это меняло форматирование
по всему файлу/теряло пробелы). Вместо этого:
  1. Парсим файл через bs4('html.parser') только для АНАЛИЗА (classify).
  2. Для каждого выбранного элемента берём tag.sourceline/sourcepos,
     находим точную позицию конца открывающего тега в ИСХОДНОМ тексте
     и вставляем туда ' data-cms="key"' - остальной текст файла не
     меняется ни на один байт.
  3. Для type="html" (весь innerHTML как один блок) значение для
     content.seed.json вырезаем ТОЖЕ напрямую из исходного текста
     (raw-slicing), а не через bs4.decode_contents() - у bs4/html.parser
     есть особенность терять пробелы отступов в чисто пробельных
     текстовых узлах между тегами при сериализации.

Запуск: python3 cms/extract_tag.py              (меняет .html на месте)
        python3 cms/extract_tag.py --dry-run     (только отчёт)
        python3 cms/extract_tag.py --only=a,b,c  (только указанные файлы)
"""
import json
import pathlib
import sys

from bs4 import BeautifulSoup, Tag

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from tagger_common import classify
from htmlpos import build_line_starts, to_abs_offset, find_insertion_point, find_matching_close

ROOT = pathlib.Path(__file__).parent.parent
SEED_PATH = ROOT / "cms" / "content.seed.json"

META_SELECTORS = [
    ("meta", {"name": "description"}),
    ("meta", {"property": "og:title"}),
    ("meta", {"property": "og:description"}),
    ("meta", {"name": "twitter:title"}),
    ("meta", {"name": "twitter:description"}),
]


def collect_targets(soup, file_stem, counter):
    """Возвращает список (tag, key, ftype, provisional_value).
    provisional_value для ftype='html' - ЗАГЛУШКА, настоящее значение
    вычисляется позже через raw-slicing в main()."""
    targets = []

    title_tag = soup.find("title")
    if title_tag and not title_tag.has_attr("data-cms") and title_tag.string:
        counter[0] += 1
        key = f"{file_stem}-{counter[0]}"
        targets.append((title_tag, key, "title", title_tag.string.strip()))

    for name, attrs in META_SELECTORS:
        tag = soup.find(name, attrs=attrs)
        if tag and tag.has_attr("content") and not tag.has_attr("data-cms") and tag["content"].strip():
            counter[0] += 1
            key = f"{file_stem}-{counter[0]}"
            targets.append((tag, key, "meta", tag["content"].strip()))

    def walk(el):
        kind, payload = classify(el)
        if kind == "skip":
            return
        if kind == "recurse":
            for child in list(el.children):
                if isinstance(child, Tag):
                    walk(child)
            return
        if kind in ("leaf-text", "leaf-text-icons"):
            value = payload.strip()
            ftype = "text"
        elif kind == "leaf-html":
            value = None  # вычислим позже через raw-slicing
            ftype = "html"
        else:
            return
        if ftype == "text" and not value:
            return
        counter[0] += 1
        key = f"{file_stem}-{counter[0]}"
        targets.append((el, key, ftype, value))

    body = soup.find("body") or soup
    for child in list(body.children):
        if isinstance(child, Tag):
            walk(child)

    return targets


def resolve_html_values(raw_text, targets):
    """Для ftype='html' вычисляет настоящее значение через raw-slicing."""
    line_starts = build_line_starts(raw_text)
    resolved = []
    for tag, key, ftype, value in targets:
        if ftype == "html":
            lt_offset = to_abs_offset(line_starts, tag.sourceline, tag.sourcepos)
            open_end = find_insertion_point(raw_text, lt_offset) + 1  # после '>'
            close_start = find_matching_close(raw_text, open_end, tag.name)
            # НЕ .strip() - сохраняем пробелы/отступы байт-в-байт, чтобы
            # сборка без правок давала идентичный исходному файлу результат.
            value = raw_text[open_end:close_start]
        resolved.append((tag, key, ftype, value))
    return resolved


def apply_insertions(raw_text, targets):
    """targets: список (tag, key). Возвращает новый текст файла."""
    line_starts = build_line_starts(raw_text)
    insertions = []
    for tag, key in targets:
        if tag.sourceline is None or tag.sourcepos is None:
            raise RuntimeError(f"нет sourceline/sourcepos для <{tag.name}> key={key}")
        lt_offset = to_abs_offset(line_starts, tag.sourceline, tag.sourcepos)
        if raw_text[lt_offset] != "<":
            raise RuntimeError(
                f"ожидался '<' в позиции {lt_offset} для <{tag.name}> key={key}, "
                f"получено {raw_text[lt_offset]!r}"
            )
        insert_at = find_insertion_point(raw_text, lt_offset)
        insertions.append((insert_at, key))

    insertions.sort(key=lambda x: x[0], reverse=True)
    text = raw_text
    for insert_at, key in insertions:
        # Всегда вставляем СО СВОИМ пробелом впереди (даже если перед этим
        # уже был пробел - тогда в шаблоне будет двойной пробел, это
        # безвредно и невидимо). Это гарантирует, что при сборке, когда
        # ' data-cms="key"' вырезается обратно, исходный пробел перед '/'
        # или '>' восстанавливается idiotично.
        snippet = f' data-cms="{key}"'
        text = text[:insert_at] + snippet + text[insert_at:]
    return text


def main():
    dry_run = "--dry-run" in sys.argv
    only = None
    for a in sys.argv[1:]:
        if a.startswith("--only="):
            only = set(a.split("=", 1)[1].split(","))

    seed = {}
    if SEED_PATH.exists():
        seed = json.loads(SEED_PATH.read_text(encoding="utf-8"))

    total_new = 0
    report = []
    for path in sorted(ROOT.glob("*.html")):
        file_stem = path.stem
        if only and file_stem not in only:
            continue
        raw = path.read_text(encoding="utf-8")
        soup = BeautifulSoup(raw, "html.parser")
        counter = [0]
        targets = collect_targets(soup, file_stem, counter)
        targets = resolve_html_values(raw, targets)

        new_count = 0
        to_insert = []
        for tag, key, ftype, value in targets:
            if key in seed:
                continue
            if not value:
                continue
            seed[key] = {"type": ftype, "value": value, "file": file_stem}
            to_insert.append((tag, key))
            new_count += 1

        total_new += new_count
        report.append((path.name, new_count))

        if not dry_run and to_insert:
            new_text = apply_insertions(raw, to_insert)
            path.write_text(new_text, encoding="utf-8")

    if not dry_run:
        SEED_PATH.write_text(json.dumps(seed, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"{'[DRY RUN] ' if dry_run else ''}Обработано файлов: {len(report)}")
    print(f"Новых полей добавлено: {total_new}")
    print(f"Итого полей в seed: {len(seed)}")
    print()
    for name, new_here in report:
        if new_here:
            print(f"  {name:55s} новых={new_here:3d}")


if __name__ == "__main__":
    main()
