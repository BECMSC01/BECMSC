#!/usr/bin/env python3
"""
Генератор статического сайта из шаблонов (git-репозиторий, HTML с
атрибутами data-cms="ключ") + content.json (текущие значения полей,
редактируются из админки, ЖИВЁТ ВНЕ git).

Логика на ключ:
  - находим тег с data-cms="key" в шаблоне (который содержит ИСХОДНОЕ/
    затравочное значение из content.seed.json) и подставляем ТЕКУЩЕЕ
    значение из content.json вместо исходного - шаблон на диске (в git)
    при этом не меняется, меняется только файл в выходной директории.

Типы полей:
  meta  - атрибут content="..." тега <meta>
  title - текст тега <title>
  text  - единственный текстовый узел внутри элемента (иконки/соседние
          теги не трогаем - ищем точное вхождение ИСХОДНОГО текста
          внутри элемента и заменяем его на текущий)
  html  - весь innerHTML элемента целиком (для абзацев со ссылками и т.п.)

Запуск:
  python3 cms/build.py --out /var/www/becmsc --content /opt/bec-admin/content.json
  python3 cms/build.py --out dist --content cms/content.seed.json   (для локальной проверки)
"""
import argparse
import html as html_mod
import json
import pathlib
import shutil
import sys

from bs4 import BeautifulSoup

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from htmlpos import build_line_starts, to_abs_offset, find_insertion_point, find_matching_close

ROOT = pathlib.Path(__file__).parent.parent
SEED_PATH = ROOT / "cms" / "content.seed.json"

# Файлы/папки, которые НИКОГДА не копируются в выходную директорию
# (служебное/разработческое, не часть сайта)
EXCLUDE_FROM_COPY = {
    ".git", "cms", "_full_site_transfer.tar.gz", ".gitignore",
}


def esc(value: str) -> str:
    return html_mod.escape(value, quote=True)



def find_attr_value_span(text, tag_start, attr_name):
    """Возвращает (value_start, value_end) для attr_name="..." внутри тега,
    начинающегося в tag_start (символ '<')."""
    tag_end = find_insertion_point(text, tag_start)  # позиция '>' или '/'
    search_zone_end = tag_end
    idx = text.find(attr_name + '="', tag_start, search_zone_end + 1)
    if idx == -1:
        raise ValueError(f"атрибут {attr_name} не найден в теге на {tag_start}")
    value_start = idx + len(attr_name) + 2
    value_end = text.find('"', value_start)
    return value_start, value_end


def apply_content(raw_text, soup, content):
    line_starts = build_line_starts(raw_text)

    # Собираем список правок (replace_start, replace_end, new_text),
    # затем применяем С КОНЦА файла, чтобы не сбивать смещения.
    edits = []

    for tag in soup.find_all(attrs={"data-cms": True}):
        key = tag["data-cms"]
        entry = content.get(key)
        if entry is None:
            continue  # поле удалено из content.json - оставляем как в шаблоне
        ftype = entry["type"]
        new_value = entry["value"]

        lt_offset = to_abs_offset(line_starts, tag.sourceline, tag.sourcepos)
        if raw_text[lt_offset] != "<":
            raise RuntimeError(f"ожидался '<' в {lt_offset} для key={key}")

        if ftype == "meta":
            v_start, v_end = find_attr_value_span(raw_text, lt_offset, "content")
            edits.append((v_start, v_end, esc(new_value)))

        elif ftype == "title":
            open_end = find_insertion_point(raw_text, lt_offset) + 1  # после '>'
            close_start = find_matching_close(raw_text, open_end, "title")
            edits.append((open_end, close_start, esc(new_value)))

        elif ftype == "text":
            open_end = find_insertion_point(raw_text, lt_offset) + 1
            close_start = find_matching_close(raw_text, open_end, tag.name)
            inner = raw_text[open_end:close_start]
            seed_value = entry.get("_seed_value")
            pos = inner.find(seed_value) if seed_value is not None else 0
            if pos == -1:
                pos = 0  # не нашли - подстрахуемся и заменим всё (редкий случай)
                seg_start, seg_end = open_end, close_start
            else:
                seg_start = open_end + pos
                seg_end = seg_start + len(seed_value)
            edits.append((seg_start, seg_end, esc(new_value)))

        elif ftype == "html":
            open_end = find_insertion_point(raw_text, lt_offset) + 1
            close_start = find_matching_close(raw_text, open_end, tag.name)
            edits.append((open_end, close_start, new_value))

    edits.sort(key=lambda e: e[0], reverse=True)
    text = raw_text
    for start, end, new_text in edits:
        text = text[:start] + new_text + text[end:]
    return text


def load_content(content_path: pathlib.Path):
    content = json.loads(content_path.read_text(encoding="utf-8"))
    if SEED_PATH.exists():
        seed = json.loads(SEED_PATH.read_text(encoding="utf-8"))
        for key, entry in seed.items():
            if key in content:
                content[key]["_seed_value"] = entry["value"] if entry["type"] in ("text",) else None
    return content


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="выходная директория (например /var/www/becmsc)")
    ap.add_argument("--content", required=True, help="путь к content.json (текущие значения)")
    args = ap.parse_args()

    out_dir = pathlib.Path(args.out)
    content_path = pathlib.Path(args.content)
    content = load_content(content_path)

    out_dir.mkdir(parents=True, exist_ok=True)

    # 1) Копируем вообще всё, кроме исключений
    for item in ROOT.iterdir():
        if item.name in EXCLUDE_FROM_COPY:
            continue
        dest = out_dir / item.name
        if item.is_dir():
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(item, dest)
        else:
            shutil.copy2(item, dest)

    # 2) Перегенерируем .html файлы с подстановкой контента
    changed = 0
    for path in sorted(ROOT.glob("*.html")):
        raw = path.read_text(encoding="utf-8")
        soup = BeautifulSoup(raw, "html.parser")
        if not soup.find(attrs={"data-cms": True}):
            continue  # нечего подставлять (например yandex-verification файл)
        new_text = apply_content(raw, soup, content)
        if new_text != raw:
            changed += 1
        # в финальной выдаче служебный атрибут data-cms не нужен
        import re as _re
        new_text = _re.sub(r' data-cms="[^"]*"', "", new_text)
        (out_dir / path.name).write_text(new_text, encoding="utf-8")

    print(f"Готово. Файлов с применённым контентом: {changed}. Выход: {out_dir}")


if __name__ == "__main__":
    main()
