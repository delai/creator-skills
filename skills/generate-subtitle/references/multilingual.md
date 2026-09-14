# Multilingual Transcription and Translation

## Languages and Engines

Subtitles default to the audio's original language, regardless of the language of SKILL.md, prompts, glossaries, or background materials. Preserve mixed languages as spoken. Correction is not translation; pass `--translate-to` only when the user explicitly requests translation.

- `--language auto` (default): for each file and channel/device track, Whisper detects the language using non-silent samples of up to 30 seconds each from the beginning, middle, and end. Use the Chinese path only if every sample identifies `zh` with high confidence; otherwise use Whisper. Record detection results in the manifest. Sampling may miss language changes within the recording, so do not claim complete language detection. Use `--engine whisper` if a change was missed; provide a hint when the audio language is known.
- `--language zh`: when the user knows the audio is Chinese, use the Chinese path directly and skip language detection. Do not set this merely because the Skill documentation is in Chinese.
- Other explicit audio languages (such as `en`, `ja`, `ko`): use Whisper. Even if a Chinese engine was requested, report the switch to Whisper to avoid forcing a Chinese model to transcribe the audio.
- The Chinese path retains the existing rules: use FunASR when voiceprint diarization is needed; otherwise prefer FireRed, falling back to FunASR if unavailable. Explicit `--engine whisper` can also handle Chinese or mixed languages.
- `--whisper-model small` (default) specifies a multilingual model name; a local checkpoint path is also accepted. Do not use `.en` or any English-only checkpoint. After loading, check support using the actual model's `is_multilingual`, `num_languages`, and tokenizer language-token set; do not hard-code an assumption that all Whisper models support the same languages. An unsupported `--language` raises an error rather than silently changing languages.
- Whisper always uses `task="transcribe"` and word timestamps; never substitute its built-in `translate` task for transcription that preserves the original. Mixed-language recognition quality still depends on the model; correction must not use translation to conceal recognition problems.

## Dependencies and Devices

The Chinese path retains the existing isolation between FunASR / FireRed environments. Automatic language detection and non-Chinese transcription require **openai-whisper** in the main script's Python environment (not another package named `whisper`). Subtitle segmentation and display width also require `regex` and `wcwidth`. Do not merge existing FunASR / FireRed venvs or modify other tools' environments for this Skill.

```bash
# Use the interpreter that actually runs the script; check installed dependencies first and install only what is missing
python -m pip install openai-whisper regex wcwidth
```

`--device auto` prefers CUDA, then MPS, then CPU. Chinese engines retain their existing device logic. The current Whisper adapter uses CPU or CUDA; its word-timestamp path has not been validated on MPS, so selecting MPS prints an explicit notice and falls back to CPU/FP32. CUDA uses FP16; CPU uses FP32. Model weights may download on first load, with size varying by model.

Translating existing subtitles alone uses the standard library and handoff; it does not require ASR, torch, ffmpeg, regex, or wcwidth.

## Optional Translation

Complete original-language subtitles and correction first. If `.uncertain.srt` / `.uncertain.vtt` exists, have a human listen to the original audio and confirm it first. Do not generate translations, highlights, or review before confirmation. **Do not automatically empty the uncertainty list merely to resume, and do not treat elapsed time as human confirmation.** After a human corrects the source subtitles, archive or empty the resolved uncertainty list, then translate those subtitles separately as input to avoid overwriting manual edits by rerunning the audio.

```bash
# Original-language transcription, adding an English translation after correction is complete and no uncertainties remain
python "$SKILL_DIR/generate_subtitle.py" talk.mp4 --language auto --translate-to en

# Known Japanese audio: retain Japanese; translation must be explicitly specified separately
python "$SKILL_DIR/generate_subtitle.py" talk.wav --language ja --whisper-model small

# Translate existing subtitles independently, without transcription or another pass of source polishing
python "$SKILL_DIR/generate_subtitle.py" talk.srt --translate-to en
# Or use the direct entry point requiring only the standard library
python "$SKILL_DIR/translate_subtitle.py" talk.srt --translate-to en
```

Translations are saved separately by default as `talk.en.srt` / `talk.ja.vtt`. `-o` can specify a new file in the same format; it cannot overwrite the source subtitles. Use an explicit target language code (such as `en`, `ja`, `zh-hant`), never `auto`. Target languages are not limited to Whisper's supported languages, since translation is handled through handoff.

Translation tasks are keyed by content and target language, with at most 100 cues per batch. After exit 10, follow `TASK.md` and write `out.json`. Resuming the original command reuses completed tasks; changing the target or editing the source subtitles regenerates affected tasks. The script preserves original cue numbers, order, complete timecode lines (including VTT settings), speaker labels, label order, and speaker line breaks; it replaces only text. It does not resplit, merge, deduplicate, or retime translated cues, nor apply correction's text-growth limit. Let the player display longer translations; do not break correspondence to control line width.

If the returned structure is damaged, no final file is written. The original answer is retained as `out.invalid.json`, and the original task re-enters handoff. If translation reveals ambiguous source text, return `uncertain`; the script exits 2 and writes `talk.translation-en.uncertain.json`. After human confirmation, correct that task's answer, remove the resolved `uncertain` field, and resume. The source subtitles are always retained.

## Text, Track Separation, and Timestamps

- Unicode `\X` grapheme clusters prevent splitting combining accents, Indic ligatures, and ZWJ emoji. `wcwidth` counts display width without counting combining marks again. Width approximates terminal columns and is not guaranteed to match video-font pixels exactly.
- For scripts with spaces, keep words consisting of Unicode letters/digits and combining marks intact; for scripts without spaces, such as Chinese/Japanese, preserve at least grapheme clusters. Thai, Burmese, and Khmer also retain grapheme clusters, without claiming dictionary-based word segmentation for those languages. Chinese jieba segmentation, classifier handling, and repair of splits across cues apply only to clearly Chinese content.
- For non-Chinese, retain original punctuation, inter-word spaces, repetitions, and discourse particles. Chinese year conversion, deduplication, particle removal, profanity replacement, and Chinese–English spacing apply only to clearly Chinese cues. Disable automatic Chinese rewrites when Japanese kana/Korean is present. Do not assume an unknown language is Chinese; let the model make conservative judgments about mixed-language content.
- Convert Whisper word timestamps from seconds to milliseconds, locate each word's text sequentially in the original string, then map it to complete graphemes. Interpolate unalignable local passages only between adjacent known anchors. Retain both original `words` and shared `timestamp`; apply multi-device offset/drift transformations to both.
- The current `paraformer-zh + fsmn-vad + ct-punc + cam++` is the established Chinese voiceprint pipeline. The presence of cam++ does not imply support for all languages. Non-Chinese mixed audio cannot use this voiceprint path; Whisper and FireRed adapters have no voiceprint diarization. Language-independent channel/recording-device track separation remains available; multiple people within one physical track still cannot be distinguished by track number.
- Language-detection caching includes the input file/track processing plan and Whisper model identity. ASR caching additionally distinguishes actual engine, audio hint, detected language, model, voiceprint speaker count, and gating/alignment plan. Local checkpoint identity includes path, size, and nanosecond modification time. Old-version caches are not reused; changing only the translation target does not invalidate ASR caches.

## Validation

```bash
python -m unittest discover -s "$SKILL_DIR/tests" -v
```

Automated tests cover language routing/model language sets, CPU/CUDA/MPS selection, Unicode source text and grapheme boundaries, word timestamps and device offsets, isolation of Chinese rules, translation handoff resumption, blocking on uncertainty, and the integrity of numbering, timecodes, and markers. ASR model tests use doubles to verify the calling protocol; they do not establish recognition quality on real recordings. After adding models or devices, still spot-check by listening to representative recordings.

Whisper implementation references: [transcription and word timestamps](https://github.com/openai/whisper/blob/main/whisper/transcribe.py), [model attributes](https://github.com/openai/whisper/blob/main/whisper/model.py), [tokenizer language set](https://github.com/openai/whisper/blob/main/whisper/tokenizer.py).
