"""Free Google Translate speech using the same public RPC as gTTS.

Protocol reference: https://github.com/pndurette/gTTS
This service has no voice or gender parameter. No Cloud account is needed.
"""
import base64
import json
from pathlib import Path
import urllib.parse
import urllib.request
from tts_network import read_url
from tts_text import text_parts as split_parts

LANGUAGES = set("af am ar bg bn bs ca cs cy da de el en es et eu fi fr fr-CA gl gu ha hi hr hu id is it iw ja jw km kn ko la lt lv ml mr ms my ne nl no pa pl pt pt-PT ro ru si sk sq sr su sv sw ta te th tl tr uk ur vi yue zh-CN zh-TW".split())
MAX_CHARS = 100

def text_parts(text, limit=MAX_CHARS):
    return split_parts(text, limit)


def language_code(language):
    from tts_voices import normalize_language
    language = normalize_language(language)
    aliases = {"he": "iw", "jv": "jw", "fil": "tl", "nb": "no", "nn": "no",
               "zh": "zh-CN", "zh-Hans": "zh-CN", "zh-Hant": "zh-TW", "zh-HK": "zh-TW", "pt-BR": "pt"}
    language = aliases.get(language, language)
    if language in LANGUAGES:
        return language
    if language.startswith("zh-Hant") or language.startswith("zh-TW") or language.startswith("zh-HK") or language.startswith("zh-MO"):
        return "zh-TW"
    if language.startswith("zh-Hans-"):
        return "zh-CN"
    base = language.split("-")[0]
    base = aliases.get(base, base)
    if base in LANGUAGES:
        return base
    raise ValueError(f"Google Translate does not support language '{language}'.")


def response_audio(data):
    """Decode JSON frames; never mistake an HTML error for speech."""
    for line in data.decode("utf-8").splitlines():
        try:
            frames = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(frames, list):
            continue
        for frame in frames:
            if isinstance(frame, list) and len(frame) > 2 and frame[0] == "wrb.fr" and frame[1] == "jQ1olc" and isinstance(frame[2], str):
                payload = json.loads(frame[2])
                if isinstance(payload, list) and payload and isinstance(payload[0], str):
                    audio = base64.b64decode(payload[0], validate=True)
                    if audio:
                        return audio
    raise RuntimeError("Google Translate returned no speech audio.")


def save(text, language, path, timeout):
    language = language_code(language)
    with Path(path).open("wb") as output:
        for part in split_parts(text, MAX_CHARS, language):
            arguments = json.dumps([part, language, None, "null"], separators=(",", ":"))
            batch = json.dumps([[["jQ1olc", arguments, None, "generic"]]], separators=(",", ":"))
            request = urllib.request.Request(
                "https://translate.google.com/_/TranslateWebserverUi/data/batchexecute",
                data=urllib.parse.urlencode({"f.req": batch}).encode("ascii"),
                headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                         "User-Agent": "Mozilla/5.0", "Referer": "https://translate.google.com/"},
            )
            output.write(response_audio(read_url(request, timeout=timeout)))
