# Third-party notices

Kokoro Voice Studio is released under the **GNU General Public License v3.0** (see `LICENSE`).
It is built on the open-source components below. Licences are taken from each package's own metadata
and project pages. This is a summary, not legal advice; read the original licence texts before redistributing.

## Speech

| Component | Licence | Notes |
|---|---|---|
| [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M) model and voices | Apache-2.0 | Downloaded on first run, **not** stored in this repository. |
| [kokoro-onnx](https://github.com/thewh1teagle/kokoro-onnx) | MIT | Runs the model. Also hosts the downloadable model files. |
| [ONNX Runtime](https://github.com/microsoft/onnxruntime) | MIT | |
| [phonemizer](https://github.com/bootphon/phonemizer) | GPL-3.0-or-later | Turns text into phonemes. |
| [eSpeak NG](https://github.com/espeak-ng/espeak-ng) (`espeak-ng.dll`, via `espeakng-loader`) | GPL-3.0-or-later | Bundled in the Windows build. Source: https://github.com/espeak-ng/espeak-ng |

Because the phonemizer and eSpeak NG are GPL-3.0, this project and the Windows build that bundles them are
distributed under GPL-3.0. If you redistribute the app, you must also offer the corresponding source code.

If you attach your own model in Settings, that model comes with its own licence. Check it before sharing audio or
redistributing the model.

## Audio

| Component | Licence | Notes |
|---|---|---|
| [python-soundfile](https://github.com/bastibe/python-soundfile) | BSD-3-Clause | Bundles libsndfile, which in turn uses LAME and mpg123 (LGPL) for MP3. |
| [libsndfile](https://github.com/libsndfile/libsndfile) | LGPL-2.1-or-later | Reads and writes MP3 and WAV. |
| [python-sounddevice](https://github.com/spatialaudio/python-sounddevice) and [PortAudio](http://www.portaudio.com/) | MIT | Plays audio. |
| [NumPy](https://numpy.org/) | BSD-3-Clause | |

## Window

| Component | Licence |
|---|---|
| [CustomTkinter](https://github.com/TomSchimansky/CustomTkinter) | MIT (its package metadata also names CC0-1.0) |
| [tkinterdnd2](https://github.com/Eliav2/tkinterdnd2) and tkdnd | MIT / BSD |
| [Pillow](https://python-pillow.org/) | HPND |
| Tcl/Tk (part of Python) | Tcl/Tk licence (BSD-style) |

## Reading documents

| Component | Licence |
|---|---|
| [pdfminer.six](https://github.com/pdfminer/pdfminer.six) | MIT |
| [python-docx](https://github.com/python-openxml/python-docx) | MIT |
| [Markdown](https://python-markdown.github.io/) | BSD-3-Clause |

## Building the Windows app

| Component | Licence | Notes |
|---|---|---|
| [PyInstaller](https://pyinstaller.org/) | GPL-2.0-or-later with a special exception | The exception allows the bundled program to use any licence. |
