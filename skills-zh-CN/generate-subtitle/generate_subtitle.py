#!/usr/bin/env python3
"""视频字幕生成脚本 —— 本地 ASR 原语言转写、保守纠错，可选 handoff 翻译。

转写引擎（`--engine`，默认 `auto` 自动选，调用方不用操心）：
- whisper：自动语言检测及非中文转写；始终保留原语言，不使用内置翻译任务
- funasr：paraformer-zh + fsmn-vad + ct-punc，可选 cam++ 声纹分离
- firered：FireRedASR2S（FireRedVAD + FireRedASR2-AED + FireRedPunc），中文/方言准确率更高，
  但**没有声纹分离**，多说话人只能靠分声道 / 分设备（见 firered_asr.py 开头）

特点：
- 输入可以是单个视频/音频，也可以是一个目录
- 目录：按「视频创建时间」排序，时间轴顺次拼接，最终只输出一个字幕文件
- 去水词 + 修正语音识别错别字（由当前会话处理 handoff 任务；关闭模型任务时使用规则模式）
- 时间码严格来自 ASR，不做任何拉伸平移

Usage:
    python generate_subtitle.py <video_or_dir> [-o output.srt] [options]

Environment Variables:
    SUBTITLE_ASR_ENGINE      本地 ASR 引擎: auto / funasr / firered / whisper (默认: auto)
    SUBTITLE_DEVICE          运行设备: auto / cpu / mps / cuda (默认: auto)
    SUBTITLE_MAX_CHARS       单行最大字宽，1 = 一个中文，英文/数字算 0.5 (默认: 16)
    SUBTITLE_BATCH_CUES      每批送 LLM 的字幕条数 (默认: 100)
    SUBTITLE_BATCH_CHARS     每批送 LLM 的最大字符数 (默认: 4000)
    SUBTITLE_CONTEXT_CUES    每批前后各附带多少条只读上下文 (默认: 12)
    SUBTITLE_LLM             是否启用 LLM: 0 或 1 (默认: 1)
    SUBTITLE_GLOSSARY        术语表：文件路径，或逗号分隔的词
"""

from __future__ import annotations

import argparse
import errno
import functools
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

# ModelScope references the Linux-only EREMOTEIO errno while handling cache
# races.  macOS (including Python 3.13) does not expose it, so provide the
# conventional Linux value before importing FunASR/ModelScope.
if not hasattr(errno, "EREMOTEIO"):
    errno.EREMOTEIO = 121

sys.path.insert(0, str(Path(__file__).resolve().parent))
from llm import (  # noqa: E402
    EXIT_HANDOFF_PENDING,
    check_agent_available,
    last_call_pending,
    load_glossary,
    parse_json_list,
    pending_handoff,
    pending_report,
    resume_command,
    run_agent_file_task,
    run_agent_task,
    set_handoff_root,
)
from validate_subtitle import check_structure, parse_text  # noqa: E402
from text_utils import (LANGUAGE_RULES, chinese_rules, grapheme_spans, sentence_boundary,
                        tokenize_spans, char_time_map, display_width)
from whisper_asr import normalize_language
from asr_router import ASRRouter
import dual_channel  # noqa: E402
import multi_track  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "podcast-episode"))
try:
    import transcript_lib  # noqa: E402  —— transcript.json（字幕唯一真相源）+ 混音持久化
except Exception:  # noqa: BLE001
    transcript_lib = None
# 转写得到的 ASR 原句（含逐字时间戳），已换到共享时间轴；收尾时写 transcript.json 用
TRANSCRIPT_RAW: list[dict] = []
BATCH_UNCERTAIN: list[dict] = []

VIDEO_EXTS = {"mp4", "mov", "mkv", "avi", "wmv", "webm", "m4v", "3gp", "mpeg", "mpg", "flv", "ts"}
AUDIO_EXTS = {"m4a", "mp3", "wav", "ogg", "flac", "aac", "wma", "opus"}
MEDIA_EXTS = VIDEO_EXTS | AUDIO_EXTS

SKIP_DIRS = {".subtitle_cache", ".subtitle_tasks", ".transcribe_tmp", ".polish_tmp", "__pycache__"}

PUNCT = "，。！？；：、,.!?;:…—～~“”\"'‘’()（）《》"
CJK_RE = re.compile(r"[㐀-鿿぀-ヿ가-힯]")


# ---------------------------------------------------------------- 媒体发现与排序


def is_media(path: Path) -> bool:
    return path.suffix.lower().lstrip(".") in MEDIA_EXTS


def collect_media(target: Path, recursive: bool) -> list[Path]:
    if target.is_file():
        return [target]
    if not target.is_dir():
        print(f"Error: target not found: {target}")
        return []

    iterator = target.rglob("*") if recursive else target.glob("*")
    files = []
    for path in iterator:
        if not path.is_file() or not is_media(path):
            continue
        if path.name.startswith("."):
            continue
        if any(part in SKIP_DIRS or part.startswith(".") for part in path.relative_to(target).parts[:-1]):
            continue
        files.append(path)
    return files


def ffprobe_creation_time(path: Path) -> float | None:
    """读取容器元数据里的 creation_time（真正的「视频创建时间」）。"""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format_tags=creation_time",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            return None
        tags = json.loads(result.stdout).get("format", {}).get("tags", {}) or {}
        raw = tags.get("creation_time") or tags.get("com.apple.quicktime.creationdate")
        if not raw:
            return None
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
    except FileNotFoundError:
        print("  Warning: ffprobe not found")
        return None
    except Exception:  # noqa: BLE001 - 元数据缺失属正常情况
        return None


def birth_time(path: Path) -> float:
    stat = path.stat()
    return getattr(stat, "st_birthtime", None) or stat.st_mtime


def sort_media(files: list[Path], mode: str) -> tuple[list[Path], str]:
    """按创建时间排序。auto 模式下所有文件必须来自同一时钟，避免顺序被打乱。"""
    if mode == "name":
        return sorted(files, key=lambda p: p.name), "文件名"
    if mode == "mtime":
        return sorted(files, key=lambda p: p.stat().st_mtime), "修改时间(mtime)"
    if mode == "birthtime":
        return sorted(files, key=birth_time), "文件创建时间(birthtime)"

    probed = {path: ffprobe_creation_time(path) for path in files}
    if mode == "ffprobe" or all(value is not None for value in probed.values()):
        if all(value is not None for value in probed.values()):
            return sorted(files, key=lambda p: probed[p]), "视频元数据 creation_time"
        print("  Warning: 部分文件缺少 creation_time 元数据，--sort-by ffprobe 退回 birthtime")
    return sorted(files, key=birth_time), "文件创建时间(birthtime)"


def media_duration(path: Path) -> float:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            return 0.0
        return float(json.loads(result.stdout).get("format", {}).get("duration", 0) or 0)
    except Exception:  # noqa: BLE001 - 时长拿不到时退回 0
        return 0.0


def extract_audio(src: Path, dst: Path, seconds: float | None = None) -> bool:
    """抽 16k 单声道 wav 给 ASR 用（比转码整段视频快很多，也没有体积上限问题）。

    `seconds` 只抽开头这么长——声纹探测取样用，整片转写不传。
    """
    cmd = ["ffmpeg", "-y", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000"]
    if seconds:
        cmd += ["-t", f"{seconds:g}"]
    cmd += ["-c:a", "pcm_s16le", str(dst)]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        if result.returncode != 0:
            print(f"  Error: ffmpeg audio extraction failed: {result.stderr.strip()[-500:]}")
            return False
        return True
    except FileNotFoundError:
        print("  Error: ffmpeg not found, please install it first")
        return False
    except subprocess.TimeoutExpired:
        print("  Error: ffmpeg audio extraction timed out (3600s)")
        return False


# ---------------------------------------------------------------- ASR 与缓存


def cache_path(cache_dir: Path, path: Path, diarize: bool, track: str = "",
               engine: str = "funasr", language: str = "auto", model: str = "") -> Path:
    stat = path.stat()
    model_tag = model or {"funasr": "paraformer-zh", "firered": "fireredasr2-aed"}.get(engine, engine)
    raw = (f"{path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}|multilingual-v1|{engine}|{language}|{model_tag}"
           f"|spk={diarize}|track={track}")
    return cache_dir / f"{hashlib.sha1(raw.encode()).hexdigest()[:16]}.json"


def analysis_cache_path(cache_dir: Path, path: Path) -> Path:
    """左右声道分析结论也缓存：handoff 每轮续跑都会重新走一遍文件列表，
    没有缓存就要把每个文件的音频重新解一遍（20 分钟视频约 5 秒，六个文件就是半分钟白等）。"""
    stat = path.stat()
    raw = f"{path.resolve()}|{stat.st_size}|{int(stat.st_mtime)}|dual-v3"
    return cache_dir / f"dual-{hashlib.sha1(raw.encode()).hexdigest()[:16]}.json"


def dual_channel_report(path: Path, cache_dir: Path | None, mode: str) -> dual_channel.DualReport | None:
    """判断这个文件的左右声道是不是两个人。mode: auto / on / off。"""
    if mode == "off":
        return None
    channels = dual_channel.channel_count(path)
    if channels < 2:
        return None

    cache_file = analysis_cache_path(cache_dir, path) if cache_dir else None
    if cache_file and cache_file.exists():
        try:
            return dual_channel.DualReport.from_json(cache_file.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - 缓存损坏就重算
            pass

    stereo = dual_channel.read_stereo(path)
    if stereo is None:
        return None
    report = dual_channel.analyze(stereo, channels)
    if mode == "on" and not report.dual:
        print(f"  Note: 声道分析判为「{report.reason}」，但 --dual-channel on 强制按双人双轨处理")
        report.dual = True
    if cache_file:
        cache_file.write_text(report.to_json(), encoding="utf-8")
    return report


def resolve_device(device: str) -> str:
    """`auto` → 挑一个能用的加速器。firered 在 cpu 上 RTF≈2.2、mps 上 ≈0.35，
    差 4 倍——80 分钟素材是「3 小时」和「28 分钟」的区别，所以默认不能是 cpu。"""
    if device != "auto":
        return device
    try:
        import torch
    except ImportError:
        return "cpu"
    try:
        if torch.cuda.is_available():
            return "cuda"
        if torch.backends.mps.is_available():
            return "mps"
    except Exception:  # noqa: BLE001 - 探测失败就老老实实用 cpu
        pass
    return "cpu"


def resolve_engine(engine: str, needs_voiceprint: bool) -> str:
    """中文沿用原选择规则；非中文由 ASRRouter 路由至 Whisper。"""
    if engine != "auto":
        return engine
    if needs_voiceprint:
        return "funasr"
    import firered_asr
    if firered_asr.missing_pieces():
        # auto 的语义是「挑个能用的」，没装就安静退回 funasr；
        # 显式 --engine firered 才该硬报错并打印安装命令
        print("Note: FireRedASR2S 未安装，本次用 funasr"
              "（装法见 --engine firered 的报错，或 firered_asr.py 开头）")
        return "funasr"
    return "firered"


def load_model(device: str, diarize: bool, engine: str = "funasr"):
    """按 --engine 选转写引擎。两条路径吐的句子结构一样，下游不用关心用的是哪个。"""
    if engine == "firered":
        import firered_asr
        return firered_asr.load(device)

    try:
        from funasr import AutoModel
    except ImportError:
        print("Error: funasr not installed. 请用 funasr 虚拟环境的 python 运行本脚本：")
        print("  $HOME/.local/pipx/venvs/funasr/bin/python generate_subtitle.py ...")
        return None

    print("Loading ASR models...")
    kwargs = dict(
        model="paraformer-zh",
        vad_model="fsmn-vad",
        vad_kwargs={"max_single_segment_time": 30000},
        punc_model="ct-punc",
        device=device,
        disable_update=True,
    )
    if diarize:
        kwargs["spk_model"] = "cam++"
    return AutoModel(**kwargs)


def sentences_from_flat(text: str, timestamps) -> list[dict]:
    """无说话人模型时 FunASR 只返回整段 text + token 级 timestamp，
    这里按句末标点切回「句子」，并把 token 时间戳分派到各句。"""
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if not text:
        return []

    spans = tokenize_spans(text)
    stamps = list(timestamps or [])
    if not spans or not stamps:
        return [{"text": text, "start": 0, "end": 0, "timestamp": None}]

    if len(stamps) != len(spans):
        # token 数对不上时按比例映射，保证整体不跑偏（局部略有误差）
        print(f"  Note: token/timestamp 数量不一致（{len(spans)} vs {len(stamps)}），按比例对齐")
        stamps = [stamps[min(int(i * len(stamps) / len(spans)), len(stamps) - 1)] for i in range(len(spans))]

    cuts, start = [], 0
    for i, ch in enumerate(text):
        if sentence_boundary(text, i, soft=False):
            cuts.append((start, i + 1))
            start = i + 1
    if start < len(text):
        cuts.append((start, len(text)))

    sentences = []
    for a, b in cuts:
        chunk = text[a:b].strip()
        if not chunk:
            continue
        idx = [i for i, (sa, sb) in enumerate(spans) if sa >= a and sb <= b]
        if not idx:
            continue
        # chunk 只去掉了首尾空白，token 数与 sub 一致；值仍是全片绝对毫秒
        sub = [stamps[i] for i in idx]
        sentences.append({
            "text": chunk,
            "start": float(sub[0][0]),
            "end": float(sub[-1][1]),
            "timestamp": sub,
        })
    return sentences


def transcribe_audio(model, wav: Path, speaker_count: int | None = None) -> list[dict]:
    if getattr(model, "ENGINE", None) == "firered":
        return model.transcribe(wav)      # VAD/标点/词级时间戳都在子进程里做完了
    kwargs = {"input": str(wav), "cache": {}, "batch_size_s": 300}
    if speaker_count is not None:
        kwargs["preset_spk_num"] = speaker_count
    result = model.generate(**kwargs)
    if not result:
        return []
    first = result[0]
    if first.get("sentence_info"):
        return first["sentence_info"]
    return sentences_from_flat(first.get("text"), first.get("timestamp"))


# ---------------------------------------------------------------- 句子 → 字幕条


# 一行最多 16 个中文的宽度。竖屏成片在常用字号下再多就会被播放器自己折成第二行，
# 而第二行是**只留给「两个人同时说话」**的（见 merge_speaker_overlaps）。
DEFAULT_MAX_WIDTH = 16.0


# 等宽切点允许在目标宽度 ±这个比例的窗口里浮动，用来把切口挪到词边界上
CUT_GAP_WINDOW = 0.25

_JIEBA = None
_TEXT_LANGUAGE = "auto"

def set_text_language(language: str) -> None:
    global _TEXT_LANGUAGE
    if language != _TEXT_LANGUAGE:
        _TEXT_LANGUAGE = language
        _word_starts.cache_clear()
        _word_spans.cache_clear()
        protected_spans.cache_clear()



@functools.lru_cache(maxsize=512)
def _word_starts(text: str) -> frozenset[int]:
    """中文分词的词首下标集合。jieba 装不上就返回空集，切点退回只看停顿。

    jieba 是 funasr 的依赖，用 funasr 引擎时一定在；FireRedASR2S 环境下不一定，
    所以这里是软依赖——拿不到就少一档判据，不报错。
    """
    global _JIEBA
    if not chinese_rules(text, _TEXT_LANGUAGE):
        return frozenset()
    if _JIEBA is None:
        try:
            import jieba
            jieba.setLogLevel(60)          # 别把 "Building prefix dict" 刷进日志
            _JIEBA = jieba
        except Exception:                  # noqa: BLE001 - 没有就没有
            _JIEBA = False
    if not _JIEBA:
        return frozenset()
    starts, cursor = {0, len(text)}, 0
    for word in _JIEBA.cut(text, HMM=True):
        starts.add(cursor)
        cursor += len(word)
    return frozenset(starts)


# ================================================================ 术语 / 词语保护
#
# 切点落在词语中间，成片上就是一条字幕以「浏览」结尾、下一条以「器」开头。最难看的是
# **产品名被劈开**——实测「接下来用 Refore HTML」/「to Figma 为例来介绍」，观众要把两条
# 字幕在脑子里拼起来才知道说的是哪个产品，而这恰恰是教程里最需要看清的那个词。
#
# 老实现把「词边界」只当成 _snap_to_pause 里的一档**软偏好**：窗口里没有词首候选就照切。
# 现在改成**硬约束**——切点不许落在保护区间内部，找不到合法切点时**宁可让这一行超宽**
# （上限 TERM_OVERFLOW 倍）。理由是两种难看程度不对称：超宽一点点播放器顶多折行，
# 而术语被劈开是语义损伤，观众看不懂。
#
# 保护区间三个来源：
#   1. 术语表里的多字/多词术语（glossary.txt + --glossary），"Refore HTML to Figma"
#      这种带空格的组合名全靠它——jieba 和 token 边界都拦不住
#   2. jieba 分出来的中文词（≥2 字）
#   3. 数字 + 量词/单位（「3 个」「19年」「16 字」）
TERM_OVERFLOW = 1.35          # 找不到合法切点时，单行最多超到 max_width 的这个倍数
MAX_JOIN_WIDTH = 3.0          # 接缝修复最多把两条并成 max_width 的这个倍数，再交给重切

_ACTIVE_TERMS: tuple[str, ...] = ()


def set_protected_terms(terms) -> None:
    """把本次运行的术语表登记为保护词表。main 里解析完参数就调一次。

    走模块级全局而不是层层传参：断句在 sentence_to_cues → split_indices → wrap_spans
    → _cut_tokens 四层里，而术语表对整次运行是常量，穿四层只会让签名全变脏。
    """
    global _ACTIVE_TERMS
    _ACTIVE_TERMS = tuple(terms or ())
    _term_regex.cache_clear()
    protected_spans.cache_clear()


def _canonical_terms(terms: tuple[str, ...]) -> list[str]:
    """术语表每行取「正确写法」那一侧：`a, b → C` 取 C，没有箭头的整行就是术语。"""
    out = []
    for line in terms:
        right = line.split("→")[-1] if "→" in line else line
        right = right.strip()
        if len(right) >= 2:
            out.append(right)
    return sorted(set(out), key=len, reverse=True)


@functools.lru_cache(maxsize=8)
def _term_regex(terms: tuple[str, ...]):
    """把术语表编成一个大正则。长的排前面，保证 "HTML to Figma" 赢过 "Figma"。"""
    parts = []
    for term in _canonical_terms(terms):
        pat = r"\s+".join(re.escape(tok) for tok in term.split())
        if term[:1].isascii() and term[:1].isalnum():
            pat = r"(?<![A-Za-z0-9])" + pat
        if term[-1:].isascii() and term[-1:].isalnum():
            pat = pat + r"(?![A-Za-z0-9])"
        parts.append(pat)
    if not parts:
        return None
    return re.compile("|".join(parts), re.IGNORECASE)


@functools.lru_cache(maxsize=512)
def _word_spans(text: str) -> tuple[tuple[int, int], ...]:
    """jieba 分出来的中文词区间（只要 ≥2 字的）。jieba 拿不到就返回空。"""
    global _JIEBA
    if not chinese_rules(text, _TEXT_LANGUAGE):
        return ()
    if _JIEBA is None:
        _word_starts(text)                       # 复用同一处 import，顺带初始化
    if not _JIEBA:
        return ()
    out, cursor = [], 0
    for word in _JIEBA.cut(text, HMM=True):
        if len(word) >= 2 and all("\u4e00" <= ch <= "\u9fff" for ch in word):
            out.append((cursor, cursor + len(word)))
        cursor += len(word)
    return tuple(out)


# 数字和它后面的量词/单位不能拆开：「3 个」「19年」「16 字宽」
NUM_UNIT_RE = re.compile(r"\d+(?:\.\d+)?\s*[个条张页家次步项秒分年月日岁倍万亿千百%％]")

# 数量短语和它的中心名词也不能拆开：「一句话」「三个页面」「一个流程」。
# 这一条是 jieba 补不上的：它把「一句话」切成「一句」+「话」——按分词是对的，
# 可这两半各自落在一条字幕上，读者看到的就是「用一句」换行「话发给…」。
NUM_MEASURE_RE = re.compile(
    r"[一二两三四五六七八九十百千万几多半\d]+\s*"
    r"[句个条张片段行本次道份只种块位名台部件套组步页篇章]\s*"
    r"[\u4e00-\u9fff]")


@functools.lru_cache(maxsize=512)
def glossary_spans(text: str) -> tuple[tuple[int, int], ...]:
    """只有**术语表命中**的区间——保护区间里证据最硬的一档。

    和 jieba 词分开，是因为两者说明的事情不一样：句号后面接一个新词是再正常不过的断句，
    而句号落在「Refore HTML | to Figma」中间只可能是标点模型判错了。所以接缝修复里
    只有这一档能推翻「前一条以句末标点收尾」的闸门。
    """
    pattern = _term_regex(_ACTIVE_TERMS)
    if pattern is None:
        return ()
    return tuple((m.start(), m.end()) for m in pattern.finditer(text))


@functools.lru_cache(maxsize=512)
def protected_spans(text: str) -> tuple[tuple[int, int], ...]:
    """不许被切点穿过的字符区间。"""
    spans: list[tuple[int, int]] = list(glossary_spans(text))
    spans += list(_word_spans(text))
    for regex in ((NUM_UNIT_RE, NUM_MEASURE_RE) if chinese_rules(text, _TEXT_LANGUAGE) else ()):
        spans += [(m.start(), m.end()) for m in regex.finditer(text)]
    return tuple(spans)


def _inside(cut: int, spans) -> bool:
    """切点是否落在某个保护区间的**内部**（贴着边界不算）。"""
    return any(a < cut < b for a, b in spans)


def _adjust_cut(text: str, tokens: list[tuple[int, int]], cursor: int, cut: int,
                spans, target: float, ceiling: float) -> int:
    """切点落在术语中间时，挪到最近的合法 token 边界上。

    前后各找一个候选，取切出来的宽度**离 target 更近**的那个；往后挪允许超宽，
    但不超过 ceiling。两边都没有合法候选（整段就是一个术语）时只能原样返回。
    """
    if not _inside(cut, spans):
        return cut
    back = fwd = None
    for span_a, _ in tokens:
        if span_a <= cursor or _inside(span_a, spans):
            continue
        if span_a < cut:
            back = span_a
        elif span_a > cut:
            fwd = span_a
            break
    candidates = [c for c in (back,) if c is not None]
    if fwd is not None and display_width(text[cursor:fwd]) <= ceiling:
        candidates.append(fwd)
    if not candidates:
        return cut
    return min(candidates, key=lambda c: abs(display_width(text[cursor:c]) - target))


def _snap_to_pause(text: str, tokens: list[tuple[int, int]], cursor: int, fallback: int,
                   target: float, max_width: float, gaps: list[float] | None,
                   spans=()) -> int:
    """把等宽切点挪到附近的词边界上。

    中文每个字都是一个 ASR token，纯按宽度切根本不知道哪儿是词边界，
    24 字限 16 会切成 12+12，正好落在「…也很需要营 | 销相关的…」这种词中间。
    在目标宽度 ±25%（且不超过硬上限）的窗口里挑候选，优先级：

    1. 落在**词首**（jieba 分词）——这条最硬，「营销」「里面」「发言人」都不会再被劈开
    2. 前面的**停顿**更长（ASR token 时间戳：词内间隙几乎为 0，词间几十到几百毫秒）
    3. 离等宽目标更近

    落在保护区间**内部**的候选先被剔掉（术语、词语不许劈开，见 protected_spans）；
    剔完一个不剩时交给 _adjust_cut 去窗口外找合法切点，找不到才原样返回等宽切点。
    """
    words = _word_starts(text)
    lo, hi = target * (1 - CUT_GAP_WINDOW), target * (1 + CUT_GAP_WINDOW)
    candidates: list[tuple[int, float]] = []
    for span_a, _ in tokens:
        if span_a <= cursor:
            continue
        width = display_width(text[cursor:span_a])
        if width > hi or width > max_width:
            break
        if width >= lo and not _inside(span_a, spans):
            candidates.append((span_a, width))
    if not candidates:
        return _adjust_cut(text, tokens, cursor, fallback, spans, target,
                           max_width * TERM_OVERFLOW)
    return max(candidates, key=lambda c: (c[0] in words,
                                          gaps[c[0]] if gaps and c[0] < len(gaps) else 0.0,
                                          -abs(c[1] - target)))[0]


def _cut_tokens(text: str, tokens: list[tuple[int, int]], a: int, b: int,
                max_width: float, pieces_wanted: int | None,
                gaps: list[float] | None = None, spans=()) -> list[tuple[int, int]]:
    """在 token 边界把 text[a:b] 切成若干片。

    `pieces_wanted=None` 是纯贪心（装不下就切，片数最少）；
    给了片数就按「每片 = 总宽/片数」等宽切，用来把贪心剩下的孤零零尾巴匀掉，
    并用 `gaps`（每个字前面的停顿毫秒数）把切口吸附到词边界上。
    贪心那一支不做等宽吸附——它的切点已经顶到宽度上限，没有挪动余地；
    但**两支都要过 _adjust_cut**：切穿术语是语义损伤，宽度顶到上限也不是切它的理由。
    """
    ceiling = max_width * TERM_OVERFLOW
    target = display_width(text[a:b]) / pieces_wanted if pieces_wanted else max_width
    pieces: list[tuple[int, int]] = []
    cursor = a
    # 每个 token 装进当前片后，这一片实际会到哪儿结束：下一个 token 的起点（或整段末尾）。
    # 要按这个量宽度，因为标点和空格不是 token——只看 token 会把「…产品经理啊」后面那个
    # 「？」漏掉，17 字宽算成 16 就白白放过去了
    ends = [tokens[k + 1][0] for k in range(len(tokens) - 1)] + [b]
    for (span_a, _), piece_end in zip(tokens, ends):
        if span_a <= cursor:
            continue                                   # 片里的第一个 token，前面无处可切
        full = (display_width(text[cursor:span_a]) >= target if pieces_wanted
                else display_width(text[cursor:piece_end]) > max_width)
        room = pieces_wanted is None or len(pieces) < pieces_wanted - 1
        if full and room:
            cut = (_snap_to_pause(text, tokens, cursor, span_a, target, max_width, gaps, spans)
                   if pieces_wanted else span_a)
            cut = _adjust_cut(text, tokens, cursor, cut, spans,
                              target if pieces_wanted else max_width, ceiling)
            if cut <= cursor:
                continue                               # 挪不动就先不切，下个 token 再试
            pieces.append((cursor, cut))
            cursor = cut
    pieces.append((cursor, b))
    return [(x, y) for x, y in pieces if text[x:y].strip()]


def wrap_spans(text: str, a: int, b: int, all_spans: list[tuple[int, int]],
               max_width: float, gaps: list[float] | None = None) -> list[tuple[int, int]]:
    """没有标点可切的长句：只在 token 边界切，避免把英文单词劈成两半。

    先贪心求出最少片数，再按这个片数等宽重切一遍——否则「26 个字、限 16」会切成
    16 + 10，而等宽是 13 + 13，两条都好读。

    等宽版本的取舍口径是「**不比贪心更宽**」而不是「不超 max_width」：为了不切穿术语，
    两支都可能被迫超宽（见 TERM_OVERFLOW），此时拿死线卡等宽版就会白白退回贪心。
    """
    tokens = [(sa, sb) for sa, sb in all_spans if sa >= a and sb <= b]
    if not tokens:
        return [(a, b)]
    spans = protected_spans(text)
    greedy = _cut_tokens(text, tokens, a, b, max_width, None, gaps, spans)
    if len(greedy) < 2:
        return greedy
    balanced = _cut_tokens(text, tokens, a, b, max_width, len(greedy), gaps, spans)
    if not balanced:
        return greedy
    widest = max(display_width(text[x:y]) for x, y in balanced)
    if widest <= max(max_width, *(display_width(text[x:y]) for x, y in greedy)):
        return balanced
    return greedy


# 句内停顿到这个长度就当成一次换气/换人，两侧绝不并进同一条字幕。
#
# 为什么需要这道：FunASR 把所有 VAD 段的文字拼成**一整条** `result["text"]` 交给 ct-punc
# 打标点，最后 `timestamp_sentence()` **只按标点切句**——VAD 段边界（也就是最硬的换人证据）
# 在它那里被丢掉了。所以「A 说完、停 1.4 秒、B 接话」只要 ct-punc 没在那儿点句号，
# 两个人就并进同一条字幕；`cam++` 的 `distribute_spk()` 又是按最大时间重叠给**整句**贴一个
# 说话人号，于是说话人也跟着错。实测 82 分钟对谈的 2604 句里，8% 内部藏着 >700ms 的停顿。
#
# 阈值取 600ms：实测 token 间隙 p95 = 220ms、p98 = 500ms，正常换气够不着；
# 再说在 600ms 的停顿处断条，本来就是自然的字幕节奏，判过头也不伤。
DEFAULT_PAUSE_SPLIT_MS = 600.0


def char_gaps(times: list[tuple[float, float]]) -> list[float]:
    """每个字**前面**的停顿毫秒数（第一个字算 0）。标点没有自己的时间戳，恒为 0。"""
    return [0.0] + [max(0.0, times[i][0] - times[i - 1][1]) for i in range(1, len(times))]


def pause_breaks(gaps: list[float], threshold_ms: float) -> frozenset[int]:
    """硬断点：这些字的前面停够了 threshold_ms，不许和前文并进同一条。"""
    if threshold_ms <= 0:
        return frozenset()
    return frozenset(i for i, gap in enumerate(gaps) if i and gap >= threshold_ms)


def split_indices(text: str, max_width: float, pauses: frozenset[int] = frozenset(),
                  gaps: list[float] | None = None) -> list[tuple[int, int]]:
    """按标点切成小块，再在不超过 max_width（单行字宽）的前提下合并回去。

    `pauses` 是停顿硬断点（字符下标），既用来额外切开、也用来禁止跨停顿合并。
    """
    breaks: list[tuple[int, int]] = []
    start = 0
    safe = {b for _, b in grapheme_spans(text)}
    pauses = frozenset(i for i in pauses if i in safe)
    for i, ch in enumerate(text):
        if i + 1 in safe and sentence_boundary(text, i):
            breaks.append((start, i + 1))
            start = i + 1
    if start < len(text):
        breaks.append((start, len(text)))
    breaks = [(a, b) for a, b in breaks if text[a:b].strip()]

    # 标点管不到的地方按停顿再切一刀（两个人的话就是从这儿并进一条的）
    if pauses:
        pieces: list[tuple[int, int]] = []
        for a, b in breaks:
            cuts = [a, *sorted(i for i in pauses if a < i < b), b]
            pieces.extend((cuts[k], cuts[k + 1]) for k in range(len(cuts) - 1))
        breaks = [(a, b) for a, b in pieces if text[a:b].strip()]

    merged: list[tuple[int, int]] = []
    for a, b in breaks:
        prev_tail = text[merged[-1][0]:merged[-1][1]].strip()[-1:] if merged else ""
        if (merged and a not in pauses and prev_tail not in "。！？!?….؟۔।॥"
                and display_width(text[merged[-1][0]:b]) <= max_width):
            merged[-1] = (merged[-1][0], b)
        else:
            merged.append((a, b))

    all_spans = tokenize_spans(text)
    final: list[tuple[int, int]] = []
    for a, b in merged:
        # 以前这里留了 1.6 倍的宽限（限 20 却放过 32 字），成片里就是活生生的两行。
        # 单行字宽是硬上限，超一点也得切。
        if display_width(text[a:b]) <= max_width:
            final.append((a, b))
            continue
        final.extend(wrap_spans(text, a, b, all_spans, max_width, gaps))
    return final


SPEAKER_PREFIX_RE = re.compile(r"^spk\d+: ")


def speaker_name(index) -> str:
    """说话人标签统一用 `spk1` / `spk2`……：合并字幕里两人同时说话时每行前面就标这个。"""
    return f"spk{int(index) + 1}"


# ---------------------------------------------------------------- 声纹旁人筛选

# cam++ 是**无监督**聚类：它只回答「这些话是不是同一个嗓子」，不回答「这个人是不是这场的
# 说话人」。路过说两句的人、隔壁桌、背景电视、甚至同一个人音质突变，都会各自聚成一「个人」。
#
# 只是标错名字倒还好，麻烦的是 merge_speaker_overlaps：它看到两个**不同 speaker** 的条目
# 时间重叠 200ms，就把它们合成一条上下两行、各自带 spkN: 前缀的字幕。于是背景里一句咳嗽
# 就能让成片凭空多出一个人。所以认完人先排个序，把不像正经说话人的簇降级。
#
# 两种判错的代价不对称，筛选策略跟着它走：
#   - 把真说话人当成旁人 → 他的话**原文照旧留在字幕里**，只是少了说话人标注（可恢复）
#   - 把旁人当成说话人   → 成片里出现假的双人两行字幕（观众直接看见）
# 所以给了 --speaker-count 就严格取前 N；没给就只砍「又短又少」的簇，宁可放过。
#
# 注意这里排序只用**说话时长**，不用响度：混音单轨没有可靠的分人能量包络
# （双声道/多设备那条路才有，见 dual_channel.mark_bleed_sentences）。时长排序对
# 「主持人只问三句话」这种真说话人不友好，所以人数已知时务必传 --speaker-count。
BYSTANDER_SHARE = 0.03            # 说话时长占全片的比例，低于它才可能被判为旁人
BYSTANDER_MAX_SECONDS = 20.0      # 且绝对说话时长要短于这个数（两个条件同时成立才降级）


def demote_bystanders(sentences: list[dict], speaker_count: int | None = None,
                      ) -> tuple[list[dict], list[dict]]:
    """把 cam++ 聚出来的旁人簇降级：原文照留，但不给说话人标注、不参与抢话合并。

    就地在句子上打 `_bystander` 标记（不删！删错等于永久丢话）。
    返回 (sentences, stats)，stats 是按时长降序的每簇统计，主流程打印和写进 manifest 用。

    cam++ 的簇号是**按文件**给的（同一个人在两个文件里未必是同一个 spk），
    所以调用方也按文件各调一次，不要把多个文件的句子并起来判。
    """
    totals: dict = {}
    for item in sentences:
        spk = item.get("spk")
        if spk is None:
            continue
        span = max(0.0, float(item.get("end") or 0) - float(item.get("start") or 0))
        totals[spk] = totals.get(spk, 0.0) + span
    if not totals:
        return sentences, []

    total = sum(totals.values()) or 1.0
    ranked = sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))
    if speaker_count:
        keep = {spk for spk, _ in ranked[:speaker_count]}
    else:
        keep = {spk for spk, ms in ranked
                if ms / total >= BYSTANDER_SHARE or ms >= BYSTANDER_MAX_SECONDS * 1000}
        keep = keep or {ranked[0][0]}      # 全都很短时至少留说得最多的那个

    stats = [{"speaker": speaker_name(spk), "seconds": round(ms / 1000.0, 1),
              "share": round(ms / total, 3), "kept": spk in keep}
             for spk, ms in ranked]
    for item in sentences:
        spk = item.get("spk")
        if spk is not None and spk not in keep:
            item["_bystander"] = True
    return sentences, stats


def format_speaker_stats(stats: list[dict]) -> str:
    """把每簇统计排成一行，给终端打印用。"""
    return "、".join(
        f"{s['speaker']} {s['share']:.0%}" + ("" if s["kept"] else "（疑似旁人）")
        for s in stats
    )


def sentence_to_cues(item: dict, max_chars: float, offset_ms: float, diarize: bool,
                     pause_split_ms: float = DEFAULT_PAUSE_SPLIT_MS) -> list[dict]:
    text = re.sub(r"\s+", " ", str(item.get("text") or "")).strip()
    if not text:
        return []
    start = float(item.get("start") or 0)
    end = float(item.get("end") or start)
    set_text_language(item.get("language", "auto"))
    times = char_time_map(text, item.get("timestamp"), start, end, item.get("words"))
    gaps = char_gaps(times)
    # 被判为旁人的簇不给标注：它一旦有 speaker，就够格和主讲人合成两行抢话字幕了
    speaker = None if item.get("_bystander") else item.get("spk")

    cues = []
    for a, b in split_indices(text, max_chars, pause_breaks(gaps, pause_split_ms), gaps):
        chunk = text[a:b].strip()
        if not chunk:
            continue
        cue_start = times[a][0] if a < len(times) else start
        cue_end = times[b - 1][1] if b - 1 < len(times) else end
        cues.append({
            "start": cue_start + offset_ms,
            "end": min(end, max(cue_end, cue_start + 1)) + offset_ms,
            "language": item.get("language", "auto"),
            "text": chunk,
            "speaker": speaker_name(speaker) if diarize and speaker is not None else None,
        })
    return cues


def normalize_timeline(cues: list[dict], min_dur: float, max_dur: float) -> list[dict]:
    """排序、消重叠、补足最短时长、砍掉过长尾巴。

    唯一的例外是**两个人抢话**：不同说话人之间的重叠是真实发生的事，
    削掉就等于假装他们是轮流说的。这种重叠留到渲染前由 merge_speaker_overlaps 合成两行字幕。
    """
    cues = sorted(cues, key=lambda c: (c["start"], c["end"]))
    for i, cue in enumerate(cues):
        nxt = cues[i + 1] if i + 1 < len(cues) else None
        cue["end"] = min(cue["end"], cue["start"] + max_dur * 1000)
        if nxt is None:
            cue["end"] = max(cue["end"], cue["start"] + min_dur * 1000)
            continue
        if cross_talk(cue, nxt):
            continue                       # 抢话：原样保留重叠，别削
        next_start = nxt["start"]
        # 尽量补到最短时长，但不侵占下一条
        cue["end"] = max(cue["end"], min(cue["start"] + min_dur * 1000, next_start - 40))
        cue["end"] = min(cue["end"], max(next_start - 40, cue["start"] + 1))
    return cues


# 重叠短于这个时长就当成分句抖动，不算抢话
CROSS_TALK_MIN_OVERLAP_MS = 200


def cross_talk(a: dict, b: dict) -> bool:
    """a、b 是不是两个人在同时说话。"""
    sa, sb = a.get("speaker"), b.get("speaker")
    if not sa or not sb or sa == sb:
        return False
    return min(a["end"], b["end"]) - max(a["start"], b["start"]) >= CROSS_TALK_MIN_OVERLAP_MS


def merge_speaker_overlaps(cues: list[dict], label: bool = True,
                           max_dur: float = 6.0) -> list[dict]:
    """把同时说话的不同说话人合成**一条多行字幕**，一人一行。

    只在开了说话人分离时有意义：同一个人的话是顺次的，时间上真重叠的必然是两个人抢话。
    渲染前才做——前面的润色、存疑清单都按「一条 = 一个人的一句话」处理，简单且不会串行。
    `label=True` 时给每行加「spk1: 」前缀，合并后 speaker 置空避免重复加前缀。
    两行字幕**默认都标**：不标的话观众只知道是两个人，不知道哪行是谁
    （尤其三四个人的场合）；单人单行的条目仍然不标，保持干净可压片。

    两条硬约束，都是为了字幕还能看：**最多两行**，且合并后跨度不超过 `max_dur`。
    不设限时一串接连的插话会被滚成一条十几秒、三行的巨块（实测真出现过 11.9 秒），
    超限就不合并，两条各自照常显示。
    """
    cues = sorted(cues, key=lambda c: (c["start"], c["end"]))
    out: list[dict] = []
    i = 0
    while i < len(cues):
        group = [cues[i]]
        span_end = cues[i]["end"]
        j = i + 1
        while j < len(cues) and len(group) < 2:
            nxt = cues[j]
            if not cross_talk(group[0], nxt):
                break
            merged_end = max(span_end, nxt["end"])
            if merged_end - group[0]["start"] > max_dur * 1000:
                break                      # 并起来太长，不如让两条各显各的
            group.append(nxt)
            span_end = merged_end
            j += 1
        if len(group) == 1:
            out.append(group[0])
        else:
            out.append({
                "start": min(member["start"] for member in group),
                "end": span_end,
                "text": "\n".join(
                    f'{member["speaker"]}: {member["text"]}'
                    if label and member.get("speaker") else member["text"]
                    for member in group
                ),
                "speaker": None,
            })
        i = j                              # 组只有一条时 j 就是 i+1，天然往前走

    # 合并后新条目可能顶到下一条，简单裁一下，保证不重叠、不倒置
    for k, cue in enumerate(out[:-1]):
        cue["end"] = min(cue["end"], max(out[k + 1]["start"] - 40, cue["start"] + 1))
    return out


def enforce_line_width(cues: list[dict], max_width: float) -> tuple[list[dict], int]:
    """渲染前最后一道兜底：仍然超宽的条目按 token 边界拆成前后两条（或多条）。

    ASR 阶段已经按字宽切过了，但纠错会让文字**变长**（`cos` → `Cursor`、中英文之间补空格），
    所以到这里还可能超。超宽的条目播放器会自己折成两行，而两行只留给抢话，
    所以宁可拆成两条前后显示。时间按各片的字宽比例分，不越出原条目的区间。

    已经是多行的（抢话合成的）不动——那是有意为之的两行。
    """
    out: list[dict] = []
    split = 0
    for cue in cues:
        set_text_language(cue.get("language", "auto"))
        text = cue["text"]
        if "\n" in text or display_width(text) <= max_width:
            out.append(cue)
            continue
        pieces = wrap_spans(text, 0, len(text), tokenize_spans(text), max_width)
        if len(pieces) < 2:
            out.append(cue)
            continue
        widths = [display_width(text[x:y]) for x, y in pieces]
        total = sum(widths) or 1.0
        start, span = cue["start"], max(cue["end"] - cue["start"], 1.0)
        acc = 0.0
        for k, ((x, y), width) in enumerate(zip(pieces, widths)):
            piece_start = start + span * acc / total
            acc += width
            out.append({
                **cue,
                "start": piece_start,
                "end": cue["end"] if k == len(pieces) - 1 else start + span * acc / total,
                "text": text[x:y].strip(),
            })
        split += 1
    return out, split


# 接缝两侧的时间间隔超过这个数就不并——那是真停顿，并了会让字幕跟不上口型
JOIN_MAX_GAP_MS = 500.0


def repair_term_splits(cues: list[dict], max_width: float) -> tuple[list[dict], int]:
    """相邻两条字幕的**接缝正好落在术语/词语中间**时，先并回一条。

    为什么光靠切点校正不够：ASR 是按自己的 VAD + 标点模型断句的，
    「接下来用 Refore HTML」和「to Figma 为例来介绍」在它那里就是**两句话**，
    压根没进过同一次 wrap_spans，切点校正管不到跨句的接缝。实测这类劈开最伤——
    被拆的往往正是教程里最该看清的产品名。

    并完不急着定稿：交给后面的 enforce_line_width 按保护区间重切，切点自然会挪到
    术语外面（「接下来用 Refore HTML to Figma」/「为例来介绍」），并不会真的输出一条超长的。

    四道闸门，任何一道不过就不并：
      - 两条都是单行（抢话合成的两行不动）、同一个说话人
      - 接缝处的时间间隔 < JOIN_MAX_GAP_MS
      - 前一条不是以句末标点收尾的（那是说完了，不是被劈开）。注意 finalize_text 会把句末
        标点删掉，所以这里优先看 `_sentence_end` 标记——那是删标点**之前**记下来的
      - 并起来别太长（超过 max_width * MAX_JOIN_WIDTH 就不并，免得滚雪球）
    """
    out: list[dict] = []
    joined = 0
    for cue in cues:
        if not out:
            out.append(dict(cue))
            continue
        prev = out[-1]
        head, tail = prev["text"], cue["text"]
        language = cue.get("language", "auto")
        set_text_language(language)
        if (language != prev.get("language", "auto")
                or not chinese_rules(head + tail, language)):
            out.append(dict(cue))
            continue
        ends_sentence = prev.get("_sentence_end")
        if ends_sentence is None:
            ends_sentence = head.rstrip()[-1:] in "。！？!?….؟۔।॥"
        if ("\n" in head or "\n" in tail
                or prev.get("speaker") != cue.get("speaker")
                or cue["start"] - prev["end"] > JOIN_MAX_GAP_MS):
            out.append(dict(cue))
            continue
        # 接缝两侧都是拉丁字母/数字时要补回那个空格，否则 "HTML"+"to" 会粘成 "HTMLto"
        left, right = head[-1:], tail[:1]
        sep = " " if (left.isascii() and left.isalnum()
                      and right.isascii() and right.isalnum()) else ""
        merged = head + sep + tail
        if display_width(merged) > max_width * MAX_JOIN_WIDTH:
            out.append(dict(cue))
            continue
        junction = len(head) + len(sep)
        # 术语表命中能推翻句末闸门：句号落在产品名中间，那是标点模型判错了，不是说完了
        if _inside(junction, glossary_spans(merged)):
            pass
        elif ends_sentence or not _inside(junction, protected_spans(merged)):
            out.append(dict(cue))
            continue
        prev["text"] = merged
        prev["end"] = cue["end"]
        joined += 1
    return out, joined


# ---------------------------------------------------------------- 去水词 / 错别字


FILLER_ONLY_RE = re.compile(
    r"^[\s，。、？！,.?!]*(?:那个|这个|然后呢|然后|就是说|就是|对吧|对不对|是吧|是不是|嗯+|呃+|啊+|哦+|唉+|欸+|哈+)"
    r"[\s，。、？！,.?!]*$"
)


# 连续重复的字 / 词组去重（**任何级别都做**，不只是 clean）：
# ASR 经常把一个字或一个词吐两遍（「因为你你否则你就」「把这个把这个」），
# 这不是说话人的语气，纯属识别噪音，留着既难读又白占单行字宽。
#
# 三条规则各有各的边界，都是为了不误伤真话：
# 1. 单字：只对**代词/虚词**生效，且只处理「刚好重复两次」——三次以上多半是真强调
#    （「对对对」「是是是」），交给 agent 判。表里刻意不收那些「重复两遍正好是另一个词」
#    的字：把（把手）、要（要求）、会（会见）、还（还钱）、得（得到）、在（在职）、
#    地（地地道道）、着（着急）、的（**「商业目的的解决方案」里的「目的」+「的」**），
#    以及「个个」「天天」「好好」「慢慢」这类合法叠词的字
# 2. 双字口头语：白名单内的（就是就是、然后然后）才压，避免误伤「研究研究」「商量商量」
# 3. 三字及以上的整段重复：一律压（「我觉得我觉得」「把这个把这个」），
#    但汉语里有两族**合法的整段重复**，必须放过（都是实测误伤出来的）：
#    - 「A 的 A」所属链：他们的**老大的老大** ≠ 老大
#    - 疑问词任意句式：你想**怎么读怎么读**、要**什么给什么**、爱**怎么说怎么说**
STUTTER_CHARS = "你我他她它您咱了是就都也很这那"
STUTTER_WORDS = ("就是", "然后", "那个", "这个", "对吧", "其实", "所以", "因为",
                 "但是", "可能", "我们", "你们", "他们", "如果")
# 单元里出现这些词就不当重复处理：它们撐起的是句式，不是卡壳
PHRASE_KEEP_RE = re.compile(r"怎么|什么|多少|哪儿|哪里")
STUTTER_CHAR_RE = re.compile(rf"(?<![{STUTTER_CHARS}])([{STUTTER_CHARS}])\1(?!\1)")
STUTTER_WORD_RE = re.compile("|".join(rf"(?:{w}){{2,}}" for w in STUTTER_WORDS))
STUTTER_PHRASE_RE = re.compile(r"([\u4e00-\u9fff]{3,6}?)\1+")


def _collapse_phrase(match: re.Match) -> str:
    unit = match.group(1)
    # 「的老大的老大」这种以「的」开头的单元 = 所属链（老大的老大），不是重复
    if unit.startswith("的") or PHRASE_KEEP_RE.search(unit):
        return match.group(0)
    return unit


def dedupe_stutter(text: str, language: str = "auto") -> str:
    """去掉连续重复的字 / 词组。"""
    if not chinese_rules(text, language):
        return text
    text = STUTTER_CHAR_RE.sub(r"\1", text)
    text = STUTTER_WORD_RE.sub(lambda m: m.group(0)[:2], text)
    text = STUTTER_PHRASE_RE.sub(_collapse_phrase, text)
    return text


# 年份一律用阿拉伯数字：口播里说的「一九年」「二零二五年」写成中文既难认又白占字宽，
# 观众扫一眼要在脑子里换算一次。但**时长**不能动——「十年前」「一年之后」「八年」
# 说的是跨度，不是年份，换成数字反而怪。
#
# 规则只认最有把握的那批，剩下的交给 agent（见 MINIMAL_RULES 第 5 条）：
# - 只处理**纯汉字数字**（不含十/百/千，所以「十年」「二十年」天然不匹配）
# - 长度只认 2（一九年 → 19年）和 4（二零二五年 → 2025年），3 位和 5 位以上都不像年份
# - 前面紧挨着数字/十/百/千/两的不动，避免把长数字串切一半
# - **相邻递增的两位放过**：「三四年」「六七年」「五六年」多半是「三四年」的约数说法，
#   不是 1934 年——这类交给 agent 结合上下文判
CN_DIGIT_MAP = {"零": "0", "〇": "0", "一": "1", "二": "2", "三": "3", "四": "4",
                "五": "5", "六": "6", "七": "7", "八": "8", "九": "9"}
YEAR_CN_RE = re.compile(r"(?<![零〇一二三四五六七八九十百千两])([零〇一二三四五六七八九]{2,4})年")


def _year_to_digits(match: re.Match) -> str:
    cn = match.group(1)
    if len(cn) not in (2, 4):
        return match.group(0)
    digits = "".join(CN_DIGIT_MAP[c] for c in cn)
    # 「三四年」「六七年」= 约数说法，不是年份
    if len(digits) == 2 and int(digits[1]) - int(digits[0]) == 1:
        return match.group(0)
    # 四位只认 19xx / 20xx：口播里的四位年份基本就这两族，
    # 别的四位连读多半是卡壳（实测「我一五、一九年就觉得」被连成「一五一九年」→ 1519 年）
    if len(digits) == 4 and not digits.startswith(("19", "20")):
        return match.group(0)
    return digits + "年"


def normalize_year_digits(text: str, language: str = "auto") -> str:
    """中文数字年份 → 阿拉伯数字（一九年 → 19年、二零二五年 → 2025年）。"""
    return YEAR_CN_RE.sub(_year_to_digits, text) if chinese_rules(text, language) else text


def apply_year_digits(cues: list[dict]) -> list[tuple[int, str, str]]:
    """就地把年份换成阿拉伯数字，返回 [(条目序号, 换前, 换后)]。

    和去重一样跑在 agent 纠错**之前**：agent 在同一轮里就能看见换过的文字，
    换错了（把约数说法当成年份）顺手改回去，不额外花一轮。
    """
    edits: list[tuple[int, str, str]] = []
    for i, cue in enumerate(cues, 1):
        after = normalize_year_digits(cue["text"], cue.get("language", "auto"))
        if after != cue["text"]:
            edits.append((i, cue["text"], after))
            cue["text"] = after
    return edits


def rule_polish(text: str, language: str = "auto") -> str:
    """规则模式去水词（LLM 不可用时的兜底）。"""
    if not chinese_rules(text, language):
        return text
    text = dedupe_stutter(text, language)
    text = normalize_year_digits(text, language)
    text = re.sub(r"([\u4e00-\u9fff])\1{3,}", r"\1\1", text)
    text = re.sub(r"[呃嗯]+", "", text)
    for ch in "啊哦吧呢嘛哈嘻":
        text = re.sub(ch + "{2,}", ch, text)
    # 句末语气词：先整词后单字，避免「对吧」被削成「对」
    text = re.sub(r"(?:对吧|是吧|对不对|是不是|好吧|好不好)(?=[，。！？；、,.!?;]|$)", "", text)
    text = re.sub(r"(?:[吧嘛啦呢啊呀哦])+(?=[，。！？；、,.!?;]|$)", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def batch_cues(cues: list[dict], max_cues: int, max_chars: int) -> list[list[int]]:
    batches: list[list[int]] = []
    current: list[int] = []
    chars = 0
    for idx, cue in enumerate(cues):
        length = len(cue["text"])
        if current and (len(current) >= max_cues or chars + length > max_chars):
            batches.append(current)
            current, chars = [], 0
        current.append(idx)
        chars += length
    if current:
        batches.append(current)
    return batches


# 默认级别：只纠错，不改写。口播的「就是」「那个」「对吧」都是说话人的真实语气，
# 删掉之后字幕就不像本人说的话了——用户明确要求保留。
MINIMAL_RULES = (
    "**核心原则：这是纠错，不是润色。默认一个字都不要动，只修明确错了的地方。**\n\n"
    "只允许做这四类修改：\n"
    "1. 明显的同音/近音错字：结合上下文有十足把握才改（如字幕教程语境里的「字母」→「字幕」）\n"
    "2. **中文片段内连续重复的字 / 词组去重**（识别重复吐字，也包括说话人卡壳时的口头重复）：\n"
    "   - 单字重复：「他他需要」→「他需要」、「因为你你否则」→「因为你否则」、"
    "「时代已经变变了」→「时代已经变了」、「环环对自自己照频录」→「环对自己照频录」\n"
    "   - 词组重复：「把这个把这个」→「把这个」、「我觉得我觉得」→「我觉得」、"
    "「就是就是说」→「就是说」\n"
    "   - **保留**这两类：真正表示强调的三连及以上（「对对对」「是是是」「不不不」）；"
    "本来就是叠词的（「一个一个」「研究研究」「慢慢」「地地道道」「看着着急」）\n"
    "3. 专有名词：按下方术语表纠正；术语表没列的，也要**结合上下文推断**"
    "（例如聊「用它写代码」时出现的 coso / cos，八成是 Cursor）。"
    "同一个词全片保持一致——如果同一个词在别处被正确识别过，就以那个正确形式为准\n"
    "4. 中文与英文/数字之间补一个空格（「19年」这种数字 + 时间单位除外，不加空格）\n"
    "5. **年份一律写成阿拉伯数字**：「一九年」→「19年」、「一四年」→「14年」、"
    "「二零二五年」→「2025年」、「九八年」→「98年」。数字和「年」之间不加空格。\n"
    "   只换**年份**——表示时长的中文数字原样保留：「十年前」「一年之后」「八年」「半年」不要动；\n"
    "   「三四年」「六七年」这种相邻数字多半是约数说法（三四年 = 三到四年），也保持原样。\n"
    "   脚本已用规则换掉了把握大的那批，你负责两件事：补上它不敢动的（如「零几年」「两千年」），"
    "把换错的改回中文\n"
    "\n"
    "**严格禁止**（这些都是说话人的真实语气，删掉就不像他说的话了）：\n"
    "- 不要删语气词和口头语：呃、嗯、啊、哦、吧、嘛、呢、那个、就是、对吧、是吧、然后 —— **全部保留**\n"
    "- 不要删重复的口头强调（「对对对」「是是是」）\n"
    "- 不要改语序、不要换同义词、不要润色成书面语、不要精简句子\n"
    "- 不要删整条，不要合并或拆分条目，不要在条目之间搬动文字\n"
    "- 不要补标点（识别给的标点保持原样）\n"
    "- 拿不准的一律**原样保留**，宁可不改也不要臆改\n"
)

# 需要「顺一遍」的场合才用：额外去水词、整条删纯语气词
CLEAN_RULES = (
    "**这一轮除了纠错，还要去水词。**\n\n"
    "纠错部分（优先级最高）：\n"
    "1. 明显的同音/近音错字：有十足把握才改；拿不准的原样保留\n"
    "2. 连续重复的字 / 词组一律去重：「他他需要」→「他需要」、「因为你你否则」→「因为你否则」、"
    "「把这个把这个」→「把这个」；表强调的三连（「对对对」）和叠词（「一个一个」）保留\n"
    "3. 专有名词按术语表纠正；没列的结合上下文推断，同一个词全片保持一致\n"
    "4. 中文与英文/数字之间补一个空格（「19年」这种数字 + 时间单位除外，不加空格）\n"
    "5. **年份一律写成阿拉伯数字**：「一九年」→「19年」、「一四年」→「14年」、"
    "「二零二五年」→「2025年」、「九八年」→「98年」。数字和「年」之间不加空格。\n"
    "   只换**年份**——表示时长的中文数字原样保留：「十年前」「一年之后」「八年」「半年」不要动；\n"
    "   「三四年」「六七年」这种相邻数字多半是约数说法（三四年 = 三到四年），也保持原样。\n"
    "   脚本已用规则换掉了把握大的那批，你负责两件事：补上它不敢动的（如「零几年」「两千年」），"
    "把换错的改回中文\n"
    "\n"
    "去水词部分：\n"
    "6. 删掉句中的语气词和口头填充：呃、嗯、啊、哦、吧、嘛、呢、那个、就是\n"
    "7. 「对吧」「是吧」「对不对」「是不是」要**整体删掉**，"
    "绝不能只删「吧」留下孤零零的「对」——「多页面导入对吧？」应变成「多页面导入？」而不是「多页面导入对？」\n"
    "8. 一条只剩语气词、没有信息量的，文字留空；但带真实反应的（如「哎，对啊」「我知道了」）要保留\n\n"
    "**严格禁止**：改语序、换同义词、润色成书面语、增删信息、"
    "在条目之间搬动文字、合并或拆分条目\n"
)


# 规则去重跑在纠错之前，所以两种模式的 agent 都会看到被削过的文字。
# whole 模式另有 dedupe.tsv 给出逐条对照（见 DEDUPE_REVIEW_RULES）；
# batch 模式看不到原文，只能靠读不通来发现，所以这里把典型误伤直接写出来。
DEDUPE_ROLLBACK_NOTE = (
    "\n**这份字幕已经被规则做过一轮「连续重复去重」**。规则只看字面、不懂语义，"
    "所以可能把汉语里合法的重复削坏了。看到下面这类读不通的地方，**把字补回去**：\n"
    "- 所属链被削：「他们的老大的老大」→「他们的老大」\n"
    "- 「目的」后面的「的」被削：「商业目的的解决方案」→「商业目的解决方案」\n"
    "- 疑问词句式被削：「你想怎么读怎么读」→「你想怎么读」、「要什么给什么」→「要什么」\n"
    "- 真强调被削：「我真的真的很累」→「我真的很累」\n"
)


def polish_rules(level: str) -> str:
    return LANGUAGE_RULES + "以下中文专项规则仅适用于明确的中文片段：\n" + (CLEAN_RULES if level == "clean" else MINIMAL_RULES) + DEDUPE_ROLLBACK_NOTE


def apply_dedupe(cues: list[dict]) -> list[tuple[int, str, str]]:
    """就地做连续重复去重，返回 [(条目序号, 去重前, 去重后)]。

    跑在 agent 纠错**之前**：这样同一轮里 agent 就能看见去重后的文字，
    误伤（「商业目的的解决方案」被削成「目的解决方案」）它顺手就能改回来，不额外花一轮。
    """
    edits: list[tuple[int, str, str]] = []
    for i, cue in enumerate(cues, 1):
        after = dedupe_stutter(cue["text"], cue.get("language", "auto"))
        if after != cue["text"]:
            edits.append((i, cue["text"], after))
            cue["text"] = after
    return edits


def dedupe_review_lines(edits: list[tuple[int, str, str]]) -> str:
    """给 agent 的只读对照表：`条目序号 <TAB> 去重前 <TAB> 去重后`。"""
    return "\n".join(f"{i}\t{before}\t{after}" for i, before, after in edits) + "\n"


DEDUPE_REVIEW_RULES = (
    "\n**dedupe.tsv**（只读）是规则已经做掉的「连续重复去重」："
    "`条目序号 <TAB> 去重前 <TAB> 去重后`。sub.srt 里是**去重后**的文字。\n"
    "你的活儿是**复核这些去重有没有改坏意思**——规则只看字面，不懂语义，"
    "汉语里有一批「看着像重复、其实是句式」的说法会被它误伤：\n"
    "- 所属链：「他们的老大的老大」≠「他们的老大」\n"
    "- 「目的」+「的」：「商业目的的解决方案」被削成「商业目的解决方案」\n"
    "- 疑问词任意句式：「你想怎么读怎么读」「要什么给什么」\n"
    "- 真正表强调的重复：「我真的真的很累」\n"
    "**发现改坏了就在 sub.srt 里把那一条改回去重前的样子**（照 dedupe.tsv 第二列抄）。\n"
    "没改坏的不用动，也不用写进 uncertain.json——这类回滚是确定性的判断，不是存疑。\n"
)


def polish_whole_file(cues: list[dict], context: str, glossary: list[str],
                      level: str = "minimal",
                      speaker_source: str = "voiceprint",
                      dedupe_edits: list[tuple[int, str, str]] | None = None,
                      ) -> tuple[list[dict], list[dict]] | None:
    """把整份字幕交给 agent 就地修改——上下文最全、也最快（实测 1765 条 11.7 分钟，
    分批要 ~72 分钟）。风险只有一个：它可能动到时间轴，所以回来必须过结构校验，
    不过就返回 None，让调用方退回分批。"""
    raw = render_subtitle(cues, "srt", False)
    context_line = f"\n视频主题背景：{context}\n" if context else ""
    glossary_line = ("\n术语表（左边是识别可能出的错，右边是正确写法）：\n"
                     + "\n".join(f"  {t}" for t in glossary) + "\n") if glossary else ""

    # 说话人另开一个只读文件，不混进 sub.srt：那份要原样回收（按时间码回填文字），
    # 混进前缀就等于让 agent 有机会改坏它。
    files = {"sub.srt": raw}
    dedupe_line = ""
    if dedupe_edits:
        files["dedupe.tsv"] = dedupe_review_lines(dedupe_edits)
        dedupe_line = DEDUPE_REVIEW_RULES
    speaker_line = ""
    if any(cue.get("speaker") for cue in cues):
        files["speakers.tsv"] = "\n".join(
            f'{i}\t{cue.get("speaker") or "?"}' for i, cue in enumerate(cues, 1)
        ) + "\n"
        origin = ("由**声道 / 录音设备**判定（每人一支麦或一台设备，各占一条轨），基本可靠"
                  if speaker_source == "channel"
                  else "由 ASR 的声纹分离自动判定，**可能判错**")
        speaker_line = (
            "\n**speakers.tsv**（只读）给出每条字幕的说话人：`条目序号 <TAB> 说话人`，"
            f"{origin}。怎么用：\n"
            "- 说话人切换处是识别最容易乱的地方（抢话、接话），重点看这些位置\n"
            "- 一条字幕**只应该是一个人的话**。如果某条明显混进了两个人"
            "（前半句是问、后半句是答；或者前后半句主语/立场对不上），"
            "**绝对不要**把它硬改成一句通顺的话——那是在编造。保留原文，"
            "写进 uncertain.json，issue 写「疑似一条里混了两个人的话」，"
            "candidates 里给出你认为的拆分方式（如 `A：近来我们陆续探讨一下 ／ B：这个夹在哪`）\n"
        )
        if speaker_source == "channel":
            speaker_line += (
                "- 各轨是**分开转写**的，所以两个人的话混进同一条的情况基本不会发生；"
                "但别人的声音会以更小的音量串进本人的轨，"
                "**同一句话在相邻两条里被说了两遍**（一条清楚一条含糊）时，"
                "不要自作主张删，写进 uncertain.json 让人定\n"
            )

    instruction = (
        f"请润色当前目录下的 sub.srt（视频口播字幕，共 {len(cues)} 条，语音识别产物），"
        "**直接就地修改这个文件**。\n"
        f"{context_line}{glossary_line}{speaker_line}{dedupe_line}\n"
        + polish_rules(level) +
        "\n额外要求：\n"
        "- **绝对不要修改序号行和时间码行**，只改文字行\n"
        "- 不要删掉整个条目（该整条删的把文字行留空即可）\n"
        f"- 全文 {len(cues)} 条都要从头看到尾，不要只改前面一部分\n"
        "- 大部分条目本来就是对的，不需要改；改动比例低是正常的\n\n"
        "\n还要额外产出一份「存疑清单」：\n"
        "凡是**明显不符合上下文语境、但你没把握该改成什么**的地方，不要擅自改动 sub.srt，"
        "而是记进当前目录下的 uncertain.json，交给人来定。\n\n"
        "**判据只有一条：人看了会卡住、或者会当成事实记错，才值得报。**\n"
        "该报（这些人一眼看不出原话是什么，只能靠听）：\n"
        "- 疑似专有名词/产品名/术语但推不出是哪个词（`欧英one` → all in one、`tret` → chat）\n"
        "- 成语、固定表达被识别成乱码（`造访天干` → 倒反天罡）\n"
        "- 人名、公司名、数字（`章山` → 张三（虚构姓名示例））\n"
        "- 整句语义断裂、半截话，猜不出原意\n"
        "- **一条里混了两个人的话**（见上方 speakers.tsv 的说明）\n"
        "- 同一个词全片出现多种写法，你不确定以哪个为准\n\n"
        "**不要报**（这些是说话人本来就那么说的，或者不影响理解，报了只是噪音）：\n"
        "- 说话人自己的口误、说反了的语序（「大众创新万众创业」）——那是他真的这么说的\n"
        "- 多字、漏字、重复，但意思清清楚楚（「本身就是物异化了」「产品不能呃要消失」）\n"
        "- 语气词碎片、口头语\n"
        "- 近义词偏差，不影响理解（「生产目标」其实是「生产模式」这种）\n\n"
        "**只列你没有改动的地方**——已经在 sub.srt 里改掉的不要再列进来，那是噪音。\n\n"
        "uncertain.json 格式（JSON 数组，没有存疑就写 []）：\n"
        '[{"time": "00:12:34,567", "text": "原文照抄", "issue": "哪里不对", '
        '"candidates": ["候选改法1", "候选改法2"], "reason": "为什么怀疑"}]\n'
        "宁可少报也别硬凑——只报**真的读不通**的，正常口语的零碎和重复不算。\n\n"
        "改完后回复：你处理到了第几条、一共改了多少条、修正的专有名词有哪些、存疑几处。"
    )

    print(f"  handoff 整份纠错（{len(cues)} 条，直接改文件）...")
    result, uncertain_raw = run_agent_file_task(
        instruction, files, "sub.srt",
        extra_reads=("uncertain.json",), label="polish-whole")
    if result is None:
        return None

    parsed_uncertain = parse_json_list(uncertain_raw)
    if parsed_uncertain is None:
        raise ValueError("纠错任务缺少有效 uncertain.json；请补齐（无存疑时 []）再续跑")
    before, after = parse_text(raw), parse_text(result)
    ok, report = check_structure(before, after)
    print(report)
    if not ok:
        print("  ⚠️  结构校验未通过，丢弃整份润色结果，退回分批模式")
        return None

    # 按时间码回填：时间轴始终以我们自己的为准，agent 只贡献文字
    text_by_time = dict(after)
    polished = [
        {**cue, "text": text_by_time.get(stamp, cue["text"])}
        for cue, (stamp, _) in zip(cues, before)
    ]

    uncertain = []
    for entry in parsed_uncertain:
        if isinstance(entry, dict) and entry.get("text"):
            uncertain.append({
                "time": str(entry.get("time") or "").strip(),
                "text": str(entry["text"]).strip(),
                "issue": str(entry.get("issue") or "").strip(),
                "candidates": [str(c).strip() for c in (entry.get("candidates") or []) if str(c).strip()],
                "reason": str(entry.get("reason") or "").strip(),
            })
    return polished, uncertain


def reconcile_cross_track(cues: list[dict], context: str,
                          glossary: list[str]) -> tuple[list[dict], int, int] | None:
    """跨轨互校：两条声道对同一句话给出了两次独立识别，让 agent 把它用起来。

    机械去重只会看「时间重叠 + 字面相似」，两轨断句边界不对齐时必漏
    （实测同一段话 L 轨一句 26 秒、R 轨一句 10 秒，相似度算下来到不了阈值）。
    这一步把**两轨都有话的每个区段**连同前后文一起交给 agent，由它按语义判：
    同一句话被两个麦都收到 → 删掉糊的那条（必要时用清楚的那条修用词）；
    两个人真的各说各的 → 一条都不许删。

    返回 (cues, 删除数, 修正数)；handoff 出题那一轮返回 None。
    """
    clusters = dual_channel.crosstalk_clusters(cues)
    if not clusters:
        return cues, 0, 0

    def entry(idx: int, inside: bool) -> dict:
        cue = cues[idx]
        item = {
            "id": idx,
            "time": format_timestamp(cue["start"]),
            "spk": cue.get("speaker") or "?",
            "text": cue["text"],
        }
        if not inside:
            item["只读上下文"] = True
        return item

    span = 2
    payload_clusters = []
    for k, members in enumerate(clusters):
        lo, hi = min(members), max(members)
        ids = list(range(max(0, lo - span), min(len(cues), hi + 1 + span)))
        inside = set(members)
        payload_clusters.append({"段号": k, "条目": [entry(i, i in inside) for i in ids]})

    payload = {
        "任务": "分轨转写字幕的跨轨互校（每人一支麦或一台设备各占一轨）",
        "视频主题背景": context or "（未提供）",
        "专有名词正确写法": glossary or "（未提供）",
        "说明": "spk 是按声道/设备判定的说话人，基本可靠；每段是两条以上的轨同时有话的一个区段",
        "待判区段": payload_clusters,
    }

    instruction = (
        LANGUAGE_RULES +
        "你在校对一份**分轨转写**的对谈字幕。每个人各有一支麦或一台录音设备、各占一条轨，"
        "但别人的声音会以更小的音量串进本人的轨，所以**同一句话有时会被两条轨各识别一遍**——"
        "一条清楚、一条含糊。你的任务就是把这种重复挑出来。\n\n"
        "读当前目录下的 in.json，逐段判断，把结论写进 out.json。\n\n"
        "**每段只有三种结论**：\n"
        "1. **同一句话被两条轨各收了一遍** → 把**含糊的那条**放进 drop；"
        "如果清楚的那条里个别词明显是被串音带偏的，可以在 fix 里给出修正文字\n"
        "2. **两个人真的各说各的**（内容不同，哪怕时间完全重叠）→ **一条都不要删**，"
        "这是真抢话，删掉就等于把一个人的话抹掉了\n"
        "3. **拿不准** → 不动，写进 unsure\n\n"
        "判断要点：\n"
        "- 看**内容**，不要只看时间：附和（「对对对」「嗯」）和对方的长句同时出现是正常的，不是重复\n"
        "- 两轨的断句边界经常对不齐：一轨的一条可能对应另一轨的两三条，反过来也一样，"
        "所以要整段一起看，别逐条比\n"
        "- 含糊的那条通常是**串音**：字面上和清楚的那条对得上一大半，但夹着识别不出来的乱字\n"
        "- 标了「只读上下文」的条目是前后文，帮你判断语义，**不要**对它们下结论\n"
        "- 说话人（spk）判错的可能性很小，不要因为「这句听起来像另一个人说的」就去删\n"
        "- **宁可少删**：删错一条 = 永久丢失一个人的话；留错一条只是多一行重复，人一眼能看出来\n\n"
        "out.json 是一个 JSON 数组，每段一个元素（没有要处理的段也要写 []）：\n"
        '[{"段号": 0, "drop": [12, 13], "fix": [{"id": 11, "text": "修正后的文字"}], '
        '"unsure": [{"id": 15, "issue": "为什么拿不准"}], "reason": "这一段为什么这么判"}]\n'
        "id 用 in.json 里给的原值，不要重编号；drop/fix/unsure 里只能出现**非只读上下文**的 id。\n"
        "只写文件，不要在回复里输出 JSON。"
    )

    print(f"  agent 跨轨互校（{len(clusters)} 个两人同时说话的区段）...")
    data = run_agent_task(instruction, payload, label=f"reconcile-{len(clusters)}seg")
    if data is None:
        return None if last_call_pending() else (cues, 0, 0)

    allowed = {i for members in clusters for i in members}
    drop: set[int] = set()
    fixes: dict[int, str] = {}
    unsure: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        for raw_id in item.get("drop") or []:
            try:
                idx = int(raw_id)
            except (TypeError, ValueError):
                continue
            if idx in allowed:
                drop.add(idx)
        for entry_fix in item.get("fix") or []:
            if not isinstance(entry_fix, dict):
                continue
            try:
                idx = int(entry_fix.get("id"))
            except (TypeError, ValueError):
                continue
            text = str(entry_fix.get("text") or "").strip()
            if idx in allowed and text:
                fixes[idx] = text
        for entry_unsure in item.get("unsure") or []:
            if isinstance(entry_unsure, dict) and entry_unsure.get("id") is not None:
                unsure.append(entry_unsure)

    # 删过头就是把一个人的话抹掉了，宁可整轮不采信
    if len(drop) > len(allowed) * 0.5:
        print(f"  ⚠️  互校要删 {len(drop)}/{len(allowed)} 条（超过一半），判为跑偏，本轮不采信")
        return cues, 0, 0

    for idx, text in fixes.items():
        if idx not in drop:
            cues[idx]["text"] = text
    kept = [cue for i, cue in enumerate(cues) if i not in drop]
    if unsure:
        print(f"  互校拿不准 {len(unsure)} 处，已保留原文（会进存疑清单的另一批由纠错那步产出）")
    return kept, len(drop), len(fixes)


# ---------------------------------------------------------------- 多文件并行（同时录制的多台设备）


def parse_offsets(raw: str, count: int) -> list[float] | None:
    if not raw.strip():
        return None
    values = [float(v) for v in raw.split(",") if v.strip()]
    if len(values) != count:
        raise SystemExit(f"Error: --offsets 给了 {len(values)} 个值，但有 {count} 个文件")
    return values


def plan_cache_path(cache_dir: Path, files: list[Path], manual: list[float] | None) -> Path:
    raw = "|".join(f"{p.resolve()}|{p.stat().st_size}|{int(p.stat().st_mtime)}" for p in files)
    raw += f"|offsets={manual}|parallel-v1"
    return cache_dir / f"parallel-{hashlib.sha1(raw.encode()).hexdigest()[:16]}.json"


def plan_parallel(files: list[Path], reports: list, cache_dir: Path | None,
                  manual_offsets: list[float] | None) -> tuple[multi_track.Plan, list, list[int]] | None:
    """对齐 + 定轨。返回 (plan, 每轨共享时间轴上的 10ms 包络, 文件重排顺序)。

    plan 里的 file_idx 指的是**按开录先后重排后**的文件序号（最早开录的设备是文件 1 / spk1），
    调用方要用返回的 order 把自己手里的 files / reports 同样重排一遍。

    对齐结论和轨定义缓存在 `.subtitle_cache/parallel-*.json`（handoff 每轮续跑都要重新走这里，
    不缓存就要把所有文件重新解一遍）；10ms 包络不缓存，只有要重新做门限分离时才重算。
    """
    import numpy as np

    cache_file = plan_cache_path(cache_dir, files, manual_offsets) if cache_dir else None
    cached = None
    if cache_file and cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - 缓存损坏就重算
            cached = None

    mix_envs, chan_envs = [], []
    for path in files:
        stereo = dual_channel.read_stereo(path)
        if stereo is None:
            print(f"  Error: 音频读取失败: {path.name}")
            return None
        mix_envs.append(multi_track.envelope_db(stereo.mean(axis=0)))
        chan_envs.append([multi_track.envelope_db(stereo[c]) for c in range(2)])
        del stereo

    if cached:
        plan = multi_track.Plan(
            alignments=[multi_track.Alignment(**a) for a in cached["alignments"]],
            tracks=[multi_track.Track(**t) for t in cached["tracks"]],
            total_ms=cached["total_ms"], notes=cached.get("notes", []))
        order = list(cached["order"])
    else:
        if manual_offsets is not None:
            alignments = [multi_track.Alignment(offset_ms=v * 1000.0, ncc=1.0, peak_ratio=float("inf"))
                          for v in manual_offsets]
            earliest = min(a.offset_ms for a in alignments)
            for a in alignments:
                a.offset_ms -= earliest
        else:
            alignments = multi_track.align_files(mix_envs)
        # 对不齐的文件留在原位（调用方会据此报错或退回接续布局）
        order = sorted(range(len(files)), key=lambda i: (not alignments[i].confident, alignments[i].offset_ms))
        plan = multi_track.Plan(alignments=[alignments[i] for i in order], tracks=[], total_ms=0.0)

    files = [files[i] for i in order]
    reports = [reports[i] for i in order]
    mix_envs = [mix_envs[i] for i in order]
    chan_envs = [chan_envs[i] for i in order]

    total_frames = max(int(round(a.offset_ms / multi_track.FRAME_MS)) + len(env)
                       for a, env in zip(plan.alignments, mix_envs))
    plan.total_ms = total_frames * multi_track.FRAME_MS

    if not cached:
        speaker = 0
        for idx, (path, report) in enumerate(zip(files, reports)):
            dual = bool(report and report.dual)
            for channel in ([0, 1] if dual else [None]):
                tag = {0: "L", 1: "R", None: "混音"}[channel]
                plan.tracks.append(multi_track.Track(idx, channel, speaker, f"文件{idx + 1}·{tag}"))
                speaker += 1

    # 每轨搬到共享时间轴，并按**设备**归一化增益（同一文件的左右声道不互相归一）
    gains = [multi_track.file_gain_db(env) for env in mix_envs]
    track_envs = []
    for track in plan.tracks:
        env = (chan_envs[track.file_idx][track.channel] if track.channel is not None
               else mix_envs[track.file_idx])
        env = env - gains[track.file_idx]
        track_envs.append(multi_track.place_on_timeline(env, plan.alignments[track.file_idx], total_frames))

    if not cached:
        for i, j, corr in multi_track.same_source_pairs(track_envs, plan.tracks):
            if plan.tracks[j].merged_into is None and plan.tracks[i].merged_into is None:
                plan.tracks[j].merged_into = plan.tracks[i].speaker
                plan.notes.append(f"{plan.tracks[j].label} 与 {plan.tracks[i].label} 包络相关性 {corr:.2f}，"
                                  "判为同一声源录了两遍，只保留前者")
        # 并掉的轨不占说话人号
        live = [t for t in plan.tracks if t.merged_into is None]
        renumber = {t.speaker: k for k, t in enumerate(live)}
        for t in plan.tracks:
            if t.merged_into is None:
                t.speaker = renumber[t.speaker]
            else:
                t.merged_into = renumber.get(t.merged_into, t.merged_into)
        if cache_file:
            data = json.loads(plan.to_json())
            data["order"] = order
            cache_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return plan, track_envs, order


def decide_gate(separation: float, mode: str, notes: list[str] | None = None) -> bool:
    """要不要做门限分离（把别人的声音硬静音掉）。

    `auto` 下先看轨间分离度：领夹麦这类各收各人的素材通常 15 dB 以上，门限压得干净、
    还能省掉后面一堆清理；几台设备摆一张桌上互相串音的只有几 dB，这时门限在 turn 边界
    必然削掉词头（实测「只不过」被削成「不过」），而放宽 HOLD_MS 只会把串音放回来。
    分离度不够就整个跳过，让句级能量 + 文字相似 + agent 互校去分——那条路是非破坏性的，
    判错了原文还带着【串音】标记留在分轨字幕里，人能捞回来；门限判错音频就没了。
    """
    shown = "n/a" if separation == float("inf") else f"{separation:.1f} dB"
    if mode == "off":
        print(f"  轨间分离度 {shown} → 不做门限分离（--gate off）")
        return False
    if mode == "on":
        print(f"  轨间分离度 {shown} → 门限分离开启（--gate on）")
        return True
    if separation >= dual_channel.MIN_SEPARATION_DB:
        print(f"  轨间分离度 {shown} → 门限分离开启")
        return True
    note = (f"轨间分离度只有 {shown}（< {dual_channel.MIN_SEPARATION_DB:.0f} dB）："
            "各轨都收到了全场，硬静音会削掉词头，因此不做门限分离，"
            "改由句级能量 + 文字相似 + agent 互校分说话人")
    print(f"  {note}")
    if notes is not None:
        notes.append(note)
    return False


def print_plan(files: list[Path], plan: multi_track.Plan) -> None:
    for i, (path, a) in enumerate(zip(files, plan.alignments), 1):
        drift = f"  漂移 {(a.speed - 1) * 1e6:+.0f} ppm" if a.speed != 1.0 else ""
        conf = "" if a.confident else "  ⚠️ 对齐不可信"
        ncc = "对齐参考" if a.peak_ratio == float("inf") else f"对齐 ncc {a.ncc:.2f}"
        print(f"  {i:>2}. [{format_timestamp(a.offset_ms)}] {path.name}  "
              f"({media_duration(path) / 60:.1f}min)  {ncc}{drift}{conf}")
    for t in plan.tracks:
        state = f"→ 并入 spk{t.merged_into + 1}" if t.merged_into is not None else f"→ spk{t.speaker + 1}"
        print(f"      {t.label} {state}")
    for note in plan.notes:
        print(f"      Note: {note}")
    print(f"共享时间轴总长：{plan.total_ms / 60000:.1f}min")


def transcribe_parallel(files: list[Path], plan: multi_track.Plan, track_envs: list,
                        args, cache_dir: Path, tmp_dir: Path, router,
                        ) -> tuple[list[dict], list[dict], dict[int, list[int]]] | None:
    """并行布局：每轨门限分离后各自转写，返回 (保留的句子, 全部句子含串音标记, 轨包络)。
    句子时间已换成共享时间轴（含漂移校正），spk 即全局说话人号。"""
    live = [(k, t) for k, t in enumerate(plan.tracks) if t.merged_into is None]
    live_envs = [track_envs[k] for k, _ in live]
    gate = (len(live) > 1
            and decide_gate(dual_channel.separation_db(live_envs, multi_track.FRAME_MS),
                            args.gate, plan.notes))
    masks = multi_track.gate_masks(live_envs) if gate else [None] * len(live)
    gate_tag = f"m{dual_channel.KEEP_MARGIN_DB}h{dual_channel.HOLD_MS}" if gate else "nogate"
    align_key = hashlib.sha1(plan.to_json().encode()).hexdigest()[:8]

    sentences: list[dict] = []
    for (k, track), mask in zip(live, masks):
        path = files[track.file_idx]
        alignment = plan.alignments[track.file_idx]
        track_key = (f"P{track.file_idx}{track.channel}@{align_key}{gate_tag}"
                     f"n{args.speaker_count if args.speakers and args.speaker_count else 'auto'}")
        wav = tmp_dir / f"{hashlib.sha1(str(path).encode()).hexdigest()[:12]}-{track.speaker}.wav"
        def make_wav():
            if wav.exists():
                return wav
            mono = multi_track.read_mono(path, track.channel)
            if mono is None:
                raise RuntimeError(f"音频读取失败: {path.name}")
            if mask is not None:
                mono = multi_track.apply_gate(mono, mask, alignment)
            dual_channel.write_mono_wav(wav, mono)
            return wav
        cached = router.transcribe(path, track_key, make_wav, diarize=False)
        wav.unlink(missing_ok=True)
        for item in cached:
            item = multi_track.shift_sentence(dict(item), alignment)
            item["spk"] = track.speaker
            sentences.append(item)

    envelopes = multi_track.pooled_envelopes(live_envs)
    envelopes = {track.speaker: envelopes[i] for i, (_, track) in enumerate(live)}
    everything = list(sentences)
    kept = sentences
    if len(live) > 1:
        kept, bled = dual_channel.mark_bleed_sentences(kept, envelopes)
        kept, deduped = dual_channel.dedupe_cross_channel(kept, envelopes)
        if bled or deduped:
            print(f"串音清理：整句都是别人在说的丢掉 {bled} 句，多轨重复的丢掉 {deduped} 句")
    return kept, everything, envelopes


def probe_dual_report(path: Path, mode: str, seconds: float):
    """探测用的声道判定：只解开头一段，不写缓存（正式跑会按全片重判一次）。"""
    if mode == "off":
        return None
    if dual_channel.channel_count(path) < 2:
        return None
    stereo = dual_channel.read_stereo(path, seconds)
    if stereo is None:
        return None
    report = dual_channel.analyze(stereo, 2)
    if mode == "on":
        report.dual = True
    return report


def probe_speakers(files: list[Path], args) -> int:
    """只回答「这段素材里有几个人」，不做转写。

    值得单开一步，是因为选错的代价不对称：`--speakers` 会进 ASR 缓存 key
    （见 cache_path），事后想加/去这个开关等于**整片重转**（76 分钟约 30~40 分钟）；
    而取样 5 分钟跑一遍 cam++ 只要 20 秒左右（RTF≈0.07）。

    左右声道已经判成两个人的文件直接跳过——那种素材声道就是最可靠的说话人标签，
    比声纹聚类准得多，不需要也不该开 --speakers。
    """
    seconds = max(10.0, float(args.probe_seconds))
    if args.engine != "funasr" and getattr(args, "engine_explicit", True):
        print(f"Note: 声纹探测只有 funasr 有（cam++），忽略 --engine {args.engine}")

    print(f"声纹探测：每个文件取样前 {seconds / 60:.1f} 分钟，不写任何文件")
    model = load_model(args.device, True, "funasr")
    if model is None:
        return 1

    tmp_dir = Path(tempfile.mkdtemp(prefix="subtitle-probe-"))
    found: list[tuple[str, str, int]] = []      # (文件名, dual/mixed/skip, 主讲人数)
    try:
        for i, path in enumerate(files, 1):
            head = f"[{i}/{len(files)}] {path.name}"
            # 声道判定也只看取样段：整片解一遍 80 分钟素材要 20 多秒，
            # 而「左右声道是不是两个人」开头几分钟就看得出来
            report = probe_dual_report(path, args.dual_channel, seconds)
            if report and report.dual:
                print(f"{head}：左右声道各是一个人（{report.reason}）"
                      f" → 走双声道分轨，不要开 --speakers")
                found.append((path.name, "dual", 2))
                continue

            wav = tmp_dir / f"{hashlib.sha1(str(path).encode()).hexdigest()[:12]}.wav"
            if not extract_audio(path, wav, seconds):
                print(f"{head}：音频抽取失败，跳过")
                found.append((path.name, "skip", 0))
                continue
            sentences = transcribe_audio(model, wav, args.speaker_count)
            wav.unlink(missing_ok=True)
            if not sentences:
                print(f"{head}：取样段里没识别到语音")
                found.append((path.name, "skip", 0))
                continue

            _, stats = demote_bystanders(sentences, args.speaker_count)
            if not stats:
                print(f"{head}：ASR 没返回说话人信息（可能取样太短）")
                found.append((path.name, "skip", 0))
                continue
            kept = [x for x in stats if x["kept"]]
            print(f"{head}：听出 {len(stats)} 个人")
            for x in stats:
                mark = "主讲" if x["kept"] else "疑似旁人"
                secs = x["seconds"]
                span = f"{secs:5.1f} 秒" if secs < 60 else f"{secs / 60:5.1f} 分"
                print(f"        {x['speaker']}  {span}  {x['share']:>4.0%}  {mark}")
            found.append((path.name, "mixed", len(kept)))
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        if model is not None and hasattr(model, "close"):
            model.close()

    # 多个音频文件会走并行分轨（每台设备一轨），那条路径上说话人 = 设备号，
    # cam++ 的结果会被轨号覆写掉，所以那时给「--speakers」的建议是错的，要分开说
    parallel = len(files) > 1 and all(p.suffix.lower().lstrip(".") in AUDIO_EXTS for p in files)
    print("\n建议：")
    if not found:
        print("  （没有可用的探测结果）")
    elif parallel:
        print("  这是多个音频文件，正式跑会按「多台设备同时录」并行分轨，说话人取的是设备号，"
              "**--speakers 在这条路径上不生效**（会被轨号覆写）。直接跑默认参数即可：")
        for name, kind, n in found:
            if kind == "mixed" and n > 1:
                print(f"    ⚠️  {name}：听出 {n} 个人 = 这台设备前坐了不止一个人，"
                      f"设备级分轨分不开。要分开只能把它单独拎出来跑一遍 --speakers，再人工对时间轴合并")
            elif kind == "dual":
                print(f"    {name}：左右声道各一个人，自动分轨")
            elif kind == "mixed":
                print(f"    {name}：1 个人，正常")
    else:
        for name, kind, n in found:
            if kind == "dual":
                print(f"  {name}: 双声道分轨（默认参数即可，auto 会走 firered，不要开 --speakers）")
            elif kind == "skip":
                print(f"  {name}: 没探测出结果")
            elif n <= 1:
                print(f"  {name}: 默认参数即可（单人素材，auto 会走 firered，中文更准），不用 --speakers")
            else:
                print(f"  {name}: --speakers --speaker-count {n}"
                      f"（引擎不用管，auto 会自动切回 funasr 走 cam++）")
    print("\n取样只看开头一段，后面才出场的人数不到——人数你自己清楚的话，"
          "直接传 --speaker-count 更可靠。")
    return 0


class Stages:
    """分阶段计时。整条链路里最贵的从来不是 ASR，而是 agent 调用的冷启动次数，
    把耗时打出来，下次想优化才知道该动哪里。"""

    def __init__(self) -> None:
        self.items: list[tuple[str, float]] = []
        self._mark = time.monotonic()

    def done(self, name: str) -> None:
        now = time.monotonic()
        self.items.append((name, now - self._mark))
        self._mark = now

    def report(self) -> None:
        total = sum(d for _, d in self.items)
        if total <= 0:
            return
        print("\n耗时分布：")
        for name, dur in self.items:
            bar = "█" * max(1, round(dur / total * 24))
            print(f"  {name:<14} {dur / 60:6.1f} 分  {dur / total:5.0%} {bar}")
        print(f"  {'合计':<14} {total / 60:6.1f} 分")


def run_downstream(output: Path, context: str, want_review: bool, want_highlights: bool) -> bool:
    """审查和花字**并行跑**：两者互不依赖，都只读同一份最终字幕。

    两个脚本各自生成 handoff 任务，交给当前会话处理后再续跑。
    用子进程而不是线程，是为了各自独占 stdout——不然两边的进度日志会交错成一团。
    """
    from concurrent.futures import ThreadPoolExecutor

    script_dir = Path(__file__).resolve().parent
    jobs = []
    if want_review:
        jobs.append(("敏感/冒犯内容审查", script_dir / "make_review.py"))
    if want_highlights:
        jobs.append(("关键重点提示（花字）", script_dir / "make_highlights.py"))

    print(f"\n--- 并行生成：{'、'.join(name for name, _ in jobs)} ---")

    def run(job):
        name, script = job
        cmd = [sys.executable, str(script), str(output)]
        if context:
            cmd += ["--context", context]
        started = time.monotonic()
        result = subprocess.run(cmd, capture_output=True, text=True)
        return name, result, time.monotonic() - started

    handoff_pending = False
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        for name, result, elapsed in pool.map(run, jobs):
            print(f"\n===== {name}（{elapsed / 60:.1f} 分）=====")
            print(result.stdout.strip() or "(无输出)")
            if result.returncode == EXIT_HANDOFF_PENDING:
                handoff_pending = True
            elif result.returncode != 0 and result.stderr.strip():
                print(f"  stderr: {result.stderr.strip()[:400]}")
    return handoff_pending


def parse_stamp(stamp: str) -> float | None:
    try:
        hh, mm, rest = stamp.split(":")
        ss, mss = rest.replace(".", ",").split(",")
        return ((int(hh) * 60 + int(mm)) * 60 + int(ss)) * 1000 + int(mss)
    except (ValueError, AttributeError):
        return None


def anchor_uncertain(items: list[dict], cues: list[dict]) -> int:
    """把存疑项的时间码**按原文回锚到真实 cue 上**，返回修正了几条。

    agent 是**手抄**时间码的，而且抄完没人校验——实测 24 条里错了 7 条，
    毫秒位几乎都对（那三位是随机的，抄错自己会发现），错的全是分钟位（+1 / +2），
    还有两条直接抄成了隔壁那条的时间。清单是给人拖进剪辑器逐个跳转用的，
    时间错了这份清单就废了。

    原文是逐字照抄的，比时间码可靠得多：拿它回原字幕匹配，命中就用那条 cue 的真实时间。
    agent 给的时间只降级为**排歧线索**（同一句话全片出现多次时挑最近的那条）。
    """
    normalized = [(i, re.sub(r"[\s，。、；：,.;:！？!?]+", "", cue["text"])) for i, cue in enumerate(cues)]
    fixed = 0
    for item in items:
        target = re.sub(r"[\s，。、；：,.;:！？!?]+", "", item.get("text") or "")
        if not target:
            continue
        claimed = parse_stamp(item.get("time") or "")
        scored = []
        for i, text in normalized:
            if not text:
                continue
            ratio = 1.0 if text == target else SequenceMatcher(None, target, text).ratio()
            if ratio >= 0.6:
                drift = abs(cues[i]["start"] - claimed) if claimed is not None else 0
                scored.append((-ratio, drift, i))
        if not scored:
            item["time_anchored"] = False
            continue
        scored.sort()
        idx = scored[0][2]
        real = format_timestamp(cues[idx]["start"])
        if real != item.get("time"):
            item["time"] = real
            fixed += 1
        item["time_anchored"] = True
    return fixed


def write_uncertain(output: Path, items: list[dict]) -> Path:
    """存疑清单：agent 觉得读不通、但没把握改的地方，交给人定夺。

    跟随原字幕输出 SRT / VTT——这份清单的用法就是丢进剪辑器逐个跳转听原声，
    单独一份 .md 表格没人会去看（用户明确要求不要了），候选改法直接写进字幕行里。
    """
    vtt = output.suffix.lower() == ".vtt"
    srt_blocks = ["WEBVTT\n"] if vtt else []
    for i, item in enumerate(items, 1):
        cand = " ／ ".join(item["candidates"]) or "-"
        start = parse_stamp(item["time"] or "")
        if start is None:
            start = 0
        # 回锚失败的（原文在字幕里找不到）明说，别让人拿着一个可能是错的时间去跳转
        if item.get("time_anchored") is False:
            cand = f"{cand}　【时间未校准】"
        srt_blocks.append(
            f"{i}\n{format_timestamp(start, vtt)} --> {format_timestamp(start + 3000, vtt)}\n"
            f"【存疑】{item['text']}　→？{cand}\n"
        )

    srt_path = output.with_name(f"{output.stem}.uncertain{output.suffix}")
    srt_path.write_text("\n".join(srt_blocks), encoding="utf-8")
    print(f"\n⚠️  {len(items)} 处存疑待你确认（拖进剪辑器逐个跳转听原声）：{srt_path}")
    return srt_path


def write_dedupe_report(output: Path, edits: list[tuple[int, str, str]],
                        cues: list[dict] | None) -> tuple[Path, int]:
    """去重对照清单：每条「去重前 → 去重后」，标出 agent 复核时回滚了哪些。

    规则去重永远可能误伤——汉语里合法的重复太多（「老大的老大」「目的的」「想怎么读怎么读」），
    白名单只能挡住已知的那几族。所以真正的把关是两道：
    agent 在纠错同一轮里复核（自动），加这份给人扫的清单（最终）。
    每条只有一行，1500 条字幕通常也就一两百行，扫一遍很快。

    返回 (清单路径, agent 回滚条数)。
    """
    rolled_back = 0
    lines = ["# 连续重复去重对照清单", "",
             "规则只看字面，不懂语义，所以这里每一条都值得扫一眼。",
             "`已回滚` = agent 复核时判定误伤、已经改回去重前的样子。",
             "剩下的如果你发现哪条削坏了意思，直接在正片字幕里把字补回来。", ""]
    for index, before, after in edits:
        final = cues[index - 1]["text"] if cues and index - 1 < len(cues) else None
        if final is not None and final == before:
            rolled_back += 1
            status = "已回滚"
        elif final is not None and final not in (before, after):
            status = "纠错后又改过"
        else:
            status = "保留"
        lines.append(f"- **{index}** [{status}] `{before}` → `{after}`"
                     + (f"　最终：`{final}`" if status == "纠错后又改过" else ""))
    lines.append("")
    lines.insert(5, f"共 {len(edits)} 条，其中 agent 回滚 {rolled_back} 条。\n")

    path = output.with_name(f"{output.stem}.dedupe.md")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path, rolled_back


def write_track_subtitles(output: Path, track_cues: dict[int, list[dict]], fmt: str,
                          keep_trailing_punct: bool, min_dur: float, max_dur: float) -> list[Path]:
    """每条轨（声道 / 设备）单独出一份**原始**字幕 `.spkN.raw.srt`。

    这是对照物，不是交付物（交付的每人一份是纠错后的 `.spkN.srt`，见 write_speaker_subtitles）：
    用来只听一个人做核对、以及回头查「机器把哪些判成串音丢了」——
    被丢掉的条目在这里带 `【串音】` 前缀原样保留，判错了人才看得见。
    """
    paths = []
    for speaker in sorted(track_cues):
        cues = normalize_timeline([dict(cue) for cue in track_cues[speaker]], min_dur, max_dur)
        rendered = []
        for cue in cues:
            text = finalize_text(cue["text"], keep_trailing_punct, cue.get("language", "auto"))
            if not text:
                continue
            rendered.append({**cue, "speaker": None,
                             "text": f"【串音】{text}" if cue.get("bleed") else text})
        path = output.with_name(f"{output.stem}.spk{speaker + 1}.raw{output.suffix}")
        path.write_text(render_subtitle(rendered, fmt, False), encoding="utf-8")
        bleed = sum(1 for cue in rendered if cue["text"].startswith("【串音】"))
        print(f"spk{speaker + 1} 单轨原始字幕：{path}（{len(rendered)} 条，其中判为串音 {bleed} 条）")
        paths.append(path)
    return paths


def write_speaker_subtitles(output: Path, cues: list[dict], fmt: str) -> list[Path]:
    """每个说话人一份**最终**字幕 `.spkN.srt`：从纠错后的正片条目里按说话人筛出来，
    时间轴和总字幕完全一致，单行、不带前缀，可以直接单独压片或交给对应的人核对。"""
    paths = []
    for speaker in sorted({c["speaker"] for c in cues if c.get("speaker")}):
        own = [{**c, "speaker": None} for c in cues if c.get("speaker") == speaker]
        path = output.with_name(f"{output.stem}.{speaker}{output.suffix}")
        path.write_text(render_subtitle(own, fmt, False), encoding="utf-8")
        print(f"{speaker} 字幕：{path}（{len(own)} 条）")
        paths.append(path)
    return paths


def merged_glossary(extra: str) -> list[str]:
    """skill 自带的 glossary.txt 打底，再叠加 --glossary / 环境变量传进来的。"""
    terms = []
    builtin = Path(__file__).resolve().parent / "glossary.txt"
    if builtin.is_file():
        terms += [
            line.strip() for line in builtin.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
    terms += load_glossary(extra)
    seen, out = set(), []
    for term in terms:
        if term not in seen:
            seen.add(term)
            out.append(term)
    return out


def context_cues() -> int:
    """每批前后各附带多少条只读上下文。"""
    return int(os.environ.get("SUBTITLE_CONTEXT_CUES", "12"))


def llm_polish_batch(cues: list[dict], indices: list[int], context: str, glossary: list[str],
                     level: str = "minimal") -> dict[int, str] | None:
    # 前后文只读，帮助判断错别字和句子边界，但不要求 agent 回传
    span = context_cues()
    lo, hi = indices[0], indices[-1]
    payload = {
        "任务": "视频口播字幕保守润色",
        "视频主题背景": context or "（未提供）",
        "专有名词正确写法": glossary or "（未提供）",
        "前文_只读参考_不要返回": " ".join(cue["text"] for cue in cues[max(0, lo - span):lo]),
        "后文_只读参考_不要返回": " ".join(cue["text"] for cue in cues[hi + 1:hi + 1 + span]),
        "待处理字幕": [{"id": idx, "text": cues[idx]["text"], "audio_language": cues[idx].get("language", "auto")} for idx in indices],
    }

    instruction = (
        "你是视频口播字幕的校对员。\n\n"
        "请读取当前目录下的 in.json，对其中「待处理字幕」数组里的每一条做保守润色，"
        "然后把结果写入当前目录下的 out.json。\n\n"
        + polish_rules(level) +
        "\n额外要求：\n"
        "- in.json 的「专有名词正确写法」就是术语表，按它纠正\n"
        "- 「前文」「后文」只是帮你判断错别字的参考，**不要**把它们写进 out.json\n"
        "- 大部分条目本来就是对的，原样回传即可\n\n"
        f"out.json 必须是一个 JSON 数组，包含且仅包含「待处理字幕」里全部 {len(indices)} 个 id，"
        "元素格式 {\"id\": 0, \"text\": \"润色后的文字\"}，id 原样保留不要重编号。\n"
        "有存疑时保留原文，并在该条添加 uncertain 字段说明问题，不猜测。\n"
        "只写文件，不要在回复里输出 JSON。写完后确认 out.json 是合法 JSON 且条数正确。"
    )

    data = run_agent_task(instruction, payload, label=f"polish-cue{lo + 1}-{hi + 1}")
    if data is None:
        return None

    result: dict[int, str] = {}
    allowed = set(indices)
    for entry in data:
        if not isinstance(entry, dict) or "id" not in entry or "text" not in entry:
            continue
        try:
            idx = int(entry["id"])
        except (TypeError, ValueError):
            continue
        if idx in allowed:
            result[idx] = str(entry["text"]).strip()
            if entry.get("uncertain"):
                result[idx] = cues[idx]["text"]
                BATCH_UNCERTAIN.append({"time": format_timestamp(cues[idx]["start"]),
                    "text": cues[idx]["text"], "issue": str(entry["uncertain"]), "candidates": [], "reason": "批次纠错存疑"})

    if not result:
        return None
    # 批次大时 agent 可能漏返；漏太多就判定失败，交给上层拆批重试
    if len(result) < len(indices) * 0.8:
        print(f"  Warning: agent 只返回 {len(result)}/{len(indices)} 条，判为失败并拆批")
        return None
    if len(result) < len(indices):
        print(f"  Note: {len(indices) - len(result)} 条未返回，保留原文")
    return result


def llm_polish_with_fallback(cues: list[dict], indices: list[int], context: str, glossary: list[str],
                             level: str = "minimal") -> dict[int, str]:
    result = llm_polish_batch(cues, indices, context, glossary, level)
    if result is not None:
        return result
    if last_call_pending():
        # handoff：题刚出还没答案，别拆批（拆了只会出一堆更碎的题），
        # 也别退规则模式（这一轮的产物本来就会被丢掉）。下一轮重跑就有答案了。
        return {}
    if len(indices) > 1:
        mid = len(indices) // 2
        print(f"  Retrying with smaller batches ({mid} + {len(indices) - mid} cues)...")
        merged = llm_polish_with_fallback(cues, indices[:mid], context, glossary, level)
        merged.update(llm_polish_with_fallback(cues, indices[mid:], context, glossary, level))
        return merged
    idx = indices[0]
    print(f"  agent failed for cue {idx + 1}, fallback to rule mode.")
    return {idx: rule_polish(cues[idx]["text"], cues[idx].get("language", "auto")) if level == "clean" else cues[idx]["text"]}


def polish_cues(cues: list[dict], use_llm: bool, context: str, glossary: list[str],
                max_batch_cues: int, max_batch_chars: int, mode: str = "whole",
                level: str = "minimal",
                speaker_source: str = "voiceprint",
                dedupe_edits: list[tuple[int, str, str]] | None = None,
                ) -> tuple[list[dict], int, list[dict]]:
    # 首选整份就地修改：上下文最全、快 6 倍；结构校验不过再退回分批
    BATCH_UNCERTAIN.clear()
    done = False
    waiting = False                            # handoff：整份的题已出，等下一轮拿答案
    uncertain: list[dict] = []
    if use_llm and mode == "whole":
        whole = polish_whole_file(cues, context, glossary, level, speaker_source, dedupe_edits)
        if whole is not None:
            cues, uncertain = whole
            done = True
        elif last_call_pending():
            waiting = True                     # 别退分批——那会把同一份内容再出 17 道题
        else:
            print("  → 退回分批模式")

    if done or waiting:
        pass                                   # 整份已润色（或在等答案），不要再叠一层规则处理
    elif use_llm:
        batches = batch_cues(cues, max_batch_cues, max_batch_chars)
        polished: dict[int, str] = {}
        for i, indices in enumerate(batches):
            print(f"  agent 润色第 {i + 1}/{len(batches)} 批（{len(indices)} 条）...")
            polished.update(llm_polish_with_fallback(cues, indices, context, glossary, level))
        for idx, cue in enumerate(cues):
            cue["text"] = polished.get(idx, cue["text"]).strip()
    elif level == "clean":
        for cue in cues:
            cue["text"] = rule_polish(cue["text"], cue.get("language", "auto"))
    else:
        # minimal 级别没有 agent 时什么都不做：规则去不了错别字，乱删水词只会更糟
        print("  纯纠错级别 + 无 agent：保持 ASR 原文不动")

    kept, dropped = [], 0
    for cue in cues:
        text = cue["text"].strip()
        # minimal 级别只丢真正空的；「嗯」「对吧」这类整条语气词是说话人的语气，必须留着
        if not text or (level == "clean" and chinese_rules(text, cue.get("language", "auto")) and FILLER_ONLY_RE.match(text)):
            dropped += 1
            continue
        kept.append(cue)
    return kept, dropped, uncertain + BATCH_UNCERTAIN


# 脏话 / 敏感口语统一做轻量脱敏，避免正片字幕直接出镜触发平台违规。
# 规则保持简单可预期：只把脏字本身替换成 X，不改动语义和句式。
DIRTY_WORD_PATTERNS = (
    (re.compile(r"他妈的"), "他X的"),
    (re.compile(r"妈的"), "X的"),
    (re.compile(r"我操"), "我X"),
    (re.compile(r"我靠"), "我X"),
    (re.compile(r"我艹"), "我X"),
    (re.compile(r"卧槽"), "我X"),
    (re.compile(r"我草"), "我X"),
    (re.compile(r"扯淡"), "X淡"),
    (re.compile(r"傻逼"), "傻X"),
    (re.compile(r"草泥马"), "X泥马"),
)


def mask_dirty_words(text: str) -> str:
    for pattern, replacement in DIRTY_WORD_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def finalize_text(text: str, keep_trailing_punct: bool, language: str = "auto") -> str:
    if not chinese_rules(text, language):
        return text.strip()
    text = re.sub(r"\s+", " ", text).strip()
    # 删掉水词后可能留下孤零零的开头标点
    text = re.sub(r"^[，。、；：,.;:！？!?]+\s*", "", text)
    # 中英文之间补空格。例外：数字后面紧跟时间单位不加空格——年份写成「19 年」
    # 会被读成「19」和「年」两块，「19年」才是一眼认得出的年份
    text = re.sub(r"([一-鿿])([A-Za-z0-9])", r"\1 \2", text)
    text = re.sub(r"([A-Za-z0-9])(?![年月日号点岁])([一-鿿])", r"\1 \2", text)
    if not keep_trailing_punct:
        # 问号叹号一并去掉：ASR 的标点模型按语调判句式，口播里一个上扬的尾音就够它点问号，
        # 实测「啊？」「直接发给 agent？」这种根本不是疑问句。何况成片字幕的惯例本来
        # 就是不带句末标点——句号逗号都删了，只留问号反而扎眼。要保留就加 --keep-trailing-punct
        text = re.sub(r"[，。、；：,.;:！？!?]+$", "", text).strip()
    text = mask_dirty_words(text)
    return text


# ---------------------------------------------------------------- 输出


def format_timestamp(ms: float, vtt: bool = False) -> str:
    ms = max(int(round(ms)), 0)
    hours, ms = divmod(ms, 3600_000)
    minutes, ms = divmod(ms, 60_000)
    seconds, millis = divmod(ms, 1000)
    sep = "." if vtt else ","
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}{sep}{millis:03d}"


def render_subtitle(cues: list[dict], fmt: str, speaker_tags: bool) -> str:
    vtt = fmt == "vtt"
    blocks = ["WEBVTT\n"] if vtt else []
    for i, cue in enumerate(cues, 1):
        text = cue["text"]
        if speaker_tags and cue.get("speaker"):
            text = f'{cue["speaker"]}: {text}'
        blocks.append(
            f"{i}\n{format_timestamp(cue['start'], vtt)} --> {format_timestamp(cue['end'], vtt)}\n{text}\n"
        )
    return "\n".join(blocks)


# ---------------------------------------------------------------- 主流程


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="视频字幕生成：转写 + 去水词 + 修错别字，多个视频按创建时间合成一个字幕文件",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例:
  # 单个视频
  python generate_subtitle.py "/path/to/video.mp4"

  # 整个目录（按视频创建时间拼成一个字幕文件）
  python generate_subtitle.py "/path/to/videos/" -o "/path/to/videos/合集.srt"

  # 先确认视频顺序，不跑转写
  python generate_subtitle.py "/path/to/videos/" --dry-run

  # 顺带生成关键重点提示字幕
  python generate_subtitle.py "/path/to/videos/" --with-highlights
""",
    )
    parser.add_argument("target", help="视频/音频/目录；已有 .srt/.vtt 配合 --translate-to 可仅翻译")
    parser.add_argument("-o", "--output",
                        help="输出字幕文件或项目目录（默认复用或新建 Publish 项目，存入 subtitles/）")
    parser.add_argument("--format", choices=("srt", "vtt"), default="srt", help="字幕格式 (默认: srt)")
    parser.add_argument("--recursive", action="store_true", help="目录模式下递归查找子目录")
    parser.add_argument("--sort-by", choices=("auto", "ffprobe", "birthtime", "mtime", "name"),
                        default="auto", help="多视频排序依据 (默认: auto)")
    parser.add_argument("--engine", default=os.environ.get("SUBTITLE_ASR_ENGINE", "auto"),
                        choices=("auto", "funasr", "firered", "whisper"),
                        help="中文：声纹分离用 funasr，否则 firered（未安装时 funasr）；非中文用 whisper")
    parser.add_argument("--language", default="auto", help="音频语言提示（如 zh/en/ja），默认 auto；不代表翻译")
    parser.add_argument("--translate-to", help="明确要求的翻译目标语言（如 en）；原文纠错后另存译文")
    parser.add_argument("--whisper-model", default="small", help="Whisper 多语言模型名或 checkpoint 路径，默认 small；拒绝英语单语模型")
    parser.add_argument("--device", default=os.environ.get("SUBTITLE_DEVICE", "auto"),
                        choices=("auto", "cpu", "mps", "cuda"),
                        help="ASR 运行设备：auto=有 cuda 用 cuda、有 mps 用 mps、否则 cpu（默认）。"
                             "Whisper 词时间戳路径选择 mps 时明确回退 cpu/FP32")
    parser.add_argument("--max-chars", "--max-width", dest="max_chars", type=float,
                        default=float(os.environ.get("SUBTITLE_MAX_CHARS", DEFAULT_MAX_WIDTH)),
                        help="Unicode 显示字宽：全角 1、半角 0.5、组合符不重复计宽 (默认: 16)")
    parser.add_argument("--min-duration", type=float, default=1.0, help="单条最短显示秒数 (默认: 1.0)")
    parser.add_argument("--max-duration", type=float, default=6.0, help="单条最长显示秒数 (默认: 6.0)")
    parser.add_argument("--context", default="", help="视频主题背景，帮助 LLM 判断错别字")
    parser.add_argument("--glossary", default=os.environ.get("SUBTITLE_GLOSSARY", ""),
                        help="术语表：文件路径或逗号分隔的词")
    parser.add_argument("--speakers", action="store_true",
                        help="启用声纹说话人分离（cam++）：按说话人切分条目，两人抢话时合成一条两行字幕")
    parser.add_argument("--speaker-count", type=int,
                        help="已知说话人数；与 --speakers 配合，固定 cam++ 聚类人数，"
                             "也决定旁人筛选保留几个主讲人")
    parser.add_argument("--probe-speakers", action="store_true",
                        help="只做声纹探测：抽开头一段跑一遍 cam++，报告有几个人、给出建议参数，"
                             "不转写不写盘（选错 --speakers 要整片重转，先花 20 秒问一句更划算）")
    parser.add_argument("--probe-seconds", type=float, default=300.0,
                        help="声纹探测的取样时长秒数 (默认: 300)")
    parser.add_argument("--dual-channel", choices=("auto", "on", "off"), default="auto",
                        help="左右声道是两个人各自的麦克风时分轨转写："
                             "auto=自动判断（默认）/ on=强制 / off=关闭")
    parser.add_argument("--gate", choices=("auto", "on", "off"), default="auto",
                        help="多轨/双声道门限分离（把别人的声音硬静音）："
                             f"auto=轨间分离度 ≥{dual_channel.MIN_SEPARATION_DB:.0f}dB 才做 (默认)")
    parser.add_argument("--pause-split", type=float, default=DEFAULT_PAUSE_SPLIT_MS / 1000.0,
                        help="句内停顿超过这么多秒就强制断成两条，"
                             f"防止两个人的话并进一条 (默认: {DEFAULT_PAUSE_SPLIT_MS / 1000.0:g}，0 = 关闭)")
    parser.add_argument("--speaker-labels", action="store_true",
                        help="每条字幕都加「spkN: 」前缀（默认只有两人同时说话合成的两行条目才加）")
    parser.add_argument("--layout", choices=("auto", "parallel", "sequential"), default="auto",
                        help="多个文件怎么拼：sequential=首尾接续（多段视频）；parallel=同时录的多台设备，"
                             "按音频互相关对齐到一条时间轴、每台设备各自分轨；"
                             "auto=全是音频文件且能对齐上就 parallel，否则 sequential（默认）")
    parser.add_argument("--offsets", default="",
                        help="parallel 布局下手动指定各文件的起始偏移秒数（逗号分隔，按排序后的文件顺序），"
                             "给了就不做自动对齐")
    parser.add_argument("--polish-level", choices=("minimal", "clean"), default="minimal",
                        help="minimal=只纠错别字/脏字/专有名词，保留全部语气词（默认）；clean=额外去水词")
    parser.add_argument("--polish-mode", choices=("whole", "batch"), default="whole",
                        help="whole=整份交给 agent 就地改（默认，快且上下文全）；batch=分批走 JSON 协议")
    parser.add_argument("--no-llm", action="store_true", help="不创建模型任务，只用规则去水词")
    parser.add_argument("--no-cache", action="store_true", help="不读写 ASR 结果缓存")
    parser.add_argument("--keep-trailing-punct", action="store_true", help="保留每条结尾的逗号句号")
    parser.add_argument("--dry-run", action="store_true", help="只打印将要处理的视频顺序")
    parser.add_argument("--with-highlights", action="store_true", help="生成后顺带产出关键重点提示字幕")
    parser.add_argument("--with-review", action="store_true", help="生成后顺带产出敏感/冒犯内容审查字幕")
    return parser.parse_args()


CONTROL_OR_SEPARATOR = re.compile(r"[\x00-\x1f\x7f/\\]+")
LEADING_DATE_TIME = re.compile(
    r"""
    ^\s*
    (?:
        (?:19|20)\d{2}[-._](?:1[0-2]|0?[1-9])[-._](?:3[01]|[12]\d|0?[1-9])
        |
        (?:19|20)\d{2}年(?:1[0-2]|0?[1-9])月(?:3[01]|[12]\d|0?[1-9])日?
        |
        (?:19|20)\d{2}(?:1[0-2]|0[1-9])(?:3[01]|[12]\d|0[1-9])
    )
    (?:
        [T\s_-]+
        (?:2[0-3]|[01]?\d)
        (?:
            (?::|[._-])?[0-5]\d
            (?:(?::|[._-])?[0-5]\d)?
        )?
    )?
    (?=$|[\s._-])
    [\s._-]*
    """,
    re.VERBOSE,
)


def sanitize_name(value: str) -> str:
    value = CONTROL_OR_SEPARATOR.sub("-", value.strip())
    value = re.sub(r"\s+", " ", value)
    value = re.sub(r"-{2,}", "-", value)
    return value.strip(" .-") or "字幕项目"


def strip_leading_date_time(value: str) -> str:
    """去掉文件名/目录名前导的录制日期时间，再拿去做目录名后缀。"""
    return LEADING_DATE_TIME.sub("", value, count=1)


def publish_parent() -> Path:
    """使用随仓库分发的 Publish，独立安装时回退当前目录。"""
    repo = Path(__file__).resolve().parents[3]
    return repo / "Publish" if (repo / ".agents").is_dir() else Path.cwd() / "Publish"


def existing_publish_project(target: Path, parent: Path) -> Path | None:
    # 不 resolve 素材软链接：项目内的视频可能指向外接盘。
    lexical = Path(target).expanduser().absolute()
    try:
        parts = lexical.relative_to(parent.absolute()).parts
    except ValueError:
        parts = ()
    if parts and not parts[0].startswith("_"):
        project = parent / parts[0]
        if project.is_dir():
            return project
    raw = target.name if target.is_dir() else target.stem
    topic = sanitize_name(strip_leading_date_time(raw)).casefold()
    matches = [p for p in parent.iterdir()
               if p.is_dir() and not p.name.startswith("_")
               and sanitize_name(strip_leading_date_time(p.name)).casefold() == topic] if parent.is_dir() else []
    return matches[0] if len(matches) == 1 else None


def create_unique_directory(parent: Path, base_name: str) -> Path:
    parent.mkdir(parents=True, exist_ok=True)
    candidate = parent / base_name
    collision_index = 1
    while True:
        try:
            candidate.mkdir()
            return candidate.resolve()
        except FileExistsError:
            collision_index += 1
            candidate = parent / f"{base_name}-{collision_index}"


SUBTITLE_SUFFIXES = {".srt", ".vtt"}


def resolve_output(target: Path, output: str | None, fmt: str, create: bool = True) -> Path:
    stem = target.name if target.is_dir() else target.stem
    if output:
        path = Path(output).expanduser().resolve()
        if not path.is_dir() and path.suffix.lower() in SUBTITLE_SUFFIXES:
            return path  # 用户明确指定的文件路径优先。
        folder = path if "subtitles" in path.parts else path / "subtitles"
    else:
        parent = publish_parent()
        project = existing_publish_project(target, parent)
        if project is None:
            suffix = sanitize_name(strip_leading_date_time(stem))
            base = parent / f"{datetime.now().strftime('%Y%m%d-%H%M')}-{suffix}"
            project = create_unique_directory(base.parent, base.name) if create else base
        folder = project / "subtitles"
    if create:
        folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{stem}.{fmt}"


def resume_command_hint(output: Path) -> str:
    """handoff 续跑命令。**必须补上 -o**：无法匹配旧项目时会新建一个带时间戳的项目目录，
    续跑就会跑到另一个空目录里去（ASR 缓存和已完成的任务全都白瞎）。"""
    argv = sys.argv
    has_output = any(a in ("-o", "--output") or a.startswith("--output=") for a in argv)
    return resume_command([] if has_output else ["-o", str(output)])


def write_transcript_json(output: Path, cues: list[dict], files: list[Path],
                          offsets: list[float], layout: str) -> None:
    """字幕的唯一真相源 `<名字>.transcript.json` + 混音 `audio/mix.flac`。

    SRT 从此只是派生物；逐字时间戳来自 TRANSCRIPT_RAW（ASR 原句），纠错改过字的按 diff 对齐。
    这一步失败不影响字幕产出，只打警告。设 SUBTITLE_NO_TRANSCRIPT=1 可跳过（省掉压混音的时间）。
    """
    if transcript_lib is None or os.environ.get("SUBTITLE_NO_TRANSCRIPT"):
        return
    try:
        base = output.parent
        mix_rel = Path("audio") / f"{output.stem}.mix.flac"
        sources = [(Path(p), float(o)) for p, o in zip(files, offsets)]
        total_ms = max((float(c["end"]) for c in cues), default=0.0)
        transcript_lib.persist_mix(sources, base / mix_rel, layout, duration_ms=int(total_ms) + 200)
        plain = [{"start_ms": int(round(c["start"])), "end_ms": int(round(c["end"])),
                  "text": c["text"], "speaker": c.get("speaker")} for c in cues]
        # The optional legacy transcript exporter expects visible-character stamps,
        # whereas ASR engines return word/token stamps. Expand before its text diff.
        raw_chars = []
        for item in TRANSCRIPT_RAW:
            original = item.get("text", "")
            times = char_time_map(original, item.get("timestamp"), item["start"], item["end"], item.get("words"))
            raw_chars.append({**item, "timestamp": [list(times[i]) for i, _ in transcript_lib._visible(original)]})
        t = transcript_lib.build_transcript(plain, raw_chars, {"mix": str(mix_rel), "tracks": {}},
                                            source_note=f"generate_subtitle {output.name}")
        # Keep all codepoints of a grapheme on one interval even after corrections.
        for cue in t["cues"]:
            for a, b in grapheme_spans(cue["text"]):
                lo = max(cue["start_ms"], min(cue["end_ms"], min(x[0] for x in cue["chars"][a:b])))
                hi = max(lo, min(cue["end_ms"], max(x[1] for x in cue["chars"][a:b])))
                cue["chars"][a:b] = [[lo, hi] for _ in range(b - a)]
        t["audio"]["meta"] = {"mix": transcript_lib.file_meta(base / mix_rel)}
        t["sources"] = [{"file": str(p), "file_rel": os.path.relpath(p, base), "offset_ms": o}
                        for p, o in sources]
        errs = transcript_lib.validate_transcript(t, base)
        path = output.with_name(f"{output.stem}.transcript.json")
        transcript_lib.dump(t, path)
        print(f"transcript：{path}（{t['stats']['cues']} 条，逐字时间戳插值 {t['stats']['approx_cues']} 条）"
              f"　混音：{base / mix_rel}")
        for e in errs:
            print(f"  ⚠️ transcript 校验：{e}")
    except Exception as exc:  # noqa: BLE001 —— 附加产物，不能拖垮主流程
        print(f"⚠️ transcript.json 未生成：{exc}")


def main() -> int:
    TRANSCRIPT_RAW.clear()
    args = parse_args()
    # 断句要靠术语表才知道 "Refore HTML to Figma" 是一个整体，所以在任何转写之前先登记
    set_protected_terms(merged_glossary(args.glossary))
    if args.speaker_count is not None:
        if not (args.speakers or args.probe_speakers):
            print("Error: --speaker-count 必须与 --speakers（或 --probe-speakers）一起使用")
            return 1
        if args.speaker_count < 1:
            print("Error: --speaker-count 必须大于 0")
            return 1
    args.language = normalize_language(args.language)
    if args.whisper_model.endswith(".en"):
        print("Error: --whisper-model 必须选择多语言模型，不能用 .en")
        return 1
    if args.translate_to:
        from translate_subtitle import target_code, translate_file
        args.translate_to = target_code(args.translate_to)
        if args.no_llm or not check_agent_available():
            raise ValueError("--translate-to 需要启用 handoff，不能与 --no-llm / SUBTITLE_LLM=0 一起使用")
    target = Path(args.target).expanduser().resolve()
    if target.suffix.lower() in SUBTITLE_SUFFIXES:
        if not args.translate_to:
            raise ValueError("已有字幕请指定 --translate-to；无需重新转写")
        dest = Path(args.output).expanduser() if args.output else None
        return translate_file(target, args.translate_to, dest, args.dry_run)
    args.device = resolve_device(args.device)
    args.engine_explicit = args.engine != "auto"
    if args.max_chars <= 0 or args.min_duration <= 0 or args.max_duration < args.min_duration:
        raise ValueError("字宽/时长必须为正，最大时长不能小于最小时长")

    files = collect_media(target, args.recursive)
    if not files:
        print(f"Error: 没有找到可处理的音视频文件: {target}")
        return 1

    stages = Stages()
    files, sort_label = sort_media(files, args.sort_by)

    if args.probe_speakers:
        # Only the existing Chinese FunASR pipeline is validated for voiceprints.
        if args.language != "zh":
            with tempfile.TemporaryDirectory(prefix="subtitle-lid-") as folder:
                router = ASRRouter(args, Path(folder), cache_path, resolve_engine, load_model)
                try:
                    for i, path in enumerate(files):
                        wav = Path(folder) / f"probe-{i}.wav"
                        def make_probe():
                            if not wav.exists() and not extract_audio(path, wav):
                                raise RuntimeError("无法抽取语言探测音频")
                            return wav
                        detected = router.detect(path, "probe", make_probe)
                        if not detected["chinese"]:
                            raise ValueError("非中文/语言不确定：不能用当前 FunASR 中文声纹探测管线；保留声道或设备分轨")
                finally:
                    router.close()
        return probe_speakers(files, args)

    output = resolve_output(Path(args.target).expanduser().absolute(), args.output, args.format, create=not args.dry_run)
    diarize = args.speakers

    # ASR 结果**值得留在项目目录里**：改润色参数重跑、handoff 分轮续跑都靠它，
    # 少了它每一轮都要重新转写整片。不想让它进工程目录就设 SUBTITLE_CACHE_DIR。
    cache_dir = Path(os.environ.get("SUBTITLE_CACHE_DIR") or (output.parent / ".subtitle_cache")).expanduser()
    if not args.no_cache and not args.dry_run:
        cache_dir.mkdir(parents=True, exist_ok=True)
    use_cache_dir = None if (args.no_cache or args.dry_run) else cache_dir

    # 多个文件是「接着录」还是「同时录」？同时录的（每人一台设备）要对齐到一条时间轴、各自分轨，
    # 而不是首尾相接。auto：全是音频文件就先试对齐，对得上才算并行
    manual_offsets = parse_offsets(args.offsets, len(files))
    layout = args.layout
    if manual_offsets is not None:
        layout = "parallel"
    elif layout == "auto":
        all_audio = all(p.suffix.lower().lstrip(".") in AUDIO_EXTS for p in files)
        layout = "parallel" if len(files) > 1 and all_audio else "sequential"
    if layout == "parallel" and len(files) < 2:
        layout = "sequential"

    plan = track_envs = None
    reports: list = []
    if layout == "parallel":
        reports = [dual_channel_report(p, use_cache_dir, args.dual_channel) for p in files]
        planned = plan_parallel(files, reports, use_cache_dir, manual_offsets)
        if planned is None:
            return 1
        plan, track_envs, order = planned
        sorted_files = files
        files = [files[i] for i in order]
        reports = [reports[i] for i in order]
        shaky = [files[i].name for i, a in enumerate(plan.alignments) if not a.confident]
        if shaky and args.layout == "auto":
            print(f"音频对不齐（{'、'.join(shaky)}），不像同时录的，退回首尾接续（sequential）")
            layout, plan, track_envs, files = "sequential", None, None, sorted_files
        elif shaky:
            print(f"Error: 这些文件和第一个文件对不齐：{'、'.join(shaky)}。"
                  "确认它们是同一场录音吗？是的话用 --offsets 手动给起始偏移")
            return 1
        else:
            sort_label = "音频互相关对齐（最早开录的在前）"

    if layout == "parallel":
        print(f"共 {len(files)} 个文件，布局：并行（同时录制的多台设备），排序依据：{sort_label}")
        print_plan(files, plan)
        offsets = [a.offset_ms for a in plan.alignments]
        cursor = plan.total_ms
    else:
        print(f"共 {len(files)} 个文件，布局：首尾接续，排序依据：{sort_label}")
        offsets, cursor = [], 0.0
        for i, path in enumerate(files, 1):
            duration = media_duration(path)
            offsets.append(cursor)
            print(f"  {i:>2}. [{format_timestamp(cursor)}] {path.name}  ({duration / 60:.1f}min)")
            cursor += duration * 1000
        print(f"合计时长：{cursor / 60000:.1f}min")
    print(f"→ {output}")
    why = "显式指定" if args.engine_explicit else ("要声纹分离" if args.speakers else "无需声纹分离")
    print(f"ASR 引擎：{args.engine}（{why}）　设备：{args.device}")

    stages.done("发现与排序")

    if args.dry_run:
        return 0

    output.parent.mkdir(parents=True, exist_ok=True)
    # handoff 任务和 ASR 缓存一样，跟着产物走，不落在当前工作目录
    set_handoff_root(output.parent / ".subtitle_tasks")

    router = ASRRouter(args, cache_dir, cache_path, resolve_engine, load_model)
    # 抽出来的 wav 是纯过程物（76 分钟访谈 ≈ 37MB），转写完立刻作废，
    # 没有任何回看价值，所以扔系统临时目录，不脏项目目录
    tmp_dir = Path(tempfile.mkdtemp(prefix="subtitle-audio-"))
    all_cues: list[dict] = []
    # 每条声道自己的一份（含被判为串音的），用来出 .spk1/.spk2 字幕
    track_cues: dict[int, list[dict]] = {}

    pause_ms = max(0.0, args.pause_split * 1000.0)
    dual_files = 0
    parallel_tracks = 0
    envelopes: dict[int, list[int]] | None = None
    # cam++ 聚出来的簇统计（含被判为旁人的），写进 manifest 备查
    bystander_stats: list[dict] = []

    try:
        if layout == "parallel":
            result = transcribe_parallel(files, plan, track_envs, args, cache_dir, tmp_dir, router)
            if result is None:
                return 1
            sentences, everything, envelopes = result
            parallel_tracks = len(envelopes)
            TRANSCRIPT_RAW.extend(dict(x) for x in sentences)
            for item in sentences:
                all_cues.extend(sentence_to_cues(item, args.max_chars, 0.0, True, pause_ms))
            for item in everything:
                marked = sentence_to_cues(item, args.max_chars, 0.0, True, pause_ms)
                for cue in marked:
                    cue["bleed"] = bool(item.get("_bleed") or item.get("_drop"))
                track_cues.setdefault(int(item.get("spk") or 0), []).extend(marked)
            track_envs = None                           # 4 轨 × 80 分钟的包络用完就放
        for i, (path, offset) in enumerate(zip(files, offsets), 1):
            if layout == "parallel":
                break
            report = dual_channel_report(path, None if args.no_cache else cache_dir, args.dual_channel)
            dual = bool(report and report.dual)
            if report:
                print(f"[{i}/{len(files)}] 声道分析：{report.summary()}")
            dual_files += 1 if dual else 0
            # 双人双轨：左右声道各转写一遍，声道就是说话人标签（比声纹聚类可靠）
            tracks = [(0, "L"), (1, "R")] if dual else [(None, "")]
            gate = dual and decide_gate(report.separation, args.gate)

            sentences: list[dict] = []
            stereo = None
            gated = None
            for speaker, tag in tracks:
                cached = None
                # 门限参数进 key：改了分离参数就是另一份音频，不能拿旧结果糊弄
                track_key = ((f"{tag}@m{dual_channel.KEEP_MARGIN_DB}h{dual_channel.HOLD_MS}"
                              if gate else f"{tag}@nogate") if dual else "")
                if diarize:
                    track_key += f"@n{args.speaker_count or 'auto'}"
                wav = tmp_dir / f"{hashlib.sha1(str(path).encode()).hexdigest()[:12]}{tag}.wav"
                def make_wav():
                    nonlocal stereo, gated
                    if wav.exists():
                        return wav
                    if dual:
                        if gated is None:
                            stereo = dual_channel.read_stereo(path)
                            if stereo is None:
                                raise RuntimeError(f"音频读取失败: {path.name}")
                            gated = dual_channel.separate(stereo) if gate else [stereo[0], stereo[1]]
                        dual_channel.write_mono_wav(wav, gated[speaker])
                    elif not extract_audio(path, wav):
                        raise RuntimeError(f"音频抽取失败: {path.name}")
                    return wav
                cached = router.transcribe(path, track_key, make_wav, diarize=diarize and not dual)
                wav.unlink(missing_ok=True)
                if dual:
                    for item in cached:
                        item["spk"] = speaker           # 声道号即说话人号
                sentences.extend(cached)
            stereo = gated = None                       # 20 分钟视频约 250MB，早点还给系统

            # 声纹认人：先把「路过说两句的人」从主讲人里挑出来，否则背景里一句咳嗽
            # 就能和主讲人凑成一条两行抢话字幕（见 demote_bystanders）
            if diarize and not dual and sentences:
                sentences, stats = demote_bystanders(sentences, args.speaker_count)
                if stats:
                    bystander_stats.append({"file": path.name, "clusters": stats})
                    dropped_secs = sum(x["seconds"] for x in stats if not x["kept"])
                    kept_n = sum(1 for x in stats if x["kept"])
                    extra = (f"，旁人 {len(stats) - kept_n} 簇（共 {dropped_secs:.1f} 秒）不参与说话人标注"
                             if kept_n < len(stats) else "")
                    print(f"  声纹筛选：主讲人 {kept_n} 人（{format_speaker_stats(stats)}）{extra}")

            everything = list(sentences)          # 分轨字幕要连被丢掉的一起留档
            if dual and sentences:
                sentences, bled = dual_channel.mark_bleed_sentences(sentences, report)
                sentences, deduped = dual_channel.dedupe_cross_channel(sentences, report)
                if bled or deduped:
                    print(f"  串音清理：整句都是对方在说的丢掉 {bled} 句，"
                          f"两轨重复的丢掉 {deduped} 句")
            if not sentences:
                print(f"  Warning: 未识别到语音: {path.name}")
                continue

            for item in sentences:
                all_cues.extend(sentence_to_cues(item, args.max_chars, offset, diarize or dual, pause_ms))
                TRANSCRIPT_RAW.append(multi_track.shift_sentence(
                    dict(item), multi_track.Alignment(offset_ms=float(offset))))
            if dual:
                for item in everything:
                    marked = sentence_to_cues(item, args.max_chars, offset, True, pause_ms)
                    for cue in marked:
                        cue["bleed"] = bool(item.get("_bleed") or item.get("_drop"))
                    track_cues.setdefault(int(item.get("spk") or 0), []).extend(marked)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        router.close()

    try:
        if cache_dir.exists() and not any(cache_dir.iterdir()):
            cache_dir.rmdir()
    except OSError:
        pass

    if not all_cues:
        print("Error: 没有识别到任何语音内容")
        return 1

    stages.done("抽音频+ASR")

    # 声道分轨、多设备分轨和 cam++ 一样会给出 speaker，往后所有「按说话人」的处理都看这个
    track_mode = dual_files > 0 or parallel_tracks > 1
    has_speakers = diarize or track_mode
    # 两人同时说话合成两行时每行都标「spkN: 」——不标的话观众不知道哪行是谁
    merge_label = True

    all_cues = normalize_timeline(all_cues, args.min_duration, args.max_duration)

    if track_cues:
        write_track_subtitles(output, track_cues, args.format, args.keep_trailing_punct,
                              args.min_duration, args.max_duration)

    agent_name = None if args.no_llm else check_agent_available()
    use_llm = agent_name is not None
    level_label = "纯纠错（保留全部语气词）" if args.polish_level == "minimal" else "纠错 + 去水词"

    # 跨轨互校要排在纠错前面：纠错是拿整份出题的，先把重复的删干净，
    # 那道题才不会带着一堆串音复述去问（也免得下一轮输入变了、题白出）
    if use_llm and track_mode:
        reconciled = reconcile_cross_track(all_cues, args.context, merged_glossary(args.glossary))
        if reconciled is None:
            print(pending_report(resume_command_hint(output)))
            return EXIT_HANDOFF_PENDING
        all_cues, dup_dropped, dup_fixed = reconciled
        print(f"跨轨互校：删掉 {dup_dropped} 条串音复述，修正 {dup_fixed} 条")
        stages.done("跨轨互校")

    # 现在才写 raw：它的用途是**和正片逐条对齐做 diff**，看纠错动了哪些字。
    # 所以基线必须取在跨轨互校**之后**——互校会删掉重复条目，raw 写在它前面两边条数就对不上，
    # validate_subtitle 会把正常的互校删除报成「结构性损伤」。
    # 互校删了什么另有去处：.spk1/.spk2 里带【串音】标记全都在，任务目录里还留着判断依据。
    raw_path = output.with_name(f"{output.stem}.raw{output.suffix}")
    raw_count = len(all_cues)
    raw_cues = [
        {**cue, "text": finalize_text(cue["text"], args.keep_trailing_punct, cue.get("language", "auto"))}
        for cue in all_cues
    ]
    raw_joined, _ = repair_term_splits([c for c in raw_cues if c["text"]], args.max_chars)
    raw_render, _ = enforce_line_width(raw_joined, args.max_chars)
    if has_speakers:
        raw_render = merge_speaker_overlaps(raw_render, merge_label, args.max_duration)
    raw_path.write_text(
        render_subtitle(raw_render, args.format, args.speaker_labels),
        encoding="utf-8",
    )
    print(f"原语言 ASR 字幕已保留（作为纠错的 diff 基线）：{raw_path}")

    if use_llm:
        print(f"✅ handoff 模式：LLM 部分交给当前会话现场处理　级别：{level_label}")
    else:
        print(f"⚠️  模型任务已关闭（--no-llm 或 SUBTITLE_LLM=0）　级别：{level_label}")

    # 规则去重排在纠错**前面**（但在 raw 之后，raw 要保持 ASR 原文）：
    # 这样 agent 在纠错的同一轮里就能复核去重结果，把误伤改回来，不额外花一轮。
    dedupe_edits = apply_dedupe(all_cues)
    if dedupe_edits:
        print(f"连续重复字/词组去重：{len(dedupe_edits)} 条"
              f"（{'交给 agent 在纠错同一轮里复核误伤' if use_llm else '⚠️ 无 agent，没人复核误伤，务必人工过一遍对照清单'}）")

    # 年份换阿拉伯数字，同样排在纠错前面：agent 在同一轮里能看见换过的文字，
    # 换错的（把「三四年」这种约数当年份）顺手改回去。
    year_edits = apply_year_digits(all_cues)
    if year_edits:
        # 改动少而且一眼能判对错，不另出清单，直接把换掉的写法打出来
        shown = "、".join(dict.fromkeys(
            f"{cn}年→{''.join(CN_DIGIT_MAP[c] for c in cn)}年"
            for _, before, _ in year_edits for cn in YEAR_CN_RE.findall(before)
        ))
        print(f"年份换阿拉伯数字：{len(year_edits)} 条（{shown}）"
              f"{'' if use_llm else '　⚠️ 无 agent，务必人工扫一眼有没有把时长当年份'}")

    pre_polish = [cue["text"] for cue in all_cues]

    all_cues, dropped, uncertain = polish_cues(
        all_cues, use_llm, args.context, merged_glossary(args.glossary),
        int(os.environ.get("SUBTITLE_BATCH_CUES", "100")),
        int(os.environ.get("SUBTITLE_BATCH_CHARS", "4000")),
        mode=args.polish_mode,
        level=args.polish_level,
        speaker_source="channel" if track_mode else "voiceprint",
        dedupe_edits=dedupe_edits,
    )
    stages.done("agent 纠错")

    if pending_handoff():
        # 题出完了就停：这一轮手里只有没纠过错的文字，写出去会覆盖掉上一轮的成果。
        # ASR 结果已经缓存，续跑不会重转写。
        # 去重清单也留到下一轮再写：这一轮 agent 还没复核，写出来每条都是「保留」，看着像结论。
        print(pending_report(resume_command_hint(output)))
        return EXIT_HANDOFF_PENDING

    # 去重对照清单：每条「去重前 → 去重后」，并标出 agent 复核时回滚了哪些。
    # 规则永远可能误伤（汉语里合法的重复太多），所以最终把关的是人——这份清单就是给人扫的。
    if dedupe_edits:
        aligned = all_cues if len(all_cues) == len(pre_polish) else None
        dedupe_path, rolled_back = write_dedupe_report(output, dedupe_edits, aligned)
        print(f"去重复核：agent 回滚 {rolled_back} 条疑似误伤　对照清单：{dedupe_path}")

    for cue in all_cues:
        # 句末标点马上就要被删掉，先把「这条是不是一句话说完了」记下来——
        # 接缝修复要靠它区分「被劈开的半句」和「说完了的下一句」
        cue["_sentence_end"] = cue["text"].rstrip()[-1:] in "。！？!?….؟۔।॥"
        cue["text"] = finalize_text(cue["text"], args.keep_trailing_punct, cue.get("language", "auto"))
    before_finalize = len(all_cues)
    all_cues = [cue for cue in all_cues if cue["text"]]
    dropped += before_finalize - len(all_cues)

    # 超宽兜底 + 抢话合并都放在最后：前面每一步都按「一条 = 一个人的一句话」处理，
    # 这里只影响最终成品的呈现（拆条 / 两行），不影响润色和存疑清单的粒度
    # （存疑清单要拿原文回锚到 all_cues，所以拆条只作用在渲染副本上）
    repaired, rejoined = repair_term_splits(all_cues, args.max_chars)
    if rejoined:
        print(f"术语/词语被切在两条字幕里，已并回重切：{rejoined} 处")
    wrapped, rewrapped = enforce_line_width(repaired, args.max_chars)
    if rewrapped:
        print(f"单行超宽（>{args.max_chars:g} 字宽）拆成前后两条：{rewrapped} 条")
    render_cues = merge_speaker_overlaps(wrapped, merge_label, args.max_duration) if has_speakers else wrapped
    merged_lines = sum(1 for c in render_cues if "\n" in c["text"])
    output.write_text(render_subtitle(render_cues, args.format, args.speaker_labels), encoding="utf-8")
    if has_speakers:
        write_speaker_subtitles(output, wrapped, args.format)

    sources = []
    for k, (path, offset) in enumerate(zip(files, offsets)):
        entry = {"file": str(path), "offset_ms": round(offset), "offset": format_timestamp(offset)}
        if plan is not None:
            a = plan.alignments[k]
            entry.update({"speed": a.speed, "align_ncc": round(a.ncc, 3),
                          "tracks": [{"channel": t.channel, "speaker": speaker_name(t.speaker),
                                      "merged_into": speaker_name(t.merged_into) if t.merged_into is not None else None}
                                     for t in plan.tracks if t.file_idx == k]})
        sources.append(entry)
    manifest = output.with_suffix(".manifest.json")
    manifest.write_text(
        json.dumps({
            "output": str(output),
            "audio_language": args.language,
            "translate_to": args.translate_to,
            "asr": router.records,
            "layout": layout,
            "sort_by": sort_label,
            "total_ms": cursor,
            "cues": len(all_cues),
            "speakers": sorted({c["speaker"] for c in all_cues if c.get("speaker")}),
            "speaker_clusters": bystander_stats,
            "sources": sources,
            "notes": plan.notes if plan is not None else [],
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    if uncertain:
        # 时间码是 agent 手抄的，先按原文回锚到真实 cue（实测 24 条能错 7 条）
        fixed = anchor_uncertain(uncertain, all_cues)
        if fixed:
            print(f"存疑清单：{fixed}/{len(uncertain)} 条时间码与原文对不上，已按原文回锚修正")
        write_uncertain(output, uncertain)
    else:
        # 这一轮没有存疑了（人已经逐条确认完、或者换了参数重跑），上一轮留下的清单必须删掉。
        # 留着比没有更糟：那是一份**已经作废**的待办，下次打开还以为有 N 处没确认
        stale = output.with_name(f"{output.stem}.uncertain{output.suffix}")
        if stale.is_file():
            stale.unlink()
            print(f"存疑已清零，删除上一轮留下的清单：{stale.name}")

    print(f"Done! {raw_count} 条 → {len(render_cues)} 条"
          f"（删除纯语气词 {dropped} 条，超宽拆条 {rewrapped} 条）")
    # 单行宽度是硬上限，别只当成一句承诺——数一遍再报
    # 「spkN: 」前缀不计入字宽预算（它只出现在两人同时说话的两行上）
    lines = [SPEAKER_PREFIX_RE.sub("", line) for cue in render_cues for line in cue["text"].split("\n")]
    widest = max((display_width(line) for line in lines), default=0.0)
    over = sum(1 for line in lines if display_width(line) > args.max_chars)
    print(f"单行字宽（上限 {args.max_chars:g}，1 = 一个中文、英文数字算 0.5，不含 spkN: 前缀）："
          f"最宽 {widest:g}，超限 {over} 行")
    if has_speakers:
        speakers = sorted({c["speaker"] for c in all_cues if c.get("speaker")})
        source = (f"多设备并行分轨（{len(files)} 个文件 → {parallel_tracks} 轨）" if parallel_tracks
                  else f"左右声道分轨（{dual_files}/{len(files)} 个文件）" if dual_files else "cam++ 声纹")
        print(f"说话人分离（{source}）：识别出 {len(speakers)} 人（{'、'.join(speakers) or '无'}），"
              f"两人同时说话合成两行字幕 {merged_lines} 条（每行带 spkN: 前缀）")
        demoted = sum(1 for f in bystander_stats for x in f["clusters"] if not x["kept"])
        if demoted:
            print(f"  其中 {demoted} 个簇判为旁人：原文照留在字幕里，但不给说话人标注、"
                  f"不参与抢话合并（明细见 manifest 的 speaker_clusters）")
    stages.done("写盘")
    print(f"字幕：{output}")
    print(f"清单：{manifest}")
    write_transcript_json(output, wrapped, files, offsets, layout)
    stages.report()

    if uncertain and (args.with_highlights or args.with_review or args.translate_to):
        print(f"\n⏸  有 {len(uncertain)} 处存疑需要你先确认（见上面的 .uncertain.srt/.vtt），"
              "确认并改完字幕后再以 .srt/.vtt 为输入单独翻译，或跑 make_review.py / make_highlights.py，"
              "这样它们读到的才是正确文本。")
        return 2 if args.translate_to else 0

    if args.translate_to:
        code = translate_file(output, args.translate_to)
        if code:
            return code

    if args.with_review or args.with_highlights:
        if run_downstream(output, args.context, args.with_review, args.with_highlights):
            print("\n⏸  下游任务（审查 / 花字）也走了 handoff：按上面各自打印的清单做完题，"
                  "再重跑对应的 make_review.py / make_highlights.py 命令")
            return EXIT_HANDOFF_PENDING

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"Error: {exc}")
        raise SystemExit(1)
