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
  4. Для type="image" (<img>) значение - путь src, плюс (если картинка
     внутри <picture> с <source type="image/webp">) путь к webp-паре -
     отдельным полем "webp" в seed-записи. Сам src НИКОГДА не меняется
     в HTML - замена картинки в будущем работает подменой ФАЙЛА по тому
     же пути при сборке (см. cms/build.py), а не правкой разметки.

ВАЖНО про повторный запуск: счётчик ключей "{file}-{n}" для каждого
файла стартует не с 0, а с МАКСИМАЛЬНОГО уже существующего суффикса для
этого файла в seed - иначе при повторном запуске (например, чтобы
добавить новый тип полей вроде картинок) новые ключи получили бы те же
номера, что уже заняты другими, ранее размеченными элементами.

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

EXTERNAL_PREFIXES = ("data:", "http://", "https://", "//")


def _webp_pair_for(img_tag):
    """Если img внутри <picture> с <source type="image/webp">, возвращает
    путь из его srcset. Иначе None."""
    parent = img_tag.parent
    if parent is None or parent.name != "picture":
        return None
    for source in parent.find_all("source"):
        type_attr = (source.get("type") or "").lower()
        srcset = (source.get("srcset") or "").strip()
        if not srcset:
            continue
        if "webp" in type_attr or srcset.lower().split("?")[0].endswith(".webp"):
            first = srcset.split(",")[0].strip().split(" ")[0]
            if first:
                return first
    return None


def collect_targets(soup, file_stem, counter):
    """Возвращает список (tag, key, ftype, provisional_value, extra).
    provisional_value для ftype='html' - ЗАГЛУШКА, настоящее значение
    вычисляется позже через raw-slicing в main(). extra - доп. метаданные
    (сейчас используется только для ftype='image': {'webp': '...'} либо None."""
    targets = []

    title_tag = soup.find("title")
    if title_tag and not title_tag.has_attr("data-cms") and title_tag.string:
        counter[0] += 1
        key = f"{file_stem}-{counter[0]}"
        targets.append((title_tag, key, "title", title_tag.string.strip(), None))

    for name, attrs in META_SELECTORS:
        tag = soup.find(name, attrs=attrs)
        if tag and tag.has_attr("content") and not tag.has_attr("data-cms") and tag["content"].strip():
            counter[0] += 1
            key = f"{file_stem}-{counter[0]}"
            targets.append((tag, key, "meta", tag["content"].strip(), None))

    def walk(el):
        kind, payload = classify(el)
        if kind == "skip":
            return
        if kind == "recurse":
            for child in list(el.children):
                if isinstance(child, Tag):
                    walk(child)
            return
        extra = None
        if kind in ("leaf-text", "leaf-text-icons"):
            value = payload.strip()
            ftype = "text"
        elif kind == "leaf-html":
            value = None  # вычислим позже через raw-slicing
            ftype = "html"
        elif kind == "leaf-image":
            img_tag = payload
            src = (img_tag.get("src") or "").strip()
            if not src or src.startswith(EXTERNAL_PREFIXES):
                return  # нет локального файла - нечем управлять
            webp = _webp_pair_for(img_tag)
            value = src
            ftype = "image"
            if webp:
                extra = {"webp": webp}
        else:
            return
        if ftype == "text" and not value:
            return
        counter[0] += 1
        key = f"{file_stem}-{counter[0]}"
        targets.append((el, key, ftype, value, extra))

    body = soup.find("body") or soup
    for child in list(body.children):
        if isinstance(child, Tag):
            walk(child)

    return targets


def resolve_html_values(raw_text, targets):
    """Для ftype='html' вычисляет настоящее значение через raw-slicing."""
    line_starts = build_line_starts(raw_text)
    resolved = []
    for tag, key, ftype, value, extra in targets:
        if ftype == "html":
            lt_offset = to_abs_offset(line_starts, tag.sourceline, tag.sourcepos)
            open_end = find_insertion_point(raw_text, lt_offset) + 1  # после '>'
            close_start = find_matching_close(raw_text, open_end, tag.name)
            # НЕ .strip() - сохраняем пробелы/отступы байт-в-байт, чтобы
            # сборка без правок давала идентичный исходному файлу результат.
            value = raw_text[open_end:close_start]
        resolved.append((tag, key, ftype, value, extra))
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
        # или '>' восстанавливается идентично.
        snippet = f' data-cms="{key}"'
        text = text[:insert_at] + snippet + text[insert_at:]
    return text


def _existing_max_suffix(seed, file_stem):
    prefix = f"{file_stem}-"
    best = 0
    for k in seed:
        if k.startswith(prefix):
            suffix = k[len(prefix):]
            if suffix.isdigit():
                best = max(best, int(suffix))
    return best


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
        # Счётчик продолжается ПОСЛЕ уже занятых номеров для этого файла -
        # см. пояснение в шапке модуля.
        counter = [_existing_max_suffix(seed, file_stem)]
        targets = collect_targets(soup, file_stem, counter)
        targets = resolve_html_values(raw, targets)

        new_count = 0
        to_insert = []
        for tag, key, ftype, value, extra in targets:
            if key in seed:
                continue
            if not value:
                continue
            entry = {"type": ftype, "value": value, "file": file_stem}
            if extra:
                entry.update(extra)
            seed[key] = entry
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
