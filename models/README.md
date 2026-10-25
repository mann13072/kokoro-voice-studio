# models/

The voice model lives here. **You do not need to do anything:** the first time you start the app it downloads
the two files below (about 190 MB, one time) and checks them against known checksums.

| File | Size | SHA-256 |
|---|---|---|
| `kokoro-v1.0.fp16.onnx` | 163,527,961 bytes | `f3a290d384fbb27966d462905c71a46cef9e5fd00516b40df32a0b4afe77ac96` |
| `voices-v1.0.bin` | 28,214,398 bytes | `bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d` |

Download them yourself (for example on a computer without internet) from the
[kokoro-onnx `model-files-v1.1` release](https://github.com/thewh1teagle/kokoro-onnx/releases/tag/model-files-v1.1)
and put both files in this folder.

You can also fetch them from a terminal: `python kokoro_app.py --download-models`.

## Using a different model

The files above are only the default. To use another Kokoro-compatible model, choose it in the app under
**Settings → Voice model → Use my own model…** (it can live anywhere, including this folder), or pass
`--model` and `--voices` on the command line. Always pick the voices file from the same release as the model.

These files are not part of the repository (see `.gitignore`).
