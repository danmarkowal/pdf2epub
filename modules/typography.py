"""Typographic refinements an editor would make to the text.

- A sign-off at the end of an introduction or preface ("James Murphy Abbots
  Langley February, 1939") is set as a signature: the name in small capitals
  on the right, then place and date in italics on lines of their own. So is
  a dateline on its own ("The Fortress, Landsberg a/L., October 16th, 1924").
- Lists whose items carry their own numbering ("(1)", "2.", "(a)") show that
  numbering, with a hanging indent, instead of bullets.
- Endnotes at the end of chapters are set smaller, each starting with its
  number in bold ("Note 6.") rather than inside brackets.
"""
from typing import Optional
from xml.sax.saxutils import escape as xml_escape

import regex as re

from modules.chapters import plain_text
from modules.cleanup import is_paragraph, split_blocks

MONTHS = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
          r"|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?")
_DAY = r"\d{1,2}(?:st|nd|rd|th)?"
DATE_AT_END = re.compile(
    rf"(?P<date>(?:(?:{_DAY}\s+(?:of\s+)?)?{MONTHS}\s*(?:{_DAY})?,?\s*)?\d{{4}})\.?$", re.I)
INITIALS = re.compile(r"^(?:\p{Lu}\.\s?){1,4}")
# Lowercase words allowed in a place name ("Stratford upon Avon")
PLACE_PARTICLES = {"on", "upon", "de", "la", "le", "of", "the", "am", "an", "bei", "sur", "en"}
# Words that can't make up a dateline's place on their own ("In 1924")
DATELINE_STOPWORDS = {"in", "on", "at", "by", "the", "since", "until", "from", "of", "and", "after", "before"}
MAX_SIGNOFF_WORDS = 15
# A sign-off closes a piece of writing: there must be prose before it
MIN_PROSE_WORDS = 25
# Short closing lines that are not places
NOT_A_PLACE = re.compile(r"[!?]|^\s*(?:the end|finis|fin|ende|amen)\b", re.I)
SECTION_BREAK = '<hr class="section-break" />'
MAX_PLACE_WORDS = 6

LIST_LABEL = re.compile(r"^\s*(\((?:\d{1,3}|[a-zA-Z]|[ivxlcIVXLC]{2,5})\)|(?:\d{1,3}|[a-zA-Z]|[ivxlcIVXLC]{2,5})[.)])\s+")
NOTE_LABEL = re.compile(r"^\s*\[?\s*((?:Note|Anm\.?|Nota)\s+\d+[a-z]?\.?|\d+\.)\s*", re.I)


def _is_place(place: str) -> bool:
    """Whether text reads like a place name ("The Fortress, Landsberg a/L.")
    rather than a sentence: capitalised words, particles and abbreviations."""
    words = [w.strip(",;") for w in place.split()]
    if len(words) > MAX_PLACE_WORDS:
        return False
    for word in words:
        if not word:
            continue
        abbreviation = len(word) <= 5 and ("." in word or "/" in word)
        if not (word[0].isupper() or word.lower() in PLACE_PARTICLES or abbreviation):
            return False
    return True


def _signoff_parts(text: str, people: list, place_only: bool = False) -> Optional[tuple[str, str, str]]:
    """(name, place, date) if `text` is a sign-off, else None.

    A sign-off starts with the name of someone credited in the book or with
    initials ("James Murphy Abbots Langley February, 1939", "—J.R."), or is a
    dateline on its own: a place and a date, or just a date ("The Fortress,
    Landsberg a/L., October 16th, 1924"). With place_only, which callers set
    when the layout confirms the end of a piece of writing, a place on its
    own also counts ("The Fortress, Landsberg am Lech."). name or place may
    be empty.
    """
    if len(text.split()) > MAX_SIGNOFF_WORDS:
        return None
    dashed = text[:1] in "—–―"
    stripped = text.lstrip("—–― ").strip()
    name = next((stripped[:len(p)] for p, _ in people if p and stripped.lower().startswith(p.lower())), None)
    if name is None:
        initials = INITIALS.match(stripped)
        if initials and (dashed or DATE_AT_END.search(stripped)):
            name = initials.group(0).strip()
    rest = stripped[len(name):].strip(" ,") if name else stripped
    date = DATE_AT_END.search(rest)
    place = (rest[:date.start()] if date else rest).strip(" ,")
    if not _is_place(place):
        return None  # a sentence, not a signature
    if not name:
        # A dateline needs a date (or, with place_only, a place of two or
        # more words), and a place that isn't just a word like "In"
        if place and set(w.lower().strip(".,") for w in place.split()) <= DATELINE_STOPWORDS:
            return None
        if not date and not (place_only and len(place.split()) >= 2 and not NOT_A_PLACE.search(text)
                             and place.split()[0].lower() not in DATELINE_STOPWORDS - {"the"}):
            return None
    return name or "", place, date.group("date") if date else ""


def _trailing_signoff(text: str, people: list):
    """A sign-off run onto the end of the last paragraph by marker
    ("...for the followers of our Movement. The Fortress, Landsberg a/L.,
    October 16th, 1924"): (text before it, its parts), or None."""
    for match in re.finditer(r"[.!?…][\"'”’)]*\s+(?=\p{Lu}|[—–―])", text):
        tail = text[match.end():]
        parts = _signoff_parts(tail, people)
        if parts:
            return text[:match.end()].rstrip(), parts
    return None


def _signoff_html(name: str, place: str, date: str) -> str:
    lines = []
    if name:
        lines.append(f'<p class="signoff-name">{xml_escape(name)}</p>')
    if place:
        lines.append(f'<p class="signoff-place">{xml_escape(place)}</p>')
    if date:
        lines.append(f'<p class="signoff-date">{xml_escape(date)}</p>')
    css_class = "signoff" if name else "signoff dateline"
    return f'<div class="{css_class}">\n' + "\n".join(lines) + "\n</div>"


def format_signoff(markdown_text: str, people: list) -> str:
    """Set sign-offs as signature blocks, each part on a line of its own.

    Looks at the end of the section and at the end of each part of it before
    an unmarked section break (see PdfInfo.mark_section_breaks): a closing
    "James Murphy Abbots Langley February, 1939", a dateline "The Fortress,
    Landsberg a/L., October 16th, 1924", or, there, just a place "The
    Fortress, Landsberg am Lech.". people are (name, role) pairs of the
    people credited in the book.
    """
    blocks = split_blocks(markdown_text)
    ends = [i for i in range(len(blocks)) if i == len(blocks) - 1 or blocks[i + 1] == SECTION_BREAK]
    changed = False
    for i in reversed(ends):
        block = blocks[i]
        # A sign-off closes a piece of writing, so it needs prose before it
        # (not, say, a page with just the author's name)
        if not is_paragraph(block) or not any(
                is_paragraph(b) and len(b.split()) >= MIN_PROSE_WORDS for b in blocks[:i]):
            continue
        text = plain_text(block)
        parts = _signoff_parts(text, people, place_only=True)
        if parts:
            blocks[i] = _signoff_html(*parts)
            changed = True
            continue
        trailing = _trailing_signoff(text, people)
        # Split the paragraph only where its markdown matches the visible text
        tail = text[len(trailing[0]):].strip() if trailing else ""
        if trailing and block.rstrip().endswith(tail):
            cut = len(block.rstrip()) - len(tail)
            blocks[i:i + 1] = [block[:cut].rstrip(), _signoff_html(*trailing[1])]
            changed = True
    return "\n\n".join(blocks) if changed else markdown_text


def _split_leading(soup, element, pattern, css_class: str, strip: str = "") -> bool:
    """Wrap the text matched by `pattern` at the start of `element` in a span."""
    first = element.find(string=True)
    if first is None:
        return False
    match = pattern.match(str(first))
    if not match:
        return False
    span = soup.new_tag("span", attrs={"class": css_class})
    span.string = match.group(1).strip(strip)
    rest = str(first)[match.end():]
    first.replace_with(span)
    span.insert_after(" " + rest if rest else " ")
    return True


def refine_html(html: str) -> str:
    """Numbered lists without bullets, and legible endnotes."""
    from bs4 import BeautifulSoup

    if "<li" not in html and "notes-heading" not in html:
        return html
    soup = BeautifulSoup(html, "html.parser")

    for lst in soup.find_all(["ul", "ol"]):
        items = lst.find_all("li", recursive=False)
        if items and all(LIST_LABEL.match(li.get_text()) for li in items):
            lst["class"] = lst.get("class", []) + ["enumerated"]
            for li in items:
                _split_leading(soup, li, LIST_LABEL, "enum")

    for heading in soup.find_all("h3", class_="notes-heading"):
        open_bracket = False
        for sibling in heading.find_next_siblings():
            if sibling.name in ("h1", "h2", "h3", "h4", "div"):
                break
            if sibling.name != "p":
                continue
            sibling["class"] = sibling.get("class", []) + ["note"]
            first = sibling.find(string=True)
            if first is not None and str(first).lstrip().startswith("["):
                open_bracket = True
            _split_leading(soup, sibling, NOTE_LABEL, "note-label", strip="[ ")
            last = sibling.find_all(string=True)
            if open_bracket and last and str(last[-1]).rstrip().endswith("]"):
                last[-1].replace_with(str(last[-1]).rstrip()[:-1])
                open_bracket = False
    return soup.decode(formatter="minimal")
