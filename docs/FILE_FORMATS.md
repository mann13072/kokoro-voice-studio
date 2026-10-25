# File formats

Everything the app writes is plain text (JSON or SubRip), so other tools can read it.

## Where files go

| Folder | Contents | Created |
|---|---|---|
| `models/` | `kokoro-v1.0.fp16.onnx`, `voices-v1.0.bin` | On first run (downloaded and checksum-verified) |
| `data/` | `settings.json`, `library.json`, `error.log` | When first needed |
| `output/` | your `.mp3`, `.srt` and `.json` files | When you convert something |

These folders sit next to `kokoro_app.py` (or `KokoroTTS.exe`). If that folder is read-only, the app uses
`%LOCALAPPDATA%\KokoroStudio\` instead. The `output/` location can be changed in the window.

## Narration files

Each conversion writes three files with the same name: `name.mp3`, `name.srt`, `name.json`.
The two small files can be switched off in the window; without `name.json` the Player shows only the waveform.

### `name.srt`

Standard SubRip subtitles, one cue per sentence. A sentence longer than 84 characters is split into several
cues only when exact word timings are available.

### `name.json` (version 1)

```jsonc
{
  "version": 1,
  "audio": "name.mp3",
  "voice": "af_heart",
  "duration": 7440.1,                  // seconds
  "source": "the original text ...",   // shown in the Player's "original" pane
  "segments": [                        // one per spoken sentence, in order
    {
      "t": "Eight contracts have a verified check.",   // the spoken text
      "s": 2243.12,                    // start time, seconds
      "e": 2246.88,                    // end time, seconds
      "w": [["Eight", 2243.12, 2243.5], ...],         // per-word times, or null
      "p": false                       // true when a paragraph ends here (a pause follows)
    }
  ],
  "chapters":  [{"title": "Chapter 1. Introduction", "start": 124.0}],
  "bookmarks": [{"t": 2243.12, "label": "Eight contracts have a verified"}]
}
```

Notes:

* `w` is `null` for a sentence whose spoken words do not line up one-to-one with the written words
  (for example a number such as "3,450", which is spoken as several words). Timing for that sentence is then
  sentence-level only. Word times are never estimated.
* Times come from the model's own phoneme durations, not from a separate alignment step.
* `chapters` and `bookmarks` may be missing in files made by older versions. The Player then finds chapters from
  the Markdown headings in `source`.

## `data/settings.json`

Written when you change a setting. Missing keys use the defaults.

| Key | Default | Meaning |
|---|---|---|
| `theme` | `"dark"` | `"dark"`, `"light"` or `"system"` |
| `voice` | `"af_heart"` | voice id |
| `speed` | `1.0` | 0.5 to 2.0 |
| `pause` | `0.4` | seconds of silence between paragraphs |
| `srt`, `sync`, `open_player` | `true` | write subtitles, write the sync file, open the Player when done |
| `output_dir` | `""` | empty means the `output/` folder |
| `tables`, `figures` | `"caption"` | `"caption"` or `"skip"` |
| `code` | `"say"` | `"say"` ("code block skipped") or `"skip"` |
| `links` | `"say"` | `"say"` ("link") or `"drop"` |
| `model_path`, `voices_path` | `""` | your own model and voices files (full paths); empty means the default model in `models/` |

## `data/library.json`

A list of the narrations shown in the Library, newest first:

```json
[{"title": "My document", "path": "C:\\...\\output\\My document.mp3", "duration": 123.4,
  "voice": "af_heart", "words": 456, "created": "2026-10-25 13:38", "source": "My document.pdf"}]
```

Removing an entry from the Library never deletes the audio file.
