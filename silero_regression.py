"""Real pinned-model regression: python silero_regression.py --output DIR."""
import argparse
import hashlib
import json
import platform
from pathlib import Path
import subprocess
import tempfile
from unittest.mock import patch


def run(output):
    import torch
    import tts_silero
    from tts_engines import Synthesizer
    from tts_pronunciation import SileroPronunciation
    cases = json.loads(Path(__file__).with_name('testdata').joinpath('silero/cases.json').read_text())
    output.mkdir(parents=True, exist_ok=True)
    accent = SileroPronunciation()
    rows = []
    original = tts_silero.quiet_pause_mel
    def checked(mel, durations, word_mask, *args):
        corrected = original(mel, durations, word_mask, *args)
        assert corrected.shape == mel.shape and torch.isfinite(corrected).all()
        position = 0
        for duration, spoken in zip(durations.flatten().tolist(), word_mask):
            end = position + int(duration)
            if spoken:
                assert torch.equal(mel[:, :, position:end], corrected[:, :, position:end])
            position = end
        return corrected
    for mode in (5, 6):
        synth = Synthesizer(mode, 'ru')
        for case in cases:
            marked = accent(case['text'])
            with torch.inference_mode(), patch.object(tts_silero, 'quiet_pause_mel', side_effect=checked):
                if synth.pause_adapter:
                    audio, reference = synth.pause_adapter.generate(marked, synth.voice, 24000, reference=True)
                    native = synth.model.apply_tts(text=marked, speaker=synth.voice, sample_rate=24000)
                    assert torch.equal(reference, native), 'reference differs from native synthesis'
                else:
                    audio = reference = synth.model.apply_tts(text=marked, speaker=synth.voice, sample_rate=24000)
            assert audio.shape == reference.shape and torch.isfinite(audio).all()
            assert audio.numel() and float(audio.square().mean().sqrt()) > 1e-4
            row = {'id':case['id'], 'speaker':synth.voice, 'seconds':audio.numel()/24000, 'audio':{}}
            for kind, data in [('before',reference),('after',audio)]:
                pcm = (data.clamp(-1,1)*32767).to(torch.int16).numpy().astype('<i2').tobytes()
                path = output / f'{synth.voice}-{case["id"]}-{kind}.flac'
                subprocess.run(['ffmpeg','-v','error','-y','-f','s16le','-ar','24000','-ac','1',
                                '-i','pipe:0',str(path)], input=pcm, check=True, timeout=30)
                row['audio'][kind] = {'file':path.name,'pcm_sha256':hashlib.sha256(pcm).hexdigest()}
            row['changed'] = not torch.equal(audio,reference)
            rows.append(row)
            print(json.dumps(row,ensure_ascii=False),flush=True)
        if synth.pause_adapter:
            with tempfile.TemporaryDirectory() as temporary, \
                 patch.object(synth.pause_adapter, 'generate', side_effect=ValueError('Injected alignment failure')):
                synth.reference_requested = True
                speech = synth.generate(accent(cases[0]['text']), Path(temporary))
                assert synth.last_processing_revision == tts_silero.UNCORRECTED_REVISION
                assert speech.read_bytes() == synth.reference_file.read_bytes()
    report = {'torch':torch.__version__, 'python':platform.python_version(),
              'model_sha256':synth.spec['sha256_digest'], 'adapter':tts_silero.MEL_PAUSE_REVISION,
              'fallback_checked':True, 'manual_listening':'pending', 'cases':rows}
    (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    run(parser.parse_args().output)
