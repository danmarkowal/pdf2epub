"""Facts about the source PDF that the markdown doesn't carry.

Page labels and title/author metadata, the text of every page (to find which
page a passage is on), and which opening pages are the cover, duplicate
covers, blank pages or full-page illustrations. marker runs OCR on cover art
and crops pictures out of it, so text and images it extracted from those
pages are noise; the cover is used as a whole-page image instead.
"""
import ctypes
import io
from collections import Counter
from typing import Optional

import regex as re

from modules.chapters import plain_text
from modules.cleanup import heading_match, is_paragraph, split_blocks

# Only the opening pages without a text layer can be cover pages
FRONT_PAGES_EXAMINED = 8
MIN_TEXT_CHARS = 20  # pages with less text than this have no text layer
COVER_IMAGE_SHARE = 0.5  # share of the page an image must cover to be the cover
ILLUSTRATION_IMAGE_SHARE = 0.3
BLANK_IMAGE_SHARE = 0.02
DUPLICATE_THRESHOLD = 25  # mean grey-level difference between 16x16 thumbnails

# Never drop more than this from the start of the markdown, in case pages
# were attributed wrongly
MAX_DROPPED_BLOCKS = 80
MAX_DROPPED_WORDS = 600

# Unmarked section breaks: a gap between two paragraphs at least this many
# times the book's usual paragraph spacing, and at least this much bigger
SECTION_GAP_RATIO = 1.5
SECTION_GAP_EXTRA = 6.0  # points
# How far ahead (in PDF lines) to look for the next paragraph's first line
LINE_SEARCH_WINDOW = 400
# A line set entirely in bold or italics, like the lines of a title page,
# is display type rather than prose; spacing around it means nothing
DISPLAY_LINE = re.compile(r"^\s*(\*{1,3}|_{1,3})[^*_\n]+\1\s*$")

COVER_HEIGHT_PX = 1600

# Heading styles: a property is used when this share of samples agree
STYLE_AGREEMENT = 0.6
BODY_SAMPLE_PAGES = 12
SANS_HINTS = ("sans", "arial", "helvetica", "verdana", "gothic", "futura", "calibri", "segoe",
              "tahoma", "gill", "franklin", "avenir", "myriad", "frutiger", "univers")


def normalize_words(text: str) -> str:
    return " ".join(re.findall(r"\w+", text.lower()))


class PdfInfo:
    def __init__(self, pdf_path=None):
        self.path = pdf_path
        self.labels: list[str] = []  # the PDF's own page labels ("iv", "12"), if any
        self.metadata: dict = {}
        self.page_words: list[str] = []  # each page's text as " word word ... "
        self.cover_page: Optional[int] = None
        self.illustration_pages: list[int] = []
        # Opening pages whose extracted text and pictures are left out: the
        # cover, duplicate covers, blank pages and full-page illustrations
        self.skipped_pages: set[int] = set()
        # Every line of text in reading order: (page, words, top, bottom)
        self.lines: list = []
        if pdf_path:
            try:
                self._read()
            except Exception as e:
                print(f"Warning: could not analyse {pdf_path}: {e}")

    def _read(self):
        import pypdfium2 as pdfium

        doc = pdfium.PdfDocument(str(self.path))
        try:
            labels = [doc.get_page_label(i) for i in range(len(doc))]
            self.labels = labels if any(labels) else []
            self.metadata = doc.get_metadata_dict()
            texts = []
            for i in range(len(doc)):
                page = doc[i]
                textpage = page.get_textpage()
                texts.append(textpage.get_text_bounded())
                self.lines.extend((i, normalize_words(text).split(), top, bottom)
                                  for top, bottom, text in _page_lines(textpage))
                textpage.close()
                page.close()
            self.page_words = [f" {normalize_words(t)} " for t in texts]
            self._find_cover_pages(doc, [len(t.strip()) >= MIN_TEXT_CHARS for t in texts])
        finally:
            doc.close()

    def _find_cover_pages(self, doc, has_text: list[bool]):
        # In a scanned book every page is an image, so only the first page
        # can be told apart (as the cover)
        scanned = not any(has_text)
        cover_thumb = None
        for i in range(min(len(doc), FRONT_PAGES_EXAMINED)):
            if has_text[i]:
                break
            page = doc[i]
            share, thumb = _image_share(page)
            page.close()
            if share < BLANK_IMAGE_SHARE:
                self.skipped_pages.add(i)  # blank page
            elif self.cover_page is None:
                if share < COVER_IMAGE_SHARE:
                    break
                self.cover_page, cover_thumb = i, thumb
                self.skipped_pages.add(i)
                if scanned:
                    break
            elif _same_picture(thumb, cover_thumb):
                self.skipped_pages.add(i)  # the cover again
            elif share >= ILLUSTRATION_IMAGE_SHARE:
                self.illustration_pages.append(i)
                self.skipped_pages.add(i)
            else:
                break
        if self.cover_page is None:
            self.skipped_pages.clear()
            self.illustration_pages.clear()

    @property
    def trusted_metadata(self) -> dict:
        """Title and author from the PDF's metadata, only if they appear in
        the book's opening pages (PDF metadata is often wrong or left over
        from a template)."""
        opening = " ".join(self.page_words[:5])
        trusted = {}
        for key in ("Title", "Author"):
            value = (self.metadata.get(key) or "").strip()
            words = normalize_words(value)
            if words and f" {words} " in opening:
                trusted[key] = value
        return trusted

    def heading_styles(self, samples: dict) -> dict:
        """How the PDF sets each kind of heading.

        samples maps a role ("chapter-label", "chapter-title", "part-label",
        "part-title") to (page, text) pairs. For each role, returns the
        properties most samples agree on: align ("left", "center" or
        "right"), family (the font's name), generic ("serif", "sans-serif" or
        "monospace"), bold, italic and size (relative to the body text).
        """
        if not self.path or not any(samples.values()):
            return {}
        import pypdfium2 as pdfium

        try:
            doc = pdfium.PdfDocument(str(self.path))
        except Exception as e:
            print(f"Warning: could not read heading styles from {self.path}: {e}")
            return {}
        try:
            body = _body_metrics(doc)
            if not body:
                return {}
            styles = {}
            for role, items in samples.items():
                measured = [m for m in (_measure_text(doc, page, text, body) for page, text in items[:12]) if m]
                if not measured:
                    continue
                style = {}
                for key in ("align", "family", "generic", "bold", "italic"):
                    values = Counter(m[key] for m in measured)
                    value, count = values.most_common(1)[0]
                    if count >= STYLE_AGREEMENT * len(measured):
                        style[key] = value
                sizes = sorted(m["size"] for m in measured)
                style["size"] = round(min(max(sizes[len(sizes) // 2], 0.8), 3.0), 2)
                styles[role] = style
            return styles
        finally:
            doc.close()

    def render_page(self, index: int, height: int = COVER_HEIGHT_PX) -> tuple[bytes, tuple[int, int]]:
        """Render a page as a JPEG; returns the bytes and (width, height)."""
        import pypdfium2 as pdfium

        doc = pdfium.PdfDocument(str(self.path))
        try:
            page = doc[index]
            image = page.render(scale=height / page.get_size()[1]).to_pil().convert("RGB")
            page.close()
        finally:
            doc.close()
        buffer = io.BytesIO()
        image.save(buffer, "JPEG", quality=88, optimize=True)
        return buffer.getvalue(), image.size

    def mark_section_breaks(self, markdown_text: str) -> tuple[str, int]:
        """Mark section breaks that the PDF shows only as extra space.

        Books often start a new section without a heading, leaving a wider
        gap instead (after "...the joint work. / The Fortress, Landsberg am
        Lech." comes a separate dedication). That space is lost in an EPUB,
        and the sections run together, so a <hr class="section-break"/> is
        inserted. Only gaps between two paragraphs count, measured from the
        last line of the one to the first line of the next on the same page.
        Returns the new markdown and the number of breaks marked.
        """
        if not self.lines:
            return markdown_text, 0
        blocks = split_blocks(markdown_text)
        gaps = {}  # block index -> space above it, in points
        pos = 0
        for i, block in enumerate(blocks):
            if not is_paragraph(block) or DISPLAY_LINE.match(block):
                continue
            words = _block_words(block)
            if not words:
                continue
            start = words[:4]
            for j in range(pos, min(pos + LINE_SEARCH_WINDOW, len(self.lines))):
                if self.lines[j][1][:len(start)] == start:
                    pos = j + 1
                    previous = self.lines[j - 1] if j else None
                    prior = (_block_words(blocks[i - 1]) if i and is_paragraph(blocks[i - 1])
                             and not DISPLAY_LINE.match(blocks[i - 1]) else [])
                    # The line above must end the previous paragraph (not be,
                    # say, a running header) and be on the same page
                    if (previous and previous[0] == self.lines[j][0] and prior
                            and previous[1][-3:] == prior[-3:][-len(previous[1][-3:]):]):
                        gaps[i] = previous[3] - self.lines[j][2]
                    break
        usual = sorted(g for g in gaps.values() if g > 0)
        if len(usual) < 10:
            return markdown_text, 0
        usual = usual[len(usual) // 2]
        breaks = {i for i, gap in gaps.items()
                  if gap >= usual * SECTION_GAP_RATIO and gap - usual >= SECTION_GAP_EXTRA}
        if not breaks:
            return markdown_text, 0
        out = []
        for i, block in enumerate(blocks):
            if i in breaks:
                out.append('<hr class="section-break" />')
            out.append(block)
        return "\n\n".join(out), len(breaks)

    def find_page(self, text: str, start: int = 0, end: Optional[int] = None) -> Optional[int]:
        """First page (0-based) from `start` whose text contains the start of `text`."""
        words = normalize_words(text).split()[:8]
        if len(words) < 3:
            return None
        needle = f" {' '.join(words)} "
        end = len(self.page_words) if end is None else min(end, len(self.page_words))
        for i in range(max(start, 0), end):
            if needle in self.page_words[i]:
                return i
        return None

    def drop_skipped_pages(self, markdown_text: str, marker_toc: list[dict]) -> tuple[str, int]:
        """Remove the markdown marker extracted from the skipped opening pages.

        marker writes pages in order, so everything before the first block
        that can be placed on a later page came from the skipped pages.
        Blocks are placed by marker's image names (_page_3_...), page anchors,
        its heading list, or by finding their text in the PDF.
        Returns the new markdown and the number of blocks removed.
        """
        if not self.skipped_pages:
            return markdown_text, 0
        keep_from = next(p for p in range(len(self.page_words) + 1) if p not in self.skipped_pages)
        blocks = split_blocks(markdown_text)
        toc = [(normalize_words(e.get("title", "")), e.get("page_id")) for e in marker_toc]
        toc_pos, last_skipped, cut = 0, -1, None

        for i, block in enumerate(blocks[:MAX_DROPPED_BLOCKS]):
            page = None
            anchor = re.search(r"_page_(\d+)_|\bid=\"page-(\d+)-", block)
            if anchor:
                page = int(anchor.group(1) or anchor.group(2))
            heading = heading_match(block)
            if page is None and heading:
                key = normalize_words(plain_text(heading.group(2)))
                for j in range(toc_pos, len(toc)):
                    if toc[j][0] == key and isinstance(toc[j][1], int):
                        page, toc_pos = toc[j][1], j + 1
                        break
            if page is None:
                page = self.find_page(plain_text(block), keep_from, keep_from + 3)
            if page is None:
                continue
            if page in self.skipped_pages:
                last_skipped = i
            elif page >= keep_from:
                cut = i
                break

        if cut is None or sum(len(b.split()) for b in blocks[:cut]) > MAX_DROPPED_WORDS:
            cut = last_skipped + 1  # only what was placed on a skipped page
        if cut <= 0:
            return markdown_text, 0
        return "\n\n".join(blocks[cut:]), cut


def _image_share(page):
    """Share of the page covered by images, and a thumbnail of the largest."""
    import pypdfium2.raw as pdfium_c

    width, height = page.get_size()
    covered, largest, largest_area = 0.0, None, 0.0
    for obj in page.get_objects(filter=[pdfium_c.FPDF_PAGEOBJ_IMAGE], max_depth=2):
        left, bottom, right, top = obj.get_pos()
        area = max(0.0, min(right, width) - max(left, 0)) * max(0.0, min(top, height) - max(bottom, 0))
        covered += area
        if area > largest_area:
            largest, largest_area = obj, area
    thumb = None
    if largest is not None:
        try:
            thumb = largest.get_bitmap().to_pil().convert("L").resize((16, 16))
        except Exception:
            pass
    return min(covered / (width * height), 1.0), thumb


def _same_picture(a, b) -> bool:
    if a is None or b is None:
        return False
    from PIL import ImageChops, ImageStat

    return ImageStat.Stat(ImageChops.difference(a, b)).mean[0] < DUPLICATE_THRESHOLD


def _alnum_chars(textpage):
    for i in range(textpage.count_chars()):
        if textpage.get_text_range(i, 1).isalnum():
            yield i


def _char_height(textpage, index) -> float:
    left, bottom, right, top = textpage.get_charbox(index, loose=True)
    return round(top - bottom, 1)


def _body_metrics(doc) -> Optional[dict]:
    """Font size (as glyph box height) and text column of the body text."""
    n = len(doc)
    heights, lefts, rights = Counter(), [], []
    for index in sorted({int(n * k / BODY_SAMPLE_PAGES) for k in range(BODY_SAMPLE_PAGES)}):
        page = doc[index]
        textpage = page.get_textpage()
        chars = list(_alnum_chars(textpage))
        if len(chars) > 500:
            heights.update(_char_height(textpage, i) for i in chars[::5])
            boxes = [textpage.get_charbox(i) for i in chars]
            lefts.append(min(b[0] for b in boxes))
            rights.append(max(b[2] for b in boxes))
        textpage.close()
        page.close()
    if not heights or not lefts:
        return None
    lefts.sort()
    rights.sort()
    return {"size": heights.most_common(1)[0][0], "left": lefts[len(lefts) // 2], "right": rights[len(rights) // 2]}


def _font_info(textpage, index):
    import pypdfium2.raw as pdfium_c

    buffer = ctypes.create_string_buffer(256)
    flags = ctypes.c_int()
    pdfium_c.FPDFText_GetFontInfo(textpage.raw, index, buffer, 256, ctypes.byref(flags))
    return buffer.value.decode("utf-8", "replace"), pdfium_c.FPDFText_GetFontWeight(textpage.raw, index)


def font_family(font_name: str) -> str:
    """"ABCDEF+BookAntiqua-Bold" -> "Book Antiqua"."""
    name = re.sub(r"^[A-Z]{6}\+", "", font_name)
    name = re.split(r"[-,]", name)[0]
    name = re.sub(r"(?:PS)?(?:MT|Std|Pro|LT)$", "", name)
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name).strip()


def _measure_text(doc, page_index: int, text: str, body: dict) -> Optional[dict]:
    """Alignment, font and size of `text` on a page."""
    if page_index is None or not 0 <= page_index < len(doc) or not text:
        return None
    page = doc[page_index]
    textpage = page.get_textpage()
    try:
        found = None
        for query in (text, " ".join(text.split()[:3])):
            searcher = textpage.search(query, match_case=False)
            found = searcher.get_next()
            searcher.close()
            if found:
                break
        if not found:
            return None
        start, count = found
        chars = [i for i in range(start, start + count) if textpage.get_text_range(i, 1).isalnum()]
        if not chars:
            return None
        boxes = [textpage.get_charbox(i) for i in chars]
        left, right = min(b[0] for b in boxes), max(b[2] for b in boxes)
        heights = sorted(_char_height(textpage, i) for i in chars)
        name, weight = _font_info(textpage, chars[0])
    finally:
        textpage.close()
        page.close()

    column_left, column_right = body["left"], body["right"]
    width = column_right - column_left
    centre = (left + right) / 2
    if abs(centre - (column_left + column_right) / 2) < 0.04 * width and left - column_left > 0.08 * width:
        align = "center"
    elif abs(right - column_right) < 0.03 * width and left - column_left > 0.3 * width:
        align = "right"
    else:
        align = "left"
    lowered = name.lower()
    generic = "monospace" if ("mono" in lowered or "courier" in lowered) else \
        "sans-serif" if any(h in lowered for h in SANS_HINTS) else "serif"
    return {
        "align": align,
        "family": font_family(name) or None,
        "generic": generic,
        "bold": weight >= 600 or any(w in lowered for w in ("bold", "black", "heavy", "semibold", "demi")),
        "italic": "italic" in lowered or "oblique" in lowered,
        "size": heights[len(heights) // 2] / body["size"],
    }


def _block_words(block: str) -> list:
    """The words of a markdown block, without its markup."""
    return normalize_words(re.sub(r"<[^>]+>|!\[[^\]]*\]\([^)]*\)|\]\([^)]*\)", " ", block)).split()


def _page_lines(textpage) -> list:
    """The page's lines as (top, bottom, text), top to bottom. pdfium gives
    text in rectangles, several per line when the font changes."""
    lines = []  # [text, bottom, top]
    for index in range(textpage.count_rects()):
        left, bottom, right, top = textpage.get_rect(index)
        text = textpage.get_text_bounded(left, bottom, right, top).strip()
        for line in lines:
            if min(line[2], top) - max(line[1], bottom) > 0.5 * min(line[2] - line[1], top - bottom):
                line[0] += " " + text
                line[1], line[2] = min(line[1], bottom), max(line[2], top)
                break
        else:
            lines.append([text, bottom, top])
    return [(top, bottom, text) for text, bottom, top in sorted(lines, key=lambda line: -line[2])]
