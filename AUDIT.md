# External audit fixes — 2026-10-09

This document records changes made after reviewing the external audit of commit
`2e8d198f63dae0dcdd9cb9433db741a34b5f165e`. It replaces the earlier audit summary,
which contained stale claims about daily online-cache invalidation and unspecified
lint checks. These results concern code, packaging and signal generation; they
are not a listening assessment or a penetration test of speech providers.

| Finding | Change | Regression coverage |
| --- | --- | --- |
| New PCM could be evicted when timestamps tied | A SQLite logical access sequence is updated under the existing cross-process file lock. Newly written PCM is excluded from eviction candidates. Existing PCM metadata remains compatible. | Identical file timestamps, access after insertion, independent cache instances, concurrent processes |
| XML entities bypassed the input limit | Declared dependency `defusedxml==0.7.1`; reject all DTDs/entities; enforce actual XML input bytes and extracted UTF-8 text bytes, maximum depth 128 and one million elements. TXT output is also bounded in UTF-8 bytes. | Plain/zipped entity fixtures, DTD without entities, nested duplicate text, excessive depth, existing encodings and inline markup |
| Silero executed packages before a mandatory fingerprint check | Nine built-in model sizes/SHA-256 values in `tts_model_pins.py`; default catalog pinned to commit `d9355348e2781dc8fa25a135d1602c530afae24c` and SHA-256. Reject automatic unpinned packages. Explicit unpinned voice overrides are treated as trusted configuration and warned about in the terminal before worker startup, as well as before package loading. | Wrong cached/downloaded hashes never reach PackageImporter; unknown unpinned model rejected; warnings precede loading; adding verification metadata preserves bookmarks for unchanged model bytes |
| ISO aliases depended on system files | Ship the ISO 639-2 terminology/bibliographic codes and English names in `tts_languages.py`; no `/usr/share/iso-codes` lookup. Three-letter-only codes remain unchanged. | `fra/fre`, `deu/ger`, `zho/chi`, `ces/cze`, script tags and language names without system data |
| Responses and PCM were unbounded | Byte ceilings on RPC/catalogs, downloads, streamed Edge audio and generated audio. HTTP opening has a bounded wait including DNS/headers; complete response bodies share the attempt's deadline. ffmpeg pipes are drained incrementally with bounded PCM/diagnostics and a 180-second wall deadline; combined fragments are bounded too. | Declared/actual oversized replies, failed download cleanup, trickled-body deadline, stalled opening and late-response close, Edge stream stop, real ffmpeg output limit |
| Base install tests failed on optional imports; no quality gate | Individual tests explicitly skip absent extras. GitHub Actions base matrix Python 3.10–3.13 plus Edge/Silero on 3.11; mpv/ffmpeg, tests, lint, build and installed-wheel smoke check. Separate manually dispatched live synthesis workflow. Ruff, build, setuptools and wheel versions pinned. | Full local suite and clean base virtual environment; installed wheel tested outside checkout |
| No distribution license | MIT license and SPDX packaging metadata; external models/engines retain their own licenses. | Built wheel includes LICENSE |
| Audit/documentation drift | Updated README resource settings, model trust boundary, ISO data, quality-gate commands and cache lifetime; replaced this audit summary. | Online PCM calendar-change regression remains enabled |

## Validation

Commands executed with Python **3.11.4** on Linux:

```bash
python3 -m unittest discover -v
python3 -m ruff check .
python3 -m build --outdir /tmp/tts-audit-dist
python3 integration_tests.py --engine all --timeout 30
```

- Full installed environment: **102 tests passed**, no skips. This includes real
  mpv PTY/control tests and native RHVoice playback; these are not mocked player
  checks.
- Clean base virtual environment, installed with `.[dev]`, without Edge, Torch
  or PyYAML: **87 passed, 15 explicitly skipped, no failures/errors**. Skipped
  optional-backend tests are not counted as verified.
- **Ruff 0.16.10**: `ruff check .` passed with the repository's explicit
  `E4,E7,E9,F` policy. `--isolated` intentionally ignores that policy and is not
  the documented lint command.
- Wheel and source archive built with **setuptools 80.9.0 / wheel 0.45.1**.
  The wheel was installed in an isolated environment and its CLI, ISO aliases,
  XML parsing and model-pin module checked from `/tmp`, outside the checkout.
- **10 real synthesis tests passed**: RHVoice Russian female/male and English,
  Edge female/male, Silero female/male, Piper female/male, Google. Tests decode
  the generated files and check duration/signal level without playing sound.
  Silero used the verified cached Russian model; its pinned bytes were also
  freshly downloaded from the official server to `/tmp` and fingerprinted.
- All nine pinned model files and the immutable catalog were downloaded and
  fingerprinted without executing their packages. The installed Russian model
  has the same SHA-256 as the freshly downloaded pinned file.
- `git diff --check` passed.

The initial local validation used Python 3.11. After the mpv codec-rendering
check was corrected, all six Ubuntu CI jobs passed on commit `17d78b9`: Python
3.10–3.13 base, plus Python 3.11 Edge/Silero. See the [successful workflow](https://github.com/bortoq/tts/actions/runs/38041032973). Live synthesis checks validate signal generation,
not pronunciation, absence of acoustic artifacts, or voice quality.

## Remaining product and trust boundaries

Silero's `torch.package` loading can execute Python code. SHA-256 pins prevent
accepting changed package bytes; they do not sandbox trusted upstream code.
Explicit unpinned overrides retain this risk and display a warning. Updating
model pins requires a separate upstream review and fingerprint update. Additional
catalog models need a known SHA-256 or an explicit trusted voice configuration.

A timed-out HTTP opener may retain a daemon thread while the system completes
DNS/header I/O. It cannot return a late response to the caller or model validator;
late responses are closed. Per-attempt deadlines do not include retry backoff:
there can be four attempts and bounded Retry-After waits. Downloads, audio and
book text remain finite but model inference still needs enough RAM for the
selected model. Limits are documented and configurable in README.

The reader targets Linux/POSIX (`fcntl`, process groups, mpv Lua/terminal).
RHVoice/Piper require their external programs and voices. Edge/Google send text
to their providers, whose voice revisions cannot be pinned. Generated online PCM
persists until size-based LRU eviction; it has no daily expiration.

## Follow-up audit of 17d78b9 — 2026-10-10

- Alternative/local Silero catalogs can no longer authorize executable packages
  with their own SHA-256 values. Only built-in URL pins, the independently pinned
  default catalog, and explicit voice configuration can supply trusted hashes.
  A known built-in URL's pin cannot be overridden by catalog metadata. Regression
  checks exercise both local catalogs and alternative downloaded revisions, plus
  the verified-default-catalog path.
- The PTY test accepts mpv's relative `-` and absolute `/.../-` stdin paths.
  Codec checks still use `audio-codec-name`; the native information panel must
  actually open. A Debian Trixie job adds mpv 0.40.x to the Ubuntu matrix and
  reports installed native-tool versions.
- Cached PCM is bounded before reading: metadata byte count and actual file
  size must agree and fit `TTS_MAX_PCM_BYTES`. The read is bounded too, and new
  oversized cache entries are skipped. Native cached-book lookup rejects blocks
  above the current limit and falls back to streaming synthesis. Lowering limits
  does not authorize replay of an oversized cached fragment.
- Current local suite: **109 passed** with installed extras; clean base environment
  **91 passed / 18 explicit skips**. Ruff 0.16.10 and `git diff --check` passed.

Live network checks remain manually dispatched to keep external service failures
separate from deterministic CI. Subjective voice quality and acoustic artifact
absence still require listening; successful signal/playback tests do not certify
those properties.

### Longer native-engine playback check

A generated Russian fixture with 120 numbered sections and **28,329 characters**
was read to completion through the production worker, cache and mpv. Three female
voices were tested sequentially: Anna/RHVoice, xenia/Silero, irina/Piper. The player
used `--ao=null` and **50x playback** to avoid sending sound to speakers and keep
the check finite. This tests tens of minutes of source audio, not tens of minutes
of wall-clock listening or a full novel.

| Engine | Source audio | Wall time | Peak process-tree RSS |
| --- | ---: | ---: | ---: |
| RHVoice | 1,720.06 s | 42.74 s | 252.4 MiB |
| Silero | 1,617.31 s | 73.85 s | 1,551.4 MiB |
| Piper | 2,131.42 s | 144.93 s | 597.0 MiB |

Each run recorded exactly **one start-file and one end-file**, completed without
restarting mpv, and cleared its reading bookmark. Source duration includes pauses;
wall time includes initialization, synthesis, caching and accelerated playback.
RSS was sampled every 0.1 seconds by summing the runner and all descendants'
`/proc/*/statm` resident pages; shared pages can be counted more than once. This is
an observed peak for this fixture/hardware, not an absolute upper bound. Silero
includes the Russian pronunciation model. Local inference allocates model/tensor
memory before encoded-audio limits can be checked. No subjective voice-quality
score or acoustic-cleanliness guarantee is inferred from this test.

## Reproduced Silero artifact — 2026-10-10

Mode 5, `xenia` / `v4_ru`, book `01. Моя жизнь. Эдем. Расследование.fb2.zip`,
source playback time **10:59:44**. Fragment 925 starts at 39,550.325 seconds. Its
quiet noisy region is at **32.78–34.08 seconds within that fragment**, corresponding
to approximately **10:59:43.1–10:59:44.4** in the book.

The pronunciation markup expands this fragment to 809 characters and splits it
into two synthesis requests of 657 and 151 characters. The second starts with a
dialogue dash. Repeating the actual production requests regenerated the cached
PCM byte for byte: SHA-256
`d1229d0fe6eda00278524cd030f47cddb66c58989934c3bcc6b5dee7ef7c46b4`.
Thus the residual signal is in synthesized PCM, before mpv playback.

The second request originally lasts 8.6125 seconds and its first substantial
voice occurs at about 1.18 seconds. Removing its leading dialogue dash yields
7.4625 seconds with the first substantial voice at about 0.06 seconds. This fixes
the source of the long leading pause for newly generated `xenia/v4_ru` requests.

Existing cached audio is cleaned conservatively, using sustained near-silence
with short fades instead of deleting samples. On the actual cached fragment, only
32.80–34.06 seconds change; the central pause's RMS drops from about 59 signed
16-bit units to zero. All other samples and the total length remain identical.
Repeated cleanup produces identical bytes. Original bookmarks retain their sample
offset when only the pause cleanup changes their audio hash.

Regression checks cover normal speech, short quiet phonemes, old leading pauses,
digital silence, idempotence, dialogue markers and cached-fragment resume without
resynthesis. Current local suite: **116 passed**; clean base environment:
**91 passed, 25 explicit skips** without optional dependencies. Acoustic quality
outside this identified residual-noise case is not certified by these checks.

### Follow-up Silero artifact — 2026-10-10

The reported `03. Непобедимый. Рассказы.fb2.zip` case around 02:47–02:48
reproduces with the updated reader. Fragment 4 starts at 164.700 s; after
«из носовой части.» the model marks 3.3625–4.0125 s as non-word audio
(absolute 02:48.0625–02:48.7125). Fresh synthesis matches the cached fragment's
SHA-256 `33ba22bba15981ccb6e0e21b400aa135c7c91ba85d515a8f9c6e6eab2cadbf18`.
This is ordinary sentence punctuation, not the previously reproduced leading
conversation dash. The previous amplitude-only fix misses this louder residue.

The worker now uses the pinned v4_ru model's duration/word-mask output in the
same inference call. It mutes only complete 10 ms frames inside non-word spans
with at least 200 ms below RMS 512 / peak 2048 in signed 16-bit PCM. The amplitude
guard protects speech overlapping approximate alignment boundaries; 20 ms
margins and 10 ms fades preserve transitions. Quiet words outside aligned
non-word intervals remain untouched. The model's timestamp formatter is replaced
temporarily to collect the raw masks without its punctuation-sensitive word-count
assertion, and restored even when inference fails.

Old cache entries without a processing revision regenerate once; processed PCM,
revision and compatible original audio fingerprints are stored atomically under
the cache lock. A second playback reuses the processed cache and retains bookmarks
referencing original PCM or the previous conservative cleanup. Unmatched regenerated
speech replays the current fragment rather than guessing its saved word position.
This supersedes the earlier amplitude-only cleanup in the production pipeline.

On the actual 45.750 s fragment, the 3.52–3.78 s noisy interval's RMS falls from
313.09 to 0. Sample count is unchanged. A production-worker test with an isolated
copy of the cache confirms the second playback needs no synthesis and keeps the
original bookmark's sample offset. mpv also plays the corrected diagnostic clip
at 3x successfully with a null audio output; this checks playback, not subjective
voice quality. Diagnostic recordings stay outside tracked source files.

Validation: 119 offline tests pass with optional dependencies; the clean base
venv passes 91 tests with 28 optional skips. Ruff and whitespace checks pass.

### Pre-vocoder Silero adapter — 2026-10-10

Replaced the prior PCM pause suppression and leading-dialogue-dash removal with
`tts_silero.py`, an adapter for the reviewed v4_ru/xenia package checksum. The
adapter reuses original text preprocessing, accentuation, duration and pitch
predictions, then replaces protected middles of long non-word acoustic intervals
with the model's silence spectrum before vocoding. Sample duration is retained.
Runs must contain a space or punctuation token and last at least 350 ms;
125/150 ms left/right guards and 25 ms blends retain transitions. No amplitude
threshold or VAD controls the correction. Word-token mel frames are untouched.

Implementation exposed a correction to the earlier description: in both exact
reproductions the long noisy token is a space after the preposition «В», not the
period/dash itself. The period/dash influences the duration prediction through
context. An initial punctuation-only adapter failed both reproductions and was
corrected to include long space intervals before release. The mechanism and
controlled acoustic-spectrum interventions remain valid; details are recorded
in SILERO_PAUSE_DIAGNOSIS.md.

Cache processing revision is now `silero-mel-pauses-v1`. Old processing algorithms
are removed entirely. Only during migration, an additional vocoder pass on the
unchanged spectrum supplies reference PCM. Its fingerprint, old cache aliases
and sample length prove which old bookmarks have unchanged timing. Unproven
old bookmarks replay their fragment; new synthesis normally uses one vocoder
pass, and subsequent processed cache hits require no synthesis.

Validation: 118 offline tests pass; the clean base venv passes 92 with 26 optional
skips. Both live Silero voice tests pass. A stratified sample of 170 fragments
from the two reported books generated 283 requests / 7243.925 s (2 h 00 m 44 s)
of speech, with 449 corrected pause interiors. Every request passed finite-spectrum,
frame-count and untouched-word-frame assertions. This is structural verification,
not a subjective listening review of two hours. The raw reference PCM hashes for
both exact reported fragments match their original hashes; durations remain
45.750 and 41.550 s. Core noise RMS falls from 313.09 to 0.653 and from 70.78
to 0.646 respectively. An isolated copy of the actual old cache confirms first
migration and later cache reuse retain an old processed-PCM bookmark offset.
Control clips play successfully in mpv at 1x and 3x with null output. Ruff,
whitespace checks, sdist/wheel builds and installed-wheel imports outside the
checkout pass; the installed worker no longer exposes either previous cleanup
function. Recordings and diagnostic model outputs are excluded from tracked files.
