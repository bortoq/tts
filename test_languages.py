"""Language and voice routing tests use local catalogs, with no network calls."""
import importlib.util
import asyncio
import base64
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import urllib.parse

import tts
import tts_engines as engines
import tts_text
import tts_network
import urllib.request
import tts_google
import tts_voices


class LanguageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.environment = patch.dict(os.environ, {"TTS_CACHE_DIR": str(self.directory / "cache"),
                                                 "TTS_VOICE_CONFIG": str(self.directory / "no-overrides.json")})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_iso_codes_regions_and_scripts(self):
        for source, expected in {"eng-US":"en-US", "fra":"fr", "ger":"de", "zho":"zh",
                                 "PT_pt":"pt-PT", "zh_hant_tw":"zh-Hant-TW", "iw":"he", "ua":"uk"}.items():
            self.assertEqual(tts.normalize_lang(source), expected)
        for invalid in ("", "english", "../ru", "en--US"):
            with self.assertRaises(ValueError):
                tts.normalize_lang(invalid)

    def test_fb2_routes_the_selected_engine_and_preserves_locale(self):
        source = self.directory / "book.fb2"
        source.write_text('<FictionBook><description><title-info><lang>pt_PT</lang>'
                          '</title-info></description><body><p>Sample text.</p></body></FictionBook>')
        with patch.object(tts, "player_command", return_value=["mpv"]), \
             patch.object(tts, "read_aloud") as stream, \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(tts.main(["8", str(source)]), 0)
            self.assertEqual(stream.call_args.args[:4], (source, 8, "pt-PT", "Sample text."))

    def test_language_override_can_appear_in_any_position(self):
        source = self.directory / "notes.txt"
        source.write_text("Sample text.")
        for args in (["-l", "en_GB", "9", str(source)],
                     [str(source), "-l", "en_GB", "9"],
                     [str(source), "9", "-l", "en_GB"]):
            self.assertEqual(tts.parse_args(args), (source, 9, "en-GB", None))

    def test_language_override_changes_the_voice_request(self):
        source = self.directory / "notes.txt"
        source.write_text("Sample text.")
        with patch.object(tts, "player_command", return_value=["mpv"]), \
             patch.object(tts, "read_aloud") as stream, contextlib.redirect_stderr(io.StringIO()):
            tts.main([str(source), "9", "-l", "de"])
            self.assertEqual(stream.call_args.args[1:3], (9, "de"))

    def test_speed_and_language_options_can_appear_in_any_order(self):
        source = self.directory / "notes.txt"
        source.write_text("Sample text.")
        for args in (["4", "-s", "2.3", str(source), "-l", "en"],
                     [str(source), "-l", "en", "-s", "2.3", "4"],
                     ["-s", "2.3", "-l", "en", "4", str(source)]):
            self.assertEqual(tts.parse_args(args), (source, 4, "en", 2.3))

    def test_invalid_speed_and_old_language_option_are_rejected(self):
        source = self.directory / "notes.txt"
        source.write_text("Sample text.")
        with contextlib.redirect_stderr(io.StringIO()):
            for speed in ("0", "-1", "nan", "inf", "101", "fast"):
                with self.assertRaises(SystemExit) as stopped:
                    tts.parse_args([str(source), "-s", speed])
                self.assertEqual(stopped.exception.code, 2)
            with self.assertRaises(SystemExit):
                tts.parse_args([str(source), "--lang", "en"])
            with self.assertRaises(SystemExit):
                tts.parse_args(["-s", "2.3"])

    def test_speed_reaches_both_playback_paths(self):
        source = self.directory / "notes.txt"
        source.write_text("Sample text.")
        for mode in (1, 9):
            with patch.object(tts, "player_command", return_value=["mpv"]), \
                 patch.object(tts, "read_aloud") as stream, contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(tts.main([str(source), str(mode), "-s", "2.3"]), 0)
                self.assertEqual(stream.call_args.args[-1], 2.3)

    @unittest.skipUnless(importlib.util.find_spec('edge_tts'), 'edge_tts optional dependency is required')
    def test_edge_prefers_region_before_default_voice(self):
        import edge_tts
        catalog = [
            {"ShortName":"en-US-Female", "Locale":"en-US", "Gender":"Female"},
            {"ShortName":"en-GB-Female", "Locale":"en-GB", "Gender":"Female"},
            {"ShortName":"en-GB-Male", "Locale":"en-GB", "Gender":"Male"},
            {"ShortName":"zh-CN-Female", "Locale":"zh-CN", "Gender":"Female"},
            {"ShortName":"zh-TW-Female", "Locale":"zh-TW", "Gender":"Female"},
        ]
        with patch.object(edge_tts, "list_voices", new=AsyncMock(return_value=catalog)):
            self.assertEqual(asyncio.run(engines.choose_edge("en-GB", "female", "en-US-Female")), "en-GB-Female")
            self.assertEqual(asyncio.run(engines.choose_edge("en-GB", "male", "unused")), "en-GB-Male")
            self.assertEqual(asyncio.run(engines.choose_edge("zh-Hant", "female", "unused")), "zh-TW-Female")
            with self.assertRaises(ValueError):
                asyncio.run(engines.choose_edge("ja", "female", "unused"))

    def test_silero_routes_all_bundled_language_models(self):
        for language, (folder, model, speakers) in tts_voices.SILERO.items():
            for gender, speaker in speakers.items():
                if gender is None:
                    continue
                with self.subTest(language=language, gender=gender):
                    spec = tts_voices.silero_voice(language, gender)
                    self.assertEqual(spec["speaker"], speaker)
                    self.assertEqual(spec["url"], f"https://models.silero.ai/models/tts/{folder}/{model}.pt")
        self.assertEqual(tts_voices.silero_voice("en-US", "female")["speaker"], "tamil_female")
        self.assertEqual(tts_voices.silero_voice("uk", "male")["speaker"], "mykyta")
        self.assertEqual(tts_voices.silero_voice("hi", "female")["script"], "Devanagari")

    def test_silero_uses_the_selected_non_russian_model(self):
        with patch.object(engines, "load_silero", return_value=MagicMock()) as load:
            synth = engines.Synthesizer(6, "de-DE")
            self.assertEqual(synth.voice, "karlsson")
            self.assertEqual(load.call_args.args[0]["model"], "v3_de")
            self.assertNotIn("v4_ru", load.call_args.args[0]["url"])

    @unittest.skipUnless(importlib.util.find_spec('yaml'), 'yaml optional dependency is required')
    def test_new_silero_languages_use_the_upstream_index(self):
        index = self.directory / "models.yml"
        index.write_text('tts_models:\n  new:\n    v6_new:\n      latest:\n        package: https://example.invalid/new.pt\n        sample_rate: [16000, 24000]\n')
        with patch.dict(os.environ, {"TTS_SILERO_INDEX":str(index)}), patch.object(tts_voices, "download") as download:
            spec = tts_voices.silero_voice("new", "male")
            self.assertEqual(spec["url"], "https://example.invalid/new.pt")
            self.assertEqual(spec["sample_rate"], 24000)
            download.assert_not_called()
            with self.assertRaisesRegex(ValueError, "no model"):
                tts_voices.silero_voice("zz", "male")

    def test_unknown_gender_is_reported_and_can_be_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()) as output:
            self.assertEqual(tts_voices.silero_voice("fr", "female")["speaker"], "fr_0")
            self.assertIn("gender cannot be guaranteed", output.getvalue())
        with patch.dict(os.environ, {"TTS_STRICT_GENDER":"1"}):
            with self.assertRaises(ValueError):
                tts_voices.silero_voice("fr", "female")
        # A known opposite gender is never substituted.
        with self.assertRaises(ValueError):
            tts_voices.silero_voice("uk", "female")

    def create_piper_model(self, name, language, gender):
        model = self.directory / f"{name}.onnx"
        model.write_bytes(b"test model")
        Path(str(model) + ".json").write_text(json.dumps({"language":{"code":language}, "gender":gender,
                                                        "dataset":"sample", "num_speakers":1}))
        return model

    def test_piper_selects_installed_models_by_language_and_gender(self):
        female = self.create_piper_model("en_GB-sample-medium", "en_GB", "female")
        male = self.create_piper_model("fr_FR-sample-medium", "fr_FR", "male")
        with patch.dict(os.environ, {"TTS_PIPER_VOICES":str(self.directory)}), \
             patch.object(tts_voices, "download") as download:
            self.assertEqual(tts_voices.piper_voice("en-GB", "female", "unused")["path"], str(female))
            self.assertEqual(tts_voices.piper_voice("fr", "male", "unused")["path"], str(male))
            download.assert_not_called()

    def test_piper_downloads_the_matching_model_not_russian(self):
        index = self.directory / "index.json"
        index.write_text(json.dumps({"en_GB-alba-medium":{
            "name":"alba", "language":{"code":"en_GB"}, "quality":"medium", "num_speakers":1,
            "files":{"en/en_GB/alba/medium/en_GB-alba-medium.onnx":{},
                     "en/en_GB/alba/medium/en_GB-alba-medium.onnx.json":{}}}}))
        def retrieve(url, path, *args):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"downloaded model")
            return path
        with patch.dict(os.environ, {"TTS_PIPER_INDEX":str(index), "TTS_PIPER_VOICES":str(self.directory / "empty")}), \
             patch.object(tts_voices, "download", side_effect=retrieve) as download:
            result = tts_voices.piper_voice("en-GB", "female", "ru_RU-irina-medium")
            self.assertEqual(result["name"], "en_GB-alba-medium")
            self.assertEqual(download.call_count, 2)
            self.assertTrue(all("/en/" in call.args[0] for call in download.call_args_list))
            self.assertTrue(Path(result["path"]).is_file())

    def test_piper_routes_multi_speaker_models(self):
        index = self.directory / "index.json"
        index.write_text(json.dumps({"bn_BD-sample-medium":{
            "name":"sample", "language":{"code":"bn_BD"}, "num_speakers":2,
            "speaker_id_map":{"female":0, "male":1}, "files":{
                "bn/bn_BD/sample/medium/bn_BD-sample-medium.onnx":{},
                "bn/bn_BD/sample/medium/bn_BD-sample-medium.onnx.json":{}}}}))
        with patch.dict(os.environ, {"TTS_PIPER_INDEX":str(index), "TTS_PIPER_VOICES":str(self.directory / "empty")}), \
             patch.object(tts_voices, "download"):
            self.assertEqual(tts_voices.piper_voice("bn", "male", "unused")["speaker_id"], 1)

    def test_explicit_voice_configuration(self):
        config = self.directory / "voices.json"
        config.write_text(json.dumps({"silero":{"fr":{"female":{"model":"custom", "speaker":"known_female",
                                                                     "url":"https://example.invalid/model.pt"}}}}))
        with patch.dict(os.environ, {"TTS_VOICE_CONFIG":str(config)}):
            self.assertEqual(tts_voices.silero_voice("fr-CA", "female")["speaker"], "known_female")

    def test_help_lists_local_engines_without_network(self):
        with patch.object(urllib.request, "urlopen") as network, contextlib.redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(SystemExit) as stopped:
                tts.parse_args(["--help"])
            self.assertEqual(stopped.exception.code, 0)
            for engine in ("RHVoice", "Edge", "Silero", "Piper", "Google", "mpv", "ffmpeg"):
                self.assertIn(engine, output.getvalue())
            self.assertIn("Detected on this system", output.getvalue())
            self.assertIn("  9       Google", output.getvalue())
            self.assertNotIn("9 / 10", output.getvalue())
            network.assert_not_called()

    def test_broken_piper_config_does_not_break_detection(self):
        model = self.directory / "broken.onnx"
        model.write_bytes(b"model")
        Path(str(model) + ".json").write_text("{not valid JSON")
        with patch.dict(os.environ, {"TTS_PIPER_VOICES":str(self.directory)}):
            self.assertEqual(tts_voices.local_piper_catalog(), [])
            self.assertEqual(len(tts_voices.engine_status()), 7)

    def test_download_checks_size_hash_and_removes_partial_files(self):
        payload = b"a small model fixture"
        path = self.directory / "model.onnx"
        with patch.object(tts_network.urllib.request, "urlopen", side_effect=lambda *a, **k: io.BytesIO(payload)), \
             contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "Incomplete download"):
                tts_voices.download("https://example.invalid/model", path, size=999)
            self.assertFalse(path.exists())
            self.assertFalse(list(self.directory.glob("*.part")))
            with self.assertRaisesRegex(RuntimeError, "Checksum mismatch"):
                tts_voices.download("https://example.invalid/model", path, md5="invalid")
            self.assertFalse(path.exists())
            tts_voices.download("https://example.invalid/model", path, len(payload), hashlib.md5(payload).hexdigest())
            self.assertEqual(path.read_bytes(), payload)


class GoogleTests(unittest.TestCase):
    def test_complete_sentences_are_not_cut_to_fill_a_request(self):
        first = "A short sentence."
        second = "This longer sentence should stay in one request because it fits the service limit on its own."
        self.assertLessEqual(len(second), 100)
        self.assertGreater(len(first + " " + second), 100)
        self.assertEqual(list(tts_google.text_parts(first + " " + second)), [first, second])

    def test_long_sentences_split_at_clauses_before_word_boundaries(self):
        first = "This first clause makes a clear point,"
        second = "while the longer second clause explains enough details to take the sentence past the service limit."
        text = first + " " + second
        parts = list(tts_google.text_parts(text))
        self.assertEqual(parts[0], first)
        self.assertTrue(all(len(part) <= 100 for part in parts))
        self.assertEqual(" ".join(parts), text)

    def test_closing_quotes_initials_abbreviations_and_cjk(self):
        for text, expected in (
            ('"Hello!" "Goodbye?"', ['"Hello!"', '"Goodbye?"']),
            ('Dr. Smith met A. Brown. They talked.', ['Dr. Smith met A. Brown.', 'They talked.']),
            ('The value is 3.14. This is a new sentence.', ['The value is 3.14.', 'This is a new sentence.']),
            ('你好。世界！', ['你好。', '世界！']),
        ):
            self.assertEqual(list(tts_text.sentences(text)), expected)

    def test_short_sentences_share_a_request_without_cutting_the_next(self):
        self.assertEqual(list(tts_google.text_parts("First sentence. Second sentence. Third sentence.")),
                         ["First sentence. Second sentence. Third sentence."])

    def test_all_google_language_codes_and_aliases(self):
        for code in tts_google.LANGUAGES:
            self.assertIn(tts_google.language_code(code), tts_google.LANGUAGES)
        for code, expected in {"he":"iw", "jv":"jw", "fil":"tl", "pt-PT":"pt-PT",
                               "pt-BR":"pt", "zh-Hant-TW":"zh-TW", "eng-US":"en"}.items():
            self.assertEqual(tts_google.language_code(code), expected)
        with self.assertRaises(ValueError):
            tts_google.language_code("zz")

    def test_rpc_decodes_audio_and_splits_long_text(self):
        payload = b"ID3" + b"sample MP3 bytes" * 8
        frames = [["wrb.fr", "jQ1olc", json.dumps([base64.b64encode(payload).decode()]), None]]
        response = (")]}'\n\n123\n" + json.dumps(frames) + "\n").encode()
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(tts_google.urllib.request, "urlopen", side_effect=lambda *a, **k: io.BytesIO(response)) as request, \
             contextlib.redirect_stderr(io.StringIO()):
            synth = engines.Synthesizer(9, "fr-CA")
            self.assertEqual(synth.voice, "fr-CA")
            output = synth.generate("A longer sample sentence. " * 8, Path(temporary))
            self.assertTrue(output.read_bytes().startswith(payload))
            self.assertGreater(request.call_count, 2)
            for call in request.call_args_list:
                fields = urllib.parse.parse_qs(call.args[0].data.decode())
                batch = json.loads(fields["f.req"][0])
                arguments = json.loads(batch[0][0][1])
                self.assertEqual(arguments[1], "fr-CA")
                self.assertLessEqual(len(arguments[0]), 100)
                self.assertIn("timeout", call.kwargs)

    def test_google_rejects_error_pages_and_empty_responses(self):
        for response in (b"<html>Service error</html>", b"", b"[]\n"):
            with self.assertRaisesRegex(RuntimeError, "no speech audio"):
                tts_google.response_audio(response)


if __name__ == "__main__":
    unittest.main()
