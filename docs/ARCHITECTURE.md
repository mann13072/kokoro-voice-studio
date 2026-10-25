# How it works

The whole app is one file, `kokoro_app.py`, organised in labelled sections. `speak.py` is a tiny stand-alone
command-line example that does not use the window.

```
 document ──► read ──► clean ──► split ──► speak ──► MP3 + SRT + JSON ──► Player
 (pdf, docx,   (text)   (prose    (sentences  (Kokoro,     (written as it       (synced text,
  md, html,             only)      in chunks)  per chunk)   is made)            waveform)
  txt, paste)
```

## 1. Reading and cleaning (`Reading and cleaning text`, `PDF -> listening edition`)

* **Encodings:** UTF-8, Windows-1252 and UTF-16 (Notepad "Unicode") are detected; binary files are refused.
* **Markdown and HTML:** parsed with a real parser. Formatting symbols, comments, images and front matter are
  dropped; link text is kept without the address; code blocks become "code block skipped".
* **Plain text:** divider lines (`---`, `* * *`), heading marks and bullet marks are removed.
* **Characters:** invisible and private-use characters are removed, math-italic letters become plain letters,
  `<` and `>` are spoken, URLs become "link".
* **PDF:** the text layer is extracted with pdfminer, then `prepare_pdf_markdown` removes page numbers, moves
  footnotes, replaces tables and figures with their captions, drops equations, code and table cells, and joins the
  hard-wrapped lines back into paragraphs. It looks for thesis or paper structure (an *Abstract*, a *Contents*
  list, `Chapter N.` headings, *References*). Documents without that structure are processed whole. Scanned PDFs
  with no text layer are not supported (there is no OCR).

## 2. Speaking (`Speech`)

* The text is split into paragraphs, then sentences, then chunks of at most 400 characters.
* Each chunk goes through Kokoro (`create_timed`), which also returns how long each phoneme lasts.
* `build_segments` turns those durations into one timed segment per sentence, and per-word times where the spoken
  and written words match one-to-one.
* Audio is appended to the MP3 as it is produced, so memory stays flat for long documents, and a cancelled
  conversion leaves a playable partial file.

## 3. The window (`The window: theme and helpers`, `class App`)

CustomTkinter widgets with a light/dark palette (`(light, dark)` colour pairs). Long work (reading a document,
loading the model, converting) runs in threads and reports back through a queue polled every 80 ms, so the window
never freezes. `App._selftest` drives the same handlers the buttons use.

## 4. The Player (`class PlayerWindow`, `class AudioPlayer`)

* The MP3 is decoded in blocks on a background thread into one `int16` array, so even a two-hour file opens at once.
* One output stream plays that array; the playhead is read from the stream's position.
* Text highlighting, chapters, the waveform and the sentence strip all come from the `.json` file.
* The "original" pane is matched to the spoken sentences by `align_segments`, a best-effort word match, so
  anything that was not spoken (page numbers, code, comments) stays grey.

## Design notes

* **Why chunk-level synthesis?** One call per sentence gave exact timings but was about 20% slower and changed
  the flow of the voice, so sentence times are read out of the chunk's phoneme timings instead.
* **Why does one file hold everything?** The brief was a single native `.exe` that is easy to read and build.
  The sections are separated by banner comments if you want to split it into modules.
* **Nothing is uploaded.** The only network use is the one-time model download.
