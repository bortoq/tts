# TTS READER — read FB2, FB2.ZIP, and TXT books aloud

```bash
tts book.fb2
tts 4 -s 2.3 book.fb2.zip
tts notes.txt 9
tts -l en-GB notes.txt 7
tts --help
```

Pass a file and, if needed, a mode number. Their order does not matter.
Mode 1 is the default. For a file named after a number, use a path such as `./9`.
Help shows engines, the player, and the converter found on this computer. These
are local checks; an installed client does not mean its service is reachable.

| Female mode | Male mode | Engine |
| --- | --- | --- |
| 1 | 2 | RHVoice; Anna / Aleksandr for Russian |
| 3 | 4 | Microsoft Edge TTS |
| 5 | 6 | Silero |
| 7 | 8 | Piper |
| 9 | — | Free Google Translate TTS |

Google Translate uses mode 9 and does not let clients select gender.
No Google Cloud account is needed. The
built-in client uses the public speech RPC used by
[gTTS](https://github.com/pndurette/gTTS); no extra package is required.
It sends text to Google. Edge sends text to Microsoft. Both need internet.

## Playback

Space pauses or resumes. `]` speeds up, `[` slows down, and `q` or `Ctrl+C` quits.
Run `tts` in an interactive terminal to use the keys. Set the initial speed with
`-s NUMBER`: `-s 2.3` plays at 2.3 times the normal speed; `-s 0.8` slows down.
The accepted range is 0.01 to 100. When resuming, the last playback speed is
restored, including when the same command contains `-s`; new readings start
at the requested speed or 1 if omitted.
Keyboard speed controls continue to work after setting the initial speed.
During preparation, a spinner appears after the engine, voice, and speed line
until the first audio is ready. mpv retains its normal terminal output.
`I`, `Ctrl+I` (or Tab) toggles mpv's native information panel.
`O` or `Ctrl+O` toggles mpv's playback status display.

RHVoice synthesizes the whole book as one continuous utterance. Its native stream
passes through ffmpeg to normalize the audio. All engines feed one continuous
24 kHz mono PCM stream to mpv; the player never restarts between pieces.

The initial buffer holds eight seconds at the chosen playback speed: at 2.3x it
holds 18.4 seconds of source audio. The producer queue holds about twice that
amount. Keyboard speed changes also update the queue target. mpv adds its own
cache. Buffering cannot overcome a service
that stays slower than playback.

Synthesis runs in a separate process. `q` and `Ctrl+C` stop that process and its
children, including active downloads, model inference, and ffmpeg. Temporary
files from the stopped worker are removed.

`q` resets the playback position so the next run starts at the beginning.
`Q` saves the position so the next run continues. Both keys retain player settings.
The reader automatically resumes from the last played audio position after
`Ctrl+C`, `Q`, or a failure. After `q`, playback starts from the beginning. It saves speed, volume, mute, the information panel,
and the OSD display alongside playback time rather than queued audio. A changed
book, language, mode, or voice/model identity starts a new reading position.
Bookmarks are removed when reading finishes. To start at the beginning:

```bash
TTS_RESUME=0 ./tts book.fb2
```

Generated PCM is cached under `TTS_CACHE_DIR/pcm`, with a default limit of 256 MiB.
Keys include text, language, voice, model fingerprint, and PCM format. LRU order
is stored in a locked SQLite index and does not depend on filesystem timestamps. Cached data
has a size and checksum; corrupt entries are regenerated. Online speech remains
available until size-based cache eviction. RHVoice uses complete-book cache
manifests when the blocks needed from the saved position remain available;
otherwise it regenerates one native stream and skips the played prefix on resume.
Audio is not saved next to the book.

Google speech requests are limited to 100 characters, following the limit used
by gTTS. Each short sentence is sent whole, with closing quotes attached. Long
sentences split at clause punctuation first and word boundaries only when needed.
Abbreviations, initials, decimal numbers, and CJK sentence endings are handled.
All engines share the same sentence detector with language-specific abbreviations.
Other engines use an 800-character bound. Each Google request's MP3 is
decoded separately before its PCM enters the continuous playback stream.

## Language and voices

FB2 and FB2.ZIP use `description/title-info/lang`. XML `encoding` sets the text
encoding, not its language. Only TXT, FB2, and FB2.ZIP are accepted. A ZIP must contain exactly one FB2.
Input files and uncompressed ZIP members are limited to 64 MiB by default;
set `TTS_MAX_BOOK_BYTES` to change the byte limit. XML is parsed incrementally,
with consumed elements removed from the tree. DTDs and XML entities are rejected.
The same byte limit applies to extracted UTF-8 text; XML depth is capped at 128
and the number of elements at one million. The extracted book text remains
in memory within that limit. The reader includes
body text and notes without XML markup or images.

ISO two-letter and three-letter codes are accepted using a bundled ISO 639-2 table;
the system `iso-codes` package is not required. Regions and scripts are kept:
`eng-US` becomes `en-US`, and `zh_Hant` becomes `zh-Hant`. Exact locales are preferred
when the engine provides them. Contradictory script tags are rejected, including
Serbian Latin/Cyrillic and Chinese Simplified/Traditional. Regional fallbacks are
reported. Piper ranks installed voices alongside the cached or downloaded catalog. Plain text and missing FB2 language metadata default
to Russian; use `-l LANGUAGE` to set another language or override FB2 metadata.
TXT supports UTF-8, UTF-16 with a BOM, and Windows-1251.

Each engine uses its own voices for the requested language. The script never
substitutes a Russian voice or switches engines to hide a missing language.

- **RHVoice:** reads installed `voice.info` files and matches language and gender.
  The bundled ISO table maps language names and ISO codes. Install RHVoice
  voice packages for additional languages.
- **Edge:** reads Microsoft's voice list and matches language, locale, and gender.
- **Silero:** selects models for English, German, Spanish, French, Ukrainian, Uzbek,
  Russian, Indic languages, and the languages in the Cyrillic model. It also reads
  the upstream index for further language models. Models download once. English
  defaults to the English Indic model, whose voices have an Indian accent. Indic
  input is romanized using `aksharamukha` before synthesis.
- **Piper:** reads language metadata from local ONNX configs. When a suitable model
  is missing, it selects and downloads one from the upstream catalog. Single and
  multi-speaker models are supported. Downloads check the supplied size and hash.
- **Google:** maps the book language to a Google Translate speech language, including
  Chinese script variants and supported regional variants.

The engines have different language and gender coverage. A missing language is an
error. A known voice of the opposite gender is never selected. Some Silero/Piper
catalogs omit gender metadata; an unlabelled voice can be used with an explicit
message that gender is unknown. Set `TTS_STRICT_GENDER=1` to reject such voices.
Spanish and French indexed Silero voices have no gender labels in the upstream
catalog. Use a voice override if you have verified their gender.

Sources: [Silero models and speakers](https://github.com/snakers4/silero-models),
[Piper voices](https://huggingface.co/rhasspy/piper-voices).

## Setup and configuration

Required: Python 3.10 or newer, mpv, and ffmpeg on Linux.
Installing the package includes `defusedxml` for safe FB2 parsing.
Install only the dependencies for the engines you use:

```bash
python3 -m pip install ".[edge]"          # Edge
python3 -m pip install ".[silero]"        # Silero
python3 -m pip install ".[silero,indic]"  # Silero Indic voices
```

RHVoice needs `RHVoice-test` and voice packages. Piper needs its executable;
`~/piper/piper/piper` is also searched. Google uses Python's standard library.

| Variable | Purpose |
| --- | --- |
| `TTS_RHVOICE_VOICES` | RHVoice voice directory |
| `TTS_PIPER_VOICES` | Local Piper model directory |
| `TTS_CACHE_DIR` | Model, catalog, audio, and bookmark cache; default `~/.cache/tts` |
| `TTS_PIPER_INDEX` | A local Piper voice catalog JSON file |
| `TTS_SILERO_INDEX` | A local Silero model catalog YAML file |
| `TTS_VOICE_CONFIG` | Voice overrides; default `~/.config/tts/voices.json` |
| `TTS_STRICT_GENDER` | Set to `1` to require known voice gender |
| `TTS_NETWORK_TIMEOUT` | Per-attempt network timeout in seconds; default `60` |
| `TTS_BUFFER_SECONDS` | Startup buffer in playback seconds; default `8`, maximum `120` |
| `TTS_PCM_CACHE_MB` | PCM cache size in MiB; default `256`; `0` disables writes |
| `TTS_MAX_BOOK_BYTES` | Maximum file/member and extracted UTF-8 text size; default `67108864` |
| `TTS_MAX_RPC_BYTES` | Maximum HTTP RPC response or downloaded catalog; default `8388608` (8 MiB) |
| `TTS_MAX_DOWNLOAD_BYTES` | Maximum model/catalog download; default `536870912` (512 MiB) |
| `TTS_MAX_AUDIO_BYTES` | Maximum generated encoded audio per request; default `8388608` |
| `TTS_MAX_PCM_BYTES` | Maximum decoded or cached PCM fragment; default `8640000` (180 audio seconds) |
| `TTS_RESUME` | `1` resumes automatically (default); `0` starts at the beginning |
| `TTS_PIPER_REVISION` | Piper catalog/model repository revision; default `main` |
| `TTS_SILERO_REVISION` | Extra Silero catalog revision; default is the commit pinned in `tts_model_pins.py` |

Online requests and downloads retry up to four times for connection errors,
timeouts, HTTP 429, and temporary server errors. Backoff is bounded and honors
`Retry-After` up to 60 seconds. Already-generated audio drains before a final
synthesis error is reported. HTTP response bodies have a deadline for the entire
attempt, including DNS, headers and connection time, rather than a timeout renewed
after each chunk. An opening timeout abandons the request and closes any late
response without writing it to disk or loading it as a model.
Edge's entire streaming request is bounded by an asyncio deadline. Decoding has
a 180-second wall deadline and stops immediately on exceeding the PCM byte limit.

Model downloads use temporary files and atomic renames. Empty responses,
`Content-Length` mismatches, and supplied size/checksum mismatches are rejected.
All nine built-in Silero packages have expected sizes and SHA-256 pins in
`tts_model_pins.py`. Checksums are verified before `PackageImporter` loads a cached
or downloaded package. The default extra-language catalog has an immutable commit
and SHA-256 pin. Automatic extra models without a known SHA-256 are rejected;
configure their verified checksum in `TTS_VOICE_CONFIG` to use them.

Silero packages can execute Python code during loading. Pins prevent accepting
changed bytes; they do not sandbox the trusted upstream code. Explicit Silero
overrides are trusted configuration. Overrides without `sha256_digest` produce
a warning before any package loading; an MD5 alone is not a SHA-256 pin.
Overrides can also supply `size_bytes` and `md5_digest`. Changing the catalog
revision or supplying a local catalog does not grant trust to an executable model.
SHA-256 values from an unverified local/alternate catalog are ignored. Trust comes
from the built-in URL pins, the SHA-256-verified default catalog, or a separate
explicit voice override. A catalog cannot replace the built-in hash for a known URL.
Remote Edge/Google voices cannot be pinned to a public model revision.

The PCM limit is checked against metadata and actual file size before reading
cached audio. Old entries exceeding a lowered limit are discarded and regenerated;
new oversized entries are not cached. RHVoice streams complete books in one-second
blocks instead of retaining a whole decoded book in memory.

Voice overrides add languages, verified genders, or private models:

```json
{
  "rhvoice": {"en": {"female": {"name": "Slt"}}},
  "edge": {"en-GB": {"female": {"name": "en-GB-SoniaNeural"}}},
  "piper": {
    "en-US": {"female": {"path": "/path/to/en_US-lessac-medium.onnx"}}
  },
  "silero": {
    "en": {"female": {
      "model": "v3_en_indic",
      "speaker": "tamil_female",
      "url": "https://models.silero.ai/models/tts/en/v3_en_indic.pt",
      "sha256_digest": "8ebf6b8bc4a762117e5f8d9a6ba30ffcbb65eb669f57cecd6954b0f563095429",
      "sample_rate": 24000
    }}
  }
}
```

The project wrapper resolves its own directory. No installation or change to
`~/bin` is needed. Installing the package creates the `tts` console entry point
in the chosen Python environment; use a virtual environment if desired.

Run directly from this project:

```bash
./tts -h
./tts 9 -s 2.3 book.fb2.zip
python3 tts.py book.fb2
```

## Tests

```bash
python3 -m pip install ".[dev]"
python3 -m unittest discover -v
python3 -m ruff check .
python3 -m build
```

Ruff is pinned to 0.16.10 with the explicit `E4,E7,E9,F` rule set. Install the Edge
and Silero extras to run their dependent unit tests; otherwise those tests report
explicit skips. Playback tests need
mpv and ffmpeg; native playback tests also need RHVoice and its Russian voices.

Offline tests cover voice selection, ISO codes, regions, scripts, Google RPC
responses, corrupt downloads and caches, retries, streamed XML, queue duration,
worker cancellation, and reading-position recovery. Real mpv runs in a pseudoterminal
with `--ao=null`; the test presses Space, `[`/`]`, and `q` and checks that two PCM
blocks have one `start-file` and no `end-file` between them. Additional PTY tests quit while preparing audio and check native RHVoice
playback, pause, cancellation, and resume. Non-English text in
test fixtures is data used to check encodings and synthesis.

Real network synthesis tests check audio decoding, duration, and signal level:

```bash
python3 integration_tests.py --engine rhvoice
python3 integration_tests.py --engine piper
python3 integration_tests.py --engine edge
python3 integration_tests.py --engine silero
python3 integration_tests.py --engine google
python3 integration_tests.py --engine all --timeout 30
```

These use real services/models, have a process deadline, and fail if a dependency
or service is unavailable. Their default model cache is `.cache/` in this project;
`TTS_CACHE_DIR` overrides it. They do not play sound on speakers.

GitHub Actions runs base installs on Python 3.10–3.13 and separate Edge/Silero
jobs on Python 3.11, with mpv/ffmpeg, lint, builds, and an installed-wheel smoke
test outside the checkout. An additional Debian Trixie/Python 3.13 job runs the
suite with Debian's mpv 0.40.x; logs record native-tool versions. PTY checks accept
both relative and absolute representations of mpv's stdin path and query codec
properties independently of the native panel's presentation.
Network synthesis is a separate manual workflow.
RHVoice/Piper integrations require their executables and voice models locally;
missing prerequisites are failures in the live integration suite.

## License

The reader is distributed under the [MIT license](LICENSE). External engines,
model weights and voice packages have their own licenses.

## Russian Silero pause cleanup

For `xenia` in `v4_ru`, a dialogue dash at the start of a synthesis request is
removed before model inference. This model otherwise emits a long noisy pause
before the first word. Words and dialogue punctuation inside requests are kept.

The worker uses the model's duration/mask alignment to locate non-word pauses.
Only aligned intervals containing at least 200 ms below roughly -36 dBFS RMS
and -24 dBFS peak are muted, with short fades. This also covers vocoder noise
after ordinary sentence punctuation; quiet words outside these intervals are
kept. Sample count is unchanged. Old cached fragments without alignment are
regenerated once, then the processed audio and processing revision are cached.
Original audio fingerprints are retained for bookmarks when regeneration matches
the original samples (or the previous conservative cleanup of those samples).
If regenerated speech differs, the current fragment is replayed to avoid skipping
words. A later cached playback retains the same bookmark compatibility.
Restart the reader to use updated worker code; `Q` exits while saving position.

## Modules

`tts.py` handles the CLI. `tts_books.py` parses input; `tts_text.py` splits sentences;
`tts_engines.py` selects and runs engines; `tts_voices.py` manages catalogs and model
downloads. `tts_config.py` and `tts_network.py` hold shared configuration and retry
rules. `tts_worker.py` produces PCM in an interruptible process; `tts_pipeline.py`
buffers it and saves positions; `tts_playback.py` controls mpv; `tts_state.py` manages
the PCM cache and bookmarks. No module imports helpers from the CLI.

### Russian Silero pronunciation

Russian Silero voices automatically use Silero Stress 1.5 for contextual stress
placement and restoration of `ё`. Install it with the Silero dependencies:
`python3 -m pip install ".[silero]"`. No extra flags or environment variables are needed.
Other engines and non-Russian Silero voices receive the original text.

Silero receives `+` before stressed vowels. Predictions can be wrong.
If processing fails or changes the underlying text beyond stress marks and
`е/ё`, the original fragment is used. Processing retains sentence and clause
boundaries. Processed audio has separate cache keys from earlier plain-text audio.

### Reliable resume and diagnostics

An explicit `-s SPEED` overrides the remembered speed; otherwise playback restores
the saved speed. `q` resets both the audio position and the text bookmark, while
retaining volume, speed and the information-panel settings; `Q` saves the position.

Edge, Google, Silero and Piper bookmarks identify original text fragments.
Resuming skips previous fragments even if their audio has been evicted. If the
current fragment's audio is unchanged, playback resumes at its saved sample
offset; if regenerated audio differs, that fragment repeats from the beginning
to avoid skipping words. Older bookmarks initially use their saved audio time
and acquire a text bookmark during playback. Online audio no longer expires
every day: normal cache size eviction controls retention. RHVoice retains
continuous native synthesis; a complete cached stream can be read directly from
the saved position. Without that cache, RHVoice regenerates the stream.

Concurrent readers and writers share a cache lock. Nonempty synthesis diagnostics,
including pronunciation failures, remain under `TTS_CACHE_DIR/logs` (normally
`~/.cache/tts/logs`). The last ten logs are retained, up to 1 MiB each.
Diagnostics remain out of the playback terminal.
