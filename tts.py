#!/usr/bin/env python3
"""Read TXT and FB2 books aloud with mpv."""
import argparse
import math
from pathlib import Path
import sys
import tempfile
import tts_voices
from tts_config import MODES
from tts_books import read_document
from tts_voices import normalize_language as normalize_lang
from tts_playback import PlaybackStopped, player_command
from tts_pipeline import read_aloud

class HelpParser(argparse.ArgumentParser):
    def format_help(self):
        rows = "\n".join(f"  {engine:<10} {status:<10} {detail}" for engine, status, detail in tts_voices.engine_status())
        return (
            "TTS READER — read FB2, FB2.ZIP, and TXT books aloud\n"
            "\nUsage\n  tts FILE [MODE]       Arguments can be in either order.\n"
            "  tts -l LANGUAGE FILE  Set the language for plain text or missing FB2 metadata.\n"
            "  tts -s SPEED FILE     Set reading speed (1 = normal, 2.3 = 2.3x).\n"
            "  -h, --help            Show this help and detected engines.\n"
            "\nVoices\n  Mode    Engine        Female / male\n"
            "  1 / 2   RHVoice       Anna / Aleksandr for Russian (default: 1)\n"
            "  3 / 4   Edge          Microsoft voices for the book's language\n"
            "  5 / 6   Silero        Local voices for the book's language\n"
            "  7 / 8   Piper         Local voices; models download when needed\n"
            "  9       Google        Free Translate TTS; gender cannot be selected\n"
            "\nExamples\n  tts book.fb2\n  tts 4 -s 2.3 book.fb2.zip\n  tts -l pt-PT notes.txt 9\n"
            "\nDetected on this system (local checks; online services are not contacted)\n"
            + rows + "\n"
        )


def parse_args(argv):
    parser = HelpParser(prog="tts", description=__doc__, allow_abbrev=False)
    parser.add_argument("-l", dest="lang", metavar="LANGUAGE")
    parser.add_argument("-s", dest="speed", type=float, default=1.0, metavar="SPEED")
    parser.add_argument("args", nargs="*", metavar="FILE/MODE")
    parsed = parser.parse_intermixed_args(argv)
    args = parsed.args
    if not args and (argv == [] or argv is None and len(sys.argv) == 1):
        parser.print_help()
        parser.exit()
    if not math.isfinite(parsed.speed) or not 0.01 <= parsed.speed <= 100:
        parser.error("speed must be a finite number from 0.01 to 100")
    if len(args) not in (1, 2):
        parser.error("provide a file and an optional mode from 1 to 9")
    mode = 1
    if len(args) == 1:
        filename = args[0]
    else:
        numbers = [i for i, value in enumerate(args) if value in map(str, MODES)]
        if len(numbers) != 1:
            parser.error("provide one file and one mode from 1 to 9; "
                         "use ./NAME for a file with a numeric name")
        index = numbers[0]
        mode = int(args[index])
        filename = args[1 - index]
    path = Path(filename).expanduser().absolute()
    if not path.is_file():
        parser.error(f"file not found: {path}")
    language = None
    if parsed.lang:
        try:
            language = normalize_lang(parsed.lang)
        except ValueError as error:
            parser.error(str(error))
    return path, mode, language, parsed.speed


def main(argv=None):
    path, mode, override_language, speed = parse_args(argv)
    try:
        text, lang = read_document(path)
        if override_language:
            lang = override_language
        if not text.strip():
            raise ValueError("The file contains no text to read.")
        player_command()  # Report a missing player before loading a TTS model.
        print(f"{MODES[mode][0]}; language: {lang}. "
              "Space: pause, [ / ]: speed, q / Ctrl+C: quit.", file=sys.stderr)
        with tempfile.TemporaryDirectory(prefix="tts_") as temporary:
            directory = Path(temporary)
            read_aloud(path, mode, lang, text, directory, speed)
        print(file=sys.stderr)
        return 0
    except KeyboardInterrupt:
        print("\nReading stopped.", file=sys.stderr)
        return 130
    except PlaybackStopped:
        print("\nReading stopped.", file=sys.stderr)
        return 0
    except Exception as error:
        print(f"\nError: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
