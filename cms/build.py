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
  image - <img>. HTML НЕ меняется (src остаётся тем же путём) - замена
          картинки работает подменой ФАЙЛА по этому пути в выходной
          директории (см. apply_image_overlays). Если у поля есть пара
          "webp" (картинка внутри <picture> с <source type="image/webp">),
          webp-версия подменяется тем же способом по своему пути.

          Исключение - "отделённые" поля (entry["split_of"] задан): это
          поле описывает ОДНО конкретное вхождение общей (global) картинки
          на ОДНОЙ конкретной странице (entry["split_file"] +
          entry["split_occurrence"] - 0-based номер по порядку среди всех
          data-cms=<split_of> в этом файле), которое должно показывать
          СВОЮ картинку независимо от остальных вхождений. Для него (и
          только для него) build.py переписывает src/srcset именно этого
          вхождения на отдельный путь assets/img/_split/<ключ>.<ext> - это
          единственный случай, когда HTML в выходной директории содержит
          другой src, чем в шаблоне (см. apply_image_splits).

Запуск:
  python3 cms/build.py --out /var/www/becmsc --content /opt/bec-admin/content.json --uploads /opt/bec-admin/uploads
  python3 cms/build.py --out dist --content cms/content.seed.json   (для локальной проверки, без картинок)
"""
import argparse
import html as html_mod
import json
import pathlib
import re
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


def split_output_rel(split_key: str, original_value: str) -> str:
    """Путь (относительно корня выходной директории, без ведущего "/"),
    по которому лежит ПРИВАТНАЯ копия картинки для отделённого поля -
    чтобы не делить физический файл с остальными вхождениями общей
    картинки (см. docstring модуля)."""
    ext = pathlib.Path(original_value).suffix
    safe = re.sub(r"[^a-zA-Z0-9_-]", "-", split_key)
    return f"assets/img/_split/{safe}{ext}"


def apply_image_splits(raw_text, soup, content, file_stem, line_starts, edits):
    """Добавляет в edits правки src/srcset для "отделённых" вхождений
    общих картинок, которые относятся к файлу file_stem (см. docstring
    модуля). Если разметка страницы с тех пор изменилась настолько, что
    нужного вхождения больше нет - пропускаем с предупреждением в stdout
    (сайт всё равно соберётся, просто это место останется показывать
    общую картинку, как было бы без отделения)."""
    split_entries = [
        (k, e) for k, e in content.items()
        if e.get("type") == "image" and e.get("split_of") and e.get("split_file") == file_stem
    ]
    if not split_entries:
        return

    by_parent = {}
    for k, e in split_entries:
        by_parent.setdefault(e["split_of"], []).append((k, e))

    for parent_key, items in by_parent.items():
        occurrences = soup.find_all(attrs={"data-cms": parent_key})
        for split_key, e in items:
            idx = e.get("split_occurrence", -1)
            if not (0 <= idx < len(occurrences)):
                print(f"WARNING: отделённое поле {split_key}: вхождение {idx} поля {parent_key} "
                      f"не найдено в {file_stem}.html (разметка страницы изменилась?) - "
                      f"оставляю как общую картинку")
                continue
            tag = occurrences[idx]
            lt_offset = to_abs_offset(line_starts, tag.sourceline, tag.sourcepos)
            if raw_text[lt_offset] != "<":
                print(f"WARNING: отделённое поле {split_key}: не нашёл '<' на ожидаемой позиции - пропускаю")
                continue

            v_start, v_end = find_attr_value_span(raw_text, lt_offset, "src")
            edits.append((v_start, v_end, esc("/" + split_output_rel(split_key, e["value"]))))

            if e.get("webp"):
                picture = tag.find_parent("picture")
                source_tag = picture.find("source", attrs={"type": "image/webp"}) if picture else None
                if source_tag is not None:
                    src_lt = to_abs_offset(line_starts, source_tag.sourceline, source_tag.sourcepos)
                    sv_start, sv_end = find_attr_value_span(raw_text, src_lt, "srcset")
                    edits.append((sv_start, sv_end, esc("/" + split_output_rel(split_key, e["webp"]))))


def apply_content(raw_text, soup, content, file_stem=None):
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

    if file_stem is not None:
        apply_image_splits(raw_text, soup, content, file_stem, line_starts, edits)

    edits.sort(key=lambda e: e[0], reverse=True)
    text = raw_text
    for start, end, new_text in edits:
        text = text[:start] + new_text + text[end:]
    return text


def apply_image_overlays(out_dir: pathlib.Path, content: dict, uploads_dir):
    """Для полей type="image" с загруженной заменой (content[key]["replaced"]
    истинно) - перезаписывает файл(ы) в out_dir поверх уже скопированной
    "родной" картинки из шаблонов. HTML не трогается: src остаётся тем же
    путём что и был, подменяется только содержимое файла по этому пути.
    Если у поля есть пара "webp", подменяется и она - отдельным файлом
    <key>.webp в uploads_dir, по пути из entry["webp"].
    Возвращает количество подменённых файлов (основных, без webp)."""
    if uploads_dir is None or not uploads_dir.exists():
        return 0
    applied = 0
    for key, entry in content.items():
        if entry.get("type") != "image" or not entry.get("replaced"):
            continue
        is_split = bool(entry.get("split_of"))
        target_rel = split_output_rel(key, entry["value"]) if is_split else entry["value"].lstrip("/")
        ext = pathlib.Path(entry["value"]).suffix
        src_upload = uploads_dir / f"{key}{ext}"
        if src_upload.exists():
            target_path = out_dir / target_rel
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_upload, target_path)
            applied += 1
        webp_rel = entry.get("webp")
        if webp_rel:
            webp_upload = uploads_dir / f"{key}.webp"
            if webp_upload.exists():
                webp_target_rel = split_output_rel(key, webp_rel) if is_split else webp_rel.lstrip("/")
                webp_target = out_dir / webp_target_rel
                webp_target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(webp_upload, webp_target)
    return applied


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
    ap.add_argument("--uploads", default=None, help="папка с загруженными заменами картинок (необязательно)")
    args = ap.parse_args()

    out_dir = pathlib.Path(args.out)
    content_path = pathlib.Path(args.content)
    uploads_dir = pathlib.Path(args.uploads) if args.uploads else None
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
        new_text = apply_content(raw, soup, content, file_stem=path.stem)
        if new_text != raw:
            changed += 1
        # в финальной выдаче служебный атрибут data-cms не нужен
        new_text = re.sub(r' data-cms="[^"]*"', "", new_text)
        (out_dir / path.name).write_text(new_text, encoding="utf-8")

    # 3) Накладываем загруженные через админку замены картинок (если есть)
    n_images = apply_image_overlays(out_dir, content, uploads_dir)

    print(f"Готово. Файлов с применённым контентом: {changed}. Картинок заменено: {n_images}. Выход: {out_dir}")


if __name__ == "__main__":
    main()
