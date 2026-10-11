"""Check all bundled Silero routes with real pinned models; emits WAVs and JSON."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch
import wave

import tts_engines
import tts_voices


SAMPLES = {
    'v3_en':'This is a short English speech test.',
    'v3_de':'Guten Tag.', 'v3_es':'Hola.', 'v3_fr':'Bonjour.',
    'v4_ru':'Привет.', 'v4_ua':'Добрий день.', 'v4_uz':'Salom.',
    # A shared supported-token probe verifies speaker routing, not fluency
    # in each language of the Cyrillic multilingual model.
    'v4_cyrillic':'Тест.',
}
INDIC_SAMPLES = {
    'hi':'नमस्ते।', 'raj':'नमस्ते।', 'ml':'നമസ്കാരം.', 'mni':'নমস্কার.',
    'bn':'নমস্কার।', 'ta':'வணக்கம்.', 'te':'నమస్కారం.',
    'gu':'નમસ્તે.', 'kn':'ನಮಸ್ಕಾರ.',
}


def run_model(model_id, output):
    output.mkdir(parents=True, exist_ok=True)
    rows = []
    model = None
    for language, (_, name, speakers) in tts_voices.SILERO.items():
        if name != model_id:
            continue
        for gender, speaker in speakers.items():
            spec = tts_voices.silero_voice(language, gender or 'female')
            if model is None:
                model = tts_engines.load_silero(spec)
            assert speaker in model.speakers, (language, speaker)
            # Reuse the already verified package within this model's child
            # process; exercise the production selector and generate method.
            with patch.object(tts_engines, 'load_silero', return_value=model):
                synth = tts_engines.Synthesizer(6 if gender == 'male' else 5, language)
            assert synth.voice == speaker and synth.spec['model'] == model_id
            text = INDIC_SAMPLES[language] if language in INDIC_SAMPLES else SAMPLES[model_id]
            generated = synth.generate(text, output)
            target = output / f'{language}-{speaker}.wav'
            generated.replace(target)
            with wave.open(str(target), 'rb') as audio:
                assert audio.getnchannels() == 1 and audio.getsampwidth() == 2
                assert audio.getframerate() == 24000 and audio.getnframes() > 0
                pcm = audio.readframes(audio.getnframes())
                assert any(pcm), (language, speaker)
                seconds = audio.getnframes() / 24000
            rows.append({'language':language, 'speaker':speaker, 'gender':gender,
                         'model':model_id, 'model_sha256':spec['sha256_digest'],
                         'text':text, 'seconds':seconds, 'file':target.name})
            print(language, speaker, round(seconds, 3), flush=True)
    assert rows, model_id
    (output / f'{model_id}.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2)+'\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--model', help='Run one model in a separate process')
    args = parser.parse_args()
    if args.model:
        run_model(args.model, args.output)
        return
    # Independent processes release all model/runtime memory between packages.
    models = sorted({name for _, name, _ in tts_voices.SILERO.values()})
    rows = []
    environment = {**os.environ, 'TTS_STRICT_GENDER':'0',
                   'TTS_VOICE_CONFIG':str(args.output.resolve() / 'no-overrides.json')}
    for model in models:
        subprocess.run([sys.executable, str(Path(__file__).resolve()), '--output',
                        str(args.output.resolve()), '--model', model],
                       env=environment, check=True, timeout=300)
        rows.extend(json.loads((args.output / f'{model}.json').read_text()))
    report = {'models':len(models), 'languages':len(tts_voices.SILERO),
              'routes':len(rows), 'manual_listening':'pending', 'cases':rows}
    (args.output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    print(f'Checked {len(rows)} speaker routes in {len(models)} models / {len(tts_voices.SILERO)} languages.')


if __name__ == '__main__':
    main()
