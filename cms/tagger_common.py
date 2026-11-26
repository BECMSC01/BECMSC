"""
Общая логика для extract_tag.py и build.py.
Определяет, какие элементы HTML считаются "редактируемым текстом",
и как получить/заменить их значение.
"""
from bs4 import NavigableString, Tag

SKIP_TAGS = {
    "script", "style", "svg", "path", "template", "noscript", "iframe",
    "head", "link", "meta_EXCLUDED",  # meta handled separately
}

# Теги, которые считаем "структурными" — всегда рекурсируем внутрь них,
# даже если у них нет вложенных тегов (на случай редких шаблонов).
ALWAYS_RECURSE_TAGS = {
    "html", "body", "header", "nav", "footer", "section", "div",
    "ul", "ol", "table", "tbody", "thead", "tr", "form", "main",
    "article", "aside", "picture", "figure",
}

# Декоративные/иконочные теги — не текст сами по себе.
ICON_TAGS = {"i", "svg", "path"}

# Чисто форматирующие inline-теги — если ВСЕ дети элемента такие,
# весь элемент можно считать "текстовым блоком" (rich/html режим).
INLINE_FORMATTING_TAGS = {"b", "strong", "em", "i", "u", "mark", "sup", "sub", "br", "span"}

# Атрибуты, помечающие элемент как управляемый JS-кодом (динамический),
# а не статический текст - такие не трогаем.
DYNAMIC_MARKERS = {"data-lead-status"}


def _is_icon_only(tag: Tag) -> bool:
    if tag.name not in ICON_TAGS:
        return False
    return not tag.get_text(strip=True)


def classify(el: Tag):
    """
    Возвращает один из:
      ('skip', None)
      ('recurse', None)
      ('leaf-text', text_node)       # единственный прямой текстовый узел
      ('leaf-text-icons', text_node) # текстовый узел среди иконок
      ('leaf-html', None)            # весь innerHTML - один блок
      ('leaf-image', img_tag)        # <img> - картинка (src + опц. webp-пара)
    """
    if not isinstance(el, Tag):
        return ("skip", None)
    if el.has_attr("data-cms"):
        # уже размечен в прошлом запуске - не трогаем повторно
        return ("skip", None)
    if el.name == "img":
        return ("leaf-image", el)
    if el.name in SKIP_TAGS:
        return ("skip", None)
    for marker in DYNAMIC_MARKERS:
        if el.has_attr(marker):
            return ("skip", None)

    children = list(el.children)
    element_children = [c for c in children if isinstance(c, Tag)]
    text_fragments = [c for c in children if isinstance(c, NavigableString) and c.strip()]

    if not element_children:
        if text_fragments:
            if len(text_fragments) == 1:
                return ("leaf-text", text_fragments[0])
            return ("leaf-html", None)
        return ("skip", None)

    if el.name in ALWAYS_RECURSE_TAGS:
        return ("recurse", None)

    # Все дети - иконки (возможно без текста)
    if all(_is_icon_only(c) or c.name in ICON_TAGS for c in element_children):
        if len(text_fragments) == 1:
            return ("leaf-text-icons", text_fragments[0])
        if not text_fragments:
            return ("skip", None)
        return ("leaf-html", None)

    # Все дети - чисто форматирующие inline-теги -> единый текстовый блок
    if all(c.name in INLINE_FORMATTING_TAGS for c in element_children):
        return ("leaf-html", None)

    return ("recurse", None)
