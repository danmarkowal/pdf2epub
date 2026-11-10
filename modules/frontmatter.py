"""Lay out front and back matter the way a book editor would.

The text before the first chapter is sorted into:
- a title page: the title once, with the author and other credits;
- a copyright notice and a dedication, each on a page of its own;
- epigraphs: short quotations followed by a dash credit ("—Lawrence Durrell"),
  set together on one page;
- untitled prose before the first chapter, such as a preface, which is
  offered a title so it can be found in the contents.
Anything else keeps its place, on pages of its own. At the back, untitled
biographies of the people credited on the title page ("Jean Raspail was born
in...") become "About the Author" and similar sections.
"""
from itertools import groupby
from typing import Optional
from xml.sax.saxutils import escape as xml_escape

import markdown
import regex as re

from modules.chapters import CHAPTER_WORD, ROMAN, Section, plain_text, similar
from modules.cleanup import TERMINAL, heading_match, is_paragraph, split_blocks

CREDIT = re.compile(
    r"^[*_]*(?P<role>by|written by|translated(?: (?:from|into) (?:the )?\p{L}+)?(?: and edited)? by"
    r"|(?:\p{L}+ )?translation by|edited(?: and translated)? by|illustrated by"
    r"|with (?:an? )?(?:introduction|foreword|preface|afterword|illustrations|notes) by"
    r"|(?:introduction|foreword|preface|afterword|illustrations) by)[*_]*(?=\W|$)"
    r"[\s:,]*(?P<rest>.*)$",
    re.I | re.S,
)
ATTRIBUTION = re.compile(r"^[—–―]\s*(?P<who>\S.*)$", re.S)
COPYRIGHT = re.compile(r"©|\bcopyright\b|all rights reserved|\bISBN\b|first published|printed in", re.I)
DEDICATION = re.compile(r"^(?:for|to|in (?:loving )?memory of|dedicated to)\b", re.I)

MAX_TITLE_WORDS = 15
MAX_NAME_WORDS = 6
MAX_SUBTITLE_WORDS = 10
MAX_ATTRIBUTION_WORDS = 12
# A quotation before a dash credit is an epigraph if it's this short;
# longer prose followed by a dash line is signed ("—J.R.")
MAX_EPIGRAPH_BLOCKS = 3
MAX_EPIGRAPH_WORDS = 150
PROSE_PARAGRAPH_WORDS = 25
MAX_DEDICATION_WORDS = 25
MAX_COPYRIGHT_WORDS = 150
# Untitled prose right before the first chapter this long is a preface or
# similar, and is offered a title
MIN_PREFACE_WORDS = 80

# Words that end a name in "Translated by Norman Shapiro And when..."
NAME_STOP = {
    "and", "the", "but", "when", "in", "it", "he", "she", "they", "we", "i",
    "this", "that", "a", "an", "for", "with", "from", "as", "on", "at", "to",
}
NAME_PARTICLES = {"de", "van", "von", "der", "den", "la", "le", "du", "da", "di", "del", "bin", "ibn"}

FRONT_TITLES = [
    ("foreword", "Foreword"), ("preface", "Preface"), ("introduction", "Introduction"),
    ("prologue", "Prologue"), ("author's note", "Author's Note"),
    ("translator's note", "Translator's Note"), ("note to the reader", "A Note to the Reader"),
]
DEFAULT_FRONT_TITLE = "Preface"

ROLE_TITLES = {
    "author": "About the Author", "translator": "About the Translator",
    "editor": "About the Editor", "illustrator": "About the Illustrator",
}
BIO_START = (
    r"\b(?:was born|is the author|is an?\b|lives|lived|has written|has published"
    r"|has translated|teaches|taught|grew up|is (?:currently )?(?:a )?professor|died)"
)

# Opening words set in capitals, as printed books often start a chapter
LEAD_IN = re.compile(r"^(?P<lead>(?:\p{Lu}[\p{Lu}'’-]*\s+)+\p{Lu}[\p{Lu}'’-]+)(?=\s+\P{Lu})")
MAX_LEAD_IN_WORDS = 8


def _epigraph_size(blocks: list) -> int:
    """How many of the blocks before a dash credit form an epigraph (0: the
    credit signs off prose instead, like "—J.R." after a preface)."""
    if not blocks:
        return 0
    sizes = [len(_block_text(b).split()) for b in blocks]
    prose = [n >= PROSE_PARAGRAPH_WORDS for n in sizes]
    if len(blocks) == 1:
        return 1 if sizes[0] <= MAX_EPIGRAPH_WORDS else 0
    # Short lines, such as the stanzas of a poem
    if not any(prose) and len(blocks) <= MAX_EPIGRAPH_BLOCKS and sum(sizes) <= MAX_EPIGRAPH_WORDS:
        return len(blocks)
    # A quotation after unrelated short lines (stray text from a cover)
    if prose[-1] and not any(prose[:-1]) and sizes[-1] <= MAX_EPIGRAPH_WORDS:
        return 1
    return 0


def _block_text(block: str) -> str:
    heading = heading_match(block)
    return plain_text(heading.group(2) if heading else block)


def _word_count(blocks) -> int:
    return sum(len(_block_text(b).split()) for b in blocks)


def soften_caps(text: str) -> str:
    """'LAWRENCE DURRELL' -> 'Lawrence Durrell', for setting in small capitals."""
    letters = [c for c in text if c.isalpha()]
    if len(letters) < 3 or any(c.islower() for c in letters):
        return text

    def soften(word):
        if "." in word or re.fullmatch(ROMAN, word):
            return word  # initials ("J.R.") and numerals ("XX") stay
        return "-".join(part[:1] + part[1:].lower() for part in word.split("-"))

    return " ".join(soften(w) for w in text.split())


def _split_name(rest: str) -> tuple[str, str]:
    """Split "*Norman Shapiro* And when..." into the name and what follows."""
    emphasized = re.match(r"^\s*(\*{1,3}|_{1,3})(.+?)\1\s*(.*)$", rest, re.S)
    if emphasized:
        return plain_text(emphasized.group(2)), emphasized.group(3).strip()
    words, name = rest.split(), []
    for word in words:
        bare = word.strip("*_,;")
        if len(name) >= MAX_NAME_WORDS or (name and bare.lower() in NAME_STOP):
            break
        if not (bare[:1].isupper() or (name and bare.lower() in NAME_PARTICLES)):
            break
        name.append(bare)
        if word.endswith((",", ";")) or (bare.endswith(".") and len(bare) > 2):
            break
    return " ".join(name), " ".join(words[len(name):]).strip()


def parse_credit(block: str):
    """("Translated by", "translator", name, rest of the block) or None."""
    text = re.sub(r"\*\*|__", "", block).strip()
    text = re.sub(r"^#{1,6}\s+", "", text)
    match = CREDIT.match(text)
    if not match:
        return None
    role = match.group("role")
    role = soften_caps(role)
    display = "by" if role.lower() == "by" else role[:1].upper() + role[1:]
    lowered = role.lower()
    if lowered in ("by", "written by"):
        key = "author"
    elif lowered.startswith("translat"):
        key = "translator"
    elif lowered.startswith("edited"):
        key = "editor"
    elif lowered.startswith("illustrated"):
        key = "illustrator"
    else:
        key = "contributor"
    name, remainder = _split_name(match.group("rest"))
    return display, key, name, remainder


def previous_chapter_title(title: Optional[str]) -> Optional[str]:
    """If the first chapter found is number two, the title of number one.

    Untitled text before "Two" is almost certainly chapter one whose heading
    was missed.
    """
    if not title:
        return None
    match = re.match(rf"^(?P<pre>{CHAPTER_WORD}\s+)?(?P<num>2|II|two)(?P<post>[.:)]?)(?:\s.*)?$",
                     title.strip(), re.I)
    if not match:
        return None
    num = match.group("num")
    if num == "2":
        one = "1"
    elif num.upper() == "II":
        one = "I"
    else:
        one = "ONE" if num.isupper() else "One" if num[0].isupper() else "one"
    return (match.group("pre") or "") + one + match.group("post")


def suggest_title(markdown_text: str, first_chapter_title: Optional[str]) -> str:
    """A title for untitled prose before the first chapter."""
    previous = previous_chapter_title(first_chapter_title)
    if previous:
        return previous
    opening = " ".join(_block_text(b) for b in split_blocks(markdown_text)[:3])[:800]
    opening = opening.lower().replace("’", "'")
    for keyword, title in FRONT_TITLES:
        if keyword in opening:
            return title
    return DEFAULT_FRONT_TITLE


def add_lead_in(markdown_text: str) -> str:
    """Set capitalised opening words ("I HAD WANTED TO WRITE a lengthy...")
    in small capitals, the way printed books do."""
    blocks = split_blocks(markdown_text)
    for i, block in enumerate(blocks):
        if heading_match(block) or block.startswith("!["):
            continue
        if not is_paragraph(block):
            return markdown_text
        match = LEAD_IN.match(block)
        if not match:
            return markdown_text
        lead = match.group("lead")
        if not 2 <= len(lead.split()) <= MAX_LEAD_IN_WORDS:
            return markdown_text
        blocks[i] = f'<span class="lead-in">{lead[0]}{lead[1:].lower()}</span>{block[match.end():]}'
        return "\n\n".join(blocks)
    return markdown_text


def title_page_html(title: str, subtitle: Optional[str], credits: list) -> str:
    """Centred title page: title, subtitle, then each credit, its role in
    small italics above the name ("Translated by" / "Norman Shapiro")."""
    lines = [f'<h1 class="book-title">{xml_escape(title)}</h1>']
    if subtitle:
        lines.append(f'<p class="subtitle">{xml_escape(soften_caps(subtitle))}</p>')
    for display, key, name in credits:
        group = "author" if key == "author" else "credit"
        if display:
            lines.append(f'<p class="role {group}-role">{xml_escape(display)}</p>')
        # Names are set in small capitals, which only works on normal case
        lines.append(f'<p class="name {group}-name">{xml_escape(soften_caps(name))}</p>')
    return '<div class="titlepage" epub:type="titlepage">\n' + "\n".join(lines) + "\n</div>"


def title_page(title: str, subtitle: Optional[str], credits: list) -> Section:
    return Section(kind="titlepage", html=title_page_html(title, subtitle, credits))


def _epigraphs_page(epigraphs) -> Section:
    parts = []
    for quote_blocks, who in epigraphs:
        # Keep the line breaks of verse
        quote = markdown.markdown("\n\n".join(b.replace("\n", "  \n") for b in quote_blocks))
        parts.append(f'<blockquote class="epigraph">\n{quote}\n'
                     f'<p class="attribution">— {xml_escape(soften_caps(who))}</p>\n</blockquote>')
    return Section(kind="epigraphs", html='<div class="epigraphs" epub:type="epigraph">\n'
                   + "\n".join(parts) + "\n</div>")


def _wrapped_page(kind: str, epub_type: str, blocks) -> Section:
    body = markdown.markdown("\n\n".join(blocks))
    return Section(kind=kind, html=f'<div class="{kind}" epub:type="{epub_type}">\n{body}\n</div>')


def _text_markdown(items) -> str:
    out = []
    for kind, payload in items:
        if kind == "signature":
            out.append(f'<p class="signature">— {xml_escape(soften_caps(payload))}</p>')
        else:
            out.append(payload)
    return "\n\n".join(out)


def layout_front_matter(markdown_text: str, book_title: str, author: Optional[str],
                        first_chapter_title: Optional[str]) -> tuple[list[Section], list]:
    """Turn the markdown before the first chapter into front-matter pages.

    Returns the pages and the people credited on the title page as
    (name, role) pairs, e.g. ("Norman Shapiro", "translator").
    """
    queue = split_blocks(markdown_text)
    items = []  # (kind, payload) in reading order
    state = "before"  # before / on / after the title page
    title_text, subtitle, credits = None, None, []

    while queue:
        block = queue.pop(0)
        text = _block_text(block)
        if not text and not block.startswith("!["):
            continue
        words = len(text.split())
        is_title = words <= MAX_TITLE_WORDS and (
            similar(text, book_title)
            or (state == "before" and heading_match(block) and queue and parse_credit(queue[0]))
        )
        if is_title:
            if state == "before":
                state, title_text = "on", text
                items.append(("titlepage", None))
            elif queue and parse_credit(queue[0]):
                state = "on"  # the full title page after a half-title or cover
            continue  # the title is shown once; repeats are dropped

        if state == "on":
            credit = parse_credit(block)
            if credit:
                display, key, name, remainder = credit
                if not name and queue and len(_block_text(queue[0]).split()) <= MAX_NAME_WORDS:
                    name = _block_text(queue.pop(0))  # "by" and the name on separate lines
                if name:
                    if not any(k == key and similar(n, name) for _, k, n in credits):
                        credits.append((display, key, name))
                    if remainder:
                        queue.insert(0, remainder)  # e.g. an epigraph run into the credit
                    continue
            if author and words <= MAX_NAME_WORDS and similar(text, author):
                if not any(key == "author" for _, key, _ in credits):
                    credits.append(("", "author", text))
                continue
            if (subtitle is None and not credits and 0 < words <= MAX_SUBTITLE_WORDS
                    and not heading_match(block) and not TERMINAL.search(text)):
                subtitle = text
                continue
            state = "after"

        attribution = ATTRIBUTION.match(text)
        if attribution and words <= MAX_ATTRIBUTION_WORDS:
            run = 0
            while run < len(items) and items[-1 - run][0] == "text":
                run += 1
            quote_size = _epigraph_size([payload for _, payload in items[len(items) - run:]])
            who = attribution.group("who").strip()
            if quote_size:
                quote = [payload for _, payload in items[len(items) - quote_size:]]
                del items[len(items) - quote_size:]
                items.append(("epigraph", (quote, who)))
            else:
                items.append(("signature", who))
        elif COPYRIGHT.search(text) and words <= MAX_COPYRIGHT_WORDS:
            items.append(("copyright", block))
        elif (DEDICATION.match(text) and words <= MAX_DEDICATION_WORDS
              and not any(kind in ("text", "epigraph") for kind, _ in items)):
            items.append(("dedication", block))
        else:
            items.append(("text", block))

    if author and author != "Unknown Author" and not any(key == "author" for _, key, _ in credits):
        credits.insert(0, ("by", "author", author))
    shown_title = book_title if book_title and book_title != "Untitled Document" else (title_text or book_title)

    # The prose right before the first chapter may be a preface
    tail_start = len(items)
    while tail_start > 0 and items[tail_start - 1][0] in ("text", "signature"):
        tail_start -= 1
    tail = items[tail_start:]

    pages = []
    group_key = lambda item: "text" if item[0] == "signature" else item[0]  # noqa: E731
    for kind, group in groupby(items[:tail_start], key=group_key):
        group = list(group)
        if kind == "titlepage":
            pages.append(title_page(shown_title, subtitle, credits))
        elif kind == "epigraph":
            pages.append(_epigraphs_page([payload for _, payload in group]))
        elif kind == "copyright":
            pages.append(_wrapped_page("copyright", "copyright-page", [p for _, p in group]))
        elif kind == "dedication":
            pages.append(_wrapped_page("dedication", "dedication", [p for _, p in group]))
        else:
            pages.append(Section(kind="text", markdown=_text_markdown(group)))

    if tail:
        tail_markdown = _text_markdown(tail)
        if _word_count([p for k, p in tail if k == "text"]) >= MIN_PREFACE_WORDS:
            pages.append(Section(
                kind="preface",
                markdown=tail_markdown,
                suggested_title=suggest_title(tail_markdown, first_chapter_title),
            ))
        else:
            pages.append(Section(kind="text", markdown=tail_markdown))

    people = [(soften_caps(name), key) for _, key, name in credits]
    return pages, people


def arrange_front_matter(sections: list[Section], book_title: str,
                         author: Optional[str]) -> tuple[list[Section], list]:
    """Replace the unarranged "front" section with laid-out pages."""
    first_chapter = next((s for s in sections if s.in_toc), None)
    out, people = [], []
    for section in sections:
        if section.kind != "front":
            out.append(section)
            continue
        pages, people = layout_front_matter(section.markdown, book_title, author,
                                            (first_chapter.label or first_chapter.title) if first_chapter else None)
        out.extend(pages)
    return out, people


def split_off_bios(sections: list[Section], people: list) -> list[Section]:
    """Give untitled biographies at the back of the book their own sections.

    A paragraph near the end that starts with the name of someone credited
    on the title page and reads like a biography ("Jean Raspail was born in
    1925...") starts an "About the Author" (or "About the Translator", ...)
    section, together with any photo right before it.
    """
    if not people:
        return sections
    last_sections = set(range(max(0, len(sections) - 3), len(sections)))
    out = []
    for index, section in enumerate(sections):
        if index not in last_sections or section.kind != "chapter" or section.html:
            out.append(section)
            continue
        blocks = split_blocks(section.markdown)
        starts = []  # (block index, title)
        for i, block in enumerate(blocks):
            if i < 1 or not is_paragraph(block):
                continue
            text = plain_text(block)
            for name, role in people:
                if re.match(rf"{re.escape(name)}\b.{{0,80}}?{BIO_START}", text, re.I | re.S):
                    start = i
                    while start - 1 > (starts[-1][0] if starts else 0) and blocks[start - 1].startswith("!["):
                        start -= 1
                    starts.append((start, ROLE_TITLES.get(role, f"About {name}")))
                    break
        if not starts:
            out.append(section)
            continue
        section.markdown = "\n\n".join(blocks[:starts[0][0]])
        out.append(section)
        for n, (start, title) in enumerate(starts):
            end = starts[n + 1][0] if n + 1 < len(starts) else len(blocks)
            out.append(Section(markdown="\n\n".join(blocks[start:end]), title=title,
                               in_toc=True, kind="bio"))
    return out
