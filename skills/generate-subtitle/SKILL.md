---
name: generate-subtitle
description: "Generate original-language subtitles for local video or audio (preserving mixed languages), with support for single files, sequential videos, and alignment of recordings from multiple devices. By default, apply conservative corrections, retain discourse particles, separate speakers into tracks, and protect terms at subtitle boundaries. Save translated subtitles separately when explicitly requested (including standalone translation of existing subtitles), and optionally generate subtitles requiring confirmation, highlight overlays, and a pre-publication content review checklist. Use when the user asks to generate subtitles, extract a spoken transcript, or check subtitle content."
---

# Video Subtitle Generation Skill

## Language Rules and Routing

By default, output subtitles in the audio's original language, **regardless of whether the Skill documentation is in Chinese or English**. Correction preserves the original language and mixed languages; it does not automatically translate. Keep a single Skill entry point, with internal Chinese / Whisper transcription paths sharing track separation, correction, translation, and timeline validation.

- `--language` is an audio-language hint, defaulting to `auto`; it does not mean translation. Audio automatically identified as Chinese follows the existing engine rules; non-Chinese or uncertain languages use Whisper.
- Set `--translate-to` only when the user explicitly requests translation. Finish original-language correction first, then hand off translation and save separate files such as `<原名>.en.srt`. Existing subtitles can be used directly as input without retranscription.
- `--whisper-model` selects a multilingual model (default `small`); actual language support is checked at runtime, and English-only models are rejected.
- Specialized rules for Chinese years, deduplication, discourse-particle removal, and Chinese–English spacing apply only to content clearly identified as Chinese, never to other languages. Corrections, highlights, and review excerpts all retain the original language.
- Use FunASR voiceprint diarization only within the capabilities of the existing Chinese pipeline; do not claim support for every language. Channel / device track separation continues to work across languages.

**Before handling non-Chinese audio, automatic language detection, mixed languages, or translation, read [Multilingual Transcription and Translation](references/multilingual.md)** for dependencies, detection limitations, MPS fallback, resuming translation, and structural validation.

## Publishing Project Directory

Articles, publishing copy, covers, and videos for the same content share `Publish/<项目>/`. First reuse an existing project confirmed to contain the same content; create a local `YYYYMMDD-HHmm-主题` directory only for new content. Put subtitles and reusable transcription intermediates in that project's `subtitles/`. Preserve existing filenames and explicit user output paths; similar topics alone do not establish that two items belong to the same project. This convention is self-contained and requires no publishing conventions from other repositories.


Follow these four steps in order; **do not skip steps**:

1. `generate_subtitle.py` — video → **raw ASR subtitles** + **corrected main subtitles** + **subtitles requiring confirmation**
2. **Manually confirm uncertain subtitles** (`.uncertain.srt`; drag it into the editor, jump to each entry, and listen to the original audio), correcting anything the machine was unsure about
3. `make_review.py` — main subtitles → sensitive/offensive content review subtitles + review checklist
4. `make_highlights.py` — main subtitles → key-point subtitles (highlight overlays, including chapter-start prompts)

**Translation is optional**: when requested, save a separate translation after step 2 using `--translate-to` or `translate_subtitle.py`. Preserve the original subtitles, numbering, timeline, and speaker markers; continue directly if there are no uncertainties.

**Step 2 is a mandatory prerequisite**: translation, review, and highlights all make semantic judgments from subtitle text, so incorrect text produces inaccurate analysis.
When step 1 includes `--translate-to`/`--with-review`/`--with-highlights`, it **stops automatically** if uncertain subtitles are produced, so you can confirm them first.

**Any step may stop partway through and hand LLM work to you** (exit 10 + a task list).
Complete the work, then rerun the same command to resume—see “Read This First: The Script Stops Partway Through to Ask You to Do Work” below.

## Triggers

- Generate subtitles for a video
- Generate **one** combined subtitle file for multiple videos in a directory (joined in video creation-time order)
- Generate subtitles for multiple audio files **recorded simultaneously** (one recorder / phone in front of each participant): automatically align start times, separate speakers by device and channel, and produce one file per person + one combined file
- Extract a spoken transcript and remove fillers such as “呃嗯啊那个就是”
- Select highlight overlays for tutorial/demo videos: key information + new-chapter prompts
- Check a video before publication for sensitive information, personal attacks, disturbing remarks, or offensive language
- Correct proper nouns in subtitles (such as `coso`→`Cursor` or `web coding`→`vibe coding`)

## Dependencies

- The Chinese path requires `funasr`, `torch`, and `torchaudio`; automatic language detection / the Whisper path requires `openai-whisper`; multilingual tokenization and display width require `regex` and `wcwidth`. The commands below use the pipx environment's interpreter, `$HOME/.local/pipx/venvs/funasr/bin/python`. Verify that the interpreter and dependencies are available before running. Reuse them if installed; for other environments, replace the interpreter in the commands with its actual path.
- **Optional second engine FireRedASR2S** (`--engine firered`) runs in a separate venv: `$HOME/.local/venvs/fireredasr2s/bin/python`,
  with weights in `$HOME/.cache/fireredasr2s/pretrained_models` (about 5.6GB). If it is not installed, the script prints complete installation commands.
  The two venvs cannot be merged: FireRedASR2S's `transformers`/`peft`/`torch` versions would break the funasr environment.
  The main script therefore still runs with the funasr interpreter, launching FireRedASR2S as a persistent subprocess (`firered_asr.py`).
- Install `ffmpeg` and `ffprobe` locally
- **The LLM stage supports handoff only**: the script generates tasks and exits. The current session reads the task files, completes correction / translation / highlights / content review, and reruns the command to resume. There is no backend to select, and no external model CLI is detected or invoked. Running in an ordinary terminal also generates pending tasks; it does not perform model analysis on its own.
- The selected ASR model downloads automatically on first run; size varies by engine and model
- `make_highlights.py` and `make_review.py` do not use ASR; run them with system `python3`

## Usage

> First locate the installed skill directory and set `SKILL_DIR` below to that directory. These examples assume a user-level installation; when using the shared repository directly, set it to the absolute path of `skills/generate-subtitle/` in that repository.

```bash
SKILL_DIR="$HOME/.agents/skills/generate-subtitle"
```

### ⚠️ Read This First: The Script Stops Partway Through to Ask You to Do Work (handoff)

When run in an agent session, the script hands off model tasks. It **does not** call an LLM itself: it writes each LLM task into its own directory,
then **exits (exit code 10)** and waits for you to finish before resuming. When you see `⏸ HANDOFF：N 个任务等你处理` (N tasks awaiting your work), do the following:

1. Read `TASK.md` in each task directory, follow its requirements, and write the results back to **the same directory**
   (write `out.json` for JSON tasks; for in-place file-editing tasks, also write a `DONE` file after editing)
2. Tasks are independent; **if there are more than 2, delegate them to subagents in parallel** (multiple Agent calls in one message).
   Give each subagent only the absolute path of its task directory and let it read `TASK.md`; do not repeat the task rules in the prompt.
3. Once all results are written, run the **resume command** printed by the script (it already includes `cd` and `-o`)
4. If HANDOFF appears again, repeat the three steps above until the script exits normally (exit 0)

> **When resuming `generate_subtitle.py`, include `-o` pointing to the original subtitle file or the same `subtitles/` directory**,
> so failure to match the old project does not create a new directory and lose access to the cache. The printed resume command already includes this.

Task directories live in `<输出目录>/.subtitle_tasks/`. Do not delete them after completion; reruns use them to reuse completed work.

### Output Directory Conventions

When the user does not specify an output directory (no `-o`), the script first reuses the input's existing `Publish/` project or the unique project with the same topic name; otherwise, it creates a project under `Publish/`. The agent should first check directories for the same topic, and explicitly pass `-o <项目>/subtitles/` when a differently named project is confirmed to be the same case. All subtitles and intermediates go into the project's `subtitles/`:

- Directory name: `<YYYYMMDD-HHmm>-<名字>`, prefixed with the current local time, for example `20260813-2306`; duplicate names automatically receive `-2`, `-3`
- `<名字>` comes from the input directory name or main video filename, with its extension and leading date/time removed (for example, `2026-08-12 sample interview` → `sample interview`)
- Location of `Publish/`: prefer the root of the repository containing the skill (`$SKILL_DIR/../../../Publish`); for a standalone installation where no repository can be located, use the current working directory
- Contents of `subtitles/`: main subtitles, manifest, highlights, review checklist, `.subtitle_cache/` (ASR cache), and `.subtitle_tasks/` (handoff tasks)
- `--dry-run` only prints the project directory path that will be used; it does not create it
- To write elsewhere (for example, beside the source video when explicitly requested), pass `-o`

**`-o` accepts either a directory or a subtitle file path**:
- A **directory** (or any path not ending in `.srt`/`.vtt`) → treated as a project directory, automatically using its `subtitles/`; if already `subtitles/` or a subdirectory of it, reuse it directly
- `xxx.srt` → write that exact file, placing intermediates in its parent directory

**Where intermediates go** (principle: reusable items stay in the project directory; purely temporary processing artifacts go into the system temporary directory):

| Item | Location | Why |
|---|---|---|
| Extracted 16k wav | System temporary directory, deleted after use | A 76-minute interview ≈ 37MB; obsolete immediately after transcription, with no value for later inspection |
| `.subtitle_cache/` ASR results | Project's `subtitles/` | **Reused repeatedly**: read when rerunning with different polishing parameters and in every handoff round. Without it, the entire recording is retranscribed each round. Set `SUBTITLE_CACHE_DIR` to move it |
| `.subtitle_tasks/` handoff tasks | Project's `subtitles/` | Contains both tasks and answers for troubleshooting and reuse on reruns. Set `SUBTITLE_HANDOFF_DIR` to move it |

### Step 0: First Confirm How Many People Are in the Material (**Mandatory for Mixed Audio**)

If you skip this step and discover incorrect speaker identification halfway through, the cost is **retranscribing the entire recording**: `--speakers` is part of the ASR cache key.
Adding or removing this flag invalidates all previous transcription (about 30~40 minutes for 76 minutes of material). Asking first takes only tens of seconds.

| Material | Action |
| --- | --- |
| One microphone per person, assigned to left/right channels (such as DJI Mic) | **Do nothing**: `--dual-channel auto` detects this; channels identify speakers more accurately than voiceprints |
| One device per person, each recording a separate file | **Do nothing**: `--layout auto` aligns and separates tracks by device |
| A mixed single track, definitely only 1 person | **Do nothing**: do not enable `--speakers` (Chinese `auto` prefers firered; other languages use Whisper) |
| A **Chinese** mixed single track, definitely N ≥ 2 people | `--speakers --speaker-count N` (no need to choose an engine; `auto` switches back to funasr for cam++) |
| A **Chinese** mixed single track, **unknown speaker count** | Run `--probe-speakers` first (below); do not guess |

If the user has not specified the count, **ask**; if you cannot get an answer, probe. This is the only point where you can prevent wasting half an hour.

```bash
# Voiceprint probe: run cam++ on the first 5 minutes to report only how many people there are; no transcription or file writes
$HOME/.local/pipx/venvs/funasr/bin/python \
  "$SKILL_DIR/generate_subtitle.py" \
  "/path/to/audio.m4a" --probe-speakers --device mps
```

For each file, it prints each cluster's share of speaking time and directly recommends parameters (literal output shown below):

```
[1/1] talk.m4a：听出 3 个人
        spk1    1.9 分   73%  主讲
        spk2    0.4 分   16%  主讲
        spk3    0.1 分    2%  疑似旁人
建议：
  talk.m4a: --speakers --speaker-count 2（引擎不用管，auto 会自动切回 funasr 走 cam++）
```

Three known limitations; do not treat the probe as conclusive:

- **It examines only the opening segment** (`--probe-seconds`, default 300 seconds); people who appear later are not counted
- Files whose left/right channels are identified as two people are skipped, with a message saying “不要开 `--speakers`” (do not enable `--speakers`)
- Multiple audio files use parallel track separation, where speaker = device number and `--speakers` has no effect (see the known limitation below).
  Here the probe only tells you which device has more than one person in front of it.

### Step 1: Generate Main Subtitles

```bash
# Single video → Publish/<YYYYMMDD-HHmm>-<视频名>/subtitles/<视频名>.srt
$HOME/.local/pipx/venvs/funasr/bin/python \
  "$SKILL_DIR/generate_subtitle.py" \
  "/path/to/video.mp4"

# Entire directory → join by video creation time into one timeline, outputting only one <目录名>.srt
$HOME/.local/pipx/venvs/funasr/bin/python \
  "$SKILL_DIR/generate_subtitle.py" \
  "/path/to/videos/"

# Audio files recorded simultaneously (one device per person) → automatically align to one timeline, with separate tracks per device
$HOME/.local/pipx/venvs/funasr/bin/python \
  "$SKILL_DIR/generate_subtitle.py" "/path/to/audios/"

# Check video order / audio alignment and track assignments before running (no transcription; returns in seconds)
$HOME/.local/pipx/venvs/funasr/bin/python \
  "$SKILL_DIR/generate_subtitle.py" "/path/to/videos/" --dry-run
```

**Always run `--dry-run` first for multiple files**: verify order for sequential layouts, and alignment offsets and speaker-to-track assignments for parallel layouts. Start transcription only after confirming these.

#### ASR Engine: Check the Audio Language First

`--language auto` detects the audio language per file and physical track. Chinese follows the existing rules: FunASR when voiceprint diarization is needed; otherwise prefer FireRed (falling back to FunASR if unavailable). Non-Chinese or uncertain detection uses Whisper. For known Chinese audio, `--language zh` skips detection; do not set it based on the Skill documentation's language.

`--engine whisper` can explicitly handle Chinese or mixed-language audio. Non-Chinese audio is routed to Whisper with a notice even if a Chinese engine was specified. Single-track voiceprint diarization supports only the current Chinese FunASR pipeline; otherwise, keep separating by channel/device. `--device auto` prefers CUDA, then MPS, then CPU; the current Whisper adapter explicitly falls back to CPU/FP32 when MPS is selected. See the [multilingual notes](references/multilingual.md).

Measured differences between the two engines (the same 3-minute, two-person Chinese conversation, `--device mps`, LLM correction disabled, comparing raw ASR only):

- **FireRedASR2S is noticeably more accurate**: it gets almost all proper nouns right (`剪映` / FunASR hears “检验”; `skill` / “sale”;
  `代账机构` / “代购机”; `找代账` / “找代驾”). FunASR gets all of these wrong.
- **FunASR can hallucinate entire sentences**: “谁啊我认识吗？” becomes “晚上吃吧”; FireRedASR2S gets it right
- **Segmentation granularity**: FireRedASR2S's VAD+punctuation is finer (109 cues versus FunASR's 81 for the same 3 minutes, with almost identical text length). Particles such as “啊/嗯/诶”
  become separate cues and laughter is retained, matching spoken rhythm better; FunASR tends to join several sentences into one cue.
- **FunASR still wins occasionally**: English acronym casing (`VP` versus firered's `bp`; both can confuse them),
  and FireRedASR2S inserts a hallucinated phrase (“才越强大”) in a few long sentences
- **Runtime**: ASR time depends on device and engine; model-task time depends on the current session's model and subtitle size. Waiting for handoff does not trigger the script's model-call timeout.
  With `--device cpu`, FireRedASR2S is another 4 times slower (RTF≈2.2). This is only a local measurement of Chinese FireRed, not evidence of Whisper/MPS compatibility.

Outputs: `<名字>.raw.srt` (raw ASR, the diff baseline for correction), `<名字>.srt` (corrected combined subtitles),
and `<名字>.uncertain.srt` (subtitles requiring confirmation). Any material with identified speakers (dual-channel / parallel multi-device / `--speakers`)
also produces **one corrected file per person**, `<名字>.spk1.srt` / `.spk2.srt`…, plus raw per-track references `<名字>.spkN.raw.srt`.

**Separated tracks require two handoff rounds**: first cross-check tracks (reduce the same utterance picked up by several microphones to one copy),
then correct the full subtitle file. In each round, complete the tasks and rerun the same command.

### Step 2: Manually Confirm Uncertain Subtitles (**Do Not Skip**)

Drag `<名字>.uncertain.srt` into the editor, jump to each entry, and listen to the original audio. Apply confirmed corrections to `<名字>.srt`.
Each entry uses the format `【存疑】原文　→？候选改法1 ／ 候选改法2` (uncertain: original text → possible correction 1 / possible correction 2).

After editing, validate the structure to ensure the timeline has not been damaged:

```bash
python3 "$SKILL_DIR/validate_subtitle.py" \
  "Publish/<项目目录>/subtitles/合集.raw.srt" \
  "Publish/<项目目录>/subtitles/合集.srt"
```

### Step 3: Generate Sensitive/Offensive Content Review Subtitles

```bash
python3 "$SKILL_DIR/make_review.py" \
  "Publish/<项目目录>/subtitles/合集.srt" \
  --context "客户访谈录播"
```

### Step 4: Generate Key-Point Subtitles

```bash
python3 "$SKILL_DIR/make_highlights.py" \
  "Publish/<项目目录>/subtitles/合集.srt" \
  --context "Claude Code skill 教程"
```

Add `--with-review --with-highlights` to step 1 to chain the stages, but **it stops whenever uncertain subtitles are produced**.
After confirmation, run steps 3 and 4 separately; otherwise they would read text that is still incorrect.

### Common Parameters

`generate_subtitle.py`:

| Parameter | Description | Default |
|------|------|--------|
| `-o, --output` | Project directory or subtitle file path; if omitted, reuse or create a `Publish/` project and place subtitles in `subtitles/` (see “Output Directory Conventions”) | Reuse or create project |
| `--engine` | Keep FunASR / FireRed selection for Chinese; use Whisper for non-Chinese; `whisper` may also be selected explicitly | `auto` |
| `--language` | Audio-language hint, not a translation target (e.g. `zh`, `en`, `ja`) | `auto` |
| `--translate-to` | Translation target explicitly requested by the user; save separately after original-text correction; existing subtitles can be translated independently | No translation |
| `--whisper-model` | Multilingual model name or checkpoint path; actual language support is checked after loading | `small` |
| `--device` | `auto`=cuda if available, otherwise mps if available, otherwise cpu / `cpu` / `mps` / `cuda` | `auto` |
| `--format` | `srt` or `vtt` | `srt` |
| `--sort-by` | Sort key: `auto`/`ffprobe`/`birthtime`/`mtime`/`name` | `auto` |
| `--recursive` | Recursively search subdirectories in directory mode | Off |
| `--polish-level` | `minimal`=correct errors only, retaining all discourse particles / `clean`=also remove fillers | `minimal` |
| `--polish-mode` | `whole`=give the entire file to the agent for in-place editing / `batch`=use the JSON protocol in batches | `whole` |
| `--context` | Video topic/background supplied to the agent to judge recognition errors | Empty |
| `--glossary` | Additional glossary: file path or comma-separated terms (bundled `glossary.txt` is always loaded). **Also protects segmentation boundaries**; see “Do Not Split Words Across Two Subtitles” | Empty |
| `--max-chars` | Maximum display width per line (1 = one Chinese character; English letters/digits count as 0.5); alias `--max-width` | `16` |
| `--min-duration` / `--max-duration` | Minimum / maximum display duration per cue in seconds | `1.0` / `6.0` |
| `--dual-channel` | Transcribe separate tracks when left/right channels are microphones for two people: `auto` detect / `on` force / `off` disable | `auto` |
| `--gate` | Whether to apply **gating** to multiple tracks/dual channels (hard-mute other speakers): `auto` = only when inter-track separation ≥6 dB / `on` force / `off` disable | `auto` |
| `--pause-split` | Force a split when an internal pause exceeds this many seconds, preventing two people's speech from entering one cue (`0` = disabled) | `0.6` |
| `--speakers` | Enable **voiceprint** speaker diarization (cam++): split cues by speaker, combining overlapping speech into two lines | Off |
| `--speaker-count` | Known speaker count; with `--speakers`, fixes cam++ cluster count and determines how many main speakers bystander filtering retains | Automatic |
| `--probe-speakers` | **Probe only, no transcription**: run cam++ on an opening sample, report speaker count + recommended parameters, and write no files (see step 0) | Off |
| `--probe-seconds` | Probe sample duration in seconds | `300` |
| `--speaker-labels` | Add the “spkN: ” prefix to every cue (by default, only combined two-line cues for simultaneous speakers are labeled) | Off |
| `--layout` | How files are combined: `sequential`=end to end (multiple videos) / `parallel`=simultaneous devices, aligned by audio and separated into tracks / `auto`=parallel if all files are audio and align successfully, otherwise sequential | `auto` |
| `--offsets` | Manually specify each file's start offset in seconds for parallel layouts (comma-separated, in sorted file order); skips automatic alignment when provided | Empty |
| `--keep-trailing-punct` | Preserve cue-ending punctuation (commas, periods, **question marks, and exclamation marks**) | Off (removed by default) |
| `--dry-run` | Print only video order and time offsets | Off |
| `--with-highlights` / `--with-review` | Also generate highlights / review subtitles | Off |
| `--no-llm` / `--no-cache` | Do not create model tasks / disable ASR result caching | Off |

`make_highlights.py`:

| Parameter | Description | Default |
|------|------|--------|
| `-o, --output` | Highlight subtitle output path | `<名字>.highlights.srt` |
| `--context` | Video topic/background | Empty |
| `--per-minute` | Maximum prompts per minute | `1.2` |
| `--min-gap` | Minimum gap between adjacent prompts in seconds | `8.0` |
| `--no-llm` | Do not create model tasks; split chapters only at long pauses | Off |

`make_review.py`:

| Parameter | Description | Default |
|------|------|--------|
| `-o, --output` | Review subtitle output path | `<名字>.review.srt` |
| `--context` | Video topic/background to help determine what is sensitive | Empty |
| `--plain` | Include only the original text in subtitles, without the `【类别·严重度】` (category·severity) prefix | Off |
| `--no-llm` | Do not create model tasks (skip this feature entirely; rules cannot make semantic judgments) | Off |

### Environment Variables

| Variable | Description | Default |
|------|------|--------|
| `SUBTITLE_DEVICE` | ASR device: `auto` / `cpu` / `mps` / `cuda` (see multilingual notes for Whisper's MPS fallback) | `auto` |
| `SUBTITLE_MAX_CHARS` | Maximum display width per line (1 = one Chinese character; English letters/digits count as 0.5) | `16` |
| `SUBTITLE_BATCH_CUES` | Subtitle cues sent to the agent per batch | `100` |
| `SUBTITLE_BATCH_CHARS` | Maximum characters sent to the agent per batch | `4000` |
| `SUBTITLE_CONTEXT_CUES` | Read-only context cues included before and after each batch | `12` |
| `SUBTITLE_HANDOFF_DIR` | Handoff task root directory | `<输出目录>/.subtitle_tasks` |
| `SUBTITLE_CACHE_DIR` | ASR result cache directory | `<输出目录>/.subtitle_cache` |
| `SUBTITLE_LLM` | Whether to enable the agent: `0` or `1` | `1` |
| `SUBTITLE_GLOSSARY` | Glossary: file path or comma-separated terms | Empty |

## Output Files

After the three steps, the output directory contains:

| File | Contents |
|------|------|
| `<名字>.raw.srt` | **Raw ASR subtitles** (before correction), **aligned cue by cue** with the main subtitles so a direct diff shows which characters changed. For dual-channel audio, the snapshot is taken **after cross-track checking**: checking deletes cues, so writing raw beforehand would leave mismatched counts and cause `validate_subtitle` to report legitimate deletions as “结构性损伤” (structural damage). See `.spk1/.spk2` and task directories for what was deleted. Overwide cues are split by the same rules in raw and main subtitles, but cues lengthened by correction may split differently, appearing as new timecodes in a diff |
| `<名字>.srt` | **Combined subtitles** (after cross-checking + correction), for burning into video; simultaneous speech becomes two-line cues, each line with a `spk1: ` prefix |
| `<名字>.spkN.srt` | Only when speakers are separated: corrected subtitles **per speaker**, on the combined timeline, single-line without prefixes, for separate burning or checking by that person |
| `<名字>.spkN.raw.srt` | **Raw** per-track reference: ASR text for each track (channel / device), including cues classified as crosstalk (prefixed `【串音】`). Use to inspect what the machine dropped |
| `<名字>.uncertain.srt` | **Subtitles requiring confirmation**: passages the machine finds incoherent but cannot confidently correct, plus candidates; drag into the editor and jump to each entry to check the original audio |
| `<名字>.dedupe.md` | **Consecutive-repetition deduplication comparison list**: one line per cue, `[状态] 去重前 → 去重后` (status, before → after); `已回滚` means the agent identified an incorrect removal and restored it. Scan to ensure rules have not damaged meaning |
| `<名字>.manifest.json` | Layout (sequential / parallel), source file order and sort basis, and each file's start offset on the timeline; parallel layouts also include alignment confidence, clock drift, and each file's channel → speaker mapping |
| `<名字>.highlights.srt` | **Key-point subtitles (highlight overlays)** for overlaying on the image; key-point prompts and `【N】` chapter prompts share one file |
| `<名字>.review.srt` | **Sensitive/offensive content review subtitles**; drag into the editor and jump through entries to decide on deletions or edits |
| `<名字>.review.md` | Risk review checklist: time / category·severity / original text / risk / suggested action |

Main subtitles:

```srt
1
00:00:05,605 --> 00:00:08,960
第二个是这里的交互式导入都是我们的特色

2
00:00:10,305 --> 00:00:11,305
第三个是什么？

3
00:00:12,000 --> 00:00:14,800
spk1: 这个我们也是特色
spk2: 对对对
```

Cue 3 contains two people speaking simultaneously: combine them into one two-line cue, **labeling the speaker before each line** (`spk1: `).
Without labels, viewers know there are two people but cannot tell which line belongs to whom. Leave single-speaker cues unlabeled for a clean display.

Key-point subtitles: chapter prompts have a `【N】` prefix; highlight prompts are plain text. **Both types go in the same file**.

```srt
1
00:01:06,600 --> 00:01:09,200
理解本质比知道能做什么更重要

2
00:01:57,933 --> 00:02:01,433
【1】AI 能帮你磨平技术门槛
```

**Do not split chapters into a separate `.chapters.srt`**: the editor (tested with 剪映) accepts only 3 subtitle files per video.
Main subtitles + `.review.srt` + `.highlights.srt` fill those slots; dragging in a fourth only displays
“字幕/歌词以默认时间戳展示” (subtitles/lyrics displayed at default timestamps). To extract chapters separately, match the line-leading `【数字】` marker (a number in these brackets).

Review subtitles: **timecodes and text are copied verbatim from the main subtitles**, with only a category label prepended (remove it with `--plain`).

```srt
1
00:04:45,200 --> 00:04:52,700
【人身攻击·中】[示例：此处原样摘录待复核的攻击性话语]
```

## Processing Rules

### Who Does the LLM Work

Only the current session's model handles it, through **handoff**. The script writes input and `TASK.md` into a task directory and exits (exit 10). Complete `out.json`, or edit in place and write `DONE`, then rerun the original command to continue.

Task directories include an input-content hash: unchanged input reuses results; changed subtitles or prompts generate new tasks. Task files remain in `.subtitle_tasks/` for recovery across reruns. Invalid result formats prompt correction; invalid subtitle structure can fall back to batched tasks.

ASR runs locally; subtitle text, background, and glossary are read by the current session's model. If the session uses a cloud model, this text enters that model's context. Cache and task files may retain complete sensitive source text and should be managed as private material. `--no-llm` or `SUBTITLE_LLM=0` disables model tasks, making semantic correction, risk review, and model-generated highlights unavailable.

### Is Batching Still Needed?

With the agent-based approach, **input is no longer the constraint** (the agent reads files directly, with no command-line length limit; even 76 minutes and 1683 cues are only ~10,000 tokens).
The remaining constraint is **how much the agent must write**. The three tasks therefore have different answers:

| Task | Output volume | Batching strategy |
|------|--------|----------|
| Polishing | 1:1 with input (every cue must be returned in full; 1683 cues ≈ 34,000 tokens) | **Must batch**, 100 cues per batch |
| Highlights | A few dozen prompts for the whole recording | **One full pass** |
| Review | Only a few matching passages | **One full pass** |

- Polishing is batched because writing tens of thousands of tokens at once can omit entries or truncate output, and we need to validate each batch and retry after halving it. Letting the agent “write in chunks” itself prevents completeness validation.
- Batching highlights/review **only causes harm**: each batch sees only its own segment, so batch boundaries inflate chapter counts.
  In a measured example using the same 76-minute subtitle file, 9 batches produced **21 chapters** (one every 3.6 minutes), versus **7** in one full pass (one every 11 minutes), which was also faster (one cold start versus nine).
- The threshold for a full pass versus slicing is based on **text volume** (`fits_single_pass`: body text ≤60,000 characters, with a fallback guard of ≤4000 cues).
  Only exceeding that triggers slicing, at which point the chapter budget becomes “at most 3 per slice.”
  **Do not decide by cue count**: enabling speaker separation produces more, shorter cues for the same video length
  (measured: 76 minutes, 1765 → 2483 cues, still 24,000 characters). A cue-count cutoff would incorrectly classify it as too long and needlessly fall back to slicing.

**When sending the full file, guard against the agent reviewing only half**: for long subtitles, it may read several chunks and stop prematurely.
The review task's `out.json` therefore uses a coverage-marker protocol, `{"scanned_to": <实际审到的 cue>, "items": [...]}` (the cue actually reviewed through).
The script continues from `scanned_to` until the whole recording is covered; if it truly stops, it explicitly reports “后面 N 条未覆盖” (the remaining N cues were not covered), rather than pretending to have finished.
Highlights intentionally lack this protection: a missing tail can be spotted from the last chapter prompt's timecode,
whereas a missing tail in a review is **invisible**, giving false reassurance that there are “only 23 issues.”

### Multi-File Layout: Sequential vs Parallel

| Layout | Material | Timeline | Speakers |
|---|---|---|---|
| `sequential` | Several video segments from one session | Segment N offset = sum of durations of the first N-1 segments | Shared across segments (L/R channels = the same two people) |
| `parallel` | A conversation with **one recorder in front of each person**, each producing a file | All aligned to one shared timeline; earliest recording start = 0 | **Each valid track of each device = one speaker**, numbered globally |

`--layout auto` (default): if a directory contains **only audio files**, try parallel alignment first; use parallel only if alignment succeeds. If there is video or alignment fails, use sequential.
You can also force `--layout parallel` for videos (for example, two cameras each recording one person).

### Parallel Layout: Simultaneous Recording on Multiple Devices

- **Align using audio, not file times**: devices may start seconds or minutes apart, and file creation times often reflect export time.
  Cross-correlate each file's loudness envelope (one frame per 10ms); the peak position gives the time difference.
  Measured with two phones recording for 80 minutes: 80.75s offset, cross-correlation ncc 0.69, peak/background ratio 90 (unrelated recordings only 5~8).
  `ncc < 0.20` or peak ratio `< 15` means alignment failed (measured unrelated recordings: ncc 0.11, peak ratio 4). In auto mode, fall back to sequential; forced parallel raises an error asking for manual `--offsets`.
- **Drift**: device sampling clocks differ by tens of ppm. Measure offsets again in 3-minute windows every 10 minutes,
  then fit a slope. Apply `speed` correction only when accumulated drift across the recording is ≥50ms (measured: two phones drifted only 30ms over 70 minutes, below threshold).
- **Track assignment**: each file first undergoes left/right channel detection (the same three criteria as single-file mode). Use two tracks if it contains two people;
  otherwise mix the file into one track. **2 files allow at most 4 speakers, subject to actual analysis**.
  In the example, both files are dual mono (left/right correlation 1.00), yielding 2 tracks.
- **The same source recorded twice**: two tracks from different files whose normalized envelopes correlate ≥0.90 on active frames are treated as the same person
  recorded on two devices. Keep only the earlier-started copy and record this in the manifest.
- **N-track gating** (a generalization of `dual_channel.separate`, **enabled according to separation**):
  place all tracks on the shared timeline, first **normalize gain per device** (using p90 of each file's active-frame loudness,
  approximately the close-talk level of the nearest person; do not normalize a file's left/right channels against each other),
  then compare “this track − loudest other track” per frame and hard-mute it when at least 2 dB quieter.
  Devices have much more crosstalk than lavalier microphones (measured median inter-track difference for phones: only 2.9 dB).
  Gating such material is **net harmful**, so the default measures separation before deciding; see the next section.
- **People the devices cannot separate**: two people sitting in front of one device are on the same track (in the measured example, `New Recording 7`
  captured two people, while `标准录音 3` was close to the third). Device-level separation identifies only the device;
  **it cannot separate further**. In track mode, track numbers overwrite `--speakers` results, so adding it does nothing
  (see “Known Limitation: `--speakers` Has No Effect in Track-Separation Mode”). Use `--probe-speakers` to identify the device,
  then run voiceprint diarization on that file separately and manually align/merge the timelines.
- **Cost**: each track is transcribed separately; ASR time = track count × single-track time. Alignment itself takes only seconds.
- **The ASR cache key includes a hash of the alignment plan**: adding a file changes gating, so old separated-track results are not reused incorrectly
- Alignment results and track definitions are cached in `.subtitle_cache/parallel-*.json`, avoiding audio re-decoding on each handoff resume

### Combine Multiple Videos into One Subtitle File (Sequential Layout)

- **Sorting**: `auto` first reads container metadata `creation_time` (actual recording/export time). If any file lacks it, fall back to file `birthtime` for the entire set. **Never mix the two clocks**, or ordering becomes unreliable.
- **Timeline**: all subtitles for video N are offset by the sum of `ffprobe` durations for the first N-1 videos. The resulting timeline represents those videos joined end to end; editing must use the same order without trimming starts or ends.
- Order and offsets are written to `manifest.json` for later verification

### Correction Levels (Default: Correct Errors, Do Not Rewrite)

Spoken expressions such as “就是”, “那个”, and “对吧” are part of the speaker's actual delivery. Removing them makes the subtitles sound unlike that person.
The default is therefore **minimal: correction only, no polishing**.

| Level | Does | Does not |
|------|--------|----------|
| `minimal` (default; the specialized rewrites below apply only to Chinese) | Homophone/near-homophone errors, **deduplication of consecutively repeated characters/phrases** (“他他需要”→“他需要”, “把这个把这个”→“把这个”), proper nouns, **years converted to Arabic numerals** (“一九年”→“19年”), Chinese–English spacing | **Remove any discourse particles or colloquialisms**, reorder words, substitute words, or add punctuation |
| `clean` | All of the above + remove fillers (呃嗯啊那个就是), delete entire filler-only cues | Reorder or substitute words |

- Use `--polish-level clean` for filler removal; use it only when you need to tidy the transcript for someone else to read
- **Proper nouns**: the skill bundles `glossary.txt` (automatically loaded; supports `错写法1, 错写法2 → 正确写法` mappings: incorrect spellings → correct spelling).
  Extend it with `--glossary`. The agent must also **infer unlisted terms from context** and keep each term consistent throughout;
  measured examples correctly fixed `coso`/`cos` → `Cursor` and `web coding` → `vibe coding`.
- **Always retain the original when unsure**, and include it in subtitles requiring human confirmation (below)
- When model tasks are disabled, minimal mode does no semantic correction but retains rule-based deduplication, year conversion, and formatting for clearly Chinese content; non-Chinese stays unchanged. Rules cannot correct recognition errors, and indiscriminate filler removal only makes things worse.
- **Batching**: by default, 100 cues / 4000 characters per batch (whichever comes first), **plus 12 read-only context cues on each side**.
  Batches cannot grow indefinitely because polishing must return every cue in full: the bottleneck is **output**, not input. Hundreds of cues at once lead to missing or truncated responses.
  Read-only context need not be returned, so it cheaply supplies surrounding information crucial to correcting errors and judging sentence boundaries.
- **Missing-response protection**: fewer than 80% of a batch's cues returned counts as failure and triggers splitting; a small number of missing cues retains the original with a notice
- **Automatic fallback on failure**: missing/unparseable answers → retain the original task and request completion; invalid result structure → split and retry; if a single cue still fails, fall back to rule mode without losing content
- **Rule mode (model tasks disabled)**: only compress consecutive particles, deduplicate repeated speech habits, and remove sentence-final particles; recognition errors are not corrected

### Chinese Year Conversion (All Levels for Chinese Content)

Writing spoken years such as “一九年” and “二零二五年” in Chinese makes them harder to recognize and wastes display width; viewers must mentally convert them.
Convert years consistently to Arabic numerals: **“一九年”→“19年”, “一四年”→“14年”, “二零二五年”→“2025年”**.

**Convert years only, not durations**: “十年前”, “一年之后”, “八年”, and “半年” express spans, not calendar years; numerals would look unnatural.

Like deduplication, this has **two layers: rules + agent** (`normalize_year_digits`, after deduplication and before correction; prints which items changed):

| Who | Responsibility |
|---|---|
| Rules | Highest-confidence cases: pure Chinese digits (no 十/百/千), 2 or 4 digits (4 digits limited to 19xx / 20xx), with no immediately preceding digit |
| Agent | Cases rules leave alone (such as “零几年” and “两千年”), plus restoring Chinese where rules converted incorrectly |

Two families are deliberately left untouched by rules (wrong conversion is more conspicuous than no conversion):

- **Anything containing “十” stays unchanged**: “十年”, “二十年”, and “十二年” are not calendar-year notation
- **Consecutive ascending two-digit pairs stay unchanged**: “三四年”, “六七年”, and “五六年” are usually approximate durations (三四年 = three to four years),
  not the year 1934; let the agent judge from context
- **Four digits must be 19xx / 20xx**: spoken four-digit years are mostly in these two families; other four-digit sequences are usually stumbles
  (in a measured case, ASR joined “我一五、一九年就觉得” into “一五一九年”, which an early rule converted to 1519年)

Related behavior: Chinese–English spacing in `finalize_text` exempts **digits + time units**,
so “19年” is not split into “19 年” (no space before `年月日号点岁`). Years in highlight overlays also use Arabic numerals.

### Deduplicating Consecutive Chinese Characters / Phrases (All Levels for Chinese Content)

Repetition such as “因为你你否则你就把…” does not convey tone: ASR emitted a character twice, or the speaker stumbled.
It hurts readability and wastes line width. In addition to agent deduplication, there is a **model-independent rule fallback**
(`dedupe_stutter`, runs in minimal / clean with or without an agent, printing the number of changed cues):

| Rule | Action | Boundary |
|---|---|---|
| Single character | Applies only to pronouns/function words (`你我他她它您咱了是就都也很这那`), compressing only **exactly two consecutive copies** | Three or more copies are often real emphasis (“对对对”); let the agent judge |
| Two-character colloquialisms | Compress only whitelist entries (就是、然后、那个、这个、对吧、其实、所以、因为、但是、可能、我们、你们、他们、如果) | Leave others untouched to avoid damaging “研究研究”, “商量商量”, and “一个一个” |
| Three or more characters | Compress any immediately repeated whole sequence (“把这个把这个”→“把这个”, “我觉得我觉得”→“我觉得”) | Only **adjacent** repetitions; intervening characters exclude them |

The single-character list deliberately **excludes** characters whose doubled appearance can involve another word: 把 (把手), 要 (要求), 会 (会见),
还 (还钱), 得 (得到), 在 (在职), 地 (地地道道), 着 (着急), and **的 (“商业目的的解决方案” contains “目的” + “的”)**.
Including these would turn “你把把手拆了” into “你把手拆了”.

Whole-sequence deduplication must also spare two families of **legitimate Chinese repetition**, both discovered through actual false removals:

- “A 的 A” possession chains: “他们的**老大的老大**” ≠ “他们的老大” → leave units beginning with “的” unchanged
- Free-choice constructions with interrogatives: “你想**怎么读怎么读**”, “要**什么给什么**” → leave units containing `怎么/什么/多少/哪儿/哪里` unchanged

### Can Deduplication Remove Valid Text? Two Review Layers

Rules inspect surface text, not meaning, and whitelists guard only **known** families. Deduplication is therefore not finished just because it ran:

1. **The agent reviews it in the same correction round** (no extra round): deduplication runs **before** correction.
   Whole-file tasks include an extra read-only `dedupe.tsv` (`条目序号 <TAB> 去重前 <TAB> 去重后`: cue number, before, after),
   requiring restoration from column two whenever meaning was damaged. Batch mode cannot see the comparison table,
   but its correction prompt describes typical false removals so the agent can restore text that no longer reads coherently.
2. **A human scans `<名字>.dedupe.md`**: one line per cue, `[状态] 去重前 → 去重后` (status, before → after).
   `已回滚` means the agent identified an incorrect removal and restored it. A 1500-cue file usually produces only one or two hundred rows, quick to scan;
   if a removal damaged meaning, restore the characters directly in the main subtitles.
3. Without an agent (`--no-llm`), layer 1 does not exist; completion explicitly warns “没人复核误伤” (no one reviewed incorrect removals)

- Deduplication runs **after** `.raw.srt`, so raw retains the corresponding ASR engine's original text and a diff immediately shows removed characters
- In the measured 76-minute interview, rules changed **216** of 1563 cues. Before the first whitelist included the two guards above,
  manual review of 217 changes found **3 actual false removals** (`商业目的的解决方案`, `老大的老大`,
  `你想怎么读怎么读`). Adding the guards protected all 3; it also recovered 2 cases that should have been deduplicated
  (`的那那个`, `的那那种`, previously blocked by the lookbehind for “的”).

### Subtitles Requiring Confirmation (Human Decision)

If the rule is “keep the original whenever unsure,” uncertain passages must not remain silently in the subtitles.
The **same agent call** that performs correction also writes `uncertain.json` (no extra round).

**There is just one criterion: report only what would stop a reader or be remembered incorrectly as fact.**

| Report | Do not report |
|---|---|
| Proper nouns/product names/terms that cannot be inferred (`欧英one` → all in one, `tret` → chat) | The speaker's own slips or reversed wording (“大众创新万众创业”): they really said it that way |
| Idioms/fixed expressions recognized as gibberish (`造访天干` → 倒反天罡) | Extra, missing, or repeated characters when the meaning is clear (“本身就是物异化了”, “产品不能呃要消失”) |
| Personal names, company names, numbers (`章山` → 张三, a fictional-name example) | Fragments of discourse particles and colloquialisms |
| Semantically broken sentences or fragments whose intent cannot be guessed | Near-synonym errors that do not affect understanding (“生产目标” actually meant “生产模式”) |
| Two people's speech mixed into one cue | Anything already corrected in the subtitles (that would be noise) |

These criteria come from **hands-on user feedback**: after checking a list of 24 items, the user changed only left-column cases (proper nouns, idioms, names).
Every right-column case was left unchanged because they were “just particles or slips that do not affect understanding.”
Over-reporting forces people to dismiss items one by one and is more annoying than missing them.

For each item, provide **original text, the uncertainty, 2~3 candidate corrections, and the reason for suspicion**. Prefer fewer reports to padding the list.
These passages retain the corresponding ASR engine's original text in the main subtitles until you listen and correct them manually.

**Do not trust timecodes copied by the agent**: the list is used to jump through the editor, and wrong times make it useless.
The agent copies timecodes by hand: in a measured set of 24 items, 7 were wrong. **Milliseconds were almost always correct** (the three digits look random, making copy errors noticeable),
but **all errors were in the minutes** (+1 / +2); two entries even copied a neighboring cue's time.
Thus `anchor_uncertain` matches each item's verbatim **original text** back to the subtitles and takes the matched cue's actual timecode.
Agent-supplied times are downgraded to disambiguation hints (choose the nearest when a sentence occurs multiple times). Unmatched items receive `【时间未校准】` (time not calibrated).

### Left/Right Channels = Two People (Automatic Dual-Track Separation)

Conversation recordings (such as dual-transmitter DJI Mic setups) often have **one lavalier microphone per person, each on its own channel**.
The channel itself is then the most reliable speaker label, much more accurate than cam++ voiceprint clustering.
Thus `--dual-channel auto` (default) checks this first and uses two tracks if the conditions hold:

- **Detection** (all three must hold; see `dual_channel.analyze`):
  1. Waveform correlation between channels < 0.5; stereo and dual mono are both close to 1 and are immediately ruled out
  2. **Both** sides have enough speech frames where they are clearly louder than the other by ≥6 dB (their own close-talk segments)
  3. The weaker side must not be too short in absolute duration (≥5 seconds and ≥5% of active speech), avoiding mistaking occasional crosstalk for a second person
- **Suppressing crosstalk (`--gate`, only with sufficient separation)**: the other person's voice leaks into this channel at -6~-12 dB.
  Compare track loudness in 20ms frames (100ms smoothing); frames where this track is quieter than the other are **set directly to zero**
  (with a 120ms hold before and after to avoid clipping word beginnings/endings).
  **It must be hard muting, not attenuation**: the ASR frontend uses CMVN, so reducing the entire signal by 25 dB has almost no effect.
  In a measured case, the track attenuated by 25 dB still transcribed the other person's entire monologue (790 characters versus 245 after muting).
- **Gating is valid only when each track really captures its own person**, so first measure **inter-track separation**
  (the median absolute value of “this track − loudest other track” on active frames, `dual_channel.separation_db`).
  Below 6 dB, skip gating entirely and leave separation to the sentence-level cleanup below; see “When Not to Apply Gating.”
- **Transcribe both tracks separately**, using channel numbers as speaker numbers. **ASR time doubles**
  (measured 20-minute video: channel analysis 20 seconds, L track 3.5 minutes, R track 5 minutes, total 8.5 minutes; single-track about 4 minutes).
  VAD skips silence, but the fragmented track has more segments with fixed per-segment overhead, so the time is not saved.
- **Crosstalk cleanup** has two layers, both using energy after ASR (text cannot reliably identify it: forced decoding of leaked speech produces a muddle
  such as “对对对对对，嗯嗯，好，嗯嗯，分不清楚”, with too little similarity to the actual utterance):
  1. **Drop sentences spoken entirely by the other person**: take p75 of this track's dB difference from the other across the sentence; drop it if below 0.
     Use p75 rather than the mean to preserve a brief “嗯” interjected during someone else's long speech:
     it counts as long as this track dominates for more than a quarter of the sentence.
  2. **Deduplicate across tracks**: when temporal overlap ≥50% **and** text similarity ≥0.55, drop the lower-energy side
     (overlap without similarity = genuine simultaneous speech; never delete it)
- **Cross-track checking** (agent, one round before correction): two tracks provide **two independent recognitions** of the same utterance.
  The two mechanical cleanup layers above use only energy and surface text; mismatched sentence boundaries can evade them
  (measured: the same passage was one 26-second L-track sentence and one 10-second R-track sentence, below the text-similarity threshold).
  Give each region with speech on both channels to the agent, with 2 context cues on either side, for semantic judgment:
  same utterance captured by two microphones → delete the garbled copy; two people actually saying different things → delete neither; uncertain → leave unchanged.
  Deleting more than half the cues in a region is treated as a bad result and invalidates the whole round (wrong deletion = permanent loss of one person's speech).
- **Final subtitles per speaker** (`.spk1.srt` / `.spk2.srt`): filter the corrected combined subtitles by person,
  retaining the timeline, single-line without prefixes, for separate burning or checking by that person. There is also a **raw** per-track reference,
  `.spkN.raw.srt`, for inspecting what the machine classified as crosstalk. Deleted cues remain verbatim here with a `【串音】` prefix,
  so humans can see incorrect classifications.
- **Simultaneous speech** (two people actually talking at once) becomes **one two-line subtitle**, with **`spk1: ` / `spk2: ` before each line**.
  Without labels, viewers know there are two people but not which line is whose (even worse with three or four people).
  Single-speaker cues stay unlabeled; add `--speaker-labels` to label every cue.
  Merging has two hard constraints: **at most two lines**, and total span no longer than `--max-duration`.
  If exceeded, do not merge; display each separately (without limits, a measured case grew into an 11.9-second, three-line block).
- Channel analysis is cached in `.subtitle_cache/dual-*.json`; each track has its own ASR result cache

If detection fails (mono, mixed stereo, or only one person speaking), automatically return to the original single-track workflow; no intervention is needed.

### When Not to Apply Gating (What `--gate auto` Checks)

Gating (hard-muting frames spoken by others) is valid only when **each track really captures its own person**.
After track separation, first measure **inter-track separation**: the median absolute value of “this track − loudest other track” on active frames
(`dual_channel.separation_db`). In `auto`, skip gating entirely below **6 dB**.

Measured values for two types of material:

| Material | Inter-track separation | Gating |
|---|---|---|
| Lavalier microphones / one close-talk mic per person (crosstalk around -20 dB) | 13~20+ dB | On |
| Two phones on the same table, both recording everyone | **2.9 dB** (36% of frames differ by less than 2 dB) | Off |

**Why gating makes low-separation audio worse** (four comparisons of the same 3.5-minute material):

| Configuration | The sentence | Two-line simultaneous-speech cues |
|---|---|---|
| Mix first, then transcribe one track | `…服务相关的主播` + `这个现在不是我的主页` | 0 (structurally impossible) |
| Separate tracks + gating hold 300ms | `…服务相关的主播` + `这个现在不是我的主页` | 4 |
| Separate tracks + gating hold 120ms | `不过这个现在不是我的主页` (initial “只” clipped) | 9 |
| **Separate tracks + no gating** | `只不过这个现在不是我的主页` ✅ | **13** |

- With separation around only 2 dB, gating inevitably clips word beginnings at **speaker-change boundaries** (`只不过` → `不过`)
- Increasing `HOLD_MS` cannot fix this: 120 → 300ms lets crosstalk back into the track, reproducing the mixed-audio error `主播`
- Stronger gating yields fewer simultaneous-speech cues because crosstalk contamination makes the two tracks too similar and cross-track deduplication removes them as duplicates, rather than because separation is cleaner
- Fundamentally, **the two errors have unequal costs**: gating multiplies the waveform by 0 **before ASR**. A wrong decision destroys audio that no later stage can recover.
  Sentence-level energy attribution is **non-destructive**: if wrong, the original text remains in `.spkN.raw.srt` with a `【串音】`
  marker, where a human can recover it.

With gating off, speaker separation relies entirely on three post-ASR stages: sentence-energy crosstalk detection → overlap + text-similarity deduplication →
agent cross-track checking. The burden is indeed greater (measured: 65% of cues on the weaker track classified as crosstalk), but every decision is traceable.

Force behavior with `--gate on` / `--gate off`. Separation is printed and, when gating is skipped, recorded in `manifest.json` under `notes`.

### Voiceprint Speaker Diarization (Existing Chinese FunASR Pipeline Only; `--speakers`; Unnecessary for Dual-Channel Material)

The most common error in a mono multi-person conversation is not a typo but **two people's speech being combined into one subtitle**.
An incoherent sentence such as “近来我们陆续探讨一下这个一个夹在哪” is essentially half a sentence from A + half from B.
A correction model cannot fix this alone (it will only guess something fluent); speakers must be separated when cues are created.

Left/right track separation already solves this physically, so **do not enable `--speakers` for dual-channel material**.
Only material recorded in mono needs it. With `--speakers`:

- ASR additionally loads `cam++`; `sentence_info` includes `spk`, and **each cue contains only one person's speech**
- Different speakers whose speech actually overlaps in time (≥200ms) = **simultaneous speech**. `normalize_timeline` no longer trims that overlap
  (which would pretend they took turns). Before rendering, `merge_speaker_overlaps` combines it into **one two-line subtitle**,
  one person per line, automatically prefixed “spkN: ” so the lines are distinguishable.
- Non-overlapping cues **have no prefix by default**, keeping them clean for burning into video; add `--speaker-labels` to label all cues
- Merging occurs only at the **final rendering stage**: polishing, uncertainty lists, review, and highlights all work with “one cue = one utterance by one person.”
  Granularity stays unchanged, and the agent never sees prefixes it might alter incorrectly.
- Cost: the ASR cache key includes `spk=`, so **toggling this parameter retranscribes the whole recording** (about 30~40 minutes for 76 minutes)

#### Bystander Filtering: Not Every Distinct Voice Is a Participant

cam++ performs **unsupervised** clustering. It answers whether utterances come from the same voice, not whether that voice belongs to a participant in this session.
A passerby saying a few words, the next table, and background TV can each become a separate “person.”

The real problem is not a mislabeled name: when `merge_speaker_overlaps` sees cues from **different speakers** overlap by
200ms, it combines them into a two-line subtitle with `spkN: ` prefixes. A background cough can thus conjure an extra participant in the final video.

After identification, first rank speakers by speaking duration (`demote_bystanders`):

- **With `--speaker-count N`** → keep exactly the top N. Note that cam++ itself has already been constrained to N clusters by `preset_spk_num`,
  so this filter is defensive in practice.
- **Without it** → demote only clusters low in both measures: speaking-time share < 3% **and** absolute duration < 20 seconds; both conditions must hold
- **Do not delete** identified bystanders (a wrong deletion permanently loses speech). Retain their text in subtitles, but **omit speaker labels and exclude them from simultaneous-speech merging**
- Each cluster's duration and share are written to `manifest.json` under `speaker_clusters`, and also printed on one terminal line

**Why duration rather than loudness**: a mixed single track lacks reliable per-person energy envelopes (available only in the dual-channel/multi-device path;
see `dual_channel.mark_bleed_sentences`). Duration ranking is unfavorable to real participants such as a host who asks only three questions,
so **always pass `--speaker-count` when you know the count**; do not make it guess.

Measured caution: cam++ does not over-cluster much on close-talk recordings (adding 3 seconds of another person's voice at the start
simply merged it into an existing cluster, without creating another person). This filter is a safety net, not something that normally triggers.

#### Known Limitation: `--speakers` Has No Effect in Track-Separation Mode

Both dual-channel and parallel multi-device track separation **overwrite** `spk` with the track number after ASR
(`item["spk"] = track.speaker` / `= speaker` in `generate_subtitle.py`),
discarding cam++ clustering entirely. Thus **adding `--speakers` does nothing when two people sit in front of one device**;
it only wastes additional loading and transcription work.

Current workaround: run that file separately with `--speakers`, then manually align and merge timelines.

### Single-Line Width: At Most 16 Chinese Characters per Line (Hard Limit)

At typical font sizes in portrait video, a line longer than 16 Chinese characters is automatically wrapped by the player.
The **second line is reserved for two people speaking simultaneously** (see overlap merging). Single-line width is therefore a hard limit:

- **One width unit = one Chinese character**: full-width/CJK counts as 1; English letters, digits, and half-width punctuation count as 0.5.
  Thus “2 English characters = 1 Chinese character,” and `--max-chars 16` means 16 Chinese / 32 English characters per line.
- Punctuation and spaces **count toward width**, since they occupy screen space too
- A single-speaker cue is **always one line**; for merged two-line simultaneous speech, **each line also stays within 16**
  (the “spkN: ” prefix does not count toward this budget)
- Older versions allowed a 1.6× tolerance (a limit of 20 let through 32 characters), creating visible two-line subtitles; that tolerance has been removed
- **The sole exception is a split that would break a term**: if no valid boundary exists, allow an overwide line,
  capped at 1.35× (`TERM_OVERFLOW`). The visual costs are unequal: a slightly overwide line may wrap,
  but splitting “Refore HTML” / “to Figma” damages meaning and prevents viewers from identifying the product.
  See “Do Not Split Words Across Two Subtitles” below.

### Segmentation and Timecodes

- All timecodes come from ASR token-level timestamps, without stretching or shifting
- Split at punctuation, then merge within `--max-chars` (single-line width). Long unpunctuated sentences split only at token boundaries,
  **never cutting an English word in half**.
- **Force a split at internal pauses ≥`--pause-split` (default 0.6 seconds); never merge the two sides into one cue**.
  This guards against combining two people's speech: FunASR joins text from all VAD segments into **one continuous sequence**
  for ct-punc, then `timestamp_sentence()` splits **only by punctuation**. VAD boundaries,
  the strongest evidence of a speaker change, are discarded. Thus “A finishes, 1.4-second pause, B responds” becomes one cue unless ct-punc
  inserts a period there. `cam++`'s `distribute_spk()` then assigns **the entire sentence** to the speaker with the greatest temporal overlap,
  also misidentifying the speaker. In a measured 82-minute conversation with 2604 ASR sentences,
  8% contained pauses >700ms. The 0.6-second threshold reflects token-gap p95 = 220ms and p98 = 500ms,
  beyond normal breaths. A split at a 0.6-second pause is natural subtitle rhythm anyway, so over-detection does no harm.
- For long sentences, **first greedily find the minimum number of pieces, then resplit into that many equal-width pieces**: width 26 with limit 16 becomes 13+13,
  not 16+10, avoiding a stranded tail (fall back to greedy if any equal-width piece exceeds the limit).
- **Equal-width boundaries snap to word boundaries** (`_snap_to_pause`): each Chinese character is an ASR token,
  so width alone does not know words. Splitting 24 characters at limit 16 into 12+12 may land inside a word, as in “…也很需要营 | 销相关的…”.
  Choose candidates in a target-width ±25% window (without exceeding the hard limit), prioritizing
  **word starts (jieba segmentation) > longer preceding pauses (token timestamps: gaps within words ≈0, between words tens to hundreds of milliseconds)
  > closer to the equal-width target**. Measured within-word splits fell from 38.8% → 2.4% (0.8% with mandatory pause splits).
  jieba is a soft dependency: if unavailable, omit that criterion without raising an error.
- **A final fallback runs before rendering** (`enforce_line_width`): correction can lengthen text (`cos` → `Cursor`,
  adding Chinese–English spaces). Cues still too wide are split into two at token boundaries, with time divided proportionally by each piece's display width.
  This acts only on rendering copies; uncertainty lists must anchor back to original text, so cue granularity must remain unchanged. It prints the number split.
- Before output, sort, remove overlaps, extend short cues to the minimum duration, and trim overly long tails, ensuring no overlapping cues or reversed start/end times

### Do Not Split Words Across Two Subtitles (Term Protection)

If one subtitle ends with “浏览” and the next starts with “器”, viewers must assemble the word mentally. **Splitting product names** is worse:
measured output “接下来用 Refore HTML” / “to Figma 为例来介绍” breaks exactly the term viewers most need to read in a tutorial.

Previously, word boundaries were only a **soft preference** in `_snap_to_pause` (if no word-start candidate existed in the window, split anyway).
They are now a **hard constraint**: boundaries cannot fall inside protected spans; if no valid boundary exists, allow an overwide line (≤1.35×).

`protected_spans()` gets protected spans from four sources:

| Source | Protects | Examples |
|---|---|---|
| Multi-character/multi-word glossary terms | **Compound product names** containing spaces, which neither jieba nor token boundaries protect | `Refore HTML to Figma`, `Claude Code`, `MCP server` |
| jieba segmentation (≥2 characters) | Ordinary Chinese words | `浏览器`, `连接器`, `自定义` |
| Number + classifier/unit | A value with its unit | `3 个`, `19年`, `16 字` |
| Quantity phrase + head noun | Cases jieba misses: it splits “一句话” into “一句” + “话”, which is valid tokenization, but splitting across cues produces “用一句” / “话发给…” | `一句话`, `三个页面`, `一个流程` |

**The glossary therefore has two roles**: tell the correction agent the correct spelling, and tell the segmenter
which characters form a single unit. The “组合名 / 复合词” (compound names / compound words) section at the end of `glossary.txt` was added specifically for the latter:
protection only, no correction. Add terms outside this project with `--glossary`, using the same syntax.

**Merge cross-sentence seams before resplitting** (`repair_term_splits`, before `enforce_line_width`):
boundary adjustment works only within a single `wrap_spans` call, whereas “接下来用 Refore HTML” and “to Figma 为例来介绍”
are **two sentences** to ASR and never entered the same split operation. Before rendering, scan adjacent cues,
merge those whose seam lies inside a protected span, and send them to `enforce_line_width` for splitting with span protection.
The boundary moves outside the term, producing “接下来用 Refore HTML to Figma” / “为例来介绍”,
rather than simply outputting an extremely long cue. Do not merge if any of these four gates fails:

1. Both cues are single-line (leave merged two-line simultaneous speech alone) and belong to the same speaker
2. Time gap at the seam < 500ms (`JOIN_MAX_GAP_MS`); larger gaps are real pauses, and merging would desynchronize subtitles from lip movements
3. Combined width no greater than 3 times `--max-chars` (`MAX_JOIN_WIDTH`), preventing snowballing
4. The previous cue does not end with `。！？!?…` (a finished sentence, not a broken word).
   **Only a glossary match can override this**: a new word after a period is a perfectly normal boundary,
   but the period inside “这个代表你看 Refore。” / “HTML to Figma” can only be a punctuation-model mistake.
   Thus `glossary_spans()` (the glossary layer) may override it; the `jieba` word layer may not.
   Note that `finalize_text` has already removed ending punctuation; use `_sentence_end`, recorded before removal.

On completion, it prints “术语/词语被切在两条字幕里，已并回重切：N 处” (N terms/words split across subtitles were merged and resplit).

### Key-Point Prompts (Highlight Overlays)

- **Two types**: `chapter` (new chapter start, 6~12-character heading, typically 3~8 per recording), `highlight` (key information, 8~14 characters)
- **Select only things viewers would want to take notes on**: core concept definitions, insightful conclusions/quotable lines, key steps/commands/paths, easily confused comparisons, practical tips
- **Do not select**: verbatim restatements, summaries with no information, trivial operational details
- **Accuracy**: content must follow from the original subtitles; do not invent filenames, paths, numbers, or product implementation details
- **One full pass** (no batching when body text ≤60,000 characters): read the whole file before dividing chapters, with a chapter budget based on total duration (`min(8, max(3, 时长/8))`, where 时长 means duration)
- Only when extremely long subtitles force slicing does the budget fall to “at most 3 chapters per slice”; carry earlier chapter titles into subsequent slices
- Density is controlled by `--per-minute`; adjacent prompts must be at least `--min-gap` seconds apart, with chapter prompts prioritized if too close
- Anchor each prompt to the cue where that information **starts being discussed**; display chapter prompts for 3.5s and highlights for 2.6s, automatically avoiding the next prompt

### Sensitive/Offensive Content Review

Four categories, with High / Medium / Low severity:

| Category | Coverage |
|------|----------|
| Sensitive information | Real name + job title combinations, phone numbers, email, WeChat/QQ, addresses, ID and bank card numbers, passwords/keys, internal company data, client lists, quotes and salaries, unannounced plans |
| Personal attacks | Belittling, insulting, humiliating, or denying the competence of a specific individual |
| Disturbing content | Violence/gore, physically unsettling material, disease details, embarrassing or humiliating descriptions |
| Offensive content | Profanity, regional/gender/racial/age/occupational discrimination, stereotypes, disparaging named third-party companies or products |

- **Prefer over-reporting to omissions**: this is preliminary screening for human review, and missed issues cost far more than false positives. There is therefore **no** count limit like the one for highlights.
- **Verbatim excerpts**: copy the text character for character and take timecodes directly from the main subtitles; do not rewrite or summarize, so entries can be located precisely in the editor
- **Merge across cues**: use `cue`+`cue_end` for a risky passage spanning multiple cues; overlapping or immediately adjacent matches merge into one entry, retaining only the highest severity per category
- **Do not report**: normal technical discussion, product introductions, or evaluations of matters rather than people
- **One full pass**: read everything for accurate judgment, including cross-segment clues such as a name mentioned early and a phone number spoken later
- A notice appears if matches cover more than 30% of the recording (the model has probably gone off track)
- **Skip entirely** when model tasks are disabled: this is purely semantic judgment; rules cannot do it, and pretending otherwise creates false reassurance

## Checks Before Delivery

1. **Structural validation** (run on all three srt files):

```bash
python3 -c "
import re, sys
text = open(sys.argv[1], encoding='utf-8').read()
blocks = [b for b in re.split(r'\n\s*\n', text.strip()) if b.strip()]
for i, b in enumerate(blocks, 1):
    lines = b.strip().split('\n')
    assert len(lines) >= 3 and lines[0].strip() == str(i) and '-->' in lines[1], f'格式异常: 第{i}块'
ts = re.findall(r'(\d{2}:\d{2}:\d{2},\d{3}) --> (\d{2}:\d{2}:\d{2},\d{3})', text)
assert all(s < e for s, e in ts), '存在起止倒置'
print(f'OK: {len(blocks)} 条，编号连续，无倒置')
" "字幕文件.srt"
```

2. **Single-line width validation** (mandatory for main subtitles: at most 16 Chinese characters per line; two lines only for simultaneous-speech cues; `spkN: ` prefixes excluded).
   **Overwidth is not necessarily a bug**: term protection permits up to 1.35× (≤21.6), so the check below uses two thresholds.
   List lines exceeding 16 for human confirmation that a word is being protected; only those exceeding 21.6 are actual anomalies:

```bash
python3 -c "
import re, sys, unicodedata
w = lambda s: sum(1.0 if unicodedata.east_asian_width(c) in ('F','W') else 0.5 for c in re.sub(r'^spk\d+: ', '', s.strip()))
text = open(sys.argv[1], encoding='utf-8-sig').read()
bad, three = [], []
for b in [b for b in re.split(r'\n\s*\n', text.strip()) if b.strip()]:
    lines = b.strip().split('\n')[2:]
    three += [b] if len(lines) > 2 else []
    bad += [(w(l), l) for l in lines if w(l) > 16]
over = [(x, l) for x, l in bad if x > 16 * 1.35]
print(f'超 16 字宽 {len(bad)} 行（其中超 1.35 倍上限 {len(over)} 行 ← 这些才是异常），三行及以上 {len(three)} 条')
[print(f'  [{x:g}] {l}') for x, l in bad[:10]]
" "字幕文件.srt"
```

3. **Scan `.dedupe.md`**: the rule-based deduplication comparison list; verify that legitimate repetitions such as “老大的老大” and “目的的” were not damaged.
   `已回滚` entries were already restored by the agent during review; no action is needed.
4. **Manually review `.highlights.srt`**: highlights are shown to viewers, so invented content is costly; confirm that every prompt is supported by the original subtitles
5. **Check `.uncertain.srt` first**: these are places where the machine explicitly says it cannot make sense of the text or confidently correct it; fix them before generating review and highlights
6. **Manually confirm every `.review.md` entry**: this is machine screening and **cannot** be treated as “passed means safe.” Both false positives and omissions are possible; humans decide whether to delete or edit.
7. **Report clearly**: layout (sequential / parallel), file order and offsets (plus alignment confidence and channel → speaker mapping for parallel layouts), cue-count changes, corrected proper nouns, number deduplicated (and restored), uncertainty count, and review matches (including high-risk count)

## Technical Notes

- **ASR models (Chinese and multilingual paths, `--engine`)**:
  - `funasr` (Chinese voiceprint path): `paraformer-zh` + `fsmn-vad` + `ct-punc`; additionally loads `cam++` with `--speakers`
  - `firered`: FireRedASR2S = `FireRedVAD` + `FireRedASR2-AED` + `FireRedPunc` (Chinese transcription only; Whisper performs language detection). **No voiceprint diarization**: `--engine firered --speakers` immediately errors.
    Multiple speakers must be separated by channel or device using `--dual-channel` / `--layout parallel`.
  - `whisper`: non-Chinese and multilingual transcription, always `task="transcribe"`; word timestamps map to Unicode graphemes through text, and actual language support is checked per checkpoint.
  - Why not first-generation FireRedASR: it outputs one continuous text string, **without word-level timestamps, VAD, or punctuation**,
    and limits each input to 60 seconds. This script's cue splitting, start/end recalculation, and crosstalk detection all rely on word timestamps, so the first generation cannot integrate.
- **ASR receives extracted 16k mono wav** (`ffmpeg -vn`), without transcoding the video itself, so there is no file-size limit and it is much faster than transcoding.
  For two-person dual tracks, decode to 16k stereo instead, suppress crosstalk, and write two mono wav files (`dual_channel.py`).
- **Three return structures are supported**: FunASR with `cam++` uses `sentence_info`; without it, uses full `text` + token-level `timestamp`, splitting sentences at ending punctuation before assigning timestamps. FireRedASR2S directly provides segmented `sentences` + globally flattened word-level `words`, assigned to sentences in time order (`firered_asr.to_sentences`).
- **ASR results are cached** in `.subtitle_cache/` under the output directory (keyed by file path + size + nanosecond modification time + **actual engine/audio hint/detected language/model identity**, one per channel for dual tracks; changing engines does not reuse incompatible results). Changing parameters or rerunning polishing alone does not require retranscription.
  In the same directory, `dual-*.json` caches channel analysis + left/right loudness envelopes every 100ms (needed for crosstalk cleanup).
- **Multi-device alignment**: `multi_track.py`, FFT cross-correlation of 10ms loudness envelopes + windowed drift fitting + N-track gating
  (gating depends on separation; see `decide_gate`).
  Each track is decoded, gated, written to wav, and transcribed separately, then released; memory does not accumulate with track count.
- **ASR cache keys include the gating setting** (`nogate` / `m-2.0h120`): gated and ungated audio are different inputs,
  so old results are not reused incorrectly.
- **Supported formats**: video mp4/mov/mkv/avi/wmv/webm/m4v/3gp/mpeg/mpg/flv/ts; audio m4a/mp3/wav/ogg/flac/aac/wma/opus
- **Runtime**: ASR time depends on device and engine; model-task time depends on the current session's model and subtitle size. Waiting for handoff does not trigger the script's model-call timeout.
- **Exit codes**: `0` normal; `1` error; `2` translation paused due to uncertainty; `10` handoff tasks await your work (not a failure: complete the task list and rerun)

## Relationship to Other Skills

Use these companion skills as needed. `podcast-episode` provides optional `transcript_lib`: when available, also generate `transcript.json` and a persistent mixdown; when absent, skip these two outputs while retaining the core subtitle functionality.

- `subtitle-polish-spoken`: polish **existing** subtitle files. This skill already includes the same conservative polishing rules; use that one when polishing externally supplied subtitles.
- `subtitle-translate-en`: an existing dedicated Chinese-to-English Skill. This Skill's multilingual translation uses its own `translate_subtitle.py` handoff, without implicitly invoking a workflow that polishes the source subtitles again.
- `analyze-video-materials`: use for frame extraction, evidence frames, and frame-by-frame audiovisual analysis; this skill handles subtitles only
