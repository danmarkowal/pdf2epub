# PDF2EPUB 📚

Convert PDF files to nicely structured Markdown and EPUB format with intelligent layout detection.

## ✨ Features

- 📖 Smart layout detection for books and academic papers
- 🔍 Advanced text extraction and OCR capabilities
- 📊 Table detection and formatting
- 🖼️ Image extraction and optimization
- 📝 Clean markdown output with preserved structure
- 📱 EPUB generation with customizable styling
- 🌍 Multi-language support
- 🚀 GPU acceleration support (NVIDIA & AMD)
- 🍎 Apple Silicon support

## 🛠️ Dependencies

- Python 3.10–3.14 (3.13 recommended, see below)
- PyTorch (with CUDA/ROCm support for GPU acceleration)
- marker-pdf==1.10.2
- transformers==4.57.6
- markdown==3.10.2
- latex2mathml==3.81.0

### ⚠️ Python version

Python 3.13 is recommended.

`marker-pdf` constrains `Pillow<11.0.0`, and Pillow only ships Python 3.14
wheels from 11.3.0 onward. On Python 3.10–3.13 every dependency installs as a
prebuilt wheel. On 3.14, pip has to build Pillow from source instead: this
works, but it is slower and requires a working C toolchain plus the image
library headers Pillow links against. On Debian/Ubuntu install them first:

```bash
sudo apt install libjpeg-dev zlib1g-dev libtiff-dev libfreetype6-dev libwebp-dev
```

Without these headers the install fails with
`RequiredDependencyException: The headers or library files could not be found for jpeg`.

## 💻 Installation

1. Create and activate a virtual environment.

On Linux/Mac:
```bash
python3.13 -m venv .venv
source .venv/bin/activate
```

On Windows:
```powershell
py -3.13 -m venv .venv
.venv\Scripts\activate
```

2. Install Python dependencies (this installs PyTorch as well):
```bash
pip install -r requirements.txt
```

3. GPU acceleration (optional):

PyTorch is installed as a dependency in step 2. On Apple Silicon that wheel
already supports MPS, so no further action is needed. For a specific CUDA or
ROCm build, reinstall PyTorch using the selector at
[pytorch.org/get-started/locally](https://pytorch.org/get-started/locally/).
For example, for AMD GPUs with ROCm:
```bash
pip uninstall torch torchvision torchaudio
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/rocm6.2
```

4. Verify GPU support:
```python
import torch
print(torch.__version__)  # PyTorch version
print(torch.cuda.is_available())  # Should return True for NVIDIA
print(torch.backends.mps.is_available())  # Should return True for Apple Silicon
print(torch.version.hip)  # Should print ROCm version for AMD
```

### 🐳 Docker

A CPU-only image can be built from the included `Dockerfile`:

```bash
docker build -t pdf2epub .
```

Run it with your PDFs mounted at `/data` and a model cache volume (marker-pdf
downloads its models on first run):

```bash
docker run -it --rm \
  -v "$(pwd)":/data \
  -v pdf2epub-models:/models \
  pdf2epub input.pdf
```

`-it` is required for EPUB generation because metadata is prompted
interactively; with `--skip-epub` it can run non-interactively.

Tagged releases are also published to
`ghcr.io/overcuriousity/pdf2epub` by the Docker workflow.

## 🚀 Usage

### Basic Usage

Convert a single PDF file:
```bash
python main.py input.pdf
```

Convert all PDFs in a directory:
```bash
python main.py input_directory/
```

EPUB generation prompts interactively for metadata (title, author, language,
and so on; press Enter to accept each default). It therefore needs a terminal —
run it non-interactively and it will fail with `EOFError`. Use `--skip-epub` to
produce only markdown without any prompts.

Each chapter starts on a new page in the EPUB. The book's structure is read
from what its headings say rather than their size:

- **Volumes, parts and chapters**: labelled headings ("Volume II", "Part Two",
  "Chapter 3", "Twelve", "IV") form the hierarchy, and chapters are nested
  under the volume or part before them. A number and a title set as separate
  headings ("Chapter 1" / "In the Home of My Parents") are treated as one.
  Volume and part headings get a page of their own.
- **Other sections**: standard ones ("Preface", "Author's Introduction",
  "Epilogue", ...) are sections too. Notes that recur after chapters stay with
  their chapter as endnotes.
- Books without numbered chapters are split at headings that introduce a
  substantial amount of text. Headings that repeat the book's title never
  count, and sentences mistaken for headings become ordinary paragraphs.

Chapter and volume headings follow the PDF's alignment, font, weight and
relative size when these can be read from it; otherwise they are centred, with
the number in small capitals above a larger title.

The pages before the first chapter are laid out like a printed book:

- **Cover**: the PDF's first page, used whole. Text and pictures marker
  extracts from cover pages (OCR of the cover art, cropped pictures), duplicate
  covers and blank pages are left out. If the PDF has no cover picture, a plain
  typographic cover is made from the title and author.
- **Title page**: the title once, with the author and other credits
  ("Translated by ...") centred beneath it. Generated from the metadata if the
  book has none.
- **Copyright notice, dedication and epigraphs** (a quotation followed by a
  dash credit such as "—Lawrence Durrell"), each on a page of their own.
- **Contents**, then any untitled text before the first chapter, such as a
  preface. You are asked for its title, with a suggestion taken from the text
  ("Preface", "Foreword", ...), so it can be found in the contents.

At the back, untitled biographies of the people credited on the title page
become "About the Author" and "About the Translator" sections.

If the book has no contents page of its own, you are asked whether to add one
(the answers to these questions are remembered in `description.json`). It
lists each chapter's number on a small line above its title, chapters nested
under their volume or part, and the page each starts on in the source PDF
(using the PDF's own page labels when it has them).

Paragraphs and lists that the PDF splits across a page break are rejoined
automatically, and numbered lists keep their own numbering ("(1)", "2.").
Sign-offs ("James Murphy, Abbots Langley, February 1939") and datelines
("The Fortress, Landsberg a/L., October 16th, 1924") are set as signatures,
and chapter endnotes in smaller type. Where the PDF starts a new section with
only extra space instead of a heading, the EPUB marks the break with a short
rule, so the sections don't run together.
To apply this to markdown produced by an older version without re-running the
slow PDF conversion, rebuild the EPUB with `--skip-md`.

### Advanced Options

```bash
python main.py [input_path] [output_path] [options]

Options:
  --max-pages INT          Maximum number of pages to process
  --start-page INT         Page number to start from
  --skip-epub              Skip EPUB generation, only create markdown
  --skip-md                Skip markdown generation, use existing markdown files
  --reconvert              Convert the PDF again even if an earlier conversion
                           of it can be reused
```

Converting the PDF to markdown is by far the slowest step (marker's layout
model reads every page). If the same, unchanged PDF was converted before with
the same page options, that conversion is reused, so running the program
again on a book takes seconds. The cleanup and EPUB layout always run again.

If `input_path` is omitted, all PDFs in `./input/` are processed.

### Examples

Process a specific range of pages:
```bash
python main.py book.pdf --start-page 10 --max-pages 50
```

Convert to markdown only:
```bash
python main.py thesis.pdf --skip-epub
```

### Output Structure

```
output_directory/
├── document_name/
│   ├── document_name.md
│   ├── document_name.epub
│   ├── document_name_metadata.json
│   └── images/
│       ├── image1.png
│       ├── image2.jpg
│       └── ...
```

## 🤝 Contributing

Contributions are welcome! Here's how you can help:

1. Fork the repository
2. Create a new branch for your feature
3. Commit your changes
4. Push to your branch
5. Create a Pull Request

Please ensure your code follows the existing style and includes appropriate tests.

### Development Setup

1. Clone the repository:
```bash
git clone https://github.com/overcuriousity/pdf2epub.git
cd pdf2epub
```

2. Create a virtual environment:
```bash
python -m venv venv
source venv/bin/activate  # Linux/Mac
venv\Scripts\activate     # Windows
```

3. Install development dependencies:
```bash
pip install -r requirements.txt
```

## 📄 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## 🐛 Known Issues

- Some image embedding might need manual adjustment
- Some complex mathematical equations might not be perfectly converted
- Certain PDF layouts with multiple columns may require manual adjustment
- Font detection might be imperfect in some cases

## 🙏 Acknowledgments

This project builds upon several excellent open-source libraries:
- [marker-pdf](https://github.com/VikParuchuri/marker) for PDF processing
- [mark2epub](https://github.com/AlexPof/mark2epub) for markdown conversion
- [PyTorch](https://pytorch.org/) for GPU acceleration
- [Transformers](https://huggingface.co/transformers) for advanced text processing
