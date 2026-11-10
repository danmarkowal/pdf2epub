import markdown
import os
from xml.dom import minidom
import zipfile
import sys
import json
from PIL import Image
import regex as re
from pathlib import Path
from datetime import datetime, timezone
import subprocess
import textwrap
from typing import Dict, Optional
from urllib.parse import quote
from xml.sax.saxutils import escape as xml_escape
import latex2mathml.converter
from modules.chapters import Section, assign_pages, first_paragraph_text, plain_text, split_into_chapters
from modules.cover import typographic_cover
from modules.frontmatter import add_lead_in, arrange_front_matter, split_off_bios, title_page
from modules.typography import format_signoff, refine_html
from modules.pdfinfo import PdfInfo
from modules.cleanup import repair_page_breaks, split_blocks

def get_user_input(prompt: str, default: str = "") -> str:
    """Get user input with a default value."""
    user_input = input(f"{prompt} [{default}]: ").strip()
    return user_input if user_input else default

def ask_yes_no(prompt: str, default: bool) -> bool:
    """Ask a yes/no question; Enter accepts the default."""
    hint = "Y/n" if default else "y/N"
    while True:
        answer = input(f"{prompt} [{hint}]: ").strip().lower()
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("Please enter 'y' or 'n'")

def get_metadata_from_user(existing_metadata: Optional[Dict] = None,
                           pdf_metadata: Optional[Dict] = None) -> Dict:
    """Interactively collect metadata from user with defaults from existing
    metadata, falling back to the title and author stored in the PDF."""
    if existing_metadata is None:
        existing_metadata = {}
    pdf_metadata = pdf_metadata or {}
    
    metadata = existing_metadata.get("metadata", {})
    
    print("\nPlease provide the following metadata for your EPUB (press Enter to use default value):")
    
    fields = {
        "dc:title": ("Title", metadata.get("dc:title", pdf_metadata.get("Title") or "Untitled Document")),
        "dc:creator": ("Author(s)", metadata.get("dc:creator", pdf_metadata.get("Author") or "Unknown Author")),
        "dc:identifier": ("Unique Identifier", metadata.get("dc:identifier", f"id-{datetime.now().strftime('%Y%m%d%H%M%S')}")),
        "dc:language": ("Language (e.g., en, de, fr)", metadata.get("dc:language", "en")),
        "dc:rights": ("Rights", metadata.get("dc:rights", "All rights reserved")),
        "dc:publisher": ("Publisher", metadata.get("dc:publisher", "PDF2EPUB")),
        "dc:date": ("Publication Date (YYYY-MM-DD)", metadata.get("dc:date", datetime.now().strftime("%Y-%m-%d")))
    }
    
    updated_metadata = {}
    for key, (prompt, default) in fields.items():
        value = get_user_input(prompt, default)
        updated_metadata[key] = value
        
    return {
        "metadata": updated_metadata,
        "default_css": existing_metadata.get("default_css", ["style.css"]),
        "chapters": existing_metadata.get("chapters", []),
        "cover_image": existing_metadata.get("cover_image", None),
        # Answer to "add a contents page?", reused as the default next time
        "contents_page": existing_metadata.get("contents_page"),
    }

def review_markdown(markdown_path: Path) -> tuple[bool, str]:
    """Ask user if they want to review the markdown file."""
    content = markdown_path.read_text(encoding='utf-8')
    
    while True:
        response = input("\nWould you like to review the markdown file before conversion? (y/n): ").lower()
        if response in ['y', 'yes']:
            try:
                if sys.platform == 'darwin':
                    subprocess.run(['open', str(markdown_path)], check=True)
                elif os.name == 'posix':
                    subprocess.run(['xdg-open', str(markdown_path)], check=True)
                else:
                    os.startfile(str(markdown_path))
                
                while True:
                    proceed = input("\nPress Enter when you're done editing (or 'q' to abort): ").lower()
                    if proceed == 'q':
                        return False, content
                    elif proceed == '':
                        updated_content = markdown_path.read_text(encoding='utf-8')
                        return True, updated_content
            except Exception as e:
                print(f"\nError opening markdown file: {e}")
                print("Proceeding with conversion...")
                return True, content
        elif response in ['n', 'no']:
            return True, content
        else:
            print("Please enter 'y' or 'n'")

def build_image_lookup(images_dir: Path) -> dict:
    """Build a {lowercase_name: actual_path} map for O(1) case-insensitive lookups."""
    lookup = {}
    try:
        for entry in images_dir.iterdir():
            lookup[entry.name.lower()] = entry
    except FileNotFoundError:
        pass
    return lookup

def process_markdown_for_images(markdown_text: str, work_dir: Path) -> tuple[str, list[str]]:
    """Process markdown content to find image references."""
    image_pattern = r'!\[(.*?)\]\((.*?)\)'
    images_found = []
    modified_text = markdown_text
    images_dir = work_dir / 'images'
    lookup = build_image_lookup(images_dir)

    for match in re.finditer(image_pattern, markdown_text):
        alt_text, image_path = match.groups()
        img_path = Path(image_path.strip())

        actual = lookup.get(img_path.name.lower())
        if actual is not None:
            images_found.append(actual.name)
            new_ref = f'![{alt_text}](images/{actual.name})'
            modified_text = modified_text.replace(match.group(0), new_ref)
        else:
            print(f"Warning: Image not found: {images_dir / img_path.name}")

    return modified_text, images_found

def copy_and_optimize_image(src_path: Path, dest_path: Path, max_dimension: int = 1800) -> None:
    """Copy image to destination path with optimization for EPUB."""
    try:
        with Image.open(src_path) as img:
            if img.mode == 'RGBA':
                img = img.convert('RGB')
                
            ratio = min(max_dimension / max(img.size[0], img.size[1]), 1.0)
            new_size = tuple(int(dim * ratio) for dim in img.size)
            
            if ratio < 1.0:
                img = img.resize(new_size, Image.Resampling.LANCZOS)
            
            if src_path.suffix.lower() in ['.jpg', '.jpeg']:
                img.save(dest_path, 'JPEG', quality=85, optimize=True)
            elif src_path.suffix.lower() == '.png':
                img.save(dest_path, 'PNG', optimize=True)
            else:
                dest_path = dest_path.with_suffix('.jpg')
                img.save(dest_path, 'JPEG', quality=85, optimize=True)
                
    except Exception as e:
        print(f"Error processing image {src_path}: {e}")
        raise

def update_package_manifest(doc: minidom.Document, image_filenames: list[str], 
                          manifest: minidom.Element) -> None:
    """
    Update package manifest with image items, ensuring proper media types.
    """
    for i, image_filename in enumerate(image_filenames):
        item = doc.createElement('item')
        item.setAttribute('id', f"image-{i:05d}")
        item.setAttribute('href', f"images/{image_filename}")
        
        # Set appropriate media type based on file extension
        ext = Path(image_filename).suffix.lower()
        if ext in ['.jpg', '.jpeg']:
            media_type = 'image/jpeg'
        elif ext == '.png':
            media_type = 'image/png'
        elif ext == '.gif':
            media_type = 'image/gif'
        else:
            print(f"Warning: Unsupported image type {ext} for {image_filename}")
            continue
            
        item.setAttribute('media-type', media_type)
        manifest.appendChild(item)
        
def get_all_filenames(the_dir, extensions=[]):
    if not os.path.exists(the_dir):
        return []
    all_files = [x for x in os.listdir(the_dir)]
    all_files = [x for x in all_files if x.split(".")[-1] in extensions]
    return all_files

def get_packageOPF_XML(spine_items, image_filenames=[], css_filenames=[], description_data=None,
                       cover_image=None):
    """Build package.opf.

    spine_items are dicts with id and href in reading order, plus optional
    properties ("svg" for pages drawn with SVG) and guide, a (type, title)
    pair for the EPUB 2 guide. cover_image is the cover picture's file name
    in images/, if there is one.
    """
    doc = minidom.Document()

    package = doc.createElement('package')
    package.setAttribute('xmlns',"http://www.idpf.org/2007/opf")
    package.setAttribute('version',"3.0")
    package.setAttribute('xml:lang',"en")
    package.setAttribute("unique-identifier","pub-id")

    ## Now building the metadata

    metadata = doc.createElement('metadata')
    metadata.setAttribute('xmlns:dc', 'http://purl.org/dc/elements/1.1/')

    for k,v in description_data["metadata"].items():
        if len(v):
            x = doc.createElement(k)
            for metadata_type,id_label in [("dc:title","title"),("dc:creator","creator"),("dc:identifier","pub-id")]:
                if k==metadata_type:
                    x.setAttribute('id',id_label)
            x.appendChild(doc.createTextNode(v))
            metadata.appendChild(x)

    # Required by EPUB 3: dcterms:modified timestamp
    modified_meta = doc.createElement('meta')
    modified_meta.setAttribute('property', 'dcterms:modified')
    modified_meta.appendChild(doc.createTextNode(datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")))
    metadata.appendChild(modified_meta)


    ## Now building the manifest

    manifest = doc.createElement('manifest')

    ## TOC.xhtml file for EPUB 3
    x = doc.createElement('item')
    x.setAttribute('id',"toc")
    x.setAttribute('properties',"nav")
    x.setAttribute('href',"TOC.xhtml")
    x.setAttribute('media-type',"application/xhtml+xml")
    manifest.appendChild(x)

    ## Ensure retrocompatibility by also providing a TOC.ncx file
    x = doc.createElement('item')
    x.setAttribute('id',"ncx")
    x.setAttribute('href',"toc.ncx")
    x.setAttribute('media-type',"application/x-dtbncx+xml")
    manifest.appendChild(x)

    for item in spine_items:
        x = doc.createElement('item')
        x.setAttribute('id', item["id"])
        x.setAttribute('href', item["href"])
        x.setAttribute('media-type', "application/xhtml+xml")
        if item.get("properties"):
            x.setAttribute('properties', item["properties"])
        manifest.appendChild(x)

    for i,image_filename in enumerate(image_filenames):
        x = doc.createElement('item')
        x.setAttribute('id',"image-{:05d}".format(i))
        x.setAttribute('href', "images/{}".format(quote(image_filename)))
        ext = Path(image_filename).suffix.lower()
        if ext == '.gif':
            x.setAttribute('media-type',"image/gif")
        elif ext in ['.jpg', '.jpeg']:
            x.setAttribute('media-type',"image/jpeg")
        elif ext == '.png':
            x.setAttribute('media-type',"image/png")
        if image_filename == cover_image:
            x.setAttribute('properties',"cover-image")

            ## Ensure compatibility by also providing a meta tag in the metadata
            y = doc.createElement('meta')
            y.setAttribute('name',"cover")
            y.setAttribute('content',"image-{:05d}".format(i))
            metadata.appendChild(y)
        manifest.appendChild(x)

    for i,css_filename in enumerate(css_filenames):
        x = doc.createElement('item')
        x.setAttribute('id',"css-{:05d}".format(i))
        x.setAttribute('href',"css/{}".format(css_filename))
        x.setAttribute('media-type',"text/css")
        manifest.appendChild(x)

    ## Now building the spine

    spine = doc.createElement('spine')
    spine.setAttribute('toc', "ncx")
    for item in spine_items:
        x = doc.createElement('itemref')
        x.setAttribute('idref', item["id"])
        x.setAttribute('linear', "yes")
        spine.appendChild(x)

    guide = doc.createElement('guide')
    for item in spine_items:
        if item.get("guide"):
            x = doc.createElement('reference')
            x.setAttribute('type', item["guide"][0])
            x.setAttribute('title', item["guide"][1])
            x.setAttribute('href', item["href"])
            guide.appendChild(x)

    package.appendChild(metadata)
    package.appendChild(manifest)
    package.appendChild(spine)
    if guide.hasChildNodes():
        package.appendChild(guide)
    doc.appendChild(package)

    return doc.toprettyxml()


def get_container_XML():
    container_data = """<?xml version="1.0" encoding="UTF-8" ?>\n"""
    container_data += """<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n"""
    container_data += """<rootfiles>\n"""
    container_data += """<rootfile full-path="OPS/package.opf" media-type="application/oebps-package+xml"/>\n"""
    container_data += """</rootfiles>\n</container>"""
    return container_data

def get_page_XML(title, body_html, css_filenames):
    """An XHTML page around ready-made body HTML (title page, epigraphs...)."""
    links = "\n".join(f'<link rel="stylesheet" href="css/{css}" type="text/css"/>' for css in css_filenames)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="en">
<head>
<meta http-equiv="default-style" content="text/html; charset=utf-8"/>
<title>{xml_escape(title)}</title>
{links}
</head>
<body>
{to_well_formed_xhtml(body_html)}
</body>
</html>"""

def get_image_page_XML(image_href, size, title, epub_type=""):
    """A page showing one picture scaled to fit the screen (the cover, or a
    full-page illustration). Uses SVG, which readers scale reliably."""
    width, height = size
    body_type = f' epub:type="{epub_type}"' if epub_type else ""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="en">
<head>
<meta http-equiv="default-style" content="text/html; charset=utf-8"/>
<title>{xml_escape(title)}</title>
<style type="text/css">html, body {{ margin: 0; padding: 0; height: 100%; }} svg {{ display: block; }}</style>
</head>
<body{body_type}>
<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" version="1.1" width="100%" height="100%" viewBox="0 0 {width} {height}" preserveAspectRatio="xMidYMid meet">
<image width="{width}" height="{height}" xlink:href="{xml_escape(image_href)}"/>
</svg>
</body>
</html>"""

def _toc_tree(toc_entries):
    """Nest entries by depth: chapters under their volume or part.
    Returns a list of (entry, children) pairs."""
    root = []
    stack = [(-1, root)]
    for entry in toc_entries:
        node = (entry, [])
        while stack[-1][0] >= entry["depth"]:
            stack.pop()
        stack[-1][1].append(node)
        stack.append((entry["depth"], node[1]))
    return root

# Stylesheet for the pages pdf2epub lays out itself: title page, chapter
# openings, contents and so on. It's written alongside style.css (which the
# user may replace) and linked from every page. Colours are left to the
# reader's theme.
BOOK_CSS_NAME = "pdf2epub.css"
BOOK_CSS = """/* pdf2epub: title page, chapter openings, contents and other book furniture */

/* Title page: everything centred, credits spaced below the title */
.titlepage { margin: 20% 0 0; text-align: center; }
.titlepage h1.book-title { font-family: serif; font-size: 2.1em; font-weight: normal; line-height: 1.25;
                           letter-spacing: 0.02em; text-align: center; margin: 0 0 0.4em; }
.titlepage p { text-align: center; text-indent: 0; margin: 0; }
.titlepage p.subtitle { font-style: italic; font-size: 1.15em; margin-top: 0.3em; }
.titlepage p.role { font-style: italic; font-size: 0.8em; margin-top: 2.5em; }
.titlepage p.author-role { margin-top: 3em; }
.titlepage p.name { font-variant: small-caps; letter-spacing: 0.08em; margin-top: 2.5em; }
.titlepage p.author-name { font-size: 1.3em; margin-top: 3em; }
.titlepage p.role + p.name { margin-top: 0.3em; }

/* Chapter and volume openings. Alignment, font and size follow the PDF
   when they can be read from it (see the rules after this stylesheet). */
.chapter-head { margin: 2.5em 0 2em; page-break-after: avoid; }
.chapter-head h2 { margin: 0; font-size: 1em; font-weight: normal; line-height: 1.3; }
.part-head { margin: 30% 0 0; }
.part-head h1 { margin: 0; font-size: 1em; font-weight: normal; line-height: 1.3; }
.chapter-label, .chapter-title, .part-label, .part-title { display: block; }
/* A heading's number and title, and a sign-off, always stay together */
.chapter-head, .part-head, .chapter-head h2, .part-head h1, .signoff {
    page-break-inside: avoid; break-inside: avoid; -webkit-column-break-inside: avoid; }
.chapter-label, .part-label { page-break-after: avoid; break-after: avoid; -webkit-column-break-after: avoid; }
.chapter-label { margin-bottom: 0.5em; }
.part-label { margin-bottom: 0.8em; }

.epigraphs { margin: 12% 8% 0 12%; }
blockquote.epigraph { margin: 0 0 3em; padding: 0; border: none; }
blockquote.epigraph p { font-style: italic; text-indent: 0; margin: 0 0 0.5em; line-height: 1.5; }
blockquote.epigraph p.attribution { font-style: normal; font-variant: small-caps; letter-spacing: 0.05em;
                                    text-align: right; margin-top: 0.8em; }

.dedication { text-align: center; font-style: italic; margin: 30% 10% 0; }
.dedication p { text-align: center; text-indent: 0; }
.copyright { font-size: 0.8em; text-align: center; margin: 40% 5% 0; }
.copyright p { text-align: center; text-indent: 0; margin: 0 0 0.6em; }

/* Sign-offs: the name on the right in small capitals, place and date in
   italics below */
p.signature, .signoff p.signoff-name { text-align: right; font-variant: small-caps; letter-spacing: 0.05em;
                                       text-indent: 0; margin: 1.5em 0 0; }
.signoff { margin-top: 2em; }
.signoff p.signoff-name { margin: 0 0 0.3em; }
.signoff p.signoff-place, .signoff p.signoff-date { font-style: italic; font-size: 0.9em; text-indent: 0;
                                                    text-align: left; margin: 0; }
.lead-in { font-variant: small-caps; letter-spacing: 0.04em; }

/* A new section without a heading, which the PDF shows only as extra space */
hr.section-break { border: none; border-top: 1px solid; width: 25%; margin: 2em auto; }

/* Lists that carry their own numbering, "(1)", "2.", "(a)": no bullets,
   numbers hanging in the margin */
ul.enumerated, ol.enumerated { list-style: none; padding-left: 0; margin-left: 2.2em; }
ul.enumerated > li, ol.enumerated > li { text-indent: -2.2em; }
.enum { display: inline-block; min-width: 1.9em; text-indent: 0; }
li { margin: 0.3em 0; }

/* Endnotes at the end of a chapter */
h3.notes-heading { font-size: 1em; font-weight: bold; text-align: left; margin: 3em 0 0.8em;
                   padding-top: 1em; border-top: 1px solid; }
p.note { font-size: 0.88em; line-height: 1.4; text-indent: 0; margin: 0 0 0.6em; }
.note-label { font-weight: bold; }

/* Contents: labels ("Chapter 1") on a small line above each title, dotted
   leaders to the page number, chapters indented under their volume */
.contents-page h1 { font-family: serif; text-align: center; font-weight: normal; font-size: 1.4em;
                    letter-spacing: 0.2em; text-transform: uppercase; margin: 1.5em 0 2em; }
ol.contents { list-style: none; margin: 0 auto; padding: 0; max-width: 30em; }
ol.contents li { margin: 0 0 0.75em; line-height: 1.3; text-indent: 0; }
/* Never split an entry's number and title across pages */
ol.contents li, ol.contents a { page-break-inside: avoid; break-inside: avoid;
                                -webkit-column-break-inside: avoid; orphans: 2; widows: 2; }
ol.contents li.depth-1 { padding-left: 1.5em; }
ol.contents li.depth-2 { padding-left: 3em; }
ol.contents li.part { margin-top: 1.8em; page-break-after: avoid; break-after: avoid;
                      -webkit-column-break-after: avoid; }
ol.contents li:first-child { margin-top: 0; }
ol.contents a { display: block; color: inherit; text-decoration: none; }
ol.contents .toc-label { display: block; font-size: 0.75em; font-variant: small-caps; letter-spacing: 0.12em;
                         margin-bottom: 0.1em; page-break-after: avoid; break-after: avoid;
                         -webkit-column-break-after: avoid; }
ol.contents li.part .toc-label, ol.contents li.part .title { font-weight: bold; }
/* Rows degrade to "Title  12" in readers without flexbox */
ol.contents .toc-line { display: flex; align-items: baseline; }
ol.contents .title { flex: 0 1 auto; }
ol.contents .leader { flex: 1 1 auto; min-width: 1.5em; margin: 0 0.4em; border-bottom: 1px dotted; opacity: 0.6; }
ol.contents .page { flex: none; font-variant-numeric: oldstyle-nums; }
"""

# Industry-standard chapter and volume headings, used when the PDF's own
# styling can't be read: centred, the number in spaced small capitals above
# a larger title
DEFAULT_HEADING_STYLES = {
    "chapter-label": {"font-size": "0.85em", "font-variant": "small-caps", "letter-spacing": "0.12em"},
    "chapter-title": {"font-size": "1.6em"},
    "part-label": {"font-size": "1em", "font-variant": "small-caps", "letter-spacing": "0.15em"},
    "part-title": {"font-size": "2em"},
}

def heading_css(styles: dict) -> str:
    """CSS for chapter and volume headings: the PDF's alignment, font,
    weight and size where it could be read (see PdfInfo.heading_styles),
    the industry-standard defaults otherwise."""
    rules = []
    for role, default in DEFAULT_HEADING_STYLES.items():
        style = styles.get(role)
        title_style = styles.get(role.replace("label", "title"), {})
        props = {"text-align": (style or title_style).get("align", "center"), "font-weight": "normal"}
        if style:
            family = re.sub(r"[\"';{}]", "", style.get("family") or "")
            generic = style.get("generic", "serif")
            props["font-family"] = f'"{family}", {generic}' if family else generic
            props["font-weight"] = "bold" if style.get("bold") else "normal"
            props["font-style"] = "italic" if style.get("italic") else "normal"
            props["font-size"] = f"{style['size']}em"
            props["font-variant"] = "normal"
            props["letter-spacing"] = "normal"
        else:
            props.update(default)
        body = "; ".join(f"{k}: {v}" for k, v in props.items())
        rules.append(f".{role} {{ {body}; }}")
        if role.endswith("title"):
            head = "chapter-head h2" if role.startswith("chapter") else "part-head h1"
            rules.append(f".{head} {{ text-align: {props['text-align']}; }}")
    return "\n/* Chapter and volume headings */\n" + "\n".join(rules) + "\n"

def section_header_html(section) -> str:
    """The opening heading of a chapter, volume or other section: its number
    ("Chapter 1") on a line above its title, or the title alone."""
    tag, prefix = ("h1", "part") if section.kind == "part" else ("h2", "chapter")
    label, title = section.label, section.title
    if label and title:
        inner = (f'<span class="{prefix}-label">{xml_escape(label)}</span> '
                 f'<span class="{prefix}-title">{xml_escape(title)}</span>')
    else:
        inner = f'<span class="{prefix}-title">{xml_escape(title or label or "")}</span>'
    anchors = "".join(section.anchors)
    return f'<div class="{prefix}-head">{anchors}<{tag}>{inner}</{tag}></div>\n'

def get_contents_page_XML(toc_entries, css_filenames, heading="Contents"):
    """Returns the XHTML for the visible contents page, laid out like a
    printed book's: centred heading, each number ("Chapter 1") on a small
    line above its title, dotted leaders, right-aligned page numbers, and
    chapters indented under their volume.

    toc_entries are dicts with href, label, title, depth, kind and page
    (label and page may be None). This is separate from the TOC.xhtml
    navigation document so that page numbers don't end up in the reader's
    own contents menu.
    """
    rows = []
    for entry in toc_entries:
        classes = f"depth-{min(entry['depth'], 2)}" + (" part" if entry.get("kind") == "part" else "")
        label, title = entry.get("label"), entry.get("title")
        label_html = f'<span class="toc-label">{xml_escape(label)}</span>' if label and title else ""
        page = ""
        if entry.get("page"):
            page = ' <span class="leader"></span> <span class="page">{}</span>'.format(xml_escape(entry["page"]))
        rows.append('<li class="{}"><a href="{}">{}<span class="toc-line"><span class="title">{}</span>{}</span></a></li>'.format(
            classes, entry["href"], label_html, xml_escape(title or label or ""), page))
    body = (f'<div class="contents-page">\n<h1>{xml_escape(heading)}</h1>\n'
            f'<ol class="contents">\n' + "\n".join(rows) + "\n</ol>\n</div>")
    return get_page_XML(heading, body, css_filenames)

def get_TOC_XML(default_css_filenames, toc_entries):
    ## Returns the XML data for the TOC.xhtml navigation document, which
    ## readers show as their contents menu. toc_entries are dicts with href,
    ## full (e.g. "Chapter 1: In the Home of My Parents") and depth.

    toc_xhtml = """<?xml version="1.0" encoding="UTF-8"?>\n"""
    toc_xhtml += """<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="en">\n"""
    toc_xhtml += """<head>\n<meta http-equiv="default-style" content="text/html; charset=utf-8"/>\n"""
    toc_xhtml += """<title>Contents</title>\n"""

    for css_filename in default_css_filenames:
        toc_xhtml += """<link rel="stylesheet" href="css/{}" type="text/css"/>\n""".format(css_filename)

    toc_xhtml += """</head>\n<body>\n"""

    def items(nodes):
        html = ""
        for entry, children in nodes:
            html += """<li><a href="{}">{}</a>""".format(entry["href"], xml_escape(entry["full"]))
            if children:
                html += "\n<ol>\n" + items(children) + "</ol>\n"
            html += "</li>\n"
        return html

    toc_xhtml += """<nav epub:type="toc" role="doc-toc" id="toc">\n<h2>Contents</h2>\n<ol>\n"""
    toc_xhtml += items(_toc_tree(toc_entries))
    toc_xhtml += """</ol>\n</nav>\n</body>\n</html>"""

    return toc_xhtml

def get_TOCNCX_XML(toc_entries, uid="", title=""):
    ## Returns the XML data for the TOC.ncx file (EPUB 2 readers); toc_entries
    ## are dicts as for get_TOC_XML

    tree = _toc_tree(toc_entries)
    play_order = 0

    def nav_point(entry, children=()):
        nonlocal play_order
        play_order += 1
        point = """<navPoint id="navpoint-{0}" playOrder="{0}">\n""".format(play_order)
        point += """<navLabel>\n<text>{}</text>\n</navLabel>""".format(xml_escape(entry["full"]))
        point += """<content src="{}"/>\n""".format(entry["href"])
        for child, grandchildren in children:
            point += nav_point(child, grandchildren)
        return point + """</navPoint>\n"""

    def depth(nodes):
        return 1 + max((depth(children) for _, children in nodes), default=0) if nodes else 0

    toc_ncx = """<?xml version="1.0" encoding="UTF-8"?>\n"""
    toc_ncx += """<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" xml:lang="en" version="2005-1">\n"""
    toc_ncx += """<head>\n"""
    toc_ncx += """<meta name="dtb:uid" content="{}"/>\n""".format(xml_escape(uid))
    toc_ncx += """<meta name="dtb:depth" content="{}"/>\n""".format(max(depth(tree), 1))
    toc_ncx += """<meta name="dtb:totalPageCount" content="0"/>\n"""
    toc_ncx += """<meta name="dtb:maxPageNumber" content="0"/>\n"""
    toc_ncx += """</head>\n"""
    toc_ncx += """<docTitle><text>{}</text></docTitle>\n""".format(xml_escape(title))
    toc_ncx += """<navMap>\n"""
    for entry, children in tree:
        toc_ncx += nav_point(entry, children)
    toc_ncx += """</navMap>\n</ncx>"""

    return toc_ncx

def convert_math_to_mathml(html_text: str) -> str:
    """Replace LaTeX math expressions in HTML with MathML, skipping code blocks."""
    # Mask <pre>/<code> blocks so their $ delimiters are never treated as math
    placeholders = {}
    counter = [0]

    def mask(m):
        key = f"\x00MASK{counter[0]}\x00"
        counter[0] += 1
        placeholders[key] = m.group(0)
        return key

    masked = re.sub(r'<pre[\s\S]*?</pre>|<code[\s\S]*?</code>', mask, html_text, flags=re.DOTALL)

    def try_convert(latex):
        try:
            return latex2mathml.converter.convert(latex)
        except Exception:
            return None

    # Standalone display math that Markdown wrapped in <p>: replace the whole
    # paragraph to avoid invalid XHTML like <p><div>...</div></p>
    def replace_display_paragraph(m):
        mathml = try_convert(m.group(1))
        return f'<div class="math-display">{mathml}</div>' if mathml else m.group(0)

    # Remaining $$...$$ (inside phrasing content): use <span> to stay valid
    def replace_display_inline(m):
        mathml = try_convert(m.group(1))
        return f'<span class="math-display">{mathml}</span>' if mathml else m.group(0)

    # Inline $...$
    def replace_inline(m):
        mathml = try_convert(m.group(1))
        return f'<span class="math-inline">{mathml}</span>' if mathml else m.group(0)

    masked = re.sub(r'<p>\s*\$\$(.*?)\$\$\s*</p>', replace_display_paragraph, masked, flags=re.DOTALL)
    masked = re.sub(r'\$\$(.*?)\$\$', replace_display_inline, masked, flags=re.DOTALL)
    masked = re.sub(r'(?<!\$)\$(?!\$)(.*?)(?<!\$)\$(?!\$)', replace_inline, masked, flags=re.DOTALL)

    for key, original in placeholders.items():
        masked = masked.replace(key, original)

    return masked

# Element names that are kept as markup when cleaning chapter HTML. Anything
# else that looks like a tag (e.g. "<brackets>" in the PDF's text) is escaped.
HTML_ELEMENTS = {
    "a", "abbr", "address", "area", "article", "aside", "audio", "b", "bdi",
    "bdo", "blockquote", "body", "br", "button", "caption", "cite", "code",
    "col", "colgroup", "data", "dd", "del", "details", "dfn", "div", "dl",
    "dt", "em", "figcaption", "figure", "footer", "h1", "h2", "h3", "h4",
    "h5", "h6", "header", "hr", "i", "img", "ins", "kbd", "label", "li",
    "main", "mark", "nav", "ol", "p", "picture", "pre", "q", "rp", "rt",
    "ruby", "s", "samp", "section", "small", "source", "span", "strong",
    "sub", "summary", "sup", "table", "tbody", "td", "tfoot", "th", "thead",
    "time", "tr", "u", "ul", "var", "video", "wbr",
}
MATHML_ELEMENTS = {
    "math", "annotation", "semantics", "menclose", "merror", "mfrac", "mi",
    "mlabeledtr", "mmultiscripts", "mn", "mo", "mover", "mpadded", "mphantom",
    "mprescripts", "mroot", "mrow", "ms", "mspace", "msqrt", "mstyle", "msub",
    "msubsup", "msup", "mtable", "mtd", "mtext", "mtr", "munder",
    "munderover", "none",
}
KNOWN_ELEMENTS = HTML_ELEMENTS | MATHML_ELEMENTS


def to_well_formed_xhtml(html_text: str) -> str:
    """Make HTML from Markdown safe to embed in an XHTML document.

    Python-Markdown passes raw HTML through untouched, so text like
    "<brackets>" or marker's own "<br>" would otherwise produce malformed XML.
    """
    from bs4 import BeautifulSoup

    def escape_unknown(m):
        if m.group(1).lower() in KNOWN_ELEMENTS:
            return m.group(0)
        return xml_escape(m.group(0))

    html_text = re.sub(r'</?([A-Za-z][\w:-]*)[^<>]*>', escape_unknown, html_text)
    # html.parser re-serializes void elements as <br/>, closes unclosed tags
    # and turns HTML-only entities such as &nbsp; into plain characters.
    return BeautifulSoup(html_text, "html.parser").decode(formatter="minimal")


def page_label(page_id: Optional[int], labels: list[str]) -> Optional[str]:
    """Page number to show for a 0-based PDF page: its label, else its number."""
    if page_id is None:
        return None
    if page_id < len(labels) and labels[page_id]:
        return labels[page_id]
    return str(page_id + 1)


# Headings that mark a book's own contents page, in common languages.
CONTENTS_TITLES = {
    "contents", "table of contents", "inhalt", "inhaltsverzeichnis",
    "table des matières", "sommaire", "índice", "indice", "sumário", "inhoud",
    "inhoudsopgave", "innehåll", "indhold", "innhold", "sisällys",
    "spis treści", "obsah", "tartalom", "содержание", "оглавление", "目录",
    "目次", "목차",
}


def has_contents_page(markdown_texts) -> bool:
    """Whether the book already has its own contents page."""
    for text in markdown_texts:
        for block in split_blocks(text):
            low = block.lower()
            if len(block) <= 80 and any(word in low for word in CONTENTS_TITLES):
                if plain_text(block.lstrip("#")).lower().strip(" .:") in CONTENTS_TITLES:
                    return True
    return False


def link_across_files(sections: list[dict]) -> None:
    """Point in-page links (href="#id") at the chapter file that holds the id.

    Footnote links and their targets often end up in different chapters once
    the book is split into one file per chapter.
    """
    owner = {}
    for section in sections:
        for anchor in re.findall(r'\bid="([^"]+)"', section["xhtml"]):
            owner.setdefault(anchor, section["href"])

    for section in sections:
        def retarget(m, here=section["href"]):
            target = owner.get(m.group(1))
            if target is None or target == here:
                return m.group(0)
            return f'href="{target}#{m.group(1)}"'

        section["xhtml"] = re.sub(r'href="#([^"]+)"', retarget, section["xhtml"])


def get_chapter_XML(work_dir: str, md_filename: str, css_filenames: list[str], content: Optional[str] = None,
                    title: Optional[str] = None, header_html: str = "") -> tuple[str, list[str]]:
    """
    Convert markdown chapter to XHTML and process images.
    Returns tuple of (XHTML content, list of images referenced in chapter)
    
    Args:
        work_dir: Working directory containing markdown files
        md_filename: Name of markdown file
        css_filenames: List of CSS files to include
        content: Optional pre-loaded markdown content. If None, content is read from file
        title: The page title
        header_html: The section's opening heading (see section_header_html)
    """
    work_dir_path = Path(work_dir)
    
    if content is None:
        with open(work_dir_path / md_filename, "r", encoding="utf-8") as f:
            markdown_data = f.read()
    else:
        markdown_data = content
    
    # Process markdown for images and get list of referenced images
    markdown_data, chapter_images = process_markdown_for_images(markdown_data, work_dir_path)
    
    # Convert to HTML
    html_text = markdown.markdown(
        markdown_data,
        extensions=["codehilite", "tables", "fenced_code", "footnotes"],
        extension_configs={"codehilite": {"guess_lang": False}}
    )

    # Convert LaTeX math to MathML for EPUB readers
    html_text = convert_math_to_mathml(html_text)
    html_text = refine_html(to_well_formed_xhtml(header_html + html_text))

    # Generate XHTML wrapper
    xhtml = f"""<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xmlns:m="http://www.w3.org/1998/Math/MathML" lang="en">
<head>
    <meta http-equiv="default-style" content="text/html; charset=utf-8"/>
    <title>{xml_escape(title or Path(md_filename).stem)}</title>
    {''.join(f'<link rel="stylesheet" href="css/{css}" type="text/css" media="all"/>' for css in css_filenames)}
</head>
<body>
{html_text}
</body>
</html>"""

    return xhtml, chapter_images



def convert_to_epub(markdown_dir: Path, output_path: Path, pdf_path: Optional[Path] = None) -> None:
    """
    Convert markdown files and images to EPUB format. The source PDF, if
    given, supplies the cover, page numbers and default title/author.
    """
    if not markdown_dir.exists():
        raise FileNotFoundError(f"Markdown directory not found: {markdown_dir}")

    if not list(markdown_dir.glob('*.md')):
        raise ValueError(f"No markdown files found in: {markdown_dir}")

    # Generate EPUB file
    epub_path = markdown_dir / f"{markdown_dir.name}.epub"
    main([str(markdown_dir), str(epub_path)] + ([str(pdf_path)] if pdf_path else []))

def load_marker_toc(work_dir, md_filename) -> list:
    """marker's list of the headings it found, with the PDF page of each."""
    meta_path = Path(work_dir) / f"{Path(md_filename).stem}_metadata.json"
    if not meta_path.exists():
        return []
    try:
        return json.loads(meta_path.read_text(encoding="utf-8")).get("table_of_contents", [])
    except (OSError, ValueError) as e:
        print(f"Warning: could not read page numbers from {meta_path.name}: {e}")
        return []

def save_description(description_path, json_data):
    with open(description_path, 'w', encoding='utf-8') as f:
        json.dump(json_data, f, indent=2)

def main(args):
    if len(args) < 2:
        print("\nUsage:\n    python md2epub.py <markdown_directory> <output_file.epub> [source.pdf]")
        exit(1)

    work_dir = args[0]
    output_path = args[1]
    pdf = PdfInfo(args[2] if len(args) > 2 else None)

    images_dir = Path(work_dir) / 'images'
    css_dir = os.path.join(work_dir, 'css/')

    try:
        # Reading/Creating the JSON file containing the description of the eBook
        description_path = os.path.join(work_dir, "description.json")
        existing_metadata = {}

        if os.path.exists(description_path):
            with open(description_path, 'r', encoding='utf-8') as f:
                existing_metadata = json.load(f)

        # Get metadata from user
        json_data = get_metadata_from_user(existing_metadata, pdf.trusted_metadata)

        # Find all markdown files if not already in metadata
        if not json_data["chapters"]:
            markdown_files = [f for f in os.listdir(work_dir) if f.endswith('.md')]
            for md_file in sorted(markdown_files):
                json_data["chapters"].append({
                    "markdown": md_file,
                    "css": ""
                })
        save_description(description_path, json_data)

        # Review markdown files and store updated content
        chapter_contents = {}
        for chapter in json_data["chapters"]:
            md_path = Path(work_dir) / chapter["markdown"]
            should_continue, content = review_markdown(md_path)
            if not should_continue:
                print("\nConversion aborted by user.")
                return
            # Rejoin paragraphs split at page breaks (also fixes markdown made
            # by older versions; text with nothing to fix is left unchanged)
            content, joins = repair_page_breaks(content)
            if joins:
                print(f"Rejoined {joins} paragraphs split across pages in {chapter['markdown']}")
            chapter_contents[chapter["markdown"]] = content

        # Get title and author
        title = json_data["metadata"].get("dc:title", "Untitled Document")
        authors = json_data["metadata"].get("dc:creator", None)
        if authors == "Unknown Author":
            authors = None

        # Compile list of files
        all_css_filenames = json_data["default_css"][:]
        for chapter in json_data["chapters"]:
            if len(chapter["css"]) and (chapter["css"] not in all_css_filenames):
                all_css_filenames.append(chapter["css"])
        all_css_filenames.append(BOOK_CSS_NAME)
        page_css = json_data["default_css"] + [BOOK_CSS_NAME]

        # Lay out each markdown file: leave out what marker extracted from the
        # cover, split into chapters, arrange the front matter (title page,
        # epigraphs, preface) and give author biographies their own sections
        print("\nLaying out the book...")
        files = json_data["chapters"]
        marker_tocs = [load_marker_toc(work_dir, chapter["markdown"]) for chapter in files]
        parts = []  # (Section, css files, index of its markdown file)
        people = []
        for n, chapter in enumerate(files):
            css_files = json_data["default_css"] + ([chapter["css"]] if chapter["css"] else []) + [BOOK_CSS_NAME]
            content = chapter_contents[chapter["markdown"]]
            if n == 0:
                content, dropped = pdf.drop_skipped_pages(content, marker_tocs[n])
                if dropped:
                    print(f"Left out {dropped} blocks of text and pictures extracted from the cover")
                content, breaks = pdf.mark_section_breaks(content)
                if breaks:
                    print(f"Marked {breaks} section break{'s' if breaks > 1 else ''} that the PDF shows only as extra space")
                file_parts = split_into_chapters(content, title)
                file_parts, people = arrange_front_matter(file_parts, title, authors)
            else:
                file_parts = split_into_chapters(content, title)
            if n == len(files) - 1:
                if authors and not any(name == authors for name, _ in people):
                    people.append((authors, "author"))
                file_parts = split_off_bios(file_parts, people)
            parts.extend((part, css_files, n) for part in file_parts)

        # A title page, generated from the metadata if the book has none, with
        # any full-page illustrations from the PDF's opening pages before it
        if not any(part.kind == "titlepage" for part, _, _ in parts):
            credits = [("by", "author", authors)] if authors else []
            parts.insert(0, (title_page(title, None, credits), page_css, 0))
        title_index = next(i for i, (part, _, _) in enumerate(parts) if part.kind == "titlepage")
        rendered_images = {}  # file name in images/ -> (JPEG bytes, size)
        for page_index in reversed(pdf.illustration_pages):
            name = f"page-{page_index + 1}.jpg"
            rendered_images[name] = pdf.render_page(page_index)
            parts.insert(title_index, (Section(kind="image", html=name), page_css, 0))

        # Untitled text before the first chapter (a preface, say) gets a
        # title so readers can find it; the answer is remembered
        saved_titles = json_data.get("section_titles", {})
        for part, _, _ in parts:
            if part.suggested_title:
                opening = first_paragraph_text(part)
                key = " ".join(opening.split()[:8])
                preview = textwrap.shorten(opening, 70, placeholder=" …")
                part.title = get_user_input(
                    f'\nThe text before the first chapter has no title. It begins:\n  "{preview}"\nTitle for it',
                    saved_titles.get(key, part.suggested_title),
                )
                part.in_toc = True
                saved_titles[key] = part.title
        if saved_titles:
            json_data["section_titles"] = saved_titles
            save_description(description_path, json_data)

        # Small capitals for capitalised opening words, and sign-offs
        # ("James Murphy, Abbots Langley, February 1939") set as signatures
        for part, _, _ in parts:
            if part.html is None:
                part.markdown = format_signoff(add_lead_in(part.markdown), people)

        for n in range(len(files)):
            assign_pages([part for part, _, i in parts if i == n], marker_tocs[n],
                         pdf.find_page if pdf.page_words else None)

        # Chapter and volume headings follow the PDF's styling where it can be read
        samples = {"chapter-label": [], "chapter-title": [], "part-label": [], "part-title": []}
        for part, _, _ in parts:
            if part.page_id is None or part.kind not in ("chapter", "part"):
                continue
            prefix = "part" if part.kind == "part" else "chapter"
            if part.label and part.title:
                samples[f"{prefix}-label"].append((part.page_id, part.label))
                samples[f"{prefix}-title"].append((part.page_id, part.title))
            else:
                samples[f"{prefix}-title"].append((part.page_id, part.title or part.label))
        heading_styles = pdf.heading_styles(samples)
        style = heading_styles.get("chapter-title")
        if style:
            look = ", ".join(x for x in (
                {"center": "centred", "left": "left-aligned", "right": "right-aligned"}.get(style.get("align"), ""),
                style.get("family") or "", "bold" if style.get("bold") else "", "italic" if style.get("italic") else "",
                f"{style['size']:g}× the body text size") if x)
            print(f"Chapter headings follow the PDF: {look}")

        # Turn every part into an XHTML file and collect image references
        print("Processing chapters and collecting image references...")
        sections = []
        all_referenced_images = set()
        for part, css_files, _ in parts:
            page_title = part.full_title or title
            if part.kind == "image":
                xhtml = get_image_page_XML(f"images/{part.html}", rendered_images[part.html][1], page_title)
                chapter_images = []
            elif part.html is not None:
                xhtml, chapter_images = get_page_XML(page_title, part.html, css_files), []
            else:
                xhtml, chapter_images = get_chapter_XML(
                    work_dir, "", css_files, content=part.markdown, title=page_title,
                    header_html=section_header_html(part) if part.in_toc else "",
                )
            n = len(sections)
            sections.append({
                "id": f"s{n:05d}",
                "href": f"s{n:05d}.xhtml",
                "title": part.title,
                "label": part.label,
                "full": part.full_title,
                "kind": part.kind,
                "in_toc": part.in_toc,
                "depth": part.depth,
                "page": page_label(part.page_id, pdf.labels),
                "xhtml": xhtml,
            })
            all_referenced_images.update(chapter_images)
        link_across_files(sections)

        toc_entries = [
            {key: s[key] for key in ("href", "label", "title", "full", "depth", "kind", "page")}
            for s in sections if s["in_toc"]
        ]
        if toc_entries:
            print(f"Found {len(toc_entries)} chapters and sections: "
                  f"{toc_entries[0]['full']} … {toc_entries[-1]['full']}")
        else:
            print("No chapter headings found; the book will be a single section")
            toc_entries = [{"href": sections[0]["href"], "label": None, "title": title, "full": title,
                            "depth": 0, "kind": "chapter", "page": None}]

        # Offer a contents page for books that don't have one. Readers show
        # the navigation menu either way; this adds a visible page too.
        contents_in_spine = False
        if len(toc_entries) >= 2 and not has_contents_page(chapter_contents.values()):
            saved = json_data.get("contents_page")
            contents_in_spine = ask_yes_no(
                "\nThis book has no contents page. Add one before the first chapter?",
                True if saved is None else saved,
            )
            json_data["contents_page"] = contents_in_spine
            save_description(description_path, json_data)

        # The cover: an image named in description.json, else the PDF's
        # first page rendered whole
        cover_name, cover_size = None, None
        if json_data.get("cover_image") and (images_dir / json_data["cover_image"]).exists():
            cover_name = json_data["cover_image"]
            with Image.open(images_dir / cover_name) as cover:
                cover_size = cover.size
            all_referenced_images.add(cover_name)
        elif pdf.cover_page is not None:
            cover_name = "cover.jpg"
            rendered_images[cover_name] = pdf.render_page(pdf.cover_page)
            cover_size = rendered_images[cover_name][1]
        else:
            # No cover picture in the PDF: a plain typographic one, so the book
            # has a cover in reading apps' libraries
            cover_name = "cover.jpg"
            rendered_images[cover_name] = typographic_cover(title, authors)
            cover_size = rendered_images[cover_name][1]
            print("The PDF has no cover picture, so a typographic cover was made")

        # Reading order: cover, front matter, contents, then the chapters
        spine_items = []
        if cover_name:
            spine_items.append({"id": "cover", "href": "cover.xhtml", "properties": "svg",
                                "guide": ("cover", "Cover")})
        first_toc = next((i for i, s in enumerate(sections) if s["in_toc"]), None)
        for i, section in enumerate(sections):
            if contents_in_spine and i == first_toc:
                spine_items.append({"id": "contents", "href": "contents.xhtml", "guide": ("toc", "Contents")})
            item = {"id": section["id"], "href": section["href"]}
            if section["kind"] == "image":
                item["properties"] = "svg"
            elif section["kind"] == "titlepage":
                item["guide"] = ("title-page", "Title Page")
            elif i == first_toc:
                item["guide"] = ("text", "Beginning")
            spine_items.append(item)

        # Process and optimize images
        print("\nProcessing and optimizing images...")
        epub_images_dir = Path(work_dir) / 'epub_images'
        processed_images = {}  # Store processed image data
        if images_dir.exists() and all_referenced_images:
            epub_images_dir.mkdir(exist_ok=True)

            for image in all_referenced_images:
                src_path = images_dir / image
                if src_path.exists():
                    try:
                        dest_path = epub_images_dir / image
                        copy_and_optimize_image(src_path, dest_path)

                        # Store processed image data
                        with open(dest_path, "rb") as f:
                            processed_images[image] = f.read()
                    except Exception as e:
                        print(f"Warning: Failed to process image {image}: {e}")
                else:
                    print(f"Warning: Referenced image not found: {src_path}")

            # Cleanup temporary directory
            import shutil
            shutil.rmtree(epub_images_dir, ignore_errors=True)
        for name, (data, _) in rendered_images.items():
            processed_images[name] = data

        # Now create the EPUB file with all prepared content
        print("\nCreating EPUB file...")
        with zipfile.ZipFile(output_path, "w") as epub:
            # Write mimetype (must be first and uncompressed)
            epub.writestr("mimetype", "application/epub+zip")

            # Write container.xml
            epub.writestr("META-INF/container.xml", get_container_XML(), zipfile.ZIP_DEFLATED)

            # Write package.opf
            epub.writestr("OPS/package.opf",
                get_packageOPF_XML(
                    spine_items,
                    image_filenames=sorted(processed_images),
                    css_filenames=all_css_filenames,
                    description_data=json_data,
                    cover_image=cover_name,
                ),
                zipfile.ZIP_DEFLATED
            )

            if cover_name:
                epub.writestr("OPS/cover.xhtml",
                    get_image_page_XML(f"images/{cover_name}", cover_size, "Cover", "cover").encode('utf-8'),
                    zipfile.ZIP_DEFLATED
                )

            if contents_in_spine:
                epub.writestr("OPS/contents.xhtml",
                    get_contents_page_XML(toc_entries, page_css).encode('utf-8'),
                    zipfile.ZIP_DEFLATED
                )

            # Write processed chapters
            print(f"Writing {len(sections)} chapter files...")
            for section in sections:
                epub.writestr(
                    f"OPS/{section['href']}",
                    section["xhtml"].encode('utf-8'),
                    zipfile.ZIP_DEFLATED
                )

            # Write processed images (only those the book uses)
            if processed_images:
                print(f"Writing {len(processed_images)} images...")
                for image_name, image_data in processed_images.items():
                    epub.writestr(f"OPS/images/{image_name}", image_data, zipfile.ZIP_DEFLATED)

            # Write TOC files
            print("Writing table of contents...")
            epub.writestr("OPS/TOC.xhtml",
                get_TOC_XML(json_data["default_css"], toc_entries),
                zipfile.ZIP_DEFLATED
            )

            epub.writestr("OPS/toc.ncx",
                get_TOCNCX_XML(
                    toc_entries,
                    uid=json_data["metadata"].get("dc:identifier", ""),
                    title=json_data["metadata"].get("dc:title", "")
                ),
                zipfile.ZIP_DEFLATED
            )

            # Copy CSS files; write a default style.css for any that are missing
            default_css_content = b"""body { font-family: serif; line-height: 1.5; margin: 5%; }
h1, h2, h3, h4, h5, h6 { font-family: serif; }
img { max-width: 100%; height: auto; }
pre, code { font-family: monospace; font-size: 0.9em; }
"""
            print(f"Writing {len(all_css_filenames)} CSS files...")
            for css in all_css_filenames:
                css_path = os.path.join(css_dir, css)
                if css == BOOK_CSS_NAME:
                    epub.writestr(f"OPS/css/{css}", (BOOK_CSS + heading_css(heading_styles)).encode('utf-8'),
                                  zipfile.ZIP_DEFLATED)
                elif os.path.exists(css_path):
                    with open(css_path, "rb") as f:
                        epub.writestr(f"OPS/css/{css}", f.read(), zipfile.ZIP_DEFLATED)
                else:
                    epub.writestr(f"OPS/css/{css}", default_css_content, zipfile.ZIP_DEFLATED)

        print(f"\nEPUB creation complete: {output_path}")

    except Exception:
        import traceback
        print(f"Error processing {work_dir}:")
        print(traceback.format_exc())
        raise

if __name__ == "__main__":
    main(sys.argv[1:])
