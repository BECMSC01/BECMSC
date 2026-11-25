"""
Утилиты для точечной (без переформатирования) правки HTML:
находим позицию открывающего тега в ИСХОДНОМ тексте файла (по
tag.sourceline/tag.sourcepos из BeautifulSoup с парсером 'html.parser')
и вставляем атрибут data-cms="..." прямо в это место, не трогая
остальной текст файла ни одним байтом.
"""


def build_line_starts(text: str):
    """Список абсолютных смещений начала каждой строки (0-indexed)."""
    starts = [0]
    for i, ch in enumerate(text):
        if ch == "\n":
            starts.append(i + 1)
    return starts


def to_abs_offset(line_starts, sourceline: int, sourcepos: int) -> int:
    # bs4 sourceline является 1-indexed
    return line_starts[sourceline - 1] + sourcepos


def find_insertion_point(text: str, lt_offset: int):
    """
    text[lt_offset] должен быть '<' начала открывающего тега.
    Возвращает смещение, ПЕРЕД которым нужно вставить ' data-cms="key"'
    (это позиция '>' либо, для самозакрывающихся тегов, позиция '/').
    """
    assert text[lt_offset] == "<", f"expected '<' at {lt_offset}, got {text[lt_offset]!r}"
    i = lt_offset + 1
    n = len(text)
    # имя тега
    while i < n and (text[i].isalnum() or text[i] in "-_:"):
        i += 1
    # теперь перебираем атрибуты до несэкранированного '>'
    while i < n:
        c = text[i]
        if c in ("'", '"'):
            quote = c
            i += 1
            while i < n and text[i] != quote:
                i += 1
            i += 1  # пропустить закрывающую кавычку
            continue
        if c == ">":
            if i > 0 and text[i - 1] == "/":
                return i - 1  # вставить перед '/'
            return i
        i += 1
    raise ValueError(f"не найден конец тега от смещения {lt_offset}")


def find_matching_close(text: str, open_tag_end: int, tag_name: str) -> int:
    """
    text[open_tag_end] - первый символ ПОСЛЕ '>' открывающего тега.
    Возвращает смещение начала '</tag_name' соответствующего закрывающего
    тега (с учётом вложенных тегов с тем же именем).
    """
    depth = 0
    i = open_tag_end
    n = len(text)
    open_pat = f"<{tag_name}"
    close_pat = f"</{tag_name}"
    while i < n:
        if text.startswith(close_pat, i):
            j = i + len(close_pat)
            while j < n and text[j] not in ">":
                j += 1
            if depth == 0:
                return i
            depth -= 1
            i = j + 1
            continue
        if text.startswith(open_pat, i):
            nxt = text[i + len(open_pat): i + len(open_pat) + 1]
            if nxt == "" or nxt in " \t\n>/":
                gt = text.find(">", i)
                if gt == -1:
                    break
                is_self_closing = text[gt - 1] == "/"
                if not is_self_closing:
                    depth += 1
                i = gt + 1
                continue
        i += 1
    raise ValueError(f"не найден закрывающий </{tag_name}> начиная с {open_tag_end}")
