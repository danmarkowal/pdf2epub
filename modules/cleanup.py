"""Repair markdown paragraphs and lists that marker split at PDF page breaks.

marker only joins a paragraph across a page break when the line positions
match exactly, so justified book text often comes out with sentences cut in
two ("...a gate to close" / "them off, open to..."). The first line of a page
is also sometimes mistaken for a heading ("#### shout:"). This module rejoins
those pieces using the text alone, so it works on existing markdown too.
"""
import regex as re

HEADING = re.compile(r'^(#{1,6})[ \t]+(.+?)[ \t#]*$')
FENCE = re.compile(r'^\s*(```|~~~)')

# Blocks starting like this are not plain paragraphs: images, tables, quotes,
# lists, code, math, footnote definitions, HTML blocks (marker's inline
# <span id="page-..."> anchors excepted), horizontal rules and indented code.
NOT_PARAGRAPH = re.compile(
    r'^(?:!\[|\||>|[-*+][ \t]|\d+[.)][ \t]|```|~~~|\$\$|\[\^|<(?!span\b)'
    r'|(?:[-*_][ \t]*){3,}$| {4}|\t)'
)

# Text that ends a paragraph: sentence punctuation, optionally followed by
# closing quotes, brackets or emphasis markers.
TERMINAL = re.compile(r'[.!?…:;][\s"\'”’»)\]*_]*$')

# Trailing footnote markers and tags, ignored when checking how a paragraph ends.
TRAILING_MARKUP = re.compile(r'(?:<sup>.*?</sup>|<[^>]+>|\[[^\]]*\]\(#[^)]*\)|\s)+$')

# Leading anchors and emphasis markers, skipped when checking how one starts.
LEADING_MARKUP = re.compile(r'^(?:<span\b[^>]*>\s*</span>|[*_]+|\s+)+')

DASHES = ("—", "–")

# A paragraph ending in one of these words continues on the next page even if
# that page starts with a capital ("of the" / "Republic"). English only; other
# languages still get the lowercase-start rule, which covers most breaks.
FUNCTION_WORDS = {
    "a", "an", "the", "and", "or", "but", "nor", "of", "to", "in", "on", "at",
    "by", "for", "from", "with", "into", "onto", "upon", "toward", "towards",
    "as", "than", "that", "which", "who", "whom", "whose", "this", "these",
    "those", "my", "your", "his", "her", "its", "our", "their", "is", "are",
    "was", "were", "be", "been", "not", "if", "since", "about", "between",
}

# Shorter paragraphs (bylines, epigraph credits, titles) are only joined when
# the next one starts lowercase.
MIN_WORDS_FOR_CAPITAL_JOIN = 8


def split_blocks(text: str) -> list[str]:
    """Split markdown into blank-line separated blocks, keeping code fences whole."""
    blocks, current, fence = [], [], None
    for line in text.split("\n"):
        match = FENCE.match(line)
        if fence:
            current.append(line)
            if match and match.group(1) == fence:
                fence = None
        elif match:
            fence = match.group(1)
            current.append(line)
        elif line.strip():
            current.append(line)
        elif current:
            blocks.append("\n".join(current))
            current = []
    if current:
        blocks.append("\n".join(current))
    return blocks


def heading_match(block: str):
    """Return the HEADING match for a single-line heading block, else None."""
    return HEADING.match(block) if "\n" not in block else None


def is_paragraph(block: str) -> bool:
    return not heading_match(block) and not NOT_PARAGRAPH.match(block)


def _unwrap_emphasis(text: str) -> str:
    match = re.fullmatch(r'(\*\*|__|\*|_)(.+)\1', text)
    return match.group(2) if match else text


def _continues(prev: str, nxt: str, is_heading: bool) -> bool:
    """Whether paragraph `prev` runs on into the text `nxt`."""
    tail = TRAILING_MARKUP.sub("", prev)
    if not tail or TERMINAL.search(tail):
        return False

    start = LEADING_MARKUP.sub("", nxt).lstrip("".join(DASHES))
    if not start:
        return False
    first = start[0]
    if first.islower():
        return True
    # Anything else (a capital, digit or quote mark) could start a new
    # paragraph, so only join when the previous one clearly runs on.
    if not first.isalnum() or len(prev.split()) < MIN_WORDS_FOR_CAPITAL_JOIN:
        return False
    if tail.endswith((",",) + DASHES):
        return True
    words = re.findall(r"[\w'’]+", tail)
    if not words:
        return False
    last = words[-1]
    if last.lower() in FUNCTION_WORDS:
        return True
    # A name broken across pages ("Captain" / "Notaras"). Not used for
    # headings, where a byline followed by a chapter title is too common.
    return not is_heading and last[0].isupper()


def _join(prev: str, nxt: str) -> str:
    prev, nxt = prev.rstrip(), nxt.lstrip()
    if prev.endswith(("­", "¬")):  # soft hyphen: the word was split
        return prev[:-1] + nxt
    if prev.endswith(("-",) + DASHES) or LEADING_MARKUP.sub("", nxt).startswith(DASHES):
        return prev + nxt
    return f"{prev} {nxt}"


# A list item line: "- text", "* text" or "3. text"
LIST_ITEM = re.compile(r"^(?P<indent>[ \t]*)(?:(?P<bullet>[-*+])|(?P<num>\d{1,3})[.)])[ \t]+(?P<text>.*)$")
_ENUM = r"(?:\d{1,3}|[a-zA-Z]|[ivxlcIVXLC]{2,5})"
# An item's own numbering written after the bullet: "(1)", "2.", "(a)", "b)", "iv."
# (also when already escaped for Markdown, "2\.")
ENUMERATOR = re.compile(rf"^(?P<label>\({_ENUM}\)|{_ENUM}\\?[.)])(?=\s)")


def is_list(block: str) -> bool:
    return bool(LIST_ITEM.match(block.split("\n", 1)[0]))


def _labelled(block: str) -> bool:
    """Whether a list's items carry their own numbering ("(1)", "2.")."""
    first = LIST_ITEM.match(block.split("\n", 1)[0])
    return bool(first and (first.group("num") or ENUMERATOR.match(first.group("text"))))


def normalize_list(block: str) -> str:
    """Write every item as "- <label> text" with its numbering kept as text.

    marker writes numbered items in several ways ("1. text", "- 2. text",
    "- (3) text"). Markdown renumbers "1." lists from one and turns "- 2."
    into a nested list, so the original numbers would be lost or doubled.
    Escaping the label ("2\\.") keeps it as the item's own text; the EPUB
    shows such lists without bullets. An OCR'd "l." is read as "1.".
    """
    out, previous = [], None
    for line in block.split("\n"):
        match = LIST_ITEM.match(line)
        if not match:
            out.append(line)
            continue
        text = match.group("text")
        if match.group("num"):
            label = match.group("num") + "."
        else:
            enum = ENUMERATOR.match(text)
            if not enum:
                out.append(line)
                continue
            label, text = enum.group("label"), text[enum.end():].lstrip()
        if re.fullmatch(r"\(?l\\?[.)]", label) and not (previous and "k" in previous.lower()):
            label = label.replace("l", "1")  # OCR read the digit 1 as a letter l
        previous = label
        escaped = re.sub(r"^(\d+)\\?([.)])$", r"\1\\\2", label)
        out.append(f"{match.group('indent')}- {escaped} {text}")
    return "\n".join(out)


def _last_item_text(block: str) -> str:
    last = block.split("\n")[-1]
    match = LIST_ITEM.match(last)
    return match.group("text") if match else last


def repair_page_breaks(text: str) -> tuple[str, int]:
    """Rejoin paragraphs and lists split across pages.

    Rejoins paragraphs (and page-top fragments mistaken for headings), list
    items cut off at a page break, and lists split into two by a page break.
    Numbered list items are also normalised (see normalize_list). Returns
    the repaired markdown and the number of joins made; text with nothing to
    fix is returned unchanged.
    """
    out: list[str] = []
    joins, changed = 0, False
    for block in split_blocks(text):
        if is_list(block):
            normalized = normalize_list(block)
            changed |= normalized != block
            block = normalized
        if out and is_list(out[-1]):
            if is_list(block) and _labelled(block) == _labelled(out[-1]):
                out[-1] = f"{out[-1]}\n{block}"
                joins += 1
                continue
            if is_paragraph(block) and _continues(_last_item_text(out[-1]), block, is_heading=False):
                out[-1] = _join(out[-1], block)
                joins += 1
                continue
        if out and is_paragraph(out[-1]):
            heading = heading_match(block)
            if heading:
                fragment = _unwrap_emphasis(heading.group(2))
                if _continues(out[-1], fragment, is_heading=True):
                    out[-1] = _join(out[-1], fragment)
                    joins += 1
                    continue
            elif is_paragraph(block) and _continues(out[-1], block, is_heading=False):
                out[-1] = _join(out[-1], block)
                joins += 1
                continue
        out.append(block)

    if not joins and not changed:
        return text, 0
    return "\n\n".join(out) + ("\n" if text.endswith("\n") else ""), joins
