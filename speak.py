"""Local Kokoro text-to-speech.

Usage:
    python speak.py "Hello, this is a test."
    python speak.py --file input.txt --output speech.wav
    python speak.py "Hello" --voice am_michael --speed 0.9

Model files (in the models/ folder; get them with `python kokoro_app.py --download-models`):
    kokoro-v1.0.fp16.onnx
    voices-v1.0.bin
"""

import argparse
import os
import sys

import onnxruntime as ort

# Hide harmless constant-folding warnings from the ONNX runtime.
ort.set_default_logger_severity(3)

import soundfile as sf
from kokoro_onnx import Kokoro

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# The model files live in models/ (fetch them with: python kokoro_app.py --download-models);
# the folder next to this script is also checked for older setups.
MODEL_DIR = next((d for d in (os.path.join(BASE_DIR, "models"), BASE_DIR)
                  if os.path.exists(os.path.join(d, "kokoro-v1.0.fp16.onnx"))), os.path.join(BASE_DIR, "models"))
MODEL_PATH = os.path.join(MODEL_DIR, "kokoro-v1.0.fp16.onnx")
VOICES_PATH = os.path.join(MODEL_DIR, "voices-v1.0.bin")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate speech locally with Kokoro.")
    parser.add_argument("text", nargs="*", help="Text to speak.")
    parser.add_argument("-f", "--file", help="Read text from a UTF-8 file instead.")
    parser.add_argument(
        "-o",
        "--output",
        default=os.path.join(BASE_DIR, "output.wav"),
        help="Output WAV path (default: output.wav next to this script).",
    )
    parser.add_argument(
        "-v",
        "--voice",
        default="af_heart",
        help="Voice name, e.g. af_heart, af_bella, am_michael, bf_emma (default: af_heart).",
    )
    parser.add_argument(
        "-s",
        "--speed",
        type=float,
        default=1.0,
        help="Speaking speed, 0.5-2.0 (default: 1.0).",
    )
    parser.add_argument(
        "-l",
        "--lang",
        default="en-us",
        help="Language code, e.g. en-us, en-gb, fr-fr, it, ja, zh (default: en-us).",
    )
    parser.add_argument(
        "--list-voices",
        action="store_true",
        help="List all available voice names and exit.",
    )
    args = parser.parse_args()

    if not os.path.exists(MODEL_PATH):
        sys.exit(f"Missing model file: {MODEL_PATH}")
    if not os.path.exists(VOICES_PATH):
        sys.exit(f"Missing voices file: {VOICES_PATH}")

    if args.list_voices:
        kokoro = Kokoro(MODEL_PATH, VOICES_PATH)
        for name in sorted(kokoro.get_voices()):
            print(name)
        return

    if args.file:
        with open(args.file, encoding="utf-8") as fh:
            text = fh.read()
    elif args.text:
        text = " ".join(args.text)
    else:
        print("Enter the text to speak, then press Enter:")
        text = input("> ")

    text = text.strip()
    if not text:
        sys.exit("No text provided.")

    print(f"Loading Kokoro (voice={args.voice}, speed={args.speed}) ...")
    kokoro = Kokoro(MODEL_PATH, VOICES_PATH)

    print("Generating speech ...")
    samples, sample_rate = kokoro.create(
        text,
        voice=args.voice,
        speed=args.speed,
        lang=args.lang,
    )

    sf.write(args.output, samples, sample_rate)
    duration = len(samples) / sample_rate
    print(f"Done: {args.output}")
    print(f"Duration: {duration:.1f}s at {sample_rate} Hz")


if __name__ == "__main__":
    main()
