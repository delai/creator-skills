#!/usr/bin/env python3
"""左右声道 = 两个人时的双轨处理。

对谈类素材（DJI Mic 之类的双发射器）里，左右声道往往是**两个人各自的领夹麦**：
每个声道以自己那个人为主，同时以 -6~-12 dB 混进对方的声音（串音）。
这种素材下声道本身就是最可靠的说话人标签，比 cam++ 的声纹聚类准得多。

四步：
1. `analyze` —— 判断这个文件的左右声道是不是两个人（而不是立体声/双单声道）
2. `separate` —— 按「谁在这一帧更响」把另一条轨上的串音**静音**掉，
   让 ASR 在每条轨上只听见一个人
3. `mark_bleed_sentences` —— 整句都是对方在说的，按能量标出来丢掉
4. `crosstalk_clusters` + `dedupe_cross_channel` —— 两轨都识别出同一句话时的去重：
   机械那道按「时间重叠 + 字面相似」，漏掉的交给 agent 按语义判（见 generate_subtitle 的跨轨互校）
"""

from __future__ import annotations

import json
import re
import subprocess
import wave
from dataclasses import asdict, dataclass, field, fields
from difflib import SequenceMatcher
from pathlib import Path

SAMPLE_RATE = 16000
FRAME_MS = 20                     # 分析帧长
ENVELOPE_MS = 100                 # 存进缓存的包络分辨率（够做跨轨去重了）
FLOOR_DB = -90.0

# 判定阈值
DOMINANCE_DB = 6.0                # 一侧比另一侧响这么多才算「这一侧在说话」
MIN_SIDE_SHARE = 0.05             # 弱势的一方至少要占到有效语音的这个比例
MIN_SIDE_SECONDS = 5.0            # 且绝对时长不能太短（防止把偶发串音当成第二个人）
MAX_CORRELATION = 0.5             # 波形相关性高于此值 = 同一支麦的立体声/双单声道

# 分离参数
# 本轨只要不比对方低 2 dB 以上就保留。**这个负号很重要**：设成 0 时两条 mask 严格互补，
# 两人音量接近的段落（开场寒暄、同时说话）掩码会来回抖，把**两条轨都切碎**——
# 实测丢了 14 秒双方都在说的内容。留 2 dB 的重叠带，串音（低 6~12 dB）照样被静音。
KEEP_MARGIN_DB = -2.0
HOLD_MS = 120                     # 判定为「在说」后向前后各延伸，避免把词头词尾切掉

# 门限分离只在「每条轨确实各收各的人」时才成立，所以先量一遍轨间分离度再决定开不开。
# 领夹麦这类近讲素材通常在 15 dB 以上，门限压得干净；
# 几台设备摆在同一张桌子上互相串音的，实测轨间差中位数只有 2.9 dB、36% 的帧差不到 2 dB，
# 这时门限在 turn 边界必然削掉词头（实测「只不过」被削成「不过」），
# 而放宽 HOLD_MS 只会把串音放回来（实测 300ms 时「主播」那个错字原样重现）。
# 关键在于两种错误的代价不对称：门限是在 ASR 之前就把波形乘 0，判错了音频就没了；
# 句级能量判归属是非破坏性的，判错了原文还带着【串音】标记躺在分轨字幕里，人能捞回来。
MIN_SEPARATION_DB = 6.0


@dataclass
class DualReport:
    """一个文件的左右声道分析结论。"""

    channels: int = 0
    dual: bool = False
    corr: float = 0.0
    left_share: float = 0.0
    right_share: float = 0.0
    active_seconds: float = 0.0
    separation: float = 0.0           # 轨间分离度 dB，决定要不要做 separate()（见 MIN_SEPARATION_DB）
    reason: str = ""
    left_db: list[int] = field(default_factory=list)    # 每 ENVELOPE_MS 一个值，用于跨轨去重
    right_db: list[int] = field(default_factory=list)

    def summary(self) -> str:
        if self.channels < 2:
            return f"单声道（{self.channels} ch）"
        sep = "" if self.channels < 2 else f"轨间分离 {self.separation:.1f}dB  "
        return (f"{self.channels}ch  相关性 {self.corr:.2f}  "
                f"左主导 {self.left_share:.0%} / 右主导 {self.right_share:.0%}  "
                f"{sep}有效语音 {self.active_seconds / 60:.1f} 分 → {self.reason}")

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> "DualReport":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in json.loads(raw).items() if k in known})


# ---------------------------------------------------------------- 读音频


def channel_count(path: Path) -> int:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=channels", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            return 0
        streams = json.loads(result.stdout).get("streams") or []
        return int(streams[0].get("channels") or 0) if streams else 0
    except Exception:  # noqa: BLE001 - 探测失败按「不是双人双轨」处理
        return 0


def read_stereo(path: Path, seconds: float | None = None):
    """解成 16k 双声道 float32，形状 (2, N)。

    `seconds` 只解开头这么长——声纹探测取样用（整片解一遍 80 分钟素材要 20 秒以上），
    正式分析不传，判定要看全片。
    """
    import numpy as np

    cmd = ["ffmpeg", "-v", "error", "-i", str(path), "-vn", "-ac", "2",
           "-ar", str(SAMPLE_RATE)]
    if seconds:
        cmd += ["-t", f"{seconds:g}"]
    cmd += ["-f", "s16le", "-acodec", "pcm_s16le", "-"]
    result = subprocess.run(cmd, capture_output=True, timeout=3600)
    if result.returncode != 0 or not result.stdout:
        return None
    data = np.frombuffer(result.stdout, dtype="<i2").astype("float32") / 32768.0
    usable = (len(data) // 2) * 2
    return data[:usable].reshape(-1, 2).T.copy()


def write_mono_wav(path: Path, samples) -> None:
    import numpy as np

    pcm = np.clip(samples, -1.0, 1.0)
    pcm = (pcm * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(SAMPLE_RATE)
        fh.writeframes(pcm.tobytes())


# ---------------------------------------------------------------- 分析


def _frame_power(x, frame: int):
    import numpy as np

    count = len(x) // frame
    if count == 0:
        return np.zeros(0, dtype="float32")
    return (x[: count * frame].reshape(count, frame) ** 2).mean(axis=1)


def _to_db(power):
    import numpy as np

    return np.maximum(10.0 * np.log10(power + 1e-12), FLOOR_DB)


def _moving_average(x, k: int):
    import numpy as np

    if k <= 1 or len(x) < k:
        return x
    return np.convolve(x, np.ones(k, dtype="float32") / k, mode="same")


def separation_db(envs, frame_ms: float = FRAME_MS) -> float:
    """轨间分离度：有效帧上「本轨 − 最响的别轨」绝对值的中位数（dB）。

    `envs` 是若干条**已经放到同一条时间轴上**的 dB 包络（长度可以不等，按最短截齐）。
    只统计「本轨在说话、且别的轨这段确实有录音」的帧——不加后一个条件，
    文件长度不同留下的空白（FLOOR_DB）会被算成 60 dB 的完美分离。

    测不出来（只有一条轨、没有有效帧）时返回 inf，也就是维持原来的「开门限」行为。
    """
    import numpy as np

    if len(envs) < 2:
        return float("inf")
    length = min(len(e) for e in envs)
    if length == 0:
        return float("inf")
    smooth = max(1, int(100 / frame_ms))            # 100ms 平滑，跟门限用的一致
    stacked = np.stack([_moving_average(np.asarray(e[:length], dtype="float32"), smooth)
                        for e in envs])
    floor = float(np.percentile(stacked.max(axis=0), 20))
    diffs = []
    for k in range(len(stacked)):
        others = np.delete(stacked, k, axis=0)
        best_other = others.max(axis=0)
        active = ((stacked[k] > floor + 8.0) & (stacked[k] > -55.0)
                  & (best_other > FLOOR_DB + 10.0))
        if active.any():
            diffs.append(np.abs(stacked[k] - best_other)[active])
    if not diffs:
        return float("inf")
    return float(np.median(np.concatenate(diffs)))


def stereo_separation_db(stereo) -> float:
    """双声道素材的分离度，判要不要对这个文件做 `separate()`。"""
    frame = int(SAMPLE_RATE * FRAME_MS / 1000)
    return separation_db([_to_db(_frame_power(stereo[c], frame)) for c in range(2)], FRAME_MS)


def analyze(stereo, channels: int) -> DualReport:
    """判断左右声道是不是两个人各自的麦克风。

    判据三条同时成立：
    - 波形相关性低（同一支麦的立体声、或双单声道，相关性都接近 1）
    - 两侧**都**有足够多「明显比对方响」的语音帧（各自的近讲段）
    - 弱势一方的绝对时长不能太短，避免把偶发串音/单边噪声当成第二个人
    """
    import numpy as np

    report = DualReport(channels=channels)
    if channels < 2 or stereo is None or stereo.shape[1] < SAMPLE_RATE:
        report.reason = "声道数不足或音频过短"
        return report

    left, right = stereo[0], stereo[1]
    denom = float(np.sqrt((left ** 2).sum() * (right ** 2).sum()))
    report.corr = float((left * right).sum() / denom) if denom > 0 else 1.0

    frame = int(SAMPLE_RATE * FRAME_MS / 1000)
    ldb, rdb = _to_db(_frame_power(left, frame)), _to_db(_frame_power(right, frame))
    loud = np.maximum(ldb, rdb)
    # 噪声底 = 较安静的那 20% 帧；比它高 8 dB 才算有人在说话
    floor = float(np.percentile(loud, 20))
    active = (loud > floor + 8.0) & (loud > -55.0)
    active_count = int(active.sum())
    report.active_seconds = active_count * FRAME_MS / 1000.0

    # 存一份粗包络给跨轨去重用（每 ENVELOPE_MS 一个值）
    step = ENVELOPE_MS // FRAME_MS
    report.left_db = [int(round(v)) for v in _pool(ldb, step)]
    report.right_db = [int(round(v)) for v in _pool(rdb, step)]

    if active_count == 0:
        report.reason = "没有检测到有效语音"
        return report

    diff = ldb[active] - rdb[active]
    report.left_share = float((diff >= DOMINANCE_DB).mean())
    report.right_share = float((diff <= -DOMINANCE_DB).mean())
    # 分离度不管判没判成双人双轨都要量：`--dual-channel on` 会绕过下面的判定强制分轨，
    # 那时也得知道该不该做 separate()。inf 存不进 JSON（非标准），
    # 钳到 99——反正远超 MIN_SEPARATION_DB，判断结果一样
    report.separation = min(separation_db([ldb, rdb], FRAME_MS), 99.0)
    min_share = max(MIN_SIDE_SHARE, MIN_SIDE_SECONDS / max(report.active_seconds, 1e-6))

    if report.corr > MAX_CORRELATION:
        report.reason = f"两声道高度相关（{report.corr:.2f}），判为立体声/双单声道"
    elif min(report.left_share, report.right_share) < min_share:
        report.reason = (f"只有一侧有主导语音（左 {report.left_share:.0%} / 右 {report.right_share:.0%}），"
                         "判为单人或非双人双轨")
    else:
        report.dual = True
        report.reason = "左右声道是两个人各自的麦克风"
    return report


def _pool(db, step: int):
    import numpy as np

    if step <= 1:
        return db
    count = len(db) // step
    if count == 0:
        return np.zeros(0, dtype="float32")
    return db[: count * step].reshape(count, step).max(axis=1)


# ---------------------------------------------------------------- 分离


def _dilate(mask, k: int):
    if k <= 0:
        return mask
    out = mask.copy()
    for shift in range(1, k + 1):
        out[shift:] |= mask[:-shift]
        out[:-shift] |= mask[shift:]
    return out


def separate(stereo, keep_margin_db: float = KEEP_MARGIN_DB, hold_ms: float = HOLD_MS):
    """按「谁更响」把串音**静音**掉，返回两条只剩各自说话人的单声道。

    这里必须是硬静音，不能只是衰减：ASR 前端做 CMVN，输入整体调小 25 dB 对它几乎没影响，
    照样能把串音里的每个字识别出来（实测衰减 25 dB 的那一轨依然完整转出了对方的整段独白）。
    只有真的置零，VAD 才会跳过这段，那一轨才真的「听不见」对方。
    """
    import numpy as np

    frame = int(SAMPLE_RATE * FRAME_MS / 1000)
    total = stereo.shape[1]
    smooth = max(1, int(100 / FRAME_MS))                # 100ms 平滑，避免音节间抖动
    hold = max(1, int(hold_ms / FRAME_MS))

    powers = [_moving_average(_frame_power(stereo[c], frame), smooth) for c in range(2)]
    dbs = [_to_db(p) for p in powers]
    diff = dbs[0] - dbs[1]

    outputs = []
    for c, own_diff in enumerate((diff, -diff)):
        keep = _dilate(own_diff > keep_margin_db, hold)
        gain = np.where(keep, 1.0, 0.0).astype("float32")
        gain = _moving_average(gain, 3)                  # 3 帧渐变，避免爆音
        expanded = np.repeat(gain, frame)
        if len(expanded) < total:                        # 末尾不足一帧的尾巴按最后一帧的增益走
            pad = np.full(total - len(expanded), expanded[-1] if len(expanded) else 1.0, dtype="float32")
            expanded = np.concatenate([expanded, pad])
        outputs.append(stereo[c] * expanded[:total])
    return outputs


# ---------------------------------------------------------------- 跨轨去重


PUNCT_RE = re.compile(r"[\s，。！？；：、,.!?;:…—～~“”\"'‘’()（）《》]+")


def _norm(text: str) -> str:
    return PUNCT_RE.sub("", str(text or ""))


def _mean_db(values: list[int], start_ms: float, end_ms: float) -> float:
    lo = max(0, int(start_ms // ENVELOPE_MS))
    hi = min(len(values), int(end_ms // ENVELOPE_MS) + 1)
    if hi <= lo:
        return FLOOR_DB
    span = values[lo:hi]
    return sum(span) / len(span)


BLEED_MARGIN_DB = 0.0


def _percentile(values: list[float], ratio: float) -> float:
    if not values:
        return -999.0
    ordered = sorted(values)
    return float(ordered[min(len(ordered) - 1, int(len(ordered) * ratio))])


def envelopes_of(source) -> dict[int, list[int]]:
    """统一成「轨号 → 每 ENVELOPE_MS 一个 dB」。单文件双声道传 DualReport，多文件并行传 dict。"""
    if isinstance(source, DualReport):
        return {0: source.left_db, 1: source.right_db}
    return source


def _own_vs_others(envelopes: dict[int, list[int]], spk: int,
                   start_ms: float, end_ms: float) -> list[float]:
    """一句话范围内逐帧的「本轨 − 最响的别轨」。"""
    own = envelopes.get(spk) or []
    others = [v for k, v in envelopes.items() if k != spk and v]
    lo = max(0, int(start_ms // ENVELOPE_MS))
    hi = min([len(own)] + [len(v) for v in others] + [int(end_ms // ENVELOPE_MS) + 1])
    if not others or hi <= lo:
        return []
    return [own[i] - max(v[i] for v in others) for i in range(lo, hi)]


def mark_bleed_sentences(sentences: list[dict], source,
                         margin_db: float = BLEED_MARGIN_DB) -> tuple[list[dict], int]:
    """丢掉「整句都是别人在说」的句子——门限没压干净漏过来的串音。

    判据是能量而不是文字：这类句子被 ASR 硬解出来往往是一团糊
    （「对对对对对，嗯嗯，好，嗯嗯，分不清楚」），文字相似度根本认不出它是谁的复述，
    但能量骗不了人——整句里本轨有 3/4 的时间都比最响的别轨轻，那就不是这个人在说话。
    取 p75 而不是均值：对方长篇大论时自己插一句「嗯」也要留住，
    只要句子里有四分之一以上的时间本轨占优就算数。

    丢掉的不是真的删掉，而是打上 `_bleed` 标记：分轨字幕里要把它们带 `【串音】` 前缀显示出来，
    机器判错了人才看得见（判成串音丢掉又不留痕，就成了永远发现不了的黑洞）。
    两轨（DualReport）和 N 轨（轨号 → 包络的 dict）都走这一个函数。
    """
    envelopes = envelopes_of(source)
    kept, dropped = [], 0
    for item in sentences:
        diff = _own_vs_others(envelopes, int(item.get("spk") or 0),
                              float(item["start"]), float(item["end"]))
        if diff and _percentile(diff, 0.75) < margin_db:
            item["_bleed"] = True
            dropped += 1
            continue
        kept.append(item)
    return kept, dropped


def crosstalk_clusters(cues: list[dict], gap_ms: float = 800.0,
                       max_span_ms: float = 40000.0) -> list[list[int]]:
    """把「两条声道在同一段时间里都有话」的区段圈出来，交给 agent 逐段判。

    为什么按**区段**而不是按**条对条**：两轨的断句边界根本对不齐——
    实测同一段话 L 轨断成一句 26 秒、R 轨断成一句 10 秒，条对条的字面相似度算下来
    连 0.55 都不到，纯机械去重必漏。把整段连同上下文一起给 agent，它才判得出
    「这是同一句话被两个麦都收到了」还是「两个人真的在各说各的」。

    连成一段的条件：与已在段里的条目时间重叠、或间隔不超过 `gap_ms`；
    段里必须两个说话人都有条目才算数（单人独白不需要判）。
    """
    order = sorted(range(len(cues)), key=lambda i: (cues[i]["start"], cues[i]["end"]))
    clusters: list[list[int]] = []
    current: list[int] = []
    span_end = 0.0
    for idx in order:
        cue = cues[idx]
        if current and cue["start"] - span_end > gap_ms:
            clusters.append(current)
            current, span_end = [], 0.0
        if current and cue["end"] - cues[current[0]]["start"] > max_span_ms:
            clusters.append(current)
            current, span_end = [], 0.0
        current.append(idx)
        span_end = max(span_end, cue["end"])
    if current:
        clusters.append(current)
    return [c for c in clusters
            if len({cues[i].get("speaker") for i in c}) > 1]


def dedupe_cross_channel(sentences: list[dict], source,
                         similarity: float = 0.55) -> tuple[list[dict], int]:
    """两条轨都识别出同一句话时，只留能量更高的那一侧（N 轨时逐对比较）。

    门限已经把大部分串音压掉了，这里是兜底：说话人音量接近、或麦克风摆位偏时，
    弱侧仍可能残留一句听得清的复述。判据是「时间重叠 + 文字高度相似」，
    只有这两条同时成立才会删——单纯时间重叠是真抢话，不能删。
    """
    envelopes = envelopes_of(source)
    kept = sorted(sentences, key=lambda s: float(s.get("start") or 0))
    dropped = 0
    for i, item in enumerate(kept):
        if item.get("_drop"):
            continue
        for other in kept[i + 1:]:
            if other.get("_drop") or other.get("spk") == item.get("spk"):
                continue
            start = max(float(item["start"]), float(other["start"]))
            end = min(float(item["end"]), float(other["end"]))
            if end <= start:
                if float(other["start"]) > float(item["end"]):
                    break
                continue
            shorter = min(float(item["end"]) - float(item["start"]),
                          float(other["end"]) - float(other["start"]))
            if shorter <= 0 or (end - start) / shorter < 0.5:
                continue
            a, b = _norm(item.get("text")), _norm(other.get("text"))
            if not a or not b or SequenceMatcher(None, a, b).ratio() < similarity:
                continue
            scores = {}
            for cue in (item, other):
                track = envelopes.get(int(cue.get("spk") or 0)) or []
                scores[id(cue)] = _mean_db(track, float(cue["start"]), float(cue["end"]))
            weaker = item if scores[id(item)] < scores[id(other)] else other
            weaker["_drop"] = True
            dropped += 1
            if weaker is item:
                break
    return [s for s in kept if not s.get("_drop")], dropped
