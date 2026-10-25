"""Kokoro Voice Studio - a native Windows app that turns documents into MP3 narration, offline.

GUI:   python kokoro_app.py
CLI:   python kokoro_app.py --convert input.txt output.mp3 [--voice af_heart] [--speed 1.0]
       python kokoro_app.py --download-models      fetch the voice model into models/
       python kokoro_app.py --prep in.md out.md    clean a PDF-derived Markdown file for listening
       python kokoro_app.py --player audio.mp3     open the synced player
       python kokoro_app.py --selftest FILE        drive the real window handlers and report

Everything the app creates lives in one folder (the folder of this script or of the .exe):
    models/   the voice model, downloaded on first run
    data/     settings.json, library.json, error.log
    output/   the MP3, SRT and JSON files you make
"""

import bisect
import codecs
import json
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
import traceback
from html.parser import HTMLParser

import customtkinter as ctk
try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    _DnD = TkinterDnD.DnDWrapper
except ImportError:  # dropping files onto the window is optional; Browse still works
    DND_FILES, TkinterDnD, _DnD = None, None, object

if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))



def _find_app_home():
    """The folder the app keeps its files in: its own folder, or a per-user one if that is read-only."""
    probe = os.path.join(BASE_DIR, ".kokoro_write_test")
    try:
        with open(probe, "w"):
            pass
        os.remove(probe)
        return BASE_DIR
    except OSError:
        return os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "KokoroStudio")


APP_HOME = _find_app_home()
MODELS_DIR = os.path.join(APP_HOME, "models")
DATA_DIR = os.path.join(APP_HOME, "data")
OUTPUT_DIR = os.path.join(APP_HOME, "output")
LOG_PATH = os.path.join(DATA_DIR, "error.log")

MODEL_NAME, VOICES_NAME = "kokoro-v1.0.fp16.onnx", "voices-v1.0.bin"
# Official files from the kokoro-onnx "model-files-v1.1" release; size and SHA-256 are checked after download.
_RELEASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.1/"
MODEL_FILES = {
    MODEL_NAME: {"url": _RELEASE + MODEL_NAME, "size": 163527961,
                 "sha256": "f3a290d384fbb27966d462905c71a46cef9e5fd00516b40df32a0b4afe77ac96"},
    VOICES_NAME: {"url": _RELEASE + VOICES_NAME, "size": 28214398,
                  "sha256": "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d"},
}


# The user's own model and voices files (chosen in Settings, or with --model / --voices). They win over the default.
CUSTOM_MODEL = {"model": "", "voices": ""}


def using_custom_model():
    return bool(CUSTOM_MODEL["model"] and CUSTOM_MODEL["voices"]
                and os.path.isfile(CUSTOM_MODEL["model"]) and os.path.isfile(CUSTOM_MODEL["voices"]))


def model_paths():
    """(model file, voices file): the user's own pair if set, else the default pair, or None when missing."""
    if using_custom_model():
        return CUSTOM_MODEL["model"], CUSTOM_MODEL["voices"]
    for folder in (os.path.join(BASE_DIR, "models"), BASE_DIR, MODELS_DIR):
        model, voices = os.path.join(folder, MODEL_NAME), os.path.join(folder, VOICES_NAME)
        if os.path.isfile(model) and os.path.isfile(voices):
            return model, voices
    return None



LANG_BY_PREFIX = {
    "a": "en-us", "b": "en-gb", "e": "es", "f": "fr-fr", "h": "hi",
    "i": "it", "j": "ja", "p": "pt-br", "z": "zh",
}
PREVIEW_BY_PREFIX = {
    "a": "Hello, this is a preview of my voice.",
    "b": "Hello, this is a preview of my voice.",
    "e": "Hola, esta es una muestra de mi voz.",
    "f": "Bonjour, ceci est un aperçu de ma voix.",
    "h": "नमस्ते, यह मेरी आवाज़ का नमूना है।",
    "i": "Ciao, questa è un'anteprima della mia voce.",
    "j": "こんにちは、これは私の声のサンプルです。",
    "p": "Olá, esta é uma prévia da minha voz.",
    "z": "你好，这是我的声音示例。",
}
MAX_CHUNK = 400
PAUSE_SECONDS = 0.4
# What to do with parts of a document that are not plain prose. The defaults are the
# command-line behaviour; the window changes these from its settings.
TEXT_OPTIONS = {"tables": "caption", "figures": "caption", "code": "say", "links": "say"}
MARKDOWN_EXTS = (".md", ".markdown")
HTML_EXTS = (".html", ".htm")
SENTENCE_END = ".!?:;,…"


def log_error(text):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")
    except OSError:
        pass


# --------------------------------------------------------------------------
# Reading and cleaning text
# --------------------------------------------------------------------------

def read_text_file(path):
    with open(path, "rb") as fh:
        raw = fh.read()
    if raw.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        text = raw.decode("utf-32", errors="replace")
    elif raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        text = raw.decode("utf-16", errors="replace")
    elif b"\x00" in raw[:8192]:
        raise ValueError("This doesn't look like a text file.")
    else:
        for enc in ("utf-8-sig", "cp1252"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            text = raw.decode("utf-8", errors="replace")
    sample = text[:20000]
    if sample:
        odd = sum(1 for c in sample if c == "�" or (ord(c) < 32 and c not in "\t\n\r\f"))
        if odd / len(sample) > 0.05:
            raise ValueError("This doesn't look like a text file.")
    return text.replace("\r\n", "\n").replace("\r", "\n")


_SPACES = re.compile("[   -   　]")
_INVISIBLE = re.compile("[­​-‏‪-‮⁠⁦-⁩﻿]")
_CONTROL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_EMOJI = re.compile("[\U0001F000-\U0001FAFF←-⇿⌀-⏿☀-➿⬀-⯿︎️]")
_URL = re.compile(r"(?:https?://|ftp://|www\.)[^\s<>\"]+", re.I)
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
_TRAILING = ".,;:!?)]}'\""
_QUOTES = {"‘": "'", "’": "'", "‚": "'", "‛": "'",
           "“": '"', "”": '"', "„": '"', "…": "..."}


def _link(match):
    token = match.group(0)
    stripped = token.rstrip(_TRAILING)
    return ("link" if TEXT_OPTIONS["links"] == "say" else "") + token[len(stripped):]


def _email(match):
    return "link" if TEXT_OPTIONS["links"] == "say" else ""


_LESS_THAN = re.compile(r"(?<=\s)<(?=\s|\d|-)")
_GREATER_THAN = re.compile(r"(?<=\s)>(?=\s|\d|-)")
_PRIVATE_USE = re.compile("[-]")


def normalize_chars(text):
    import unicodedata
    text = _MATH_ALNUM.sub(lambda m: unicodedata.normalize("NFKC", m.group()), text)
    text = _PRIVATE_USE.sub("", text)
    text = _LESS_THAN.sub("less than", text)
    text = _GREATER_THAN.sub("greater than", text)
    text = _INVISIBLE.sub("", text)
    text = _SPACES.sub(" ", text)
    text = _CONTROL.sub("", text)
    text = _EMOJI.sub("", text)
    for src, dst in _QUOTES.items():
        text = text.replace(src, dst)
    text = _URL.sub(_link, text)
    text = _EMAIL.sub(_email, text)
    return text


def ensure_period(line):
    line = line.rstrip()
    if line and line[-1] not in SENTENCE_END:
        line += "."
    return line


_BULLET = re.compile(r"^\s*(?:[-*+•]|(\d+[.)]))\s+(.*)$")
_HEADING = re.compile(r"^\s*#{1,6}\s+(.*?)\s*#*\s*$")
_BOLD = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1")
_STAR_EM = re.compile(r"(?<![\w*])\*(?=\S)(.+?)(?<=\S)\*(?![\w*])")
_UNDER_EM = re.compile(r"(?<![\w_])_(?=\S)(.+?)(?<=\S)_(?![\w_])")
_LONE_STAR = re.compile(r"(?<=\s)[*_~]+(?=\s)")


def plain_clean(text):
    """Light cleanup for plain text: drop divider lines, headings and bullet marks."""
    out = []
    for line in text.split("\n"):
        if not re.search(r"[^\W_]", line):
            out.append("")  # symbol-only line (---, * * *): a pause
            continue
        heading = _HEADING.match(line)
        bullet = _BULLET.match(line)
        if heading:
            out += ["", ensure_period(heading.group(1)), ""]
            continue
        if bullet:
            body = bullet.group(2)
            if bullet.group(1):
                body = f"{bullet.group(1)} {body}"
            out += ["", ensure_period(body), ""]
            continue
        line = re.sub(r"^\s*>+\s?", "", line)
        out.append(line)
    text = "\n".join(out)
    text = _BOLD.sub(r"\2", text)
    text = _STAR_EM.sub(r"\1", text)
    text = _UNDER_EM.sub(r"\1", text)
    text = _LONE_STAR.sub("", text)
    return text.replace("`", "")


_BLOCK_TAGS = {"p", "div", "br", "hr", "ul", "ol", "li", "table", "tr", "blockquote",
               "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "dl", "dt", "dd"}
_PERIOD_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6", "li", "dt", "dd", "th", "td"}
_SKIP_TAGS = {"script", "style", "head", "title", "noscript"}


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip = 0
        self.pre = 0

    def _break(self):
        self.parts.append("\n\n")

    def _end_item(self):
        for j in range(len(self.parts) - 1, -1, -1):
            body = self.parts[j].rstrip()
            if not body:
                continue
            if body[-1] not in SENTENCE_END:
                self.parts[j] = body + "."
                del self.parts[j + 1:]
            return

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self.skip += 1
        elif tag == "pre":
            if self.pre == 0 and self.skip == 0:
                self._break()
                if TEXT_OPTIONS["code"] == "say":
                    self.parts.append("Code block skipped.")
                    self._break()
            self.pre += 1
        elif tag in _BLOCK_TAGS:
            if tag in ("ul", "ol"):
                self._end_item()  # a list nested inside an item ends that item's text
            self._break()

    def handle_startendtag(self, tag, attrs):
        if tag in _BLOCK_TAGS and not self.skip and not self.pre:
            self._break()

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS:
            self.skip = max(0, self.skip - 1)
        elif tag == "pre":
            self.pre = max(0, self.pre - 1)
        elif self.skip or self.pre:
            return
        elif tag in _PERIOD_TAGS:
            self._end_item()
            self.parts.append(" " if tag in ("th", "td") else "\n\n")
        elif tag in _BLOCK_TAGS:
            self._break()

    def handle_data(self, data):
        if not self.skip and not self.pre:
            self.parts.append(data)


def html_to_text(html):
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    return "".join(parser.parts)


def markdown_to_text(text):
    import markdown

    if text.startswith("---\n"):
        end = re.search(r"\n(?:---|\.\.\.)[ \t]*(?:\n|$)", text[3:])
        if end:
            text = text[3 + end.end():]
    html = markdown.markdown(text, extensions=["markdown.extensions.extra"])
    return html_to_text(html)


def prepare_text(text, kind="plain"):
    """kind: 'plain', 'markdown' or 'html'. Returns text ready for chunking."""
    if kind == "markdown":
        text = markdown_to_text(text)
    elif kind == "html":
        text = html_to_text(text)
    else:
        text = plain_clean(text)
    return normalize_chars(text)


# --------------------------------------------------------------------------
# PDF -> listening edition (cleans the Markdown that markitdown makes from a PDF)
# --------------------------------------------------------------------------

_PAGE_LABEL = re.compile(r"^(?:\d{1,3}|[ivxlcdm]{1,6})$", re.I)
_CAPTION = re.compile(r"^(Table|Figure)\s+\d+(?:\.\d+)?[.:]\s")
_SUPERSCRIPTS = str.maketrans("", "", "¹²³⁰⁴⁵⁶⁷⁸⁹")
_FOOTNOTE_LINE = re.compile(r"^(\d{1,2})\s+[A-Z][a-z]")
_CID = re.compile(r"\(cid:\d+\)")
_CHAPTER = re.compile(r"^Chapter\s+\d+\.\s+\S")
_BULLET_START = re.compile(r"^(?:[•●▪-]\s|\d{1,2}[.)]\s+[A-Z])")
_MATH_ALNUM = re.compile("[\U0001D400-\U0001D7FF]")
_CODE_LINE = re.compile(r"\.py:\d|==|!=")
_CODE_WORDS = {"continue", "break", "pass", "else:", "return"}


def _pdf_pages(raw):
    """One list of cleaned, non-blank lines per PDF page (pages are split by form feeds)."""
    import unicodedata
    pages = []
    for page in raw.replace("\r\n", "\n").split("\x0c"):
        lines = []
        for line in page.split("\n"):
            line = _MATH_ALNUM.sub(lambda m: unicodedata.normalize("NFKC", m.group()), line)
            line = _CID.sub("", line).replace(" ", " ").replace("", "•")
            line = " ".join(line.split())
            if line:
                lines.append(line)
        pages.append(lines)
    return pages


def _drop_page_numbers(pages, log):
    """Remove each page's own number; tables can bury it, so the expected number is also searched."""
    import collections
    votes = collections.Counter()
    for i, lines in enumerate(pages):
        if lines and re.fullmatch(r"\d{1,3}", lines[-1]):
            votes[i + 1 - int(lines[-1])] += 1
    offset = votes.most_common(1)[0][0] if votes else None
    removed = 0
    for i, lines in enumerate(pages):
        if lines and _PAGE_LABEL.match(lines[-1]):
            lines.pop()
            removed += 1
        elif offset is not None:
            label = str(i + 1 - offset)
            for j in range(len(lines) - 1, max(len(lines) - 5, -1), -1):
                if lines[j] == label:
                    del lines[j]
                    removed += 1
                    break
    log.append(f"page numbers removed: {removed} (page-number offset {offset})")


def _pull_footnotes(pages, log):
    """Take footnote blocks off the page bottoms. Returns {page index: [footnote text]}."""
    found = {}
    for i, lines in enumerate(pages):
        if not any(ch in "".join(lines) for ch in "¹²³⁴⁵⁶⁷⁸⁹"):
            continue
        for j in range(max(len(lines) - 12, 0), len(lines)):
            m = _FOOTNOTE_LINE.match(lines[j])
            if m and len(lines[j]) > 25:
                found.setdefault(i, []).append(f"Footnote {m.group(1)}: " + " ".join(lines[j:])[len(m.group(1)) + 1:])
                del lines[j:]
                log.append(f"footnote moved (page {i + 1}): {found[i][-1][:70]}")
                break
    return found


def _is_prose(line, width):
    return len(line) >= 0.6 * width and len(line.split()) >= 9


def prepare_pdf_markdown(raw):
    """Turn raw markitdown output of a thesis-style PDF into a clean listening edition.

    Keeps the abstract and the chapters up to the references. Tables and figures are
    replaced by their captions. Returns (markdown text, log lines).
    """
    log = []
    pages = _pdf_pages(raw)
    _drop_page_numbers(pages, log)
    footnotes = _pull_footnotes(pages, log)
    stream = [(line, i) for i, lines in enumerate(pages) for line in lines]
    texts = [s[0] for s in stream]

    def find(pred, start=0):
        return next((k for k in range(start, len(texts)) if pred(texts[k])), None)

    a0 = find(lambda t: t == "Abstract")
    a1 = find(lambda t: t == "Contents", a0 or 0)
    b0 = find(lambda t: bool(_CHAPTER.match(t)) and "...." not in t, a1 or 0)
    b1 = find(lambda t: t == "References", b0 or 0)
    if None in (a0, a1, b0, b1):
        log.append("WARNING: abstract/chapter anchors not found, processing the whole document")
        keep = stream
    else:
        keep = stream[a0:a1] + stream[b0:b1]
        log.append(f"scope: abstract (lines {a0}-{a1}) + chapters to the references (lines {b0}-{b1}) of {len(stream)}")

    prose_lens = sorted(len(t) for t, _ in keep if len(t) >= 50)
    width = prose_lens[int(len(prose_lens) * 0.9)] if prose_lens else 90
    log.append(f"typical full line width: {width} characters")

    # tables and figures: keep the caption, drop the cells/labels until prose resumes
    items, i = [], 0
    while i < len(keep):
        text, page = keep[i]
        if _CAPTION.match(text):
            cap, j = [text], i + 1
            while (j < len(keep) and not re.search(r"[.!?]$", cap[-1]) and len(cap) < 4
                   and not _CHAPTER.match(keep[j][0]) and keep[j][0] not in ("Conclusion", "Abstract")
                   and (len(cap[-1]) >= 0.92 * width or keep[j][0][:1].islower())):
                cap.append(keep[j][0])
                j += 1
            full = " ".join(cap)
            label = _CAPTION.match(full).end()
            first = full[:label] + re.split(r"(?<=[.!?])\s+(?=[A-Z])", full[label:], maxsplit=1)[0]
            rest = full[len(first):].strip()
            if rest:
                log.append(f"  text after the caption sentence kept as normal text ({len(rest)} chars): {rest[:50]!r}")
            cap = [first]
            while (j < len(keep) and not _is_prose(keep[j][0], width) and not _CHAPTER.match(keep[j][0])
                   and keep[j][0] not in ("Conclusion", "Abstract")):
                j += 1
            if j < len(keep) and re.match(r"^(Source|Sources|Note|Notes)\b[.:]", keep[j][0]):
                while j < len(keep) and not re.search(r"[.!?]$", keep[j][0]):
                    j += 1
                j += 1
            log.append(f"{cap[0][:60]!r}: kept caption, dropped {j - i - len(cap)} lines")
            if TEXT_OPTIONS["tables" if text.startswith("Table") else "figures"] == "caption":
                items.append(("cap", " ".join(cap), page))
            if rest:
                items.append(("line", rest, page))
            i = j
            continue
        items.append(("brk" if text in ("•", "-") else "line", text, page))
        i += 1

    # drop display equations and runs of short unpunctuated lines (uncaptioned table cells)
    cleaned, i = [], 0
    while i < len(items):
        kind, text, page = items[i]
        if kind == "line":
            letters = re.findall(r"[A-Za-z]{4,}", text)
            if (re.search(r"\(\d+\.\d+\)\s*$", text) and len(letters) < 6) or (
                    "=" in text and len(letters) < 3 and len(text) < 60 and not text.endswith(".")):
                log.append(f"equation dropped: {text[:60]!r}")
                i += 1
                continue
            if _CODE_LINE.search(text) or text in _CODE_WORDS:
                j = i
                while j < len(items) and items[j][0] == "line" and (
                        _CODE_LINE.search(items[j][1]) or items[j][1] in _CODE_WORDS):
                    j += 1
                log.append(f"code excerpt replaced ({j - i} lines): {text[:50]!r}")
                if TEXT_OPTIONS["code"] == "say":
                    cleaned.append(("line", "Code excerpt skipped.", page))
                i = j
                continue
            j = i
            while (j < len(items) and items[j][0] == "line" and len(items[j][1]) < 45
                   and not _CHAPTER.match(items[j][1]) and items[j][1] not in ("Conclusion", "Abstract")):
                j += 1
            if j - i >= 3:
                log.append(f"table-like run dropped ({j - i} lines): {text[:40]!r} ...")
                i = j
                continue
        cleaned.append(items[i])
        i += 1

    # vocabulary for deciding whether a line-end hyphen is a split word or a real compound
    vocab = set(re.findall(r"[a-z]+", " ".join(t for k, t, _ in cleaned if k == "line").lower()))

    def join(lines):
        out = lines[0]
        for nxt in lines[1:]:
            m1, m2 = re.search(r"([A-Za-z]+)-$", out), re.match(r"[A-Za-z]+", nxt)
            if m1 and m2:
                joined = (m1.group(1) + m2.group()).lower()
                out = out[:-1] + nxt if joined in vocab else out + nxt
            else:
                out = f"{out} {nxt}"
        return out.translate(_SUPERSCRIPTS)

    paras, cur, deferred = [], [], []
    last_page = cleaned[0][2] if cleaned else 0

    def close():
        if cur:
            paras.append(("p", join(cur)))
            cur.clear()
            paras.extend(("p", f) for f in deferred)
            deferred.clear()

    for n, (kind, text, page) in enumerate(cleaned):
        if page != last_page:
            for f in footnotes.get(last_page, []):
                (deferred if cur else paras).append(f if cur else ("p", f))
            last_page = page
        if kind == "brk":
            close()  # a lone bullet mark: the next lines are a new list item
            continue
        if kind == "cap":
            close()
            paras.append(("p", text))
            continue
        if _CHAPTER.match(text):
            close()
            paras.append(("h1", text))
            continue
        if text in ("Abstract", "Conclusion"):
            close()
            paras.append(("h1", text))
            continue
        nxt = cleaned[n + 1][1] if n + 1 < len(cleaned) else ""
        if (not cur and 15 <= len(text) < 80 and len(text.split()) >= 2 and not re.search(r"[.!?:,;]$", text)
                and nxt[:1].isupper() and not _BULLET_START.match(text) and cleaned[n + 1][0] == "line"):
            paras.append(("h2", text))
            continue
        if cur and _BULLET_START.match(text):
            close()
        cur.append(text)
        if re.search(r"[.!?:]$", text) and len(text) < 0.8 * width:
            close()
    close()
    for f in footnotes.get(last_page, []):
        paras.append(("p", f))

    kept = []
    split_paras = []
    for k, t in paras:
        if k == "p" and "•" in t:
            split_paras += [("p", part.strip()) for part in t.split("•") if part.strip()]
        else:
            split_paras.append((k, t))
    for k, t in split_paras:
        words = len(t.split())
        if (k == "p" and words < 4 and not (t.endswith(":") and words >= 2) and not _CAPTION.match(t + " ")
                and t != "Code excerpt skipped."):
            log.append(f"leftover fragment dropped: {t!r}")
            continue
        kept.append((k, t))
    prefix = {"h1": "# ", "h2": "## ", "p": ""}
    return "\n\n".join(prefix[k] + t for k, t in kept) + "\n", log


def load_source(path):
    """Return (original text, text prepared for speech)."""
    ext = os.path.splitext(path)[1].lower()
    kind = "markdown" if ext in MARKDOWN_EXTS else "html" if ext in HTML_EXTS else "plain"
    raw = read_text_file(path)
    return raw, prepare_text(raw, kind)


def load_document(path):
    return load_source(path)[1]


# --------------------------------------------------------------------------
# Documents, settings and library for the window
# --------------------------------------------------------------------------

SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")
LIBRARY_PATH = os.path.join(DATA_DIR, "library.json")
WORDS_PER_SECOND = 2.2  # spoken words per second at 1.0x (measured on a long document)
REALTIME_FACTOR = 1.9   # audio made per second of waiting (measured on this PC)
VOICE_LANGS = {"a": "English (US)", "b": "English (UK)", "e": "Spanish", "f": "French", "h": "Hindi",
               "i": "Italian", "j": "Japanese", "p": "Portuguese", "z": "Chinese"}
DEFAULT_SETTINGS = {"theme": "dark", "voice": "af_heart", "speed": 1.0, "pause": PAUSE_SECONDS,
                    "srt": True, "sync": True, "open_player": True, "output_dir": "",
                    "tables": "caption", "figures": "caption", "code": "say", "links": "say",
                    "model_path": "", "voices_path": ""}


def load_json_file(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def save_json_file(path, data):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except OSError:
        log_error(traceback.format_exc())


def load_settings():
    settings = dict(DEFAULT_SETTINGS)
    settings.update(load_json_file(SETTINGS_PATH, {}))
    return settings


def library_add(entry):
    items = [e for e in load_json_file(LIBRARY_PATH, []) if e.get("path") != entry["path"]]
    items.insert(0, entry)
    save_json_file(LIBRARY_PATH, items)


def library_remove(path):
    save_json_file(LIBRARY_PATH, [e for e in load_json_file(LIBRARY_PATH, []) if e.get("path") != path])


def default_output_dir(settings):
    return settings.get("output_dir") or OUTPUT_DIR


def unique_path(folder, stem, ext=".mp3"):
    """A path in folder that does not exist yet, so an earlier recording is never overwritten."""
    path, n = os.path.join(folder, stem + ext), 2
    while os.path.exists(path):
        path = os.path.join(folder, f"{stem} ({n}){ext}")
        n += 1
    return path


def voice_info(voice_id):
    """(display name, language, gender) from a voice id such as 'af_heart'."""
    name = voice_id.split("_", 1)[-1].replace("_", " ").title()
    gender = {"f": "Female", "m": "Male"}.get(voice_id[1:2], "")
    return name, VOICE_LANGS.get(voice_id[:1], "Other"), gender


def estimate(body, speed):
    """(words, audio seconds, seconds to convert) for a text at a speaking speed."""
    words = len(body.split())
    audio = words / WORDS_PER_SECOND / max(speed, 0.1)
    return words, audio, audio / REALTIME_FACTOR


def pdf_to_text(path):
    from pdfminer.high_level import extract_text
    return extract_text(path)


def docx_to_markdown(path):
    import docx
    parts = []
    for p in docx.Document(path).paragraphs:
        text = p.text.strip()
        if not text:
            continue
        style = p.style.name if p.style is not None else ""
        m = re.match(r"Heading (\d)", style)
        if style == "Title":
            parts.append("# " + text)
        elif m:
            parts.append("#" * min(int(m.group(1)), 3) + " " + text)
        else:
            parts.append(text)
    return "\n\n".join(parts)


def split_sections(md):
    """[(title or None, markdown)] split at the top heading level; headings in code fences are ignored."""
    lines, in_code, heads = md.split("\n"), False, []
    for n, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            in_code = not in_code
        m = None if in_code else re.match(r"^(#{1,3})\s+(\S.*)$", line)
        if m:
            heads.append((n, len(m.group(1)), m.group(2).strip()))
    if not heads:
        return [(None, md)]
    top = min(level for _, level, _ in heads)
    marks = [(n, title) for n, level, title in heads if level == top]
    sections = []
    if marks[0][0] > 0:
        sections.append((None, "\n".join(lines[:marks[0][0]])))
    for k, (n, title) in enumerate(marks):
        end = marks[k + 1][0] if k + 1 < len(marks) else len(lines)
        sections.append((title, "\n".join(lines[n:end])))
    return [s for s in sections if s[1].strip()]


def build_document(path, pdf_raw=None):
    """Read a file into a dict the window can show: sections to pick from and prepared text.

    pdf_raw is the already-extracted text of a PDF, so changing an option does not read it again.
    """
    ext = os.path.splitext(path)[1].lower()
    doc = {"path": path, "name": os.path.basename(path), "ext": ext, "log": [], "pages": None}
    if ext == ".pdf":
        raw = pdf_raw if pdf_raw is not None else pdf_to_text(path)
        doc["pdf_raw"] = raw
        doc["pages"] = raw.count("\x0c") or 1
        md, doc["log"] = prepare_pdf_markdown(raw)
        doc.update(kind="markdown", source=raw.replace("\x0c", "\n\n"), sections=split_sections(md))
    elif ext == ".docx":
        md = docx_to_markdown(path)
        doc.update(kind="markdown", source=md, sections=split_sections(md))
    else:
        raw = read_text_file(path)
        kind = "markdown" if ext in MARKDOWN_EXTS else "html" if ext in HTML_EXTS else "plain"
        doc.update(kind=kind, source=raw, sections=split_sections(raw) if kind == "markdown" else [(None, raw)])
    doc["words_raw"] = len(doc["source"].split())
    return doc


def document_text(doc, selected=None):
    """Text ready for speech from the chosen sections (all of them when selected is None)."""
    parts = [text for k, (title, text) in enumerate(doc["sections"]) if selected is None or k in selected]
    return prepare_text("\n\n".join(parts), doc["kind"])


def split_chunks(text, limit=MAX_CHUNK):
    """Return [(chunk, pause_after)]; pause_after is True at the end of a paragraph."""
    chunks = []
    for para in re.split(r"\n\s*\n", text):
        para = " ".join(para.split())
        if not re.search(r"[^\W_]", para):
            continue
        pieces, current = [], ""
        for sentence in re.split(r"(?<=[.!?])\s+", para):
            while len(sentence) > limit:
                cut = sentence.rfind(" ", 0, limit)
                cut = cut if cut > 0 else limit
                if current:
                    pieces.append(current)
                    current = ""
                pieces.append(sentence[:cut].strip())
                sentence = sentence[cut:].strip()
            if current and len(current) + len(sentence) + 1 > limit:
                pieces.append(current)
                current = sentence
            else:
                current = f"{current} {sentence}".strip()
        if current:
            pieces.append(current)
        pieces = [p for p in pieces if re.search(r"[^\W_]", p)]
        for i, piece in enumerate(pieces):
            chunks.append((piece, i == len(pieces) - 1))
    return chunks


# --------------------------------------------------------------------------
# Speech
# --------------------------------------------------------------------------

def load_kokoro():
    import onnxruntime as ort
    ort.set_default_logger_severity(3)
    from kokoro_onnx import Kokoro
    paths = model_paths()  # looked up now, so files downloaded during this session are found
    if paths is None:
        raise FileNotFoundError("The voice model files are missing. Run the app once with an internet "
                                "connection, or see the README for how to download them by hand.")
    return Kokoro(*paths)


def download_models(progress=None):
    """Download the model files that are missing into models/, verifying size and SHA-256.

    progress(bytes_done, bytes_total, file_name) is called as data arrives.
    """
    import hashlib
    import urllib.request
    os.makedirs(MODELS_DIR, exist_ok=True)
    total = sum(info["size"] for info in MODEL_FILES.values())
    done = 0
    for name, info in MODEL_FILES.items():
        dest = os.path.join(MODELS_DIR, name)
        if os.path.isfile(dest) and os.path.getsize(dest) == info["size"]:
            done += info["size"]
            continue
        part, digest = dest + ".part", hashlib.sha256()
        request = urllib.request.Request(info["url"], headers={"User-Agent": "KokoroVoiceStudio"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response, open(part, "wb") as out:
                while True:
                    block = response.read(1 << 20)
                    if not block:
                        break
                    out.write(block)
                    digest.update(block)
                    done += len(block)
                    if progress:
                        progress(done, total, name)
            if os.path.getsize(part) != info["size"] or digest.hexdigest() != info["sha256"]:
                raise OSError(f"{name} did not download correctly (size or checksum mismatch).")
            os.replace(part, dest)
        except BaseException:
            try:
                os.remove(part)
            except OSError:
                pass
            raise


def speak(kokoro, chunk, voice, speed):
    """Return (samples, rate, timings), or None when the chunk has nothing speakable."""
    lang = LANG_BY_PREFIX.get(voice[:1], "en-us")
    try:
        return kokoro.create_timed(chunk, voice=voice, speed=speed, lang=lang)
    except ValueError as exc:
        if "no phonemes" in str(exc):
            return None
        raise


_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def _word_times(text, timings):
    """Per-word [word, start, end] when the spoken words match the written ones, else None."""
    groups, cur = [], []
    for t in timings:
        if t.phoneme == " ":
            if cur:
                groups.append(cur)
                cur = []
        else:
            cur.append(t)
    if cur:
        groups.append(cur)
    words = text.split()
    if not words or len(words) != len(groups):
        return None
    return [[w, g[0].start, g[-1].end] for w, g in zip(words, groups)]


def _sentence_groups(timings):
    groups, cur = [], []
    for t in timings:
        if cur and cur[-1].phoneme in ".!?" and t.phoneme not in ".!?":
            groups.append(cur)
            cur = []
        cur.append(t)
    if cur:
        groups.append(cur)
    return groups


def build_segments(chunk, timings, offset, chunk_seconds, paragraph_end):
    """Timed segments for one synthesized chunk, one per sentence when they can be told apart."""
    sentences = [s for s in _SENTENCE_SPLIT.split(chunk) if s.strip()]
    if not timings:
        return [{"t": chunk, "s": round(offset, 3), "e": round(offset + chunk_seconds, 3),
                 "w": None, "p": paragraph_end}]
    groups = _sentence_groups(timings)
    if len(groups) != len(sentences):
        sentences, groups = [chunk], [list(timings)]
    segments = []
    for k, (text, group) in enumerate(zip(sentences, groups)):
        spoken = [t for t in group if t.phoneme != " "] or group
        start, end = offset + spoken[0].start, offset + spoken[-1].end
        words = _word_times(text, group)
        if words:
            words = [[w, round(offset + s, 3), round(offset + e, 3)] for w, s, e in words]
        segments.append({"t": text, "s": round(start, 3), "e": round(max(end, start + 0.01), 3),
                         "w": words, "p": paragraph_end and k == len(groups) - 1})
    return segments


def synthesize(kokoro, text, out_path, voice, speed, progress=None, cancel=None,
               pause=PAUSE_SECONDS, on_chunk=None):
    """Speak text into an MP3 written chunk by chunk.

    progress(i, total) runs after each chunk; on_chunk(i, total, text) runs before it.
    Returns (duration in seconds, timed segments).
    """
    import numpy as np
    import soundfile as sf

    chunks = split_chunks(text)
    if not chunks:
        raise ValueError("There is no speakable text to convert.")
    total, written, pause_next = len(chunks), 0, False
    segments = []
    out = None
    try:
        for i, (chunk, pause_after) in enumerate(chunks, 1):
            if cancel is not None and cancel.is_set():
                raise InterruptedError("Cancelled.")
            if on_chunk:
                on_chunk(i, total, chunk)
            result = speak(kokoro, chunk, voice, speed)
            if result is not None:
                samples, rate, timings = result
                if out is None:
                    out = sf.SoundFile(out_path, "w", samplerate=rate, channels=1, format="MP3")
                if pause_next and pause > 0:
                    silence = np.zeros(int(pause * rate), dtype="float32")
                    out.write(silence)
                    written += len(silence)
                segments += build_segments(chunk, timings, written / rate, len(samples) / rate, pause_after)
                out.write(samples)
                written += len(samples)
                pause_next = pause_after
            if progress:
                progress(i, total)
        if out is None:
            raise ValueError("There is no speakable text to convert.")
        return written / rate, segments
    finally:
        if out is not None:
            out.close()


# --------------------------------------------------------------------------
# Subtitles and timing files
# --------------------------------------------------------------------------

def srt_time(seconds):
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def build_srt(segments, limit=84):
    """SubRip text. Long sentences are split only where real word timings exist."""
    cues = []
    for seg in segments:
        if len(seg["t"]) <= limit or not seg["w"]:
            cues.append((seg["s"], seg["e"], seg["t"]))
            continue
        line, line_start, line_end = [], None, None
        for word, s, e in seg["w"]:
            if line and len(" ".join(line + [word])) > limit:
                cues.append((line_start, line_end, " ".join(line)))
                line, line_start = [], None
            if line_start is None:
                line_start = s
            line.append(word)
            line_end = e
        if line:
            cues.append((line_start, line_end, " ".join(line)))
    return "\n".join(
        f"{n}\n{srt_time(s)} --> {srt_time(e)}\n{text}\n" for n, (s, e, text) in enumerate(cues, 1))


def write_sidecars(out_path, segments, duration, voice, source, srt=True, sync=True, chapters=None):
    """Write name.srt and name.json (the timing data the Player reads) next to the audio file."""
    stem = os.path.splitext(out_path)[0]
    if srt:
        with open(stem + ".srt", "w", encoding="utf-8") as fh:
            fh.write(build_srt(segments))
    if sync:
        data = {"version": 1, "audio": os.path.basename(out_path), "voice": voice,
                "duration": round(duration, 3), "source": source, "segments": segments,
                "chapters": chapters or [], "bookmarks": []}
        with open(stem + ".json", "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)


def load_sidecar(audio_path):
    path = os.path.splitext(audio_path)[0] + ".json"
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if data.get("version") == 1 and data.get("segments"):
            return data
    except (OSError, ValueError):
        pass
    return None


_TOKEN = re.compile(r"[^\W_]+")


def find_chapters(titles, segments):
    """[{"title", "start"}]: where each chapter title is spoken, searching forward through the sentences."""
    found, cursor = [], 0
    for title in titles:
        words = [m.group().lower() for m in _TOKEN.finditer(title)]
        if not words:
            continue
        for k in range(cursor, len(segments)):
            spoken, j = [], k
            while j < len(segments) and len(spoken) < len(words):  # a title can span "Chapter 1." + "Introduction."
                spoken += [m.group().lower() for m in _TOKEN.finditer(segments[j]["t"])]
                j += 1
            if spoken[:len(words)] == words:
                found.append({"title": title, "start": segments[k]["s"]})
                cursor = k + 1
                break
    return found


def align_segments(source, segments, max_scan=20000):
    """Best-effort: the (start, end) character span in source that each segment came from.

    A segment that cannot be found in order (e.g. "Code block skipped.") gets None.
    """
    tokens = [(m.group().lower(), m.start(), m.end()) for m in _TOKEN.finditer(source)]
    words = [t[0] for t in tokens]
    positions = {}
    for i, w in enumerate(words):
        positions.setdefault(w, []).append(i)
    cursor, spans = 0, []
    for seg in segments:
        spoken = [m.group().lower() for m in _TOKEN.finditer(seg["t"])]
        if not spoken:
            spans.append(None)
            continue
        found = None
        for size, reach in ((3, max_scan), (2, max_scan), (1, 60)):
            head = spoken[:min(size, len(spoken))]
            cands = positions.get(head[0], [])
            for j in range(bisect.bisect_left(cands, cursor), len(cands)):
                if cands[j] > cursor + reach:
                    break
                if words[cands[j]:cands[j] + len(head)] == head:
                    found = cands[j]
                    break
            if found is not None:
                break
        if found is None:
            spans.append(None)
            continue
        tail = spoken[-min(2, len(spoken)):]
        end = found + len(spoken)
        lo = found + max(len(spoken) - 5, 0)
        for j in range(lo, min(found + len(spoken) + 80, len(words)) - len(tail) + 1):
            if words[j:j + len(tail)] == tail:
                end = j + len(tail)
                break
        end = min(max(end, found + 1), len(tokens))
        cursor = end
        spans.append((tokens[found][1], tokens[end - 1][2]))
    return spans


# --------------------------------------------------------------------------
# Player: waveform, sentence strip and synced text
# --------------------------------------------------------------------------

def enable_dpi_awareness():
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


def fmt_clock(seconds):
    minutes, secs = divmod(max(0, int(seconds)), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


class AudioPlayer:
    """Plays int16 audio through one output stream and reports the playback position."""

    def __init__(self, data, rate):
        import sounddevice as sd
        self.data, self.rate = data, rate
        self.pos = 0
        self.playing = False
        self.volume = 1.0
        self.stream = sd.OutputStream(samplerate=rate, channels=data.shape[1], dtype="int16",
                                      callback=self._fill)
        self.stream.start()

    def _fill(self, outdata, frames, time_info, status):
        if not self.playing:
            outdata.fill(0)
            return
        end = min(self.pos + frames, len(self.data))
        n = end - self.pos
        chunk = self.data[self.pos:end]
        if self.volume < 0.999:
            chunk = (chunk * self.volume).astype("int16")
        outdata[:n] = chunk
        if n < frames:
            outdata[n:] = 0
            self.playing = False
        self.pos = end

    @property
    def time(self):
        return self.pos / self.rate

    def play(self):
        if self.pos >= len(self.data):
            self.pos = 0
        self.playing = True

    def pause(self):
        self.playing = False

    def seek(self, seconds):
        self.pos = max(0, min(int(seconds * self.rate), len(self.data)))

    def close(self):
        self.playing = False
        try:
            self.stream.stop()
            self.stream.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# The window: theme and helpers
# --------------------------------------------------------------------------

# (light, dark) colour pairs, so every widget follows the Light/Dark switch
BG, SIDE, PANEL = ("#f4f1ea", "#0e1014"), ("#ece8de", "#12151b"), ("#ffffff", "#171b22")
CARD, CARD2, LINE = ("#faf8f3", "#1c212b"), ("#e3ddcd", "#222835"), ("#e6e1d6", "#2a303d")
TEXT, MUTED, DIM = ("#1c1f26", "#e9ecf3"), ("#6b7280", "#8c95a9"), ("#9a9486", "#5d6578")
ACCENT, ON_ACCENT, ACCENT_SOFT = ("#0f766e", "#2dd4bf"), ("#ffffff", "#04211d"), ("#d5efe9", "#15332f")
AMBER, FONT = "#ffd166", "Segoe UI"
SHORT_LANG = {"English (US)": "US", "English (UK)": "UK"}
TABLES_MENU = {"Caption only": "caption", "Skip": "skip"}
CODE_MENU = {"Say “code skipped”": "say", "Skip silently": "skip"}
LINKS_MENU = {"Say “link”": "say", "Remove": "drop"}
EXT_COLORS = {".pdf": "#e85555", ".md": "#3b82f6", ".markdown": "#3b82f6", ".txt": "#8b5cf6",
              ".docx": "#2563eb", ".html": "#f59e0b", ".htm": "#f59e0b"}


def mode_color(pair):
    return pair[1] if ctk.get_appearance_mode() == "Dark" else pair[0]


def font(size=13, weight="normal", family=FONT):
    return ctk.CTkFont(family, size, weight)


def label(parent, text, size=13, weight="normal", color=TEXT, **kw):
    return ctk.CTkLabel(parent, text=text, font=font(size, weight), text_color=color, **kw)


def fmt_duration(seconds):
    seconds = int(round(seconds))
    if seconds >= 3600:
        return f"{seconds // 3600} h {seconds % 3600 // 60:02d} m"
    if seconds >= 90:
        return f"{round(seconds / 60)} min"
    return f"{seconds} s"


def card_frame(parent, **kw):
    return ctk.CTkFrame(parent, corner_radius=16, fg_color=PANEL, border_width=1, border_color=LINE, **kw)


def action_button(parent, text, command=None, primary=False, width=None, height=36, **kw):
    opts = dict(text=text, command=command, height=height, corner_radius=10, font=font(13, "bold"))
    if width:
        opts["width"] = width
    if primary:
        opts.update(fg_color=ACCENT, hover_color=ACCENT, text_color=ON_ACCENT)
    else:
        opts.update(fg_color="transparent", hover_color=CARD2, text_color=TEXT, border_width=1, border_color=LINE)
    opts.update(kw)
    return ctk.CTkButton(parent, **opts)


def fit_window(win, max_w, max_h, min_w, min_h):
    """Size a window to fit the usable screen (taskbar excluded) at any display scaling."""
    left, top, width, height = 0, 0, win.winfo_screenwidth(), win.winfo_screenheight() - 60
    try:
        import ctypes
        from ctypes import wintypes
        area = wintypes.RECT()
        ctypes.windll.user32.SystemParametersInfoW(0x30, 0, ctypes.byref(area), 0)  # SPI_GETWORKAREA
        left, top, width, height = area.left, area.top, area.right - area.left, area.bottom - area.top
    except Exception:
        pass
    try:
        scale = win._get_window_scaling()
    except Exception:
        scale = 1.0
    w, h = int(min(max_w, (width - 30) / scale)), int(min(max_h, (height - 60) / scale))
    win.geometry(f"{w}x{h}+{int(left + (width - w * scale) / 2)}+{top + 4}")
    win.minsize(min(min_w, w), min(min_h, h))


def reveal_in_folder(path):
    import subprocess
    try:
        subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
    except OSError:
        log_error(traceback.format_exc())


class PlayerWindow(ctk.CTkToplevel):
    """Synced reader: spoken text, original text, chapters and a waveform timeline."""

    CANVAS_H, STRIP_H = 150, 22

    def __init__(self, master, path, autoplay=False, on_close=None):
        super().__init__(master)
        import soundfile as sf
        self.on_close, self.closed, self.ready, self.autoplay = on_close, False, False, autoplay
        self.path = path
        self.title(f"Player - {os.path.basename(path)}")
        self.configure(fg_color=BG)
        fit_window(self, 1360, 880, 1000, 640)
        try:
            self.scale = self._get_window_scaling()
        except Exception:
            self.scale = 1.0
        info = load_sidecar(path)
        self.segments = info["segments"] if info else []
        self.source = info.get("source") if info else None
        self.starts = [s["s"] for s in self.segments]
        self.bookmarks = list(info.get("bookmarks", [])) if info else []
        self.chapters = self._chapters(info)
        self.duration = (info or {}).get("duration") or sf.info(path).duration
        self.voice = (info or {}).get("voice", "")
        self.data, self.rate, self.player, self.decoded, self.load_pct = None, 24000, None, None, 0
        self.cur, self.cur_word, self.cur_ws, self.cur_offsets, self.cur_chapter = -1, -1, None, None, -1
        self.loop_a, self.loop_b, self.sleep_at = None, None, None
        self.hover_job, self.draw_job, self.head = None, None, None
        self._build()
        self._fill_text()
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<space>", lambda e: (self.toggle(), "break")[1])
        self.bind("<Left>", lambda e: self.skip(-5))
        self.bind("<Right>", lambda e: self.skip(5))
        self.bind("<Control-b>", lambda e: self.add_bookmark())
        threading.Thread(target=self._decode, daemon=True).start()
        self.after(60, self._tick)
        self.after(250, lambda: (self.lift(), self.focus_force()))

    # ---- data ------------------------------------------------------------

    def _chapters(self, info):
        if info and info.get("chapters"):
            return info["chapters"]
        source = (info or {}).get("source") or ""
        if "\n#" in "\n" + source:
            titles = [t for t, _ in split_sections(source) if t]
            return find_chapters(titles, self.segments) if len(titles) > 1 else []
        return []

    def _decode(self):
        """Read the whole file into one int16 array in blocks, without a second full copy."""
        try:
            import numpy as np
            import soundfile as sf
            with sf.SoundFile(self.path) as f:
                total, channels = f.frames, f.channels
                data = np.zeros((total, channels), dtype="int16")
                pos = 0
                while pos < total:
                    block = f.read(1 << 16, dtype="int16", always_2d=True)
                    if len(block) == 0:
                        break
                    data[pos:pos + len(block)] = block
                    pos += len(block)
                    self.load_pct = int(100 * pos / max(total, 1))
                self.decoded = (data[:pos], f.samplerate)
        except Exception:
            log_error(traceback.format_exc())
            self.decoded = ("error", traceback.format_exc().splitlines()[-1])

    def _on_decoded(self):
        data, rate = self.decoded
        if isinstance(data, str):
            self.load_label.configure(text="Could not read this audio file.")
            return
        self.data, self.rate = data, rate
        self.duration = len(data) / rate
        try:
            self.player = AudioPlayer(data, rate)
        except Exception as exc:
            log_error(traceback.format_exc())
            self.load_label.configure(text=f"No audio output available ({exc})")
        self.ready = True
        self.load_label.configure(text="")
        self._draw()
        if self.autoplay and self.player:
            self.player.play()

    # ---- building the window ---------------------------------------------

    def px(self, n):
        return int(n * self.scale)

    def _build(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        bar = ctk.CTkFrame(self, corner_radius=0, fg_color=SIDE, height=58)
        bar.grid(row=0, column=0, sticky="ew")
        titles = ctk.CTkFrame(bar, fg_color="transparent")
        titles.pack(side="left", padx=22, pady=8)
        label(titles, os.path.splitext(os.path.basename(self.path))[0], 16, "bold").pack(anchor="w")
        meta = [fmt_duration(self.duration)]
        if self.voice:
            meta.insert(0, "Narrated by " + voice_info(self.voice)[0])
        label(titles, "  ·  ".join(meta), 12, color=MUTED).pack(anchor="w")
        if self.segments:
            ctk.CTkLabel(bar, text="  ● Synced to text  ", corner_radius=20, fg_color=ACCENT_SOFT, text_color=ACCENT,
                         font=font(12, "bold")).pack(side="left", padx=8)
        action_button(bar, "Show file", lambda: reveal_in_folder(self.path), width=96, height=34, text_color=MUTED).pack(side="right", padx=18)
        self.view = ctk.CTkSegmentedButton(bar, values=["Reader", "Original", "Both"], height=34, command=self._set_view,
                                           fg_color=PANEL, selected_color=CARD2, selected_hover_color=CARD2,
                                           unselected_color=PANEL, unselected_hover_color=CARD, text_color=TEXT,
                                           font=font(12, "bold"))
        self.view.set("Both" if self.source else "Reader")
        self.view.pack(side="right", padx=4)

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.grid(row=1, column=0, sticky="nsew")
        body.grid_columnconfigure(1, weight=1, uniform="panes")
        body.grid_columnconfigure(2, weight=1, uniform="panes")
        body.grid_rowconfigure(0, weight=1)
        self.rail = ctk.CTkScrollableFrame(body, width=232, corner_radius=0, fg_color=SIDE)
        self.rail.grid(row=0, column=0, sticky="nsew")
        self.reader_box = ctk.CTkFrame(body, fg_color="transparent")
        self.reader_box.grid(row=0, column=1, sticky="nsew")
        self.original_box = ctk.CTkFrame(body, corner_radius=0, fg_color=SIDE)
        self.original_box.grid(row=0, column=2, sticky="nsew")
        self.reader = self._text_pane(self.reader_box, "SPOKEN SCRIPT", ("Georgia", 15), mode_color(TEXT), mode_color(BG), 14)
        self.original = self._text_pane(self.original_box, "ORIGINAL TEXT  ·  grey = not spoken", ("Consolas", 10),
                                        mode_color(DIM), mode_color(SIDE), 10)
        self._build_rail()

        dock = ctk.CTkFrame(self, corner_radius=0, fg_color=SIDE)
        dock.grid(row=2, column=0, sticky="ew")
        self.hover_label = label(dock, "", 12, color=MUTED, anchor="w")
        self.hover_label.pack(fill="x", padx=24, pady=(8, 0))
        self.canvas = tk.Canvas(dock, height=self.px(self.CANVAS_H), bg=mode_color(SIDE), highlightthickness=0)
        self.canvas.pack(fill="x", padx=24)
        self.canvas.bind("<Configure>", self._schedule_draw)
        self.canvas.bind("<Button-1>", self._click_canvas)
        self.canvas.bind("<B1-Motion>", self._click_canvas)
        self.canvas.bind("<Motion>", self._hover)
        self.canvas.bind("<Leave>", lambda e: self.hover_label.configure(text=""))
        trans = ctk.CTkFrame(dock, fg_color="transparent")
        trans.pack(fill="x", padx=24, pady=(6, 14))
        self.time_label = label(trans, "00:00", 20, "bold")
        self.time_label.pack(side="left")
        self.total_label = label(trans, f" / {fmt_clock(self.duration)}", 14, color=MUTED)
        self.total_label.pack(side="left", pady=(4, 0))
        self.load_label = label(trans, "Loading audio… 0%", 12, color=MUTED)
        self.load_label.pack(side="left", padx=14)
        centre = ctk.CTkFrame(trans, fg_color="transparent")
        centre.place(relx=.5, rely=.5, anchor="center")
        for text, command in (("−10", lambda: self.skip(-10)), ("Prev", self.prev_sentence)):
            action_button(centre, text, command, width=54, height=38, corner_radius=19).pack(side="left", padx=3)
        self.play_btn = ctk.CTkButton(centre, text="Play", width=88, height=46, corner_radius=23, fg_color=ACCENT,
                                      hover_color=ACCENT, text_color=ON_ACCENT, font=font(14, "bold"), command=self.toggle)
        self.play_btn.pack(side="left", padx=6)
        for text, command in (("Next", self.next_sentence), ("+10", lambda: self.skip(10))):
            action_button(centre, text, command, width=54, height=38, corner_radius=19).pack(side="left", padx=3)
        extras = ctk.CTkFrame(trans, fg_color="transparent")
        extras.pack(side="right")
        self.vol = ctk.CTkSlider(extras, from_=0, to=1, width=90, progress_color=ACCENT, button_color="#ffffff",
                                 button_hover_color="#ffffff", fg_color=CARD2, command=self._volume)
        self.vol.set(1.0)
        self.vol.pack(side="right", padx=(10, 0))
        label(extras, "Volume", 12, color=MUTED).pack(side="right")
        self.sleep_menu = ctk.CTkOptionMenu(extras, values=["Sleep: off", "Sleep: 15 min", "Sleep: 30 min", "Sleep: 60 min"],
                                            width=120, height=32, corner_radius=9, fg_color=CARD2, button_color=CARD2,
                                            button_hover_color=CARD2, text_color=TEXT, font=font(12),
                                            dropdown_font=font(12), command=self._sleep_changed)
        self.sleep_menu.pack(side="right", padx=8)
        self.loop_btn = action_button(extras, "A–B loop", self._loop_click, width=88, height=32)
        self.loop_btn.pack(side="right", padx=4)
        self.mark_btn = action_button(extras, "★ Bookmark", self.add_bookmark, width=100, height=32)
        self.mark_btn.pack(side="right", padx=4)
        self._set_view(self.view.get())

    def _text_pane(self, parent, title, fnt, fg, bg, pad):
        label(parent, title, 11, "bold", MUTED).pack(anchor="w", padx=22, pady=(16, 6))
        holder = ctk.CTkFrame(parent, fg_color="transparent")
        holder.pack(fill="both", expand=True, padx=(18, 8), pady=(0, 10))
        text = tk.Text(holder, wrap="word", fg=fg, bg=bg, insertbackground=fg, relief="flat", borderwidth=0,
                       highlightthickness=0, padx=self.px(pad), pady=self.px(pad), cursor="arrow", font=fnt,
                       spacing1=self.px(2), spacing3=self.px(5), selectbackground=mode_color(CARD2))
        bar = ctk.CTkScrollbar(holder, command=text.yview)
        text.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        text.pack(side="left", fill="both", expand=True)
        dark = ctk.get_appearance_mode() == "Dark"
        text.tag_configure("cur", background="#3b3413" if dark else "#fff3b0")
        text.tag_configure("word", background=AMBER if dark else "#ffc83d", foreground="#171207")
        return text

    def _set_view(self, value):
        self.reader_box.grid_remove()
        self.original_box.grid_remove()
        if value in ("Reader", "Both") or not self.source:
            self.reader_box.grid(row=0, column=1, columnspan=1 if value == "Both" and self.source else 2, sticky="nsew")
        if value in ("Original", "Both") and self.source:
            self.original_box.grid(row=0, column=2 if value == "Both" else 1, columnspan=1 if value == "Both" else 2,
                                   sticky="nsew")

    def _build_rail(self):
        for child in self.rail.winfo_children():
            child.destroy()
        label(self.rail, "CHAPTERS", 10, "bold", DIM).pack(anchor="w", padx=10, pady=(8, 6))
        self.chapter_btns = []
        if not self.chapters:
            label(self.rail, "No chapters in this file", 12, color=MUTED).pack(anchor="w", padx=10, pady=4)
        for k, ch in enumerate(self.chapters):
            btn = ctk.CTkButton(self.rail, text=f"{ch['title'][:26]}\n{fmt_clock(ch['start'])}", anchor="w", height=48,
                                corner_radius=10, fg_color="transparent", hover_color=CARD, text_color=MUTED,
                                font=font(12, "bold"), command=lambda t=ch["start"]: self.seek(t))
            btn.pack(fill="x", pady=1)
            self.chapter_btns.append(btn)
        label(self.rail, "BOOKMARKS", 10, "bold", DIM).pack(anchor="w", padx=10, pady=(18, 6))
        if not self.bookmarks:
            label(self.rail, "Press Ctrl+B to add one", 12, color=MUTED).pack(anchor="w", padx=10, pady=2)
        for mark in sorted(self.bookmarks, key=lambda m: m["t"]):
            row = ctk.CTkFrame(self.rail, fg_color="transparent")
            row.pack(fill="x")
            ctk.CTkButton(row, text=f"★ {fmt_clock(mark['t'])}  {mark.get('label', '')[:18]}", anchor="w", height=30,
                          corner_radius=8, fg_color="transparent", hover_color=CARD, text_color=MUTED, font=font(12),
                          command=lambda t=mark["t"]: self.seek(t)).pack(side="left", fill="x", expand=True)
            ctk.CTkButton(row, text="✕", width=26, height=26, corner_radius=8, fg_color="transparent",
                          hover_color=CARD, text_color=DIM, font=font(11),
                          command=lambda m=mark: self.remove_bookmark(m)).pack(side="right")

    def _fill_text(self):
        rd, self.sp_range = self.reader, []
        rd.tag_configure("heading", font=("Georgia", 22, "bold"), spacing1=self.px(14), spacing3=self.px(10))
        rd.tag_configure("past", foreground=mode_color(DIM))
        chapter_starts = {ch["start"] for ch in self.chapters}
        for i, seg in enumerate(self.segments):
            a = rd.index("end-1c")
            tags = (f"s{i}", "heading") if seg["s"] in chapter_starts else (f"s{i}",)
            rd.insert("end", seg["t"], tags)
            self.sp_range.append((a, rd.index("end-1c")))
            rd.insert("end", "\n\n" if seg["p"] or seg["s"] in chapter_starts else " ")
        if not self.segments:
            rd.insert("1.0", "No transcript was saved with this audio file, so only the waveform is shown.")
        rd.tag_raise("cur")
        rd.tag_raise("word")
        rd.bind("<Button-1>", lambda e: self._click_text(rd, e, "s"))
        rd.configure(state="disabled")
        self.src_range = [None] * len(self.segments)
        if not self.source:
            return
        og = self.original
        og.insert("1.0", self.source)
        og.tag_configure("spoken", foreground=mode_color(TEXT))
        starts = [0] + [m.end() for m in re.finditer("\n", self.source)]

        def index(offset):
            line = bisect.bisect_right(starts, offset)
            return f"{line}.{offset - starts[line - 1]}"

        for i, span in enumerate(align_segments(self.source, self.segments)):
            if span:
                a, b = index(span[0]), index(span[1])
                self.src_range[i] = (a, b)
                og.tag_add("spoken", a, b)
                og.tag_add(f"o{i}", a, b)
        og.tag_raise("cur")
        og.tag_raise("word")
        og.bind("<Button-1>", lambda e: self._click_text(og, e, "o"))
        og.configure(state="disabled")

    # ---- timeline ---------------------------------------------------------

    def _schedule_draw(self, _event=None):
        if self.draw_job:
            self.after_cancel(self.draw_job)
        self.draw_job = self.after(150, self._draw)

    def _draw(self):
        import numpy as np
        self.draw_job = None
        c = self.canvas
        c.delete("all")
        c.configure(bg=mode_color(SIDE))
        width, height = max(c.winfo_width(), 100), int(c["height"])
        strip = self.px(self.STRIP_H)
        wave_h = height - strip - self.px(14)
        mid = self.px(12) + wave_h / 2
        if self.data is not None and len(self.data):
            mono = self.data[:, 0]
            n = len(mono)
            bins = min(width, n)
            edges = np.linspace(0, n, bins + 1).astype(int)[:-1]
            lows = np.minimum.reduceat(mono, edges).astype(float)
            highs = np.maximum.reduceat(mono, edges).astype(float)
            peak = max(float(mono.max()), -float(mono.min()), 1.0)
            scale = (wave_h / 2 - 2) / peak
            for k in range(bins):
                x = k * width / bins
                c.create_line(x, mid - highs[k] * scale, x, mid - lows[k] * scale - 1, fill="#3c8f9a")
        if self.loop_a is not None:
            x0 = self.loop_a / self.duration * width
            x1 = (self.loop_b if self.loop_b is not None else self.loop_a) / self.duration * width
            c.create_rectangle(x0, 0, max(x1, x0 + 2), height - strip, fill="#1d3b4d", outline="#38bdf8", stipple="gray50")
        last_label = -999
        for ch in self.chapters:
            x = ch["start"] / self.duration * width
            c.create_line(x, 0, x, height - strip, fill=mode_color(DIM))
            if x - last_label > self.px(78):
                c.create_text(x + 4, 6, text=ch["title"].replace("Chapter ", "Ch ")[:14], anchor="nw",
                              fill=mode_color(MUTED), font=("Segoe UI", 8))
                last_label = x
        for mark in self.bookmarks:
            x = mark["t"] / self.duration * width
            c.create_text(x, height - strip - 4, text="★", fill=AMBER, font=("Segoe UI", 9))
        top = height - strip
        for i, seg in enumerate(self.segments):
            x0 = seg["s"] / self.duration * width
            x1 = seg["e"] / self.duration * width
            c.create_rectangle(x0, top, max(x1, x0 + 1), height - 2, fill=("#2a3b52", "#243044")[i % 2] if
                               ctk.get_appearance_mode() == "Dark" else ("#c9d3e0", "#b5c2d3")[i % 2], outline="")
        self.head = c.create_line(0, 0, 0, height, fill="#ff5252", width=2)

    def _time_at(self, event):
        return max(0.0, min(event.x / max(self.canvas.winfo_width(), 1) * self.duration, self.duration))

    def _click_canvas(self, event):
        if not self.ready:
            return
        t = self._time_at(event)
        if event.y >= int(self.canvas["height"]) - self.px(self.STRIP_H) and self.segments:
            t = self.starts[max(bisect.bisect_right(self.starts, t) - 1, 0)]
        self.seek(t)

    def _hover(self, event):
        t = self._time_at(event)
        text = fmt_clock(t)
        if self.segments:
            i = max(bisect.bisect_right(self.starts, t) - 1, 0)
            text += "   “" + self.segments[i]["t"][:90] + "”"
        self.hover_label.configure(text=text)

    def _click_text(self, widget, event, prefix):
        pattern = re.compile(rf"{prefix}(\d+)$")
        for tag in widget.tag_names(f"@{event.x},{event.y}"):
            m = pattern.match(tag)
            if m:
                self.seek(self.segments[int(m.group(1))]["s"])
                return

    # ---- controls ---------------------------------------------------------

    def seek(self, t):
        if self.player:
            self.player.seek(t)

    def toggle(self):
        if self.player:
            self.player.pause() if self.player.playing else self.player.play()

    def skip(self, seconds):
        if self.player:
            self.player.seek(self.player.time + seconds)

    def prev_sentence(self):
        if self.player and self.segments:
            t = self.player.time
            i = bisect.bisect_right(self.starts, t - 1.5) - 1
            self.player.seek(self.starts[max(i, 0)])

    def next_sentence(self):
        if self.player and self.segments:
            i = bisect.bisect_right(self.starts, self.player.time)
            if i < len(self.starts):
                self.player.seek(self.starts[i])

    def _volume(self, value):
        if self.player:
            self.player.volume = float(value)

    def _loop_click(self):
        if not self.player:
            return
        t = self.player.time
        if self.loop_a is None:
            self.loop_a = t
            self.loop_btn.configure(text="Set end…")
        elif self.loop_b is None:
            self.loop_a, self.loop_b = sorted((self.loop_a, t))
            self.loop_btn.configure(text="Loop on ✕")
        else:
            self.loop_a = self.loop_b = None
            self.loop_btn.configure(text="A–B loop")
        self._draw()

    def _sleep_changed(self, value):
        minutes = {"Sleep: 15 min": 15, "Sleep: 30 min": 30, "Sleep: 60 min": 60}.get(value)
        self.sleep_at = time.time() + minutes * 60 if minutes else None

    def add_bookmark(self):
        if not self.player or not self.source and not self.segments:
            return
        t = self.player.time
        i = max(bisect.bisect_right(self.starts, t) - 1, 0) if self.segments else None
        self.bookmarks.append({"t": round(t, 2), "label": self.segments[i]["t"] if i is not None else ""})
        self._save_bookmarks()

    def remove_bookmark(self, mark):
        self.bookmarks = [m for m in self.bookmarks if m is not mark]
        self._save_bookmarks()

    def _save_bookmarks(self):
        path = os.path.splitext(self.path)[0] + ".json"
        data = load_json_file(path, None)
        if data is not None:
            data["bookmarks"] = self.bookmarks
            save_json_file(path, data)
        self._build_rail()
        self._draw()

    # ---- live updates -----------------------------------------------------

    def _tick(self):
        if self.closed:
            return
        if self.decoded is not None and not self.ready:
            self._on_decoded()
        elif not self.ready:
            self.load_label.configure(text=f"Loading audio… {self.load_pct}%")
        t = self.player.time if self.player else 0.0
        if self.player:
            if self.loop_b is not None and self.player.playing and t >= self.loop_b:
                self.player.seek(self.loop_a)
            if self.sleep_at and time.time() >= self.sleep_at:
                self.player.pause()
                self.sleep_at = None
                self.sleep_menu.set("Sleep: off")
            self.play_btn.configure(text="Pause" if self.player.playing else "Play")
        if self.head is not None:
            x = t / self.duration * max(self.canvas.winfo_width(), 1)
            self.canvas.coords(self.head, x, 0, x, int(self.canvas["height"]))
        self.time_label.configure(text=fmt_clock(t))
        self._highlight(t)
        self.after(40, self._tick)

    def _highlight(self, t):
        if self.chapters and self.chapter_btns:
            k = bisect.bisect_right([c["start"] for c in self.chapters], t) - 1
            if k != self.cur_chapter:
                for n, btn in enumerate(self.chapter_btns):
                    on = n == k
                    btn.configure(fg_color=CARD if on else "transparent", text_color=TEXT if on else MUTED)
                self.cur_chapter = k
        if not self.segments:
            return
        i = bisect.bisect_right(self.starts, t) - 1
        if i >= 0 and t > self.segments[i]["e"] + 0.2:
            i = -1
        if i != self.cur:
            self._set_current(i)
        if i >= 0 and self.cur_ws:
            j = bisect.bisect_right(self.cur_ws, t) - 1
            if j != self.cur_word:
                self.reader.tag_remove("word", "1.0", "end")
                self.cur_word = j
                if j >= 0:
                    a = self.sp_range[i][0]
                    s, e = self.cur_offsets[j]
                    self.reader.tag_add("word", f"{a}+{s}c", f"{a}+{e}c")

    def _set_current(self, i):
        rd = self.reader
        for widget in (rd, self.original):
            widget.tag_remove("cur", "1.0", "end")
        rd.tag_remove("word", "1.0", "end")
        rd.tag_remove("past", "1.0", "end")
        self.cur, self.cur_word, self.cur_ws, self.cur_offsets = i, -1, None, None
        if i < 0:
            return
        a, b = self.sp_range[i]
        rd.tag_add("past", "1.0", a)
        rd.tag_add("cur", a, b)
        rd.see(a)
        if self.src_range[i]:
            oa, ob = self.src_range[i]
            self.original.tag_add("cur", oa, ob)
            self.original.see(oa)
        seg = self.segments[i]
        if seg["w"]:
            self.cur_ws = [w[1] for w in seg["w"]]
            self.cur_offsets = [(m.start(), m.end()) for m in re.finditer(r"\S+", seg["t"])]

    def close(self):
        self.closed = True
        if self.player:
            self.player.close()
        self.destroy()
        if self.on_close:
            self.on_close()


class App(ctk.CTk, _DnD):
    def __init__(self, selftest=None, preload=None, screen=None, autostart=False):
        self.settings = load_settings()
        self.autostart = autostart
        self.quiet, self.last_model_error = False, ""  # quiet: no pop-up dialogs (the self-test sets it)
        ctk.set_appearance_mode(self.settings["theme"])
        super().__init__()
        self.dnd_ready = False
        if TkinterDnD is not None:
            try:
                self.TkdndVersion = TkinterDnD._require(self)
                self.dnd_ready = True
            except Exception:
                log_error(traceback.format_exc())
        TEXT_OPTIONS.update({k: self.settings[k] for k in TEXT_OPTIONS})
        if not CUSTOM_MODEL["model"]:  # a --model given on the command line wins over the saved choice
            CUSTOM_MODEL.update(model=self.settings["model_path"], voices=self.settings["voices_path"])
        self.title("Kokoro Voice Studio")
        self._fit_to_screen()
        self.configure(fg_color=BG)

        self.kokoro, self.doc, self.body = None, None, ""
        self.busy, self.cancel, self.q, self.player = False, threading.Event(), queue.Queue(), None
        self.voice = self.settings["voice"]
        self.all_voices = self._list_voices()
        self.sec_boxes, self.preview_playing, self.source_mode = [], False, "File"
        self.paste_job = None

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self.screens = {}
        self._build_sidebar()
        self.content = ctk.CTkFrame(self, fg_color="transparent")
        self.content.grid(row=0, column=1, sticky="nsew")
        self.content.grid_columnconfigure(0, weight=1)
        self.content.grid_rowconfigure(0, weight=1)
        self._build_create()
        self._build_right()
        self._build_library()
        self._build_settings()
        self.show("create")
        threading.Thread(target=self._load_model, daemon=True).start()
        self.after(80, self._poll)
        self.protocol("WM_DELETE_WINDOW", self._quit)
        if selftest:
            self.after(600, lambda: self._selftest(selftest))
        if preload:
            self.after(800, lambda: self.load_path(preload))
        if screen:
            self.after(200, lambda: self.show(screen))

    def _fit_to_screen(self):
        fit_window(self, 1500, 960, 1120, 700)

    # ---- small helpers ---------------------------------------------------

    def _list_voices(self):
        paths = model_paths()
        if paths is None:  # the model is still to be downloaded; the list is filled in once it is loaded
            return [self.voice]
        try:
            import numpy as np
            return sorted(np.load(paths[1]).files)
        except Exception:
            return [self.voice]

    def save_settings(self):
        save_json_file(SETTINGS_PATH, self.settings)

    def _quit(self):
        self.cancel.set()
        if self.player is not None:
            self.player.close()
        try:
            import sounddevice as sd
            sd.stop()
        except Exception:
            pass
        self.destroy()

    def _poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                handler = getattr(self, "_on_" + msg[0], None)
                if handler:
                    handler(*msg[1:])
        except queue.Empty:
            pass
        self.after(80, self._poll)

    def _load_model(self):
        """Load the voice model, downloading it first when this is the first run."""
        try:
            if model_paths() is None:
                self.q.put(("download_progress", 0, 1, ""))
                try:
                    download_models(progress=lambda done, total, name: self.q.put(("download_progress", done, total, name)))
                except OSError as exc:
                    log_error(traceback.format_exc())
                    self.q.put(("fatal", f"Could not download the voice model ({exc}). Check your internet "
                                         "connection and try again, or follow the manual steps in the README."))
                    return
            self.q.put(("loaded", load_kokoro()))
        except Exception as exc:
            log_error(traceback.format_exc())
            self.q.put(("fatal", str(exc)))

    def _on_download_progress(self, done, total, name):
        self.status_title.configure(text="●  Downloading voice model")
        self.status_sub.configure(text=f"{done / 1e6:.0f} of {total / 1e6:.0f} MB. One time only.")
        self.status_bar.set(done / max(total, 1))
        self.status_bar.pack(fill="x", padx=14, pady=(0, 12))
        self.retry_btn.pack_forget()

    def _on_loaded(self, kokoro):
        self.kokoro = kokoro
        self.status_bar.pack_forget()
        self.retry_btn.pack_forget()
        self.status_title.configure(text="●  Your model ready" if using_custom_model() else "●  Kokoro 82M ready")
        self.status_sub.configure(text="Offline · runs on your CPU")
        self.all_voices = sorted(kokoro.get_voices())
        if self.voice not in self.all_voices:  # a different voices file: keep the language if possible
            self.voice = next((v for v in self.all_voices if v[:1] == self.voice[:1]), self.all_voices[0])
            self.settings["voice"] = self.voice
            self.save_settings()
            self._update_voice_card()
        languages = ["All languages"] + [VOICE_LANGS[c] for c in VOICE_LANGS if any(v.startswith(c) for v in self.all_voices)]
        self.lang_menu.configure(values=languages)
        self.lang_menu.set(voice_info(self.voice)[1])
        self._fill_voices()
        self._update_buttons()
        self._refresh_model_card()

    def _use_own_model(self):
        """Ask for a model file and its voices file, then switch to them if they load."""
        from tkinter import filedialog
        start = MODELS_DIR if os.path.isdir(MODELS_DIR) else os.path.expanduser("~")
        model = filedialog.askopenfilename(title="Choose the model file (.onnx)", initialdir=start,
                                           filetypes=[("ONNX model", "*.onnx"), ("All files", "*.*")])
        if not model:
            return
        voices = filedialog.askopenfilename(title="Now choose its voices file (.bin)", initialdir=os.path.dirname(model),
                                            filetypes=[("Voices", "*.bin *.npz"), ("All files", "*.*")])
        if voices:
            self.switch_model(model, voices)

    def switch_model(self, model, voices):
        """Load the given files in the background. The model in use is replaced only if they load."""
        previous = dict(CUSTOM_MODEL)
        CUSTOM_MODEL.update(model=model, voices=voices)
        self.status_title.configure(text="●  Loading your model…")
        self.status_sub.configure(text=os.path.basename(model))

        def work():
            try:
                kokoro = load_kokoro()
                if not kokoro.get_voices():
                    raise ValueError("The voices file contains no voices.")
                self.q.put(("model_ready", kokoro, model, voices))
            except Exception as exc:
                log_error(traceback.format_exc())
                CUSTOM_MODEL.update(previous)
                self.q.put(("model_failed", str(exc)))
        threading.Thread(target=work, daemon=True).start()

    def _on_model_ready(self, kokoro, model, voices):
        self.settings.update(model_path=model, voices_path=voices)
        self.save_settings()
        self._on_loaded(kokoro)

    def _on_model_failed(self, message):
        from tkinter import messagebox
        self.status_title.configure(text="●  Your model ready" if using_custom_model() else "●  Kokoro 82M ready")
        self.status_sub.configure(text="The previous model is still in use")
        if "NOT_IMPLEMENTED" in message:
            reason = ("This computer's ONNX Runtime cannot run that type of model (it needs operations that are not "
                      "available), so it was not used. A fp16 or fp32 version of Kokoro works.")
        else:
            reason = ("Pick the model (.onnx) and the matching voices file (.bin) from the same release.\n\n"
                      + message[:300])
        self.last_model_error = reason
        if not self.quiet:
            messagebox.showerror("Kokoro Voice Studio",
                                 "Those files could not be loaded as a Kokoro model, so nothing was changed.\n\n" + reason)

    def _use_default_model(self):
        CUSTOM_MODEL.update(model="", voices="")
        self.settings.update(model_path="", voices_path="")
        self.save_settings()
        self.status_title.configure(text="●  Loading model…")
        self.status_sub.configure(text="Back to the default model")
        threading.Thread(target=self._load_model, daemon=True).start()

    def _refresh_model_card(self):
        if not hasattr(self, "model_info"):
            return
        found = model_paths()
        if found is None:
            self.model_info.configure(text="Not downloaded yet. The default model downloads on first start.")
        else:
            kind = "your own model" if using_custom_model() else "default: Kokoro 82M (fp16)"
            timing = ""
            if self.kokoro is not None:
                timing = ("  ·  word-level sync available" if getattr(self.kokoro, "has_timings", False)
                          else "  ·  this model reports no timings, so sync is per paragraph")
            self.model_info.configure(text=f"In use: {os.path.basename(found[0])}  ({kind}){timing}\n"
                                           f"Voices: {os.path.basename(found[1])}\nFolder: {os.path.dirname(found[0])}")
        self.default_model_btn.configure(state="normal" if using_custom_model() else "disabled")

    def _on_fatal(self, message):
        self.status_bar.pack_forget()
        self.status_title.configure(text="●  Voice model not ready")
        self.status_sub.configure(text=message[:110])
        self.retry_btn.pack(fill="x", padx=14, pady=(0, 12))

    def _retry_model(self):
        self.retry_btn.pack_forget()
        self.status_title.configure(text="●  Loading model…")
        self.status_sub.configure(text="Trying again")
        threading.Thread(target=self._load_model, daemon=True).start()

    def _update_buttons(self):
        ready = self.kokoro is not None and not self.busy
        usable = ready and bool(self.body.strip())
        self.convert_btn.configure(state="normal" if usable else "disabled",
                                   fg_color=ACCENT if usable else ("#a9d3cd", "#1c5a54"))
        self.preview_btn.configure(state="normal" if self.kokoro is not None else "disabled")

    # ---- navigation ------------------------------------------------------

    def show(self, name):
        for key, frame in self.screens.items():
            frame.grid_remove() if key != name else frame.grid(row=0, column=0, sticky="nsew")
        for key, button in self.nav.items():
            on = key == name
            button.configure(fg_color=CARD if on else "transparent", text_color=TEXT if on else MUTED)
        if name == "create":
            self.right.grid(row=0, column=2, sticky="nsew")
        else:
            self.right.grid_remove()
        if name == "library":
            self._refresh_library()
        self._refresh_recent()

    def _build_sidebar(self):
        side = ctk.CTkFrame(self, width=232, corner_radius=0, fg_color=SIDE)
        side.grid(row=0, column=0, sticky="nsew")
        side.grid_propagate(False)
        brand = ctk.CTkFrame(side, fg_color="transparent")
        brand.pack(fill="x", padx=18, pady=(24, 22))
        logo = ctk.CTkFrame(brand, width=36, height=36, corner_radius=11, fg_color=ACCENT)
        logo.pack(side="left")
        label(logo, "|||", 12, "bold", ON_ACCENT).place(relx=.5, rely=.5, anchor="center")
        title = ctk.CTkFrame(brand, fg_color="transparent")
        title.pack(side="left", padx=11)
        label(title, "Kokoro", 16, "bold").pack(anchor="w")
        label(title, "Voice Studio", 11, color=MUTED).pack(anchor="w")
        self.nav = {}
        for key, text in (("create", "New narration"), ("library", "Library"), ("settings", "Settings")):
            self.nav[key] = ctk.CTkButton(side, text="   " + text, anchor="w", height=42, corner_radius=10,
                                          fg_color="transparent", hover_color=CARD, text_color=MUTED,
                                          font=font(13, "bold"), command=lambda k=key: self.show(k))
            self.nav[key].pack(fill="x", padx=14, pady=2)
        label(side, "RECENT", 10, "bold", DIM).pack(anchor="w", padx=26, pady=(22, 6))
        self.recent_box = ctk.CTkFrame(side, fg_color="transparent")
        self.recent_box.pack(fill="x", padx=14)
        status = ctk.CTkFrame(side, corner_radius=12, fg_color=CARD, border_width=1, border_color=LINE)
        status.pack(side="bottom", fill="x", padx=14, pady=16)
        self.status_title = label(status, "●  Loading model…", 12, "bold")
        self.status_title.pack(anchor="w", padx=14, pady=(12, 2))
        self.status_sub = label(status, "This takes a few seconds", 11, color=MUTED, wraplength=170, justify="left")
        self.status_sub.pack(anchor="w", padx=14, pady=(0, 12))
        self.status_bar = ctk.CTkProgressBar(status, height=6, progress_color=ACCENT, fg_color=CARD2)
        self.retry_btn = action_button(status, "Try again", self._retry_model, height=30)

    def _refresh_recent(self):
        for child in self.recent_box.winfo_children():
            child.destroy()
        items = load_json_file(LIBRARY_PATH, [])[:3]
        if not items:
            label(self.recent_box, "Nothing yet", 12, color=DIM).pack(anchor="w", padx=12, pady=4)
        for entry in items:
            row = ctk.CTkButton(self.recent_box, text="●  " + entry.get("title", "")[:22], anchor="w", height=30,
                                corner_radius=8, fg_color="transparent", hover_color=CARD, text_color=MUTED,
                                font=font(12), command=lambda p=entry["path"]: self.open_player(p))
            row.pack(fill="x", pady=1)

    # ---- New narration screen --------------------------------------------

    def _build_create(self):
        screen = ctk.CTkFrame(self.content, fg_color="transparent")
        self.screens["create"] = screen
        screen.grid_columnconfigure((0, 1), weight=1, uniform="cols")
        screen.grid_rowconfigure(2, weight=1)
        head = ctk.CTkFrame(screen, fg_color="transparent")
        head.grid(row=0, column=0, columnspan=2, sticky="ew", padx=34, pady=(26, 0))
        titles = ctk.CTkFrame(head, fg_color="transparent")
        titles.pack(side="left")
        label(titles, "New narration", 24, "bold").pack(anchor="w")
        label(titles, "Drop a PDF, Word, Markdown or text file. We prepare it for listening, you decide what is read.",
              13, color=MUTED).pack(anchor="w")
        self.source_seg = ctk.CTkSegmentedButton(
            head, values=["File", "Paste text"], height=36, corner_radius=10, command=self._switch_source,
            fg_color=PANEL, selected_color=CARD2, selected_hover_color=CARD2, unselected_color=PANEL,
            unselected_hover_color=CARD, text_color=TEXT, font=font(13, "bold"))
        self.source_seg.set("File")
        self.source_seg.pack(side="right")

        self.src_card = card_frame(screen)
        self.src_card.grid(row=1, column=0, columnspan=2, sticky="ew", padx=34, pady=(20, 18))
        self._build_drop_zone()
        self._build_file_chip()
        self._build_paste_box()
        self._build_progress_card(screen)
        self._show_source_state()

        # what to read
        box1 = card_frame(screen)
        box1.grid(row=2, column=0, sticky="nsew", padx=(34, 9), pady=(0, 26))
        label(box1, "WHAT SHOULD BE READ", 11, "bold", MUTED).pack(anchor="w", padx=20, pady=(18, 4))
        self.sections_frame = ctk.CTkScrollableFrame(box1, fg_color="transparent", height=96)
        self.sections_frame.pack(fill="both", expand=True, padx=8)
        self.no_sections = label(self.sections_frame, "Choose a file to see its chapters.", 12, color=MUTED)
        self.no_sections.pack(anchor="w", padx=12, pady=6)
        options = ctk.CTkFrame(box1, fg_color="transparent")
        options.pack(fill="x", padx=20, pady=(2, 12))
        self.option_menus = {}
        for key, title, menu in (("tables", "Tables", TABLES_MENU), ("figures", "Figures", TABLES_MENU),
                                 ("code", "Code blocks", CODE_MENU), ("links", "Web links", LINKS_MENU)):
            row = ctk.CTkFrame(options, fg_color="transparent")
            row.pack(fill="x", pady=2)
            label(row, title, 13, "bold").pack(side="left")
            names = list(menu)
            current = next(n for n, v in menu.items() if v == self.settings[key])
            menu_widget = ctk.CTkOptionMenu(row, values=names, width=178, height=30, corner_radius=8,
                                            fg_color=CARD2, button_color=CARD2, button_hover_color=CARD2,
                                            text_color=TEXT, font=font(12), dropdown_font=font(12),
                                            command=lambda value, k=key, m=menu: self._option_changed(k, m[value]))
            menu_widget.set(current)
            menu_widget.pack(side="right")
            self.option_menus[key] = menu_widget

        # preview
        box2 = card_frame(screen)
        box2.grid(row=2, column=1, sticky="nsew", padx=(9, 34), pady=(0, 26))
        label(box2, "PREVIEW OF WHAT WILL BE SPOKEN", 11, "bold", MUTED).pack(anchor="w", padx=20, pady=(18, 8))
        self.preview = ctk.CTkTextbox(box2, corner_radius=10, fg_color=PANEL, text_color=("#222222", "#cdd2de"),
                                      font=font(15, family="Georgia"), wrap="word", height=170)
        self.preview.pack(fill="both", expand=True, padx=14)
        self.preview.configure(state="disabled")
        stats = ctk.CTkFrame(box2, fg_color="transparent")
        stats.pack(fill="x", padx=10, pady=(10, 4))
        self.stat_values = []
        for k, caption in enumerate(("words to read", "audio length", "to convert")):
            cell = ctk.CTkFrame(stats, corner_radius=11, fg_color=CARD)
            cell.grid(row=0, column=k, sticky="ew", padx=4)
            stats.grid_columnconfigure(k, weight=1)
            value = label(cell, "–", 19, "bold")
            value.pack(anchor="w", padx=12, pady=(10, 0))
            label(cell, caption, 11, color=MUTED).pack(anchor="w", padx=12, pady=(0, 10))
            self.stat_values.append(value)
        self.extra_stats = label(box2, "", 12, color=MUTED, wraplength=380, justify="left")
        self.extra_stats.pack(anchor="w", padx=18, pady=(2, 14))

    def _build_drop_zone(self):
        self.drop_zone = ctk.CTkFrame(self.src_card, corner_radius=14, fg_color=CARD, border_width=2, border_color=LINE)
        label(self.drop_zone, "Drop a file here", 18, "bold").pack(pady=(26, 2))
        label(self.drop_zone, "PDF, Word, Markdown, HTML or plain text", 12, color=MUTED).pack()
        action_button(self.drop_zone, "Browse files", self._browse, primary=True, width=140, height=38).pack(pady=(14, 24))
        if self.dnd_ready:
            for widget in (self.drop_zone, self.src_card):
                try:
                    widget.drop_target_register(DND_FILES)
                    widget.dnd_bind("<<Drop>>", self._on_drop)
                except Exception:
                    log_error(traceback.format_exc())

    def _build_file_chip(self):
        self.file_chip = ctk.CTkFrame(self.src_card, corner_radius=13, fg_color=CARD, border_width=1, border_color=LINE)
        self.chip_badge = ctk.CTkFrame(self.file_chip, width=44, height=52, corner_radius=8, fg_color="#e85555")
        self.chip_badge.pack(side="left", padx=16, pady=14)
        self.chip_badge.pack_propagate(False)
        self.chip_ext = label(self.chip_badge, "PDF", 12, "bold", "#ffffff")
        self.chip_ext.place(relx=.5, rely=.5, anchor="center")
        texts = ctk.CTkFrame(self.file_chip, fg_color="transparent")
        texts.pack(side="left")
        self.chip_name = label(texts, "", 15, "bold")
        self.chip_name.pack(anchor="w")
        self.chip_meta = label(texts, "", 12, color=MUTED)
        self.chip_meta.pack(anchor="w")
        action_button(self.file_chip, "Replace", self._browse, width=84, height=34, text_color=MUTED).pack(side="right", padx=16)
        self.chip_state = ctk.CTkLabel(self.file_chip, text="", corner_radius=20, fg_color=ACCENT_SOFT,
                                       text_color=ACCENT, font=font(12, "bold"))
        self.chip_state.pack(side="right", padx=4)

    def _build_paste_box(self):
        self.paste_box = ctk.CTkTextbox(self.src_card, height=150, corner_radius=12, fg_color=CARD,
                                        text_color=TEXT, font=font(14), wrap="word", border_width=1, border_color=LINE)
        self.paste_box.bind("<KeyRelease>", self._paste_changed)

    def _show_source_state(self):
        for widget in (self.drop_zone, self.file_chip, self.paste_box):
            widget.pack_forget()
        if self.source_mode == "Paste text":
            self.paste_box.pack(fill="x", padx=18, pady=18)
        elif self.doc is None or self.doc.get("pasted"):
            self.drop_zone.pack(fill="x", padx=18, pady=18)
        else:
            self.file_chip.pack(fill="x", padx=18, pady=18)

    def _switch_source(self, value):
        self.source_mode = value
        self._show_source_state()
        if value == "Paste text":
            self._paste_changed()
        elif self.doc is not None and self.doc.get("pasted"):
            self.doc = None
            self._on_doc_cleared()

    def _browse(self):
        from tkinter import filedialog
        path = filedialog.askopenfilename(
            title="Choose a document",
            filetypes=[("Documents", "*.pdf *.docx *.md *.markdown *.txt *.html *.htm"), ("All files", "*.*")])
        if path:
            self.load_path(path)

    def _on_drop(self, event):
        paths = self.tk.splitlist(event.data)
        if paths:
            self.load_path(paths[0])

    def load_path(self, path):
        if self.busy:
            return
        self.source_mode = "File"
        self.source_seg.set("File")
        self.chip_name.configure(text=os.path.basename(path))
        self.chip_meta.configure(text="Reading and preparing…")
        self.chip_state.configure(text="  Working…  ")
        self.chip_ext.configure(text=os.path.splitext(path)[1].lstrip(".").upper()[:4])
        self.chip_badge.configure(fg_color=EXT_COLORS.get(os.path.splitext(path)[1].lower(), "#6b7280"))
        self.doc = {"path": path, "pending": True, "sections": [], "log": []}
        self._show_source_state()
        self._load_in_thread(path)

    def _load_in_thread(self, path, pdf_raw=None):
        def work():
            try:
                self.q.put(("doc", build_document(path, pdf_raw)))
            except Exception as exc:
                log_error(traceback.format_exc())
                self.q.put(("doc_error", path, str(exc)))
        threading.Thread(target=work, daemon=True).start()

    def _on_doc(self, doc):
        if self.doc is None or self.doc.get("path") != doc["path"]:
            return
        self.doc = doc
        self.chip_name.configure(text=doc["name"])
        pages = f"{doc['pages']} pages · " if doc.get("pages") else ""
        self.chip_meta.configure(text=f"{pages}{doc['words_raw']:,} words")
        self.chip_state.configure(text="  ✓ Listening edition ready  " if doc["ext"] == ".pdf" else "  ✓ Ready  ")
        self._fill_sections()
        self._refresh_preview()
        if self.autostart and self.kokoro is not None:
            self.autostart = False
            self.after(300, self.start_convert)

    def _on_doc_error(self, path, message):
        from tkinter import messagebox
        self.doc = None
        self._show_source_state()
        self._on_doc_cleared()
        messagebox.showerror("Kokoro Voice Studio", f"Could not read {os.path.basename(path)}:\n{message}")

    def _on_doc_cleared(self):
        self.body = ""
        self._fill_sections()
        self._refresh_preview()

    def _paste_changed(self, _event=None):
        if self.paste_job:
            self.after_cancel(self.paste_job)
        self.paste_job = self.after(350, self._apply_paste)

    def _apply_paste(self):
        text = self.paste_box.get("1.0", "end").strip()
        if not text:
            self.doc = None
            self._on_doc_cleared()
            return
        self.doc = {"path": "", "name": "Pasted text", "ext": ".txt", "pasted": True, "kind": "plain",
                    "sections": [(None, text)], "source": text, "log": [], "pages": None, "words_raw": len(text.split())}
        self._fill_sections()
        self._refresh_preview()

    def _fill_sections(self):
        for child in self.sections_frame.winfo_children():
            child.destroy()
        self.sec_boxes = []
        sections = self.doc["sections"] if self.doc and not self.doc.get("pending") else []
        titled = [s for s in sections if s[0]]
        if not sections:
            self.no_sections = label(self.sections_frame, "Choose a file to see its chapters.", 12, color=MUTED)
            self.no_sections.pack(anchor="w", padx=12, pady=6)
            return
        if not titled:
            label(self.sections_frame, "No chapters detected. The whole document will be read.", 12,
                  color=MUTED, wraplength=330, justify="left").pack(anchor="w", padx=12, pady=6)
        for k, (title, text) in enumerate(sections):
            if not titled:
                break
            row = ctk.CTkFrame(self.sections_frame, fg_color="transparent")
            row.pack(fill="x", pady=4, padx=8)
            box = ctk.CTkCheckBox(row, text=(title or "Start of document")[:34], checkbox_width=20,
                                  checkbox_height=20, corner_radius=6, fg_color=ACCENT, hover_color=ACCENT,
                                  checkmark_color=ON_ACCENT, border_color=MUTED, text_color=TEXT,
                                  font=font(13, "bold"), command=self._refresh_preview)
            box.select()
            box.pack(side="left")
            label(row, f"{len(text.split()):,} words", 12, color=MUTED).pack(side="right")
            self.sec_boxes.append((k, box))

    def _selected(self):
        if not self.sec_boxes:
            return None
        return {k for k, box in self.sec_boxes if box.get()}

    def _option_changed(self, key, value):
        TEXT_OPTIONS[key] = value
        self.settings[key] = value
        self.save_settings()
        if self.doc and self.doc.get("ext") == ".pdf" and not self.doc.get("pending"):
            self.chip_state.configure(text="  Updating…  ")
            self._load_in_thread(self.doc["path"], self.doc["pdf_raw"])
        else:
            self._refresh_preview()

    def _refresh_preview(self):
        if self.doc is None or self.doc.get("pending"):
            self.body = ""
            self._set_preview("")
            for value in self.stat_values:
                value.configure(text="–")
            self.extra_stats.configure(text="")
        else:
            self.body = document_text(self.doc, self._selected())
            self._set_preview(self.body[:1800] + ("…" if len(self.body) > 1800 else ""))
            self._update_stats()
        self._update_buttons()
        self._update_estimate()

    def _set_preview(self, text):
        self.preview.configure(state="normal")
        self.preview.delete("1.0", "end")
        self.preview.insert("1.0", re.sub(r"\n{3,}", "\n\n", text) or "Nothing to preview yet.")
        self.preview.configure(state="disabled")

    def _update_stats(self):
        words, audio, wait = estimate(self.body, self.settings["speed"])
        for value, text in zip(self.stat_values, (f"{words:,}", fmt_duration(audio), "≈ " + fmt_duration(wait))):
            value.configure(text=text)
        log = self.doc.get("log") or []
        removed = next((int(m.group(1)) for line in log if (m := re.match(r"page numbers removed: (\d+)", line))), None)
        captions = sum(1 for line in log if "kept caption" in line)
        notes = []
        if removed is not None:
            notes.append(f"{removed} page numbers removed")
        if captions:
            notes.append(f"{captions} tables and figures reduced to captions")
        self.extra_stats.configure(text="  ·  ".join(notes))

    # ---- right panel: voice and settings ---------------------------------

    def _build_right(self):
        right = ctk.CTkFrame(self, width=372, corner_radius=0, fg_color=SIDE)
        self.right = right
        right.grid_propagate(False)
        self.convert_btn = ctk.CTkButton(right, text="Convert to MP3", height=54, corner_radius=14, fg_color=ACCENT,
                                         hover_color=ACCENT, text_color=ON_ACCENT, font=font(16, "bold"),
                                         command=self.start_convert, state="disabled",
                                         text_color_disabled=("#c9e6e2", "#0d4a43"))
        self.convert_btn.pack(side="bottom", fill="x", padx=24, pady=(4, 20))
        self.estimate_label = label(right, "", 12, color=MUTED, wraplength=320)
        self.estimate_label.pack(side="bottom", pady=(0, 2))
        label(right, "VOICE", 11, "bold", MUTED).pack(anchor="w", padx=24, pady=(22, 8))
        self.voice_card = ctk.CTkFrame(right, corner_radius=16, fg_color=("#e3f1ee", "#1a2a30"), border_width=1,
                                       border_color=("#9bd5cb", "#2b5b57"))
        self.voice_card.pack(fill="x", padx=24)
        self.voice_avatar = ctk.CTkFrame(self.voice_card, width=52, height=52, corner_radius=15, fg_color="#ffb3b8")
        self.voice_avatar.pack(side="left", padx=14, pady=12)
        self.voice_avatar.pack_propagate(False)
        self.voice_letter = label(self.voice_avatar, "H", 22, "bold", "#6b2d3a")
        self.voice_letter.place(relx=.5, rely=.5, anchor="center")
        texts = ctk.CTkFrame(self.voice_card, fg_color="transparent")
        texts.pack(side="left")
        self.voice_name = label(texts, "", 17, "bold")
        self.voice_name.pack(anchor="w")
        self.voice_sub = label(texts, "", 12, color=MUTED)
        self.voice_sub.pack(anchor="w")
        self.preview_btn = ctk.CTkButton(self.voice_card, text="▶", width=40, height=40, corner_radius=20,
                                         fg_color=ACCENT, hover_color=ACCENT, text_color=ON_ACCENT,
                                         command=self._preview_voice, state="disabled")
        self.preview_btn.pack(side="right", padx=16)

        langs = ["All languages"] + [VOICE_LANGS[c] for c in VOICE_LANGS if any(v.startswith(c) for v in self.all_voices)]
        self.lang_menu = ctk.CTkOptionMenu(right, values=langs, height=32, corner_radius=9, fg_color=CARD2,
                                           button_color=CARD2, button_hover_color=CARD2, text_color=TEXT,
                                           font=font(12), dropdown_font=font(12), command=lambda _v: self._fill_voices())
        self.lang_menu.set(voice_info(self.voice)[1])
        self.lang_menu.pack(fill="x", padx=24, pady=(10, 4))
        self.voice_list = ctk.CTkScrollableFrame(right, height=112, fg_color="transparent")
        self.voice_list.pack(fill="x", padx=16)

        self.speed_label = label(right, "", 13)
        self.speed_label.pack(anchor="w", padx=24, pady=(10, 2))
        self.speed_slider = ctk.CTkSlider(right, from_=0.5, to=2.0, number_of_steps=30, progress_color=ACCENT,
                                          button_color="#ffffff", button_hover_color="#ffffff", fg_color=CARD2,
                                          command=self._speed_changed)
        self.speed_slider.set(self.settings["speed"])
        self.speed_slider.pack(fill="x", padx=24)
        self.pause_label = label(right, "", 13)
        self.pause_label.pack(anchor="w", padx=24, pady=(8, 2))
        self.pause_slider = ctk.CTkSlider(right, from_=0.0, to=1.5, number_of_steps=15, progress_color=ACCENT,
                                          button_color="#ffffff", button_hover_color="#ffffff", fg_color=CARD2,
                                          command=self._pause_changed)
        self.pause_slider.set(self.settings["pause"])
        self.pause_slider.pack(fill="x", padx=24)
        self.switches = {}
        for key, text in (("srt", "Subtitles (.srt)"), ("sync", "Word-level sync data"),
                          ("open_player", "Open player when done")):
            sw = ctk.CTkSwitch(right, text=text, progress_color=ACCENT, button_color=("#ffffff", "#e9ecf3"),
                               fg_color=CARD2, text_color=TEXT, font=font(13),
                               command=lambda k=key: self._switch_changed(k))
            sw.select() if self.settings[key] else sw.deselect()
            sw.pack(anchor="w", padx=24, pady=(8, 0))
            self.switches[key] = sw
        out = ctk.CTkFrame(right, fg_color="transparent")
        out.pack(fill="x", padx=24, pady=(12, 0))
        label(out, "Save to", 12, color=MUTED).pack(anchor="w")
        self.out_label = label(out, "", 12, wraplength=250, justify="left")
        self.out_label.pack(side="left", anchor="w")
        action_button(out, "Change", self._choose_output_dir, width=70, height=28, text_color=MUTED).pack(side="right")
        self._fill_voices()
        self._update_voice_card()
        self._speed_changed(self.settings["speed"], save=False)
        self._pause_changed(self.settings["pause"], save=False)
        self._update_out_label()

    def _fill_voices(self):
        for child in self.voice_list.winfo_children():
            child.destroy()
        wanted = self.lang_menu.get()
        for vid in self.all_voices:
            name, lang, gender = voice_info(vid)
            if wanted != "All languages" and lang != wanted:
                continue
            selected = vid == self.voice
            row = ctk.CTkFrame(self.voice_list, corner_radius=10, fg_color=CARD if selected else "transparent")
            row.pack(fill="x", pady=1)
            dot = ctk.CTkFrame(row, width=28, height=28, corner_radius=9,
                               fg_color="#ffb3b8" if gender == "Female" else "#a0c4ff")
            dot.pack(side="left", padx=(8, 10), pady=6)
            dot.pack_propagate(False)
            label(dot, name[:1], 12, "bold", "#10131a").place(relx=.5, rely=.5, anchor="center")
            label(row, name, 13, color=TEXT if selected else MUTED).pack(side="left")
            label(row, f"{SHORT_LANG.get(lang, lang[:7])} · {gender[:1]}", 11, color=DIM).pack(side="right", padx=10)
            for widget in [row] + list(row.winfo_children()):
                widget.bind("<Button-1>", lambda _e, v=vid: self._select_voice(v))

    def _select_voice(self, vid):
        self.voice = vid
        self.settings["voice"] = vid
        self.save_settings()
        self._update_voice_card()
        self._fill_voices()

    def _update_voice_card(self):
        name, lang, gender = voice_info(self.voice)
        self.voice_name.configure(text=name)
        self.voice_sub.configure(text=f"{lang} · {gender}" if gender else lang)
        self.voice_letter.configure(text=name[:1])
        self.voice_avatar.configure(fg_color="#ffb3b8" if gender == "Female" else "#a0c4ff")

    def _speed_changed(self, value, save=True):
        value = round(float(value) * 20) / 20
        self.settings["speed"] = value
        self.speed_label.configure(text=f"Speed   {value:.2f}×")
        if save:
            self.save_settings()
            if self.body:
                self._update_stats()
        self._update_estimate()

    def _pause_changed(self, value, save=True):
        value = round(float(value), 1)
        self.settings["pause"] = value
        self.pause_label.configure(text=f"Pause between paragraphs   {value:.1f} s")
        if save:
            self.save_settings()

    def _switch_changed(self, key):
        self.settings[key] = bool(self.switches[key].get())
        self.save_settings()

    def _update_estimate(self):
        if self.body.strip():
            words, audio, wait = estimate(self.body, self.settings["speed"])
            self.estimate_label.configure(
                text=f"≈ {fmt_duration(wait)} to make · {fmt_duration(audio)} of audio")
        else:
            self.estimate_label.configure(text="Choose a file or paste some text")

    def _update_out_label(self):
        folder = default_output_dir(self.settings)
        self.out_label.configure(text=("…" + folder[-34:]) if len(folder) > 36 else folder)

    def _choose_output_dir(self):
        from tkinter import filedialog
        folder = filedialog.askdirectory(title="Where should the MP3 files go?", initialdir=default_output_dir(self.settings))
        if folder:
            self.settings["output_dir"] = folder
            self.save_settings()
            self._update_out_label()

    def _preview_voice(self):
        import sounddevice as sd
        if self.preview_playing:
            sd.stop()
            self.preview_playing = False
            self.preview_btn.configure(text="▶")
            return
        voice, speed = self.voice, self.settings["speed"]
        sample = PREVIEW_BY_PREFIX.get(voice[:1], PREVIEW_BY_PREFIX["a"])
        self.preview_playing = True
        self.preview_btn.configure(text="■")

        def work():
            try:
                result = speak(self.kokoro, sample, voice, speed)
                if result is not None:
                    sd.play(result[0], result[1])
                    sd.wait()
            except Exception:
                log_error(traceback.format_exc())
            self.q.put(("preview_done",))
        threading.Thread(target=work, daemon=True).start()

    def _on_preview_done(self):
        self.preview_playing = False
        self.preview_btn.configure(text="▶")

    # ---- conversion with a live progress card -----------------------------

    def _build_progress_card(self, screen):
        card = card_frame(screen)
        self.progress_card = card
        self.ring = tk.Canvas(card, width=116, height=116, highlightthickness=0, bg=mode_color(PANEL))
        self.ring.pack(side="left", padx=(26, 18), pady=22)
        right = ctk.CTkFrame(card, fg_color="transparent")
        right.pack(side="left", fill="both", expand=True, padx=(0, 22), pady=18)
        self.prog_title = label(right, "", 16, "bold")
        self.prog_title.pack(anchor="w")
        self.prog_detail = label(right, "", 12, color=MUTED)
        self.prog_detail.pack(anchor="w", pady=(2, 8))
        self.prog_say = ctk.CTkLabel(right, text="", font=font(14, family="Georgia"), justify="left", anchor="w",
                                     wraplength=720, fg_color=CARD, corner_radius=12, text_color=TEXT)
        self.prog_say.pack(fill="x", pady=(0, 10), ipady=8, ipadx=8)
        self.prog_buttons = ctk.CTkFrame(right, fg_color="transparent")
        self.prog_buttons.pack(anchor="w")
        self._draw_ring(0.0)

    def _draw_ring(self, fraction, done=False):
        self.ring_state = (fraction, done)
        c = self.ring
        c.configure(bg=mode_color(PANEL))
        c.delete("all")
        c.create_arc(12, 12, 104, 104, start=90, extent=-359.9, style="arc", width=11, outline=mode_color(CARD2))
        if fraction > 0:
            c.create_arc(12, 12, 104, 104, start=90, extent=-max(min(fraction, 0.9999), 0.001) * 360, style="arc",
                         width=11, outline=mode_color(ACCENT))
        c.create_text(58, 58, text="✓" if done else f"{int(fraction * 100)}%", fill=mode_color(TEXT),
                      font=(FONT, 20, "bold"))

    def _show_progress(self, on):
        if on:
            self.src_card.grid_remove()
            self.progress_card.grid(row=1, column=0, columnspan=2, sticky="ew", padx=34, pady=(20, 18))
        else:
            self.progress_card.grid_remove()
            self.src_card.grid(row=1, column=0, columnspan=2, sticky="ew", padx=34, pady=(20, 18))

    def _set_progress_buttons(self, specs):
        for child in self.prog_buttons.winfo_children():
            child.destroy()
        for text, command, primary in specs:
            action_button(self.prog_buttons, text, command, primary=primary, width=130, height=34).pack(side="left", padx=(0, 8))

    def start_convert(self):
        from tkinter import messagebox
        if self.busy or self.kokoro is None or not self.body.strip():
            return
        doc = self.doc
        stem = os.path.splitext(doc["name"])[0] if not doc.get("pasted") else "Narration " + time.strftime("%Y-%m-%d %H%M")
        folder = default_output_dir(self.settings)
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("Kokoro Voice Studio", f"Cannot use the folder {folder}:\n{exc}")
            return
        out = unique_path(folder, stem)
        body, voice, speed, pause = self.body, self.voice, self.settings["speed"], self.settings["pause"]
        source, name, srt, sync = doc["source"], doc["name"], self.settings["srt"], self.settings["sync"]
        picked = self._selected()
        titles = [t for k, (t, _) in enumerate(doc["sections"]) if t and (picked is None or k in picked)]
        weights = [len(c) for c, _ in split_chunks(body)]
        total_chars, started = max(sum(weights), 1), time.time()
        self.job = {"out": out, "weights": weights, "total": total_chars, "started": started, "name": name,
                    "voice": voice, "words": len(body.split())}
        self.busy = True
        self.cancel.clear()
        self._update_buttons()
        self._draw_ring(0.0)
        self.prog_title.configure(text=f"Converting {name}")
        self.prog_detail.configure(text="Starting…")
        self.prog_say.configure(text="")
        self._set_progress_buttons([("Cancel", self._cancel_convert, False)])
        self._show_progress(True)

        def work():
            try:
                dur, segments = synthesize(
                    self.kokoro, body, out, voice, speed, cancel=self.cancel, pause=pause,
                    progress=lambda i, n: self.q.put(("progress", i, n)),
                    on_chunk=lambda i, n, text: self.q.put(("chunk", i, n, text)))
                write_sidecars(out, segments, dur, voice, source, srt=srt, sync=sync,
                               chapters=find_chapters(titles, segments))
                library_add({"title": os.path.splitext(name)[0] if name != "Pasted text" else os.path.basename(out)[:-4],
                             "path": out, "duration": round(dur, 1), "voice": voice, "words": len(body.split()),
                             "created": time.strftime("%Y-%m-%d %H:%M"), "source": name})
                self.q.put(("done", out, dur))
            except InterruptedError:
                self.q.put(("cancelled", out))
            except Exception as exc:
                log_error(traceback.format_exc())
                self.q.put(("error", str(exc)))
        threading.Thread(target=work, daemon=True).start()

    def _cancel_convert(self):
        self.cancel.set()
        self.prog_detail.configure(text="Stopping after this sentence…")

    def _on_chunk(self, i, total, text):
        self.prog_say.configure(text="  " + (text if len(text) < 260 else text[:257] + "…"))

    def _on_progress(self, i, total):
        job = self.job
        done_chars = sum(job["weights"][:i])
        fraction = done_chars / job["total"]
        elapsed = time.time() - job["started"]
        left = elapsed * (1 - fraction) / fraction if fraction > 0.02 else None
        self._draw_ring(fraction)
        self.prog_detail.configure(
            text=f"Part {i} of {total}" + (f"  ·  about {fmt_duration(left)} left" if left is not None else ""))

    def _finish_job(self):
        self.busy = False
        self._update_buttons()

    def _on_done(self, out, duration):
        self._finish_job()
        self._draw_ring(1.0, done=True)
        self.prog_title.configure(text="Finished")
        self.prog_detail.configure(text=f"{fmt_duration(duration)} of audio saved to {out}")
        self.prog_say.configure(text="  Subtitles and sync data were saved next to it.")
        self._set_progress_buttons([("Open player", lambda: self.open_player(out), True),
                                    ("Show in folder", lambda: self.show_in_folder(out), False),
                                    ("New narration", self._new_narration, False)])
        self._refresh_recent()
        if self.settings["open_player"]:
            self.open_player(out)

    def _on_cancelled(self, out):
        self._finish_job()
        self.prog_title.configure(text="Stopped")
        self.prog_detail.configure(text="The audio made so far was kept.")
        self._set_progress_buttons([("New narration", self._new_narration, True)])

    def _on_error(self, message):
        self._finish_job()
        self.prog_title.configure(text="Something went wrong")
        self.prog_detail.configure(text=message[:300])
        self._set_progress_buttons([("Back", self._new_narration, True)])

    def _new_narration(self):
        self._show_progress(False)

    def show_in_folder(self, path):
        reveal_in_folder(path)

    def open_player(self, path):
        from tkinter import messagebox
        if not os.path.isfile(path):
            messagebox.showerror("Kokoro Voice Studio", f"The file is missing:\n{path}")
            return
        if self.player is not None:
            self.player.close()
        try:
            self.player = PlayerWindow(self, path, on_close=lambda: setattr(self, "player", None))
        except Exception as exc:
            self.player = None
            log_error(traceback.format_exc())
            messagebox.showerror("Kokoro Voice Studio", f"Could not open the player:\n{exc}")

    # ---- library ----------------------------------------------------------

    def _build_library(self):
        screen = ctk.CTkFrame(self.content, fg_color="transparent")
        self.screens["library"] = screen
        screen.grid_columnconfigure(0, weight=1)
        screen.grid_rowconfigure(1, weight=1)
        head = ctk.CTkFrame(screen, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=34, pady=(26, 14))
        titles = ctk.CTkFrame(head, fg_color="transparent")
        titles.pack(side="left")
        label(titles, "Library", 24, "bold").pack(anchor="w")
        label(titles, "Everything you have narrated. Play it with synced text, or find the file.", 13, color=MUTED).pack(anchor="w")
        action_button(head, "Open audio file…", self._open_audio, width=150, height=38).pack(side="right")
        self.lib_list = ctk.CTkScrollableFrame(screen, fg_color="transparent")
        self.lib_list.grid(row=1, column=0, sticky="nsew", padx=26, pady=(0, 20))

    def _refresh_library(self):
        for child in self.lib_list.winfo_children():
            child.destroy()
        items = load_json_file(LIBRARY_PATH, [])
        if not items:
            label(self.lib_list, "No narrations yet. Convert a document and it appears here.", 14, color=MUTED).pack(pady=40)
        for entry in items:
            row = card_frame(self.lib_list)
            row.pack(fill="x", padx=8, pady=5)
            ext = os.path.splitext(entry.get("source", ""))[1].lower()
            badge = ctk.CTkFrame(row, width=42, height=48, corner_radius=9, fg_color=EXT_COLORS.get(ext, "#0f766e"))
            badge.pack(side="left", padx=16, pady=12)
            badge.pack_propagate(False)
            label(badge, (ext.lstrip(".") or "MP3").upper()[:4], 11, "bold", "#ffffff").place(relx=.5, rely=.5, anchor="center")
            texts = ctk.CTkFrame(row, fg_color="transparent")
            texts.pack(side="left")
            label(texts, entry.get("title", ""), 15, "bold").pack(anchor="w")
            exists = os.path.isfile(entry["path"])
            meta = [fmt_duration(entry.get("duration", 0)), voice_info(entry["voice"])[0] if entry.get("voice") else "",
                    entry.get("created", "")]
            label(texts, "  ·  ".join(m for m in meta if m) if exists else "File not found: " + entry["path"],
                  12, color=MUTED if exists else "#ff6b6b").pack(anchor="w")
            action_button(row, "Remove", lambda p=entry["path"]: self._lib_remove(p), width=80, height=32,
                          text_color=MUTED).pack(side="right", padx=(0, 16))
            if exists:
                action_button(row, "Folder", lambda p=entry["path"]: self.show_in_folder(p), width=76, height=32).pack(side="right", padx=4)
                action_button(row, "Play", lambda p=entry["path"]: self.open_player(p), primary=True, width=76, height=32).pack(side="right", padx=4)

    def _lib_remove(self, path):
        library_remove(path)
        self._refresh_library()
        self._refresh_recent()

    def _open_audio(self):
        from tkinter import filedialog
        path = filedialog.askopenfilename(title="Open an audio file", filetypes=[("Audio", "*.mp3 *.wav"), ("All files", "*.*")])
        if not path:
            return
        import soundfile as sf
        info = sf.info(path)
        library_add({"title": os.path.splitext(os.path.basename(path))[0], "path": path, "duration": round(info.duration, 1),
                     "voice": "", "words": 0, "created": time.strftime("%Y-%m-%d %H:%M"), "source": ""})
        self._refresh_library()
        self.open_player(path)

    # ---- settings ----------------------------------------------------------

    def _build_settings(self):
        screen = ctk.CTkFrame(self.content, fg_color="transparent")
        self.screens["settings"] = screen
        screen.grid_columnconfigure(0, weight=1)
        titles = ctk.CTkFrame(screen, fg_color="transparent")
        titles.grid(row=0, column=0, sticky="w", padx=34, pady=(26, 14))
        label(titles, "Settings", 24, "bold").pack(anchor="w")
        label(titles, "Everything runs on this computer. Nothing is uploaded.", 13, color=MUTED).pack(anchor="w")
        box = card_frame(screen)
        box.grid(row=1, column=0, sticky="ew", padx=34, pady=8)
        label(box, "APPEARANCE", 11, "bold", MUTED).pack(anchor="w", padx=20, pady=(18, 8))
        seg = ctk.CTkSegmentedButton(box, values=["Dark", "Light", "System"], height=34, command=self._theme_changed,
                                     fg_color=CARD, selected_color=ACCENT, selected_hover_color=ACCENT,
                                     unselected_color=CARD, unselected_hover_color=CARD2, text_color=TEXT,
                                     font=font(13, "bold"))
        seg.set(self.settings["theme"].title())
        seg.pack(anchor="w", padx=20, pady=(0, 18))
        box2 = card_frame(screen)
        box2.grid(row=2, column=0, sticky="ew", padx=34, pady=8)
        label(box2, "FILES", 11, "bold", MUTED).pack(anchor="w", padx=20, pady=(18, 8))
        label(box2, "MP3 files are saved to:", 12, color=MUTED).pack(anchor="w", padx=20)
        self.settings_out = label(box2, default_output_dir(self.settings), 13)
        self.settings_out.pack(anchor="w", padx=20, pady=(2, 8))
        row = ctk.CTkFrame(box2, fg_color="transparent")
        row.pack(anchor="w", padx=20, pady=(0, 18))
        action_button(row, "Change folder", self._choose_output_dir_settings, width=130).pack(side="left")
        action_button(row, "Clear library list", self._clear_library, width=140, text_color=MUTED).pack(side="left", padx=8)
        box3 = card_frame(screen)
        box3.grid(row=3, column=0, sticky="ew", padx=34, pady=8)
        label(box3, "VOICE MODEL", 11, "bold", MUTED).pack(anchor="w", padx=20, pady=(18, 8))
        self.model_info = label(box3, "", 13, wraplength=900, justify="left")
        self.model_info.pack(anchor="w", padx=20)
        label(box3, "You can attach your own Kokoro-compatible model: pick the model file (.onnx) and its voices file (.bin) "
                    "from the same release. The files are checked first, and nothing changes if they cannot be used.",
              12, color=MUTED, wraplength=900, justify="left").pack(anchor="w", padx=20, pady=(8, 8))
        row3 = ctk.CTkFrame(box3, fg_color="transparent")
        row3.pack(anchor="w", padx=20, pady=(0, 18))
        action_button(row3, "Use my own model…", self._use_own_model, width=170).pack(side="left")
        self.default_model_btn = action_button(row3, "Back to the default", self._use_default_model, width=160, text_color=MUTED)
        self.default_model_btn.pack(side="left", padx=8)
        box4 = card_frame(screen)
        box4.grid(row=4, column=0, sticky="ew", padx=34, pady=8)
        label(box4, "ABOUT", 11, "bold", MUTED).pack(anchor="w", padx=20, pady=(18, 8))
        label(box4, f"Settings and library: {DATA_DIR}\nYour MP3 files: {default_output_dir(self.settings)}",
              12, color=MUTED, wraplength=900, justify="left").pack(anchor="w", padx=20, pady=(0, 18))
        self._refresh_model_card()

    def _theme_changed(self, value):
        self.settings["theme"] = value.lower()
        self.save_settings()
        ctk.set_appearance_mode(value.lower())
        self._recolor()

    def _recolor(self):
        self._draw_ring(*self.ring_state)

    def _choose_output_dir_settings(self):
        self._choose_output_dir()
        self.settings_out.configure(text=default_output_dir(self.settings))

    def _clear_library(self):
        from tkinter import messagebox
        if messagebox.askyesno("Kokoro Voice Studio", "Remove all entries from the library list?\nYour MP3 files are not deleted."):
            save_json_file(LIBRARY_PATH, [])
            self._refresh_recent()

    # ---- self-test: drives the real handlers, no clicking ------------------

    def _selftest(self, spec):
        """spec is a file path, 'paste' (type text into the paste box) or 'cancel:FILE' (stop part-way)."""
        report, started = [], time.time()
        cancel_mode = spec.startswith("cancel:")
        attach = spec[7:].split("|") if spec.startswith("attach:") else None  # attach:MODEL|VOICES|FILE
        bad = spec[11:].split("|") if spec.startswith("attach-bad:") else None  # attach-bad:MODEL|VOICES
        path = attach[2] if attach else spec[7:] if cancel_mode else spec
        self.quiet = True

        def note(step, ok, extra=""):
            report.append(f"{'PASS' if ok else 'FAIL'}  {step}  {extra}".rstrip())

        def finish():
            text = "\n".join(report) + f"\nfinished in {time.time() - started:.1f}s\n"
            try:
                os.makedirs(DATA_DIR, exist_ok=True)
                with open(os.path.join(DATA_DIR, "selftest.txt"), "w", encoding="utf-8") as fh:
                    fh.write(text)
            except OSError:
                pass
            print(text, flush=True)
            self._quit()

        def wait(condition, then, step, timeout):
            deadline = time.time() + timeout

            def check():
                if condition():
                    note(step, True)
                    then()
                elif time.time() > deadline:
                    note(step, False, "timed out")
                    finish()
                else:
                    self.after(250, check)
            check()

        def ready_to_convert():
            doc = self.doc
            note("document sections", bool(doc and doc["sections"]), f"{len(doc['sections'])} section(s)")
            note("spoken text prepared", len(self.body.split()) > 5, f"{len(self.body.split())} words")
            note("convert button enabled", self.convert_btn.cget("state") == "normal")
            if len(self.body.split()) > 3000:
                note("conversion skipped", True, "long document: only loading is checked")
                self.after(300, finish)
                return
            self.settings["open_player"] = not cancel_mode
            self.start_convert()
            note("progress card shown", self.progress_card.winfo_manager() == "grid")
            if cancel_mode:
                wait(lambda: "Part 2 " in self.prog_detail.cget("text") or "Part 3 " in self.prog_detail.cget("text"),
                     do_cancel, "conversion reached part 2", 120)
            else:
                wait(lambda: not self.busy and self.prog_title.cget("text") in ("Finished", "Something went wrong"),
                     after_convert, "conversion finished", 300)

        def do_cancel():
            self._cancel_convert()
            wait(lambda: not self.busy and self.prog_title.cget("text") == "Stopped", after_cancel, "cancel stopped the job", 60)

        def after_cancel():
            out = self.job["out"]
            note("partial mp3 kept", os.path.isfile(out) and os.path.getsize(out) > 500, out)
            note("convert button usable again", self.convert_btn.cget("state") == "normal")
            note("cancelled job not added to library", not any(e["path"] == out for e in load_json_file(LIBRARY_PATH, [])))
            self.after(300, finish)

        def after_convert():
            out = self.job["out"]
            note("finished without error", self.prog_title.cget("text") == "Finished", self.prog_detail.cget("text")[:80])
            note("mp3 written", os.path.isfile(out) and os.path.getsize(out) > 1000, out)
            stem = os.path.splitext(out)[0]
            note("srt + json written", os.path.isfile(stem + ".srt") and os.path.isfile(stem + ".json"))
            note("library entry added", any(e["path"] == out for e in load_json_file(LIBRARY_PATH, [])))
            wait(lambda: self.player is not None and self.player.ready, lambda: after_player(out), "player opened and audio loaded", 90)

        def after_player(out):
            segs = len(self.player.segments)
            note("player has synced sentences", segs > 0, f"{segs} sentences")
            note("player audio output ready", self.player.player is not None)
            self.player.player.play()
            self.after(1500, lambda: after_play(out))

        def after_play(out):
            moved = self.player.player.time
            note("playback position advances", moved > 0.5, f"{moved:.1f}s")
            self.player.seek(1.0)
            self.player.add_bookmark()
            note("bookmark saved to the sync file", bool(load_sidecar(out).get("bookmarks")))
            self.after(300, preview_step)

        def preview_step():
            self._preview_voice()
            wait(lambda: not self.preview_playing, finish, "voice preview played", 40)

        def start_with_text():
            if spec == "paste":
                self.source_seg.set("Paste text")
                self._switch_source("Paste text")
                self.paste_box.insert("1.0", "This is pasted text. It has two short sentences.\n\nAnd a second paragraph here.")
                self._apply_paste()
                note("paste text accepted", self.doc is not None and self.doc.get("pasted"))
                ready_to_convert()
            else:
                self.load_path(path)
                wait(lambda: self.doc is not None and not self.doc.get("pending"), ready_to_convert, "document loaded", 90)

        def attach_model():
            self.switch_model(attach[0], attach[1])
            wait(lambda: self.settings["model_path"] == attach[0] and self.kokoro is not None,
                 lambda: (note("own model in use", using_custom_model(), os.path.basename(attach[0])),
                          note("settings page shows it", os.path.basename(attach[0]) in self.model_info.cget("text")),
                          note("timings reported", True, "yes" if getattr(self.kokoro, "has_timings", False) else "no"),
                          start_with_text()), "own model attached", 120)

        def attach_bad_model():
            before = self.kokoro
            self.switch_model(bad[0], bad[1])
            wait(lambda: bool(self.last_model_error), lambda: (
                note("unusable model refused with a message", True, self.last_model_error[:90].replace("\n", " ")),
                note("previous model still in use", self.kokoro is before and not using_custom_model()),
                note("settings unchanged", self.settings["model_path"] == ""),
                finish()), "bad model rejected", 60)

        wait(lambda: self.kokoro is not None,
             attach_model if attach else attach_bad_model if bad else start_with_text, "model loaded", 120)


def main():
    args = sys.argv[1:]
    for flag, key in (("--model", "model"), ("--voices", "voices")):  # use your own model files
        if flag in args and args.index(flag) + 1 < len(args):
            at = args.index(flag)
            CUSTOM_MODEL[key] = os.path.abspath(args[at + 1])
            del args[at:at + 2]
    if args and args[0] == "--download-models":
        last = [-1]

        def show(done, total, name):
            if int(done * 100 / total) != last[0]:
                last[0] = int(done * 100 / total)
                print(f"\rdownloading {name}: {last[0]}%", end="", flush=True)
        try:
            if model_paths() is not None:
                print("The model files are already in place:", *model_paths(), sep="\n  ")
            else:
                download_models(progress=show)
                print(f"\nDone. Files are in {MODELS_DIR}")
        except OSError as exc:
            sys.exit(f"\nDownload failed: {exc}")
        return
    if args and args[0] == "--prep" and len(args) == 3:
        with open(args[1], encoding="utf-8") as fh:
            text, log = prepare_pdf_markdown(fh.read())
        with open(args[2], "w", encoding="utf-8") as fh:
            fh.write(text)
        sys.stdout.reconfigure(errors="replace")
        print("\n".join(log))
        print(f"OK {args[2]} ({len(text.split())} words)")
        return
    if args and args[0] == "--player" and len(args) > 1:
        enable_dpi_awareness()
        root = ctk.CTk()
        root.withdraw()
        try:
            PlayerWindow(root, args[1], autoplay="--autoplay" in args, on_close=root.destroy)
        except Exception:
            log_error(traceback.format_exc())
            raise
        root.mainloop()
        return
    if args and args[0] == "--convert":
        import argparse
        p = argparse.ArgumentParser()
        p.add_argument("--convert", nargs=2, metavar=("IN", "OUT"))
        p.add_argument("--voice", default="af_heart")
        p.add_argument("--speed", type=float, default=1.0)
        a = p.parse_args(args)
        try:
            k = load_kokoro()
            raw, body = load_source(a.convert[0])
            dur, segments = synthesize(k, body, a.convert[1], a.voice, a.speed,
                                       progress=lambda i, n: print(f"chunk {i}/{n}", flush=True))
            write_sidecars(a.convert[1], segments, dur, a.voice, raw)
            print(f"OK {a.convert[1]} {dur:.2f}s", flush=True)
        except Exception:
            log_error(traceback.format_exc())
            traceback.print_exc()
            sys.exit(1)
        return
    try:
        enable_dpi_awareness()
        opts = {a: args[i + 1] for i, a in enumerate(args[:-1]) if a in ("--selftest", "--preload", "--screen")}
        App(selftest=opts.get("--selftest"), preload=opts.get("--preload"), screen=opts.get("--screen"),
            autostart="--autostart" in args).mainloop()
    except Exception:
        log_error(traceback.format_exc())
        raise


if __name__ == "__main__":
    main()
