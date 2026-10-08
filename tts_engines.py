"""Speech engine adapters; no player or command-line dependencies."""
import asyncio
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import wave

import tts_google
import tts_voices
from tts_config import MODES, network_timeout, executable
from tts_voices import normalize_language as normalize_lang
from tts_network import retry

def run(command, **kwargs):
    result = subprocess.run(command, check=False, stderr=subprocess.PIPE, **kwargs)
    if result.returncode:
        message = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"{Path(command[0]).name}: {message or 'exit code ' + str(result.returncode)}")


def choose_rhvoice(lang, gender, preferred):
    custom = tts_voices.configured_voice("rhvoice", lang, gender)
    if custom:
        return custom["name"]
    return tts_voices.select_voice(tts_voices.rhvoice_catalog(), lang, gender, preferred)["name"]


async def choose_edge(lang, gender, preferred):
    import edge_tts
    custom = tts_voices.configured_voice("edge", lang, gender)
    if custom:
        return custom["name"]
    if lang == "ru":
        return preferred
    voices = await asyncio.wait_for(edge_tts.list_voices(), timeout=network_timeout())
    catalog = [{"name":v["ShortName"], "language":v["Locale"], "gender":v["Gender"].lower()} for v in voices]
    return tts_voices.select_voice(catalog, lang, gender, preferred)["name"]


def silero_path(spec):
    revision = hashlib.sha256(spec['url'].encode()).hexdigest()[:12]
    return tts_voices.cache_dir() / f"{spec['model']}-{revision}.pt"


def load_silero(spec):
    import torch
    model_path = silero_path(spec)
    torch.set_num_threads(min(4, os.cpu_count() or 1))
    loaded = []
    def validate(candidate):
        model = torch.package.PackageImporter(str(candidate)).load_pickle("tts_models", "model")
        model.to(torch.device("cpu"))
        loaded.append(model)
    # Cached files and new downloads both pass package loading before use.
    # Downloaded candidates are validated before their atomic final rename.
    tts_voices.download(spec['url'], model_path, spec.get('size_bytes'), spec.get('md5_digest'),
                        sha256=spec.get('sha256_digest'), validator=validate)
    return loaded[-1]


class Synthesizer:
    def __init__(self, mode, lang):
        lang = normalize_lang(lang)
        self.language = lang
        self.engine, gender, self.voice = MODES[mode]
        self.model = None
        if self.engine == "RHVoice":
            self.binary = executable("RHVoice-test")
            self.voice = choose_rhvoice(lang, gender, self.voice)
        elif self.engine == "Edge":
            self.voice = retry(lambda: asyncio.run(choose_edge(lang, gender, self.voice)))
        elif self.engine == "Silero":
            self.spec = tts_voices.silero_voice(lang, gender)
            self.voice = self.spec["speaker"]
            self.sample_rate = self.spec.get("sample_rate", 24000)
            if self.spec.get("script"):
                try:
                    from aksharamukha import transliterate
                except ImportError:
                    raise ValueError("Indic Silero voices need aksharamukha. Install it with: python3 -m pip install aksharamukha")
                self.transliterate = transliterate
            self.model = load_silero(self.spec)
            if self.voice is None:
                speakers = getattr(self.model, "speakers", [])
                if isinstance(speakers, (list, tuple)) and speakers:
                    choices = [{"name":speaker, "language":lang,
                                "gender":"female" if "female" in speaker else "male" if "male" in speaker else None}
                               for speaker in speakers if speaker != "random"]
                    selected = tts_voices.select_voice(choices, lang, gender)
                    self.voice = selected["name"]
                else:
                    if os.environ.get("TTS_STRICT_GENDER", "0") == "1":
                        raise ValueError(f"Silero has no gender metadata for '{lang}'.")
                    print(f"Silero has no gender metadata for '{lang}'; using the model's default voice.", file=sys.stderr)
                    self.voice = "default"
        elif self.engine == "Piper":
            self.binary = executable("piper", Path.home() / "piper/piper/piper")
            self.spec = tts_voices.piper_voice(lang, gender, self.voice)
            self.voice = self.spec.get("name", Path(self.spec["path"]).stem)
            self.model = Path(self.spec["path"]).expanduser()
            if not self.model.is_file() or not Path(str(self.model) + ".json").is_file():
                raise ValueError(f"Piper model or its JSON config is missing: {self.model}")
        elif self.engine == "Google":
            self.voice = tts_google.language_code(lang)
            print("Google Translate TTS uses the service's voice; gender cannot be selected.", file=sys.stderr)

    def generate(self, text, directory):
        output = directory / ("speech.mp3" if self.engine in ("Edge", "Google") else "speech.wav")
        output.unlink(missing_ok=True)
        if self.engine == "RHVoice":
            source = directory / "text.txt"
            source.write_text(text, encoding="utf-8")
            run([self.binary, "-p", self.voice, "-i", str(source), "-o", str(output)], timeout=180)
        elif self.engine == "Piper":
            command = [self.binary, "--model", str(self.model), "--output_file", str(output)]
            if "speaker_id" in self.spec:
                command += ["--speaker", str(self.spec["speaker_id"])]
            run(command,
                input=(text + "\n").encode("utf-8"), stdout=subprocess.DEVNULL, timeout=180)
        elif self.engine == "Edge":
            import edge_tts
            async def save():
                await asyncio.wait_for(edge_tts.Communicate(text, self.voice).save(str(output)), timeout=network_timeout())
            retry(lambda: asyncio.run(save()))
        elif self.engine == "Google":
            tts_google.save(text, self.voice, output, network_timeout())
        else:
            import torch
            if self.spec.get("script"):
                options = {"pre_options":["TamilTranscribe"]} if self.spec["script"] == "Tamil" else {}
                text = self.transliterate.process(self.spec["script"], "ISO", text, **options)
            with torch.inference_mode():
                arguments = {"text":text, "sample_rate":self.sample_rate}
                if self.voice != "default":
                    arguments["speaker"] = self.voice
                audio = self.model.apply_tts(**arguments)
            samples = (audio.detach().cpu().clamp(-1, 1) * 32767).to(torch.int16).numpy().astype("<i2").tobytes()
            with wave.open(str(output), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(self.sample_rate)
                wav.writeframes(samples)
        if not output.is_file() or output.stat().st_size < 44:
            raise RuntimeError(f"{self.engine}: no audio was generated")
        return output


