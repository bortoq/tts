"""Language tags, engine discovery, voice catalogs, and cached model downloads."""
from functools import lru_cache
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import urllib.request


@lru_cache(maxsize=1)
def language_registry():
    aliases = {"rus": "ru", "eng": "en", "ukr": "uk", "ua": "uk", "bel": "be",
               "iw": "he", "jw": "jv", "cz": "cs"}
    names = {}
    for path in (Path("/usr/share/iso-codes/json/iso_639-2.json"), Path("/usr/share/iso-codes/json/iso_639-3.json")):
        if path.is_file():
            data = json.loads(path.read_text())
            for rows in data.values():
                for row in rows:
                    code = row.get("alpha_2", row.get("alpha_3"))
                    for key in ("alpha_2", "alpha_3", "bibliographic"):
                        if row.get(key):
                            aliases[row[key]] = code
                    for key in ("name", "common_name", "inverted_name"):
                        for name in row.get(key, "").split(";"):
                            if name.strip():
                                names[name.strip().casefold()] = code
    for code, name in {"ru":"Russian", "en":"English", "uk":"Ukrainian", "pl":"Polish", "pt":"Portuguese",
                       "cs":"Czech", "sk":"Slovak", "ky":"Kyrgyz", "uz":"Uzbek", "sr":"Serbian", "sq":"Albanian",
                       "hr":"Croatian", "mk":"Macedonian", "eo":"Esperanto", "tt":"Tatar"}.items():
        names[name.casefold()] = code
    names.update({"brazilian-portuguese":"pt-BR", "mandarin":"zh-CN", "cantonese":"yue"})
    return aliases, names


def normalize_language(value):
    value = value.strip().replace("_", "-")
    parts = value.split("-")
    if not re.fullmatch(r"[a-zA-Z]{2,3}(?:-[a-zA-Z0-9]{2,8})*", value):
        raise ValueError(f"Invalid language tag '{value}'. Use an ISO code such as en or pt-BR.")
    parts[0] = language_registry()[0].get(parts[0].lower(), parts[0].lower())
    for index in range(1, len(parts)):
        parts[index] = parts[index].title() if len(parts[index]) == 4 else parts[index].upper()
    return "-".join(parts)


def base_language(value):
    return normalize_language(value).split("-")[0]


def language_from_name(name):
    return language_registry()[1].get(name.strip().casefold())


def locale_score(requested, offered):
    requested, offered = normalize_language(requested), normalize_language(offered)
    if base_language(requested) != base_language(offered):
        return -1
    if requested == offered:
        return 3
    def features(tag):
        parts = tag.split('-')[1:]
        script = next((p for p in parts if len(p) == 4), None)
        region = next((p for p in parts if len(p) == 2 or p.isdigit()), None)
        if tag.startswith('zh') and script is None:
            script = 'Hant' if region in ('TW', 'HK', 'MO') else 'Hans' if region in ('CN', 'SG') else None
        return script, region
    rs, rr = features(requested)
    oscript, region = features(offered)
    if rs and oscript and rs != oscript:
        return -1
    if rs and rs == oscript or rr and rr == region:
        return 2
    return 1


def cache_dir():
    return Path(os.environ.get("TTS_CACHE_DIR", str(Path.home() / ".cache/tts")))


def download(url, path, size=None, md5=None, sha256=None, validator=None):
    from tts_config import network_timeout
    from tts_network import retry
    path = Path(path)
    def valid(candidate):
        if not candidate.is_file() or not candidate.stat().st_size:
            return False
        if size is not None and candidate.stat().st_size != size:
            return False
        if md5 or sha256:
            digest = hashlib.md5() if md5 else hashlib.sha256()
            with candidate.open('rb') as source:
                while block := source.read(1024 * 1024):
                    digest.update(block)
            if digest.hexdigest() != (md5 or sha256):
                return False
        if md5 and sha256:
            with candidate.open('rb') as source:
                sha_hasher = hashlib.sha256()
                while block := source.read(1024 * 1024):
                    sha_hasher.update(block)
                sha = sha_hasher.hexdigest()
            if sha != sha256:
                return False
        if validator:
            try:
                validator(candidate)
            except Exception:
                return False
        return True
    if valid(path):
        return path
    path.unlink(missing_ok=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f"tts-{os.getpid()}-", suffix=".part", delete=False) as temporary:
        partial = Path(temporary.name)
    try:
        print(f"Downloading {path.name}...", file=sys.stderr)
        def transfer():
            digest = hashlib.md5()
            with urllib.request.urlopen(url, timeout=network_timeout()) as response, partial.open("wb") as output:
                while block := response.read(1024 * 1024):
                    output.write(block)
                    digest.update(block)
                output.flush()
                headers = getattr(response, 'headers', {})
                length = headers.get('Content-Length')
                if length and partial.stat().st_size != int(length):
                    raise ConnectionError('Incomplete HTTP response.')
            return digest
        digest = retry(transfer)
        if size is not None and partial.stat().st_size != size:
            raise RuntimeError(f"Incomplete download: {path.name}.")
        if md5 and digest.hexdigest() != md5:
            raise RuntimeError(f"Checksum mismatch: {path.name}.")
        if not partial.stat().st_size:
            raise RuntimeError(f"Empty download: {path.name}.")
        if sha256:
            sha = hashlib.sha256()
            with partial.open('rb') as source:
                while block := source.read(1024 * 1024):
                    sha.update(block)
            if sha.hexdigest() != sha256:
                raise RuntimeError(f"Checksum mismatch: {path.name}.")
        if validator:
            validator(partial)
        partial.replace(path)
        return path
    finally:
        partial.unlink(missing_ok=True)


def overrides():
    path = Path(os.environ.get("TTS_VOICE_CONFIG", str(Path.home() / ".config/tts/voices.json")))
    return json.loads(path.read_text()) if path.is_file() else {}


def configured_voice(engine, language, gender):
    voices = overrides().get(engine, {})
    return voices.get(normalize_language(language), voices.get(base_language(language), {})).get(gender)


def select_voice(voices, language, gender, preferred=None):
    matches = [voice for voice in voices if locale_score(language, voice["language"]) >= 0 and voice.get("gender") == gender]
    if not matches:
        unknown = [voice for voice in voices if locale_score(language, voice["language"]) >= 0 and voice.get("gender") is None]
        if unknown and os.environ.get("TTS_STRICT_GENDER", "0") != "1":
            matches = unknown
            print(f"Voice gender metadata is unavailable for {language}; gender cannot be guaranteed. "
                  "Set TTS_STRICT_GENDER=1 to require a known gender.", file=sys.stderr)
    if not matches:
        raise ValueError(f"No {gender} voice is available for '{language}' in this engine. "
                         "Choose another mode or add a voice to TTS_VOICE_CONFIG.")
    selected = sorted(matches, key=lambda v: (-locale_score(language, v["language"]), v.get("name") != preferred,
                                        v.get("quality") != "medium", v["name"]))[0]
    if '-' in normalize_language(language) and locale_score(language, selected['language']) < 3:
        print(f"Requested locale {language}; using voice locale {selected['language']}.", file=sys.stderr)
    return selected


def rhvoice_directories():
    if os.getenv("TTS_RHVOICE_VOICES"):
        return [Path(os.environ["TTS_RHVOICE_VOICES"])]
    return [Path("/usr/local/share/RHVoice/voices"), Path("/usr/share/RHVoice/voices"),
            Path.home() / ".local/share/RHVoice/voices"]


def rhvoice_catalog():
    voices = []
    for directory in rhvoice_directories():
        for info in sorted(directory.glob("*/voice.info")):
            try:
                fields = dict(line.split("=", 1) for line in info.read_text().splitlines() if "=" in line)
            except (OSError, UnicodeError):
                continue
            language = language_from_name(fields.get("language", ""))
            if language and fields.get("name"):
                voices.append({"name": fields["name"], "language": language,
                               "gender": fields.get("gender", "").lower() or None})
    return voices


# Model/speaker metadata from the upstream Silero README and models.yml.
# Unlabelled indexed voices remain gender=None rather than guessed from names.
SILERO = {
    "ru": ("ru", "v4_ru", {"female":"xenia", "male":"aidar"}),
    "en": ("en", "v3_en_indic", {"female":"tamil_female", "male":"tamil_male"}),
    "de": ("de", "v3_de", {"female":"eva_k", "male":"karlsson"}),
    "es": ("es", "v3_es", {None:"es_0"}),
    "fr": ("fr", "v3_fr", {None:"fr_0"}),
    "uk": ("ua", "v4_ua", {"male":"mykyta"}),
    "uz": ("uz", "v4_uz", {"female":"dilnavoz"}),
}
for code, speakers in {
    "av":{"female":"b_ava"}, "ba":{"male":"b_bashkir"}, "bg":{"male":"b_bulb"},
    "ce":{"male":"b_che"}, "cv":{"female":"cv_ekaterina", "male":"b_cv"},
    "myv":{"male":"b_myv"}, "xal":{"female":"kalmyk_delghir", "male":"kalmyk_erdni"},
    "krc":{"male":"b_krc"}, "kk":{"female":"kz_F1", "male":"kz_M1"},
    "kjh":{"female":"b_kjh"}, "kv":{"male":"b_kpv"}, "lez":{"male":"b_lez"},
    "mhr":{"female":"b_mhr"}, "mrj":{"male":"b_mrj"}, "nog":{"female":"b_nog"},
    "os":{"male":"b_oss"}, "tt":{"male":"marat_tt"}, "tyv":{"male":"b_tyv"},
    "udm":{"male":"b_udm"}, "sah":{"male":"b_sah"},
}.items():
    SILERO[code] = ("cyr", "v4_cyrillic", speakers)
INDIC = {"hi":("hindi", "Devanagari"), "ml":("malayalam", "Malayalam"),
         "mni":("manipuri", "Bengali"), "bn":("bengali", "Bengali"),
         "raj":("rajasthani", "Devanagari"), "ta":("tamil", "Tamil"),
         "te":("telugu", "Telugu"), "gu":("gujarati", "Gujarati"), "kn":("kannada", "Kannada")}
for code, (name, script) in INDIC.items():
    genders = ("female",) if code in ("mni", "raj") else ("female", "male")
    SILERO[code] = ("indic", "v4_indic", {gender: f"{name}_{gender}" for gender in genders})


def silero_voice(language, gender):
    custom = configured_voice("silero", language, gender)
    if custom:
        return custom
    code = base_language(language)
    if code not in SILERO:
        return extra_silero_model(language)
    folder, model, speakers = SILERO[code]
    choices = [{"name": speaker, "language": code, "gender": sex} for sex, speaker in speakers.items()]
    selected = select_voice(choices, language, gender)
    result = {"speaker": selected["name"], "model": model,
              "url": f"https://models.silero.ai/models/tts/{folder}/{model}.pt", "sample_rate":24000}
    if code in INDIC:
        result["script"] = INDIC[code][1]
    return result


def extra_silero_model(language):
    """Also route models added upstream without requiring a code update."""
    try:
        import yaml
    except ImportError:
        raise ValueError("Additional Silero models need PyYAML: python3 -m pip install PyYAML")
    revision = os.environ.get('TTS_SILERO_REVISION', 'master')
    suffix = '-' + hashlib.sha256(revision.encode()).hexdigest()[:12] if 'TTS_SILERO_REVISION' in os.environ else ''
    path = Path(os.environ.get("TTS_SILERO_INDEX", str(cache_dir() / f"silero-models{suffix}.yml")))
    if not path.is_file():
        download(f"https://raw.githubusercontent.com/snakers4/silero-models/{revision}/models.yml", path)
    models = yaml.safe_load(path.read_text())["tts_models"].get(base_language(language), {})
    candidates = []
    for name, versions in models.items():
        latest = versions.get("latest", {})
        if latest.get("package"):
            candidates.append((name, latest))
    if not candidates:
        raise ValueError(f"Silero has no model for '{language}'. Choose another engine or configure a model.")
    name, model = sorted(candidates, key=lambda row: [int(n) for n in re.findall(r"\d+", row[0])], reverse=True)[0]
    rates = model.get("sample_rate", [24000])
    rates = [rates] if isinstance(rates, int) else rates
    return {"model":name, "url":model["package"], "speaker":None,
            "sample_rate":24000 if 24000 in rates else max(rates)}


PIPER_GENDERS = {
    "irina":"female", "dmitri":"male", "denis":"male", "ruslan":"male",
    "lessac":"female", "amy":"female", "kathleen":"female", "ljspeech":"female",
    "ryan":"male", "danny":"male", "joe":"male", "alan":"male", "jenny_dioco":"female",
    "alba":"female", "cori":"female", "southern_english_female":"female",
    "eva_k":"female", "kerstin":"female", "ramona":"female", "thorsten":"male", "karlsson":"male",
    "siwis":"female", "gilles":"male",
    "kareem":"male", "dimitar":"male", "upc_ona":"female", "upc_pau":"male",
    "jirka":"male", "rapunzelina":"female", "harri":"male", "norman":"male",
    "riccardo":"male", "paola":"female", "faber":"male", "gugusse":"male",
    "natasa":"female", "hfc_female":"female", "hfc_male":"male",
}


def piper_directories():
    if os.getenv("TTS_PIPER_VOICES"):
        return [Path(os.environ["TTS_PIPER_VOICES"])]
    return [Path.home() / "piper/voices", cache_dir() / "piper",
            Path.home() / ".local/share/piper", Path.home() / ".local/share/com.tauri.shiori/piper_voices"]


def local_piper_catalog():
    voices = []
    for directory in piper_directories():
        for config in sorted(directory.glob("**/*.onnx.json")):
            model = Path(str(config)[:-5])
            if not model.is_file():
                continue
            try:
                info = json.loads(config.read_text())
                name = model.stem
                speaker = info.get("dataset", name.split("-")[1] if "-" in name else name)
                language = normalize_language(info.get("language", {}).get("code", name.split("-")[0]))
                gender = info.get("gender", PIPER_GENDERS.get(speaker))
                gender = gender.lower() if isinstance(gender, str) else None
            except (OSError, ValueError, TypeError, AttributeError):
                continue  # A broken optional model must not break help or other voices.
            if info.get("num_speakers", 1) == 1:
                voices.append({"name":name, "language":language, "gender":gender,
                               "quality":info.get("audio", {}).get("quality"), "path":str(model)})
            else:
                for speaker, identifier in info.get("speaker_id_map", {}).items():
                    voices.append({"name":f"{name}:{speaker}", "language":language,
                                   "gender":None, "path":str(model), "speaker_id":identifier})
    return voices


def piper_voice(language, gender, preferred):
    custom = configured_voice("piper", language, gender)
    if custom:
        return custom
    local = [v for v in local_piper_catalog() if locale_score(language, v["language"]) >= 0]
    revision = os.environ.get('TTS_PIPER_REVISION', 'main')
    suffix = '-' + hashlib.sha256(revision.encode()).hexdigest()[:12] if 'TTS_PIPER_REVISION' in os.environ else ''
    index_path = Path(os.environ.get("TTS_PIPER_INDEX", str(cache_dir() / f"piper-voices{suffix}.json")))
    # An exact installed locale needs no network. A cached catalog participates
    # in ranking before falling back to a different region.
    if not index_path.is_file() and any(v.get('gender') == gender and (locale_score(language, v['language']) == 3 or '-' not in normalize_language(language)) for v in local):
        return select_voice(local, language, gender, preferred)
    try:
        if not index_path.is_file():
            download(f"https://huggingface.co/rhasspy/piper-voices/resolve/{revision}/voices.json", index_path)
        index = json.loads(index_path.read_text())
    except OSError:
        if local:
            return select_voice(local, language, gender, preferred)
        raise
    voices = []
    for key, voice in index.items():
        language_code = voice["language"]["code"]
        if locale_score(language, language_code) < 0:
            continue
        item = {"name":key, "language":language_code, "gender":voice.get("gender", PIPER_GENDERS.get(voice["name"])),
                "quality":voice.get("quality"), "files":voice["files"]}
        if voice.get("num_speakers", 1) == 1:
            voices.append(item)
        else:
            for speaker, identifier in voice.get("speaker_id_map", {}).items():
                sex = "female" if "female" in speaker else "male" if "male" in speaker else None
                voices.append({**item, "name":f"{key}:{speaker}", "gender":sex, "speaker_id":identifier})
    selected = select_voice(voices + local, language, gender, preferred)
    if "path" in selected:
        return selected
    for filename, metadata in selected["files"].items():
        if filename.endswith((".onnx", ".onnx.json")):
            destination = cache_dir() / "piper" / Path(filename).name
            download(f"https://huggingface.co/rhasspy/piper-voices/resolve/{revision}/" + filename,
                     destination, metadata.get("size_bytes"), metadata.get("md5_digest"))
            if filename.endswith(".onnx"):
                selected["path"] = str(destination)
    return selected


def module_present(name):
    try:
        return importlib.util.find_spec(name) is not None
    except (ModuleNotFoundError, ValueError):
        return False


def engine_status():
    rhvoices = rhvoice_catalog()
    pipervoices = local_piper_catalog()
    piper = shutil.which("piper") or (str(Path.home() / "piper/piper/piper") if os.access(Path.home() / "piper/piper/piper", os.X_OK) else None)
    rh_languages = ", ".join(sorted({v["language"] for v in rhvoices})) or "no voices"
    piper_languages = ", ".join(sorted({v["language"] for v in pipervoices})) or "models download on demand"
    return [
        ("RHVoice", "ready" if shutil.which("RHVoice-test") and rhvoices else "missing", f"{len(rhvoices)} voices; {rh_languages}"),
        ("Edge", "installed" if module_present("edge_tts") else "missing", "online; voice list from Microsoft"),
        ("Silero", "installed" if module_present("torch") else "missing", "local; language models download on demand"),
        ("Piper", "installed" if piper else "missing", f"{len(pipervoices)} local voices; {piper_languages}"),
        ("Google", "built in", "online; free Translate TTS; gender not selectable"),
        ("mpv", "installed" if shutil.which("mpv") else "missing", "audio player"),
        ("ffmpeg", "installed" if shutil.which("ffmpeg") else "missing", "PCM conversion for all engines"),
    ]
