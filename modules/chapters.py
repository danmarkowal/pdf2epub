"""Work out a book's structure: volumes or parts, chapters and other sections.

marker marks any large or bold text as a heading and gives headings
unreliable levels, so the structure is read from what the headings say
rather than from their size:
- Labelled headings ("Volume II", "Part Two", "Chapter 3", "Twelve", "IV")
  form the hierarchy. Labels are ranked by their word: rarer labels contain
  commoner ones, so "Volume" contains "Chapter".
- A label on its own followed by a short heading is a single heading with a
  number and a title ("Chapter 1" + "In the Home of My Parents"); so is a
  label run together with its title ("Volume II The National Socialist
  Movement").
- Standard sections ("Preface", "Author's Introduction", "Epilogue") are
  sections too, except notes that recur after chapters: those are the
  chapters' endnotes and stay with them.
- In books without labelled chapters, headings that introduce a substantial
  amount of text are the chapters.
Headings that repeat the book's title never count. Other headings stay in
the text as subheadings, or become paragraphs when they are sentences that
marker mistook for headings.
"""
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

import markdown
import regex as re

from modules.cleanup import heading_match, split_blocks

_UNITS = "one|two|three|four|five|six|seven|eight|nine"
_TEENS = "ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen"
_TENS = "twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety"
NUMBER_WORD = rf"(?:(?:{_TENS})(?:[- ](?:{_UNITS}))?|{_TEENS}|{_UNITS}|(?:one )?hundred)"
# A non-empty Roman numeral that is a whole word ("IV", not the "C" of "Cover")
ROMAN = r"(?=[MDCLXVI]+\b)M{0,3}(?:CM|CD|D?C{0,3})(?:XC|XL|L?X{0,3})(?:IX|IV|V?I{0,3})\b"

# Label words and what they are, by language
LABEL_KINDS = {
    "volume": "volume", "vol": "volume", "tome": "volume", "band": "volume",
    "part": "part", "partie": "part", "teil": "part", "parte": "part", "deel": "part",
    "часть": "part", "act": "part",
    "book": "book", "buch": "book", "livre": "book", "libro": "book",
    "chapter": "chapter", "chap": "chapter", "kapitel": "chapter", "chapitre": "chapter",
    "capítulo": "chapter", "capitulo": "chapter", "capitolo": "chapter",
    "hoofdstuk": "chapter", "rozdział": "chapter", "глава": "chapter",
}
CHAPTER_WORD = "(?:" + "|".join(sorted(map(re.escape, LABEL_KINDS), key=len, reverse=True)) + r")\.?"
# When two kinds of label are equally common, the usual order of containment
KIND_ORDER = ["volume", "part", "book", "chapter"]

_NUMBER = rf"(?:\d+|{NUMBER_WORD}|{ROMAN})"
# "Chapter 3", "Part Two: The Storm", "Volume II The National Socialist Movement"
LABEL_WITH_WORD = re.compile(
    rf"^(?P<label>(?P<word>{CHAPTER_WORD})\s+(?P<num>{_NUMBER}))(?:\s*[.:—–-]\s*|\s+|$)(?P<title>.*)$",
    re.I | re.S,
)
# "12", "12.", "Twelve", "Twenty-one"; "IV" (capitals only, so "mix" isn't one)
BARE_LABEL = [re.compile(rf"^(?P<label>\d+|{NUMBER_WORD})[.:)]?$", re.I), re.compile(rf"^(?P<label>{ROMAN})[.:)]?$")]
# "3. The Storm", "3 Methods", "IV. The Storm"
BARE_WITH_TITLE = [
    re.compile(r"^(?P<label>\d+)(?:[.:)]\s*|\s+)(?P<title>\p{Lu}.*)$"),
    re.compile(rf"^(?P<label>{ROMAN})(?:[.:)]|\s+[—–-])\s*(?P<title>\p{{Lu}}.*)$"),
]

SPECIAL = re.compile(
    r"^(?:(?:the\s+)?(?:author|translator|editor|publisher)['’]?s\s+)?"
    r"(?:prologue|prolog|preface|foreword|introduction|introductory note|epilogue|afterword"
    r"|postscript|conclusion|interlude|note|notes|endnotes|acknowledge?ments?"
    r"|appendix(?:\s+\S+)?|glossary|bibliography|references|index|dedication)$"
    r"|^about the (?:authors?|translators?|editors?|illustrators?)$|^a note on .+"
    r"|^(?:vorwort|einleitung|nachwort|anhang|anmerkungen|préface|avant-propos|épilogue|annexe"
    r"|prólogo|epílogo|apéndice|introducción)$",
    re.I,
)
NOTES = re.compile(r"^(?:end)?notes?$|^references$|^anmerkungen$", re.I)
# Headings ending like a sentence were mistaken for headings
SENTENCE_END = re.compile(r"[.:;,][\"'”’)\]]*$")

MAX_TITLE_WORDS = 15
# Books without labelled chapters: a chapter heading introduces at least
# this many words; so do unlabelled sections before the first chapter
MIN_CHAPTER_WORDS = 150
# Headings repeated within this share of the book (cover, half-title and
# title pages) are treated as the book's title
FRONT_MATTER_SHARE = 0.1


def plain_text(md_text: str) -> str:
    """Render a snippet of markdown and return its visible text."""
    from bs4 import BeautifulSoup

    html = markdown.markdown(md_text)
    return " ".join(BeautifulSoup(html, "html.parser").get_text().split())


def _words(text: str) -> set[str]:
    return set(re.findall(r"\w+", text.lower()))


def similar(a: str, b: str) -> bool:
    wa, wb = _words(a), _words(b)
    return bool(wa and wb) and len(wa & wb) / len(wa | wb) >= 0.75


def _normalize(text: str) -> str:
    return " ".join(re.findall(r"\w+", text.lower()))


def parse_label(text: str):
    """Split a heading into (kind, label, title).

    kind is "volume", "part", "book", "chapter", "number" (a bare number) or
    None for an unlabelled heading; label and title may be None.
    """
    text = text.strip()
    match = LABEL_WITH_WORD.match(text)
    if match:
        kind = LABEL_KINDS.get(match.group("word").lower().rstrip("."))
        title = match.group("title").strip(" .:—–-") or None
        return kind, match.group("label").strip(), title
    for pattern in BARE_LABEL:
        match = pattern.match(text)
        if match:
            return "number", match.group("label"), None
    for pattern in BARE_WITH_TITLE:
        match = pattern.match(text)
        if match:
            return "number", match.group("label"), match.group("title").strip()
    return None, None, text


@dataclass
class Heading:
    block: int  # index into the markdown blocks
    level: int
    text: str
    kind: Optional[str] = None  # see parse_label
    label: Optional[str] = None
    title: Optional[str] = None
    blocks: int = 1  # blocks the heading spans (2 for "Chapter 1" + its title)
    consumed: bool = False  # the title half of a label + title pair
    words: int = 0  # words up to the next heading at this level or above
    own_words: int = 0  # words up to the next heading of any level
    special: bool = False
    notes: bool = False
    title_like: bool = False
    sentence: bool = False
    role: Optional[str] = None  # "container", "chapter" or "section" once chosen
    tier: int = 0


@dataclass
class Section:
    """One XHTML file of the EPUB."""
    markdown: str = ""
    title: Optional[str] = None  # the heading's title ("In the Home of My Parents")
    label: Optional[str] = None  # its number ("Chapter 1"), if any
    in_toc: bool = False
    depth: int = 0  # nesting in the contents: chapters inside a volume are 1
    heading_line: str = ""  # the original heading markdown
    anchors: list = field(default_factory=list)  # link targets from the heading
    page_id: Optional[int] = None  # 0-based PDF page the section starts on
    # "chapter", "part" (a volume, part or book page), "section" (a preface,
    # notes...), or for front and back matter "front" (not yet arranged),
    # "titlepage", "epigraphs", "dedication", "copyright", "image", "text",
    # "preface" or "bio"
    kind: str = "chapter"
    html: Optional[str] = None  # ready-made page body, used instead of markdown
    # Untitled text that should get a title: the suggested one
    suggested_title: Optional[str] = None

    @property
    def full_title(self) -> Optional[str]:
        """"Chapter 1: In the Home of My Parents", for menus and page titles."""
        if self.label and self.title:
            if " " not in self.label:  # a bare number: "1. Introduction"
                return f"{self.label.rstrip('.')}. {self.title}"
            return f"{self.label}: {self.title}"
        return self.title or self.label


def _bold_title(block: str) -> Optional[str]:
    """Text of a short, entirely bold paragraph (a title that marker didn't
    recognise as a heading)."""
    match = re.fullmatch(r"\*\*([^*\n]+)\*\*\s*", block)
    if match and len(match.group(1).split()) <= MAX_TITLE_WORDS:
        return match.group(1).strip()
    return None


def find_headings(blocks: list[str], book_title: str = "") -> list[Heading]:
    headings = []
    for i, block in enumerate(blocks):
        match = heading_match(block)
        if match:
            text = plain_text(match.group(2))
            if text:
                headings.append(Heading(i, len(match.group(1)), text))

    front_end = len(blocks) * FRONT_MATTER_SHARE
    front = [h for h in headings if h.block < front_end]
    for h in headings:
        key = h.text.strip(" .:")
        h.kind, h.label, h.title = parse_label(key)
        h.special = h.kind is None and bool(SPECIAL.match(key))
        h.notes = h.special and bool(NOTES.match(key))
        h.sentence = h.kind is None and bool(SENTENCE_END.search(h.text)) and len(h.text.split()) >= 3
        h.title_like = h.kind is None and (similar(h.text, book_title) or (
            h.block < front_end and any(o is not h and similar(h.text, o.text) for o in front)))

    # "Chapter 1" followed straight away by its title, as a heading or as a
    # bold line, is one heading
    by_block = {h.block: h for h in headings}
    for h in headings:
        if not h.label or h.title or h.consumed:
            continue
        nxt = by_block.get(h.block + 1)
        if nxt:
            if (nxt.kind is None and not nxt.special and not nxt.sentence
                    and len(nxt.text.split()) <= MAX_TITLE_WORDS):
                h.title, h.blocks, nxt.consumed = nxt.text, 2, True
        elif h.block + 1 < len(blocks):
            title = _bold_title(blocks[h.block + 1])
            if title and not SENTENCE_END.search(title):
                h.title, h.blocks = plain_text(title), 2

    headings = [h for h in headings if not h.consumed]
    block_words = [len(b.split()) for b in blocks]
    for n, h in enumerate(headings):
        end = next((o.block for o in headings[n + 1:] if o.level <= h.level), len(blocks))
        h.words = sum(block_words[h.block + h.blocks:end])
        following = headings[n + 1].block if n + 1 < len(headings) else len(blocks)
        h.own_words = sum(block_words[h.block + h.blocks:following])
    return headings


def _unlabelled_chapters(headings: list[Heading]) -> list[Heading]:
    """Chapters in books whose chapters have titles but no numbers."""
    candidates = [
        h for h in headings
        if h.kind is None and not h.title_like and not h.sentence and not h.notes
        and len(h.text.split()) <= MAX_TITLE_WORDS and (h.special or h.words >= MIN_CHAPTER_WORDS)
    ]
    counts = Counter(h.level for h in candidates)
    repeated = [lvl for lvl, n in counts.items() if n >= 2]
    if not repeated:
        return []
    level = min(repeated)
    return [h for h in candidates if h.level <= level]


def choose_sections(headings: list[Heading]) -> list[Heading]:
    """Pick the headings that start sections and set their role and tier."""
    labelled = [h for h in headings if h.kind]
    word_counts = Counter(h.kind for h in labelled if h.kind != "number")
    numbers = [h for h in labelled if h.kind == "number"]

    chapters, containers = [], []
    if "chapter" in word_counts:
        # Bare numbers are then subsections within chapters
        chapters = [h for h in labelled if h.kind == "chapter"]
        containers = [h for h in labelled if h.kind not in ("chapter", "number")]
    elif len(numbers) >= 2:
        # Numbers at a shallower heading level, and rarer, are containers
        levels = Counter(h.level for h in numbers)
        main = max(levels, key=lambda lvl: (levels[lvl], lvl))
        chapters = [h for h in numbers if h.level >= main]
        containers = [h for h in labelled if h.kind != "number"]
        for h in numbers:
            if h.level < main and levels[h.level] < levels[main]:
                h.kind = "upper-number"
                containers.append(h)
    elif word_counts:
        # Only "Part One", "Book Two"...: titled chapters may sit inside them
        titled = [h for h in _unlabelled_chapters(headings) if not h.special]
        if len(titled) >= 2:
            chapters, containers = titled, labelled
        else:
            common = max(word_counts, key=lambda k: (word_counts[k], KIND_ORDER.index(k)))
            chapters = [h for h in labelled if h.kind == common]
            containers = [h for h in labelled if h.kind != common]
    if len(chapters) < 2:
        chosen = _unlabelled_chapters(headings)
        for h in chosen:
            h.role = "section" if h.special else "chapter"
        return chosen if len(chosen) >= 2 else []

    # Rarer kinds of label contain commoner ones (volume > chapter)
    counts = Counter(h.kind for h in containers)
    order = sorted(counts, key=lambda k: (counts[k], KIND_ORDER.index(k) if k in KIND_ORDER else 9))
    for h in containers:
        h.role, h.tier = "container", order.index(h.kind)
    for h in chapters:
        h.role, h.tier = "chapter", len(order)

    # Standard sections, and unlabelled sections before the first chapter
    # or volume ("Author's Introduction"). Notes that recur are endnotes.
    recurring_notes = sum(h.notes for h in headings) > 1
    first_structural = min(h.block for h in chapters + containers)
    for h in headings:
        if h.role or h.kind or (h.notes and recurring_notes):
            continue
        if h.special or (h.block < first_structural and not h.title_like and not h.sentence
                         and len(h.text.split()) <= MAX_TITLE_WORDS and h.own_words >= MIN_CHAPTER_WORDS):
            h.role = "section"
    return sorted((h for h in headings if h.role), key=lambda h: h.block)


def _depths(chosen: list[Heading]) -> list[int]:
    """Nesting of each section: chapters inside the volume or part before them.

    Sections such as a preface before the first chapter or volume, or notes
    after the last chapter, are top level; between chapters they belong to
    the current volume.
    """
    structural = [k for k, h in enumerate(chosen) if h.role in ("container", "chapter")]
    chapter_positions = [k for k, h in enumerate(chosen) if h.role == "chapter"]
    first = structural[0] if structural else 0
    last_chapter = chapter_positions[-1] if chapter_positions else len(chosen)
    depths, stack = [], []
    for k, h in enumerate(chosen):
        if h.role == "container":
            while stack and stack[-1] >= h.tier:
                stack.pop()
            depths.append(len(stack))
            stack.append(h.tier)
        elif h.role == "chapter":
            depths.append(len(stack))
        elif k < first or k > last_chapter:
            depths.append(0)
        else:
            depths.append(len(stack))
    return depths


def _body_block(block: str, heading: Optional[Heading], endnotes: bool) -> str:
    """A heading inside a section: an endnotes heading, a subheading, or
    (for a sentence marker mistook for a heading) a plain paragraph."""
    if heading is None:
        return block
    if endnotes and heading.notes:
        return f'<h3 class="notes-heading">{heading.text}</h3>'
    if heading.sentence:
        return heading_match(block).group(2)
    return f"### {heading.text}"


def split_into_chapters(markdown_text: str, book_title: str = "") -> list[Section]:
    """Split markdown into sections: volume or part pages, chapters and
    other sections such as a preface.

    Each section becomes its own XHTML file, which EPUB readers start on a
    new page. Its opening heading is taken out of the markdown and kept as a
    label and title, so it can be laid out consistently. Everything before
    the first section is returned as one section of kind "front", to be laid
    out by modules.frontmatter.
    """
    blocks = split_blocks(markdown_text)
    headings = find_headings(blocks, book_title)
    chosen = choose_sections(headings)
    if not chosen:
        return [Section(markdown=markdown_text)]

    endnotes = sum(h.notes for h in headings) > 1
    by_block = {h.block: h for h in headings}
    sections = []
    if chosen[0].block > 0:
        sections.append(Section(markdown="\n\n".join(blocks[:chosen[0].block]), kind="front"))

    for n, (h, depth) in enumerate(zip(chosen, _depths(chosen))):
        end = chosen[n + 1].block if n + 1 < len(chosen) else len(blocks)
        heading_blocks = blocks[h.block:h.block + h.blocks]
        body = [_body_block(blocks[i], by_block.get(i), endnotes) for i in range(h.block + h.blocks, end)]
        sections.append(Section(
            markdown="\n\n".join(body),
            title=h.title if h.kind else h.text,
            label=h.label if h.kind else None,
            in_toc=True,
            depth=depth,
            kind={"container": "part", "chapter": "chapter"}.get(h.role, "section"),
            heading_line="\n\n".join(heading_blocks),
            anchors=re.findall(r'<span id="[^"]+"></span>', "".join(heading_blocks)),
        ))
    return sections


def first_paragraph_text(section: Section) -> str:
    """Visible text of a section's first paragraph (skipping headings and images)."""
    for block in split_blocks(section.markdown):
        if not heading_match(block) and not block.startswith(("![", "<h3")):
            return plain_text(block)
    return ""


def assign_pages(sections: list[Section], marker_toc: list[dict], find_page=None) -> None:
    """Record the PDF page each contents entry starts on.

    marker_toc is the "table_of_contents" list from marker's metadata JSON:
    every heading it found, in reading order, with its 0-based page_id.
    Falls back to marker's <span id="page-N-..."> anchors in the heading, then
    to find_page(text, start_page), which looks the section's first paragraph
    up in the PDF's text (needed for titles added here, like "Preface").
    """
    entries = [(_normalize(e.get("title", "")), e.get("page_id")) for e in marker_toc]
    pos, last_page = 0, -1
    for section in sections:
        if not section.in_toc:
            continue
        keys = {_normalize(k) for k in (section.label, section.title,
                                       f"{section.label or ''} {section.title or ''}") if k}
        keys.discard("")
        for j in range(pos, len(entries)):
            if entries[j][0] in keys and isinstance(entries[j][1], int):
                section.page_id = entries[j][1]
                pos = j + 1
                break
        if section.page_id is None:
            anchor = re.search(r'id="page-(\d+)-', section.heading_line)
            if anchor:
                section.page_id = int(anchor.group(1))
        if section.page_id is None and find_page:
            section.page_id = find_page(first_paragraph_text(section), max(last_page, 0))
        # Page numbers in a contents page never go backwards
        if section.page_id is not None:
            if section.page_id < last_page:
                section.page_id = None
            else:
                last_page = section.page_id
