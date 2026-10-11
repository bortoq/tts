"""The checked-in listening corpus must match its recorded lossless PCM hashes."""
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from tts_pipeline import AudioWorker


class CorpusTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('ffmpeg'), 'ffmpeg is required')
    def test_recordings_match_their_pcm_hashes(self):
        root = Path(__file__).with_name('testdata')/'silero/audio'
        report = json.loads((root/'report.json').read_text())
        self.assertEqual(len(report['cases']),16)
        for case in report['cases']:
            for recording in case['audio'].values():
                result = subprocess.run(['ffmpeg','-v','error','-i',str(root/recording['file']),
                                         '-f','s16le','-ac','1','-ar','24000','pipe:1'],
                                        capture_output=True,check=True,timeout=30)
                self.assertEqual(hashlib.sha256(result.stdout).hexdigest(),recording['pcm_sha256'])
                self.assertAlmostEqual(len(result.stdout)/48000,case['seconds'])

    def test_worker_notices_are_reported_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            (root/'notices.json').write_text(json.dumps(['Updating saved speech.','Using standard synthesis.']))
            worker=AudioWorker(root,1)
            output=io.StringIO()
            with patch('sys.stderr',output):
                worker.report_notices()
                worker.report_notices()
            self.assertEqual(output.getvalue().splitlines(),['Updating saved speech.','Using standard synthesis.'])
