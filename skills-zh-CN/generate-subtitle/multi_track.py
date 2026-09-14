#!/usr/bin/env python3
"""多个**同时录制**的音频文件 → 一条共享时间轴上的 N 条说话人轨。

场景：对谈时每个人（或每两个人）面前放一台录音设备，各自录成一个文件。
这些文件是**并行**的（同一段时间的不同视角），不是接续的，所以：

1. `align` —— 起始时刻对齐。各设备按键的时刻差几秒到几分钟，文件创建时间又不可靠，
   所以用**音频本身**对：每个文件的响度包络做互相关，峰值位置就是时差；
   再在若干窗口里分别测一次，拟合出时钟漂移（不同设备的采样钟有 ppm 级差异，
   80 分钟能差几十毫秒）。最早开录的文件定为时间轴 0 点。
2. 每个文件各自做左右声道判定（`dual_channel.analyze`）：是两个人就拆成两轨，否则一轨。
3. `gate_masks` —— 把所有轨放到共享时间轴上，按「归一化后谁更响」做 N 轨门限：
   一轨只保留自己占优（不比最响的别轨低 2 dB 以上）的帧，其余硬静音。
   这是 `dual_channel.separate` 从两轨到 N 轨的推广，多了一步**按设备归一化增益**
   （不同设备的电平不可比，同一设备的左右声道则保持原样）。
   **这一步是有条件的**：先用 `dual_channel.separation_db` 量一遍轨间分离度，
   低于 `MIN_SEPARATION_DB` 说明几台设备都把全场收进来了，门限只会削词头，直接跳过
   （见 `generate_subtitle.decide_gate`）。
4. 各轨分别转写，轨号即说话人号；串音清理、跨轨去重、互校都在共享时间轴上做。

包络分辨率 10ms：对齐精度 ±10ms，对字幕足够；80 分钟文件只有 48 万个点，互相关秒出。
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

from dual_channel import (
    ENVELOPE_MS,
    FLOOR_DB,
    HOLD_MS,
    KEEP_MARGIN_DB,
    SAMPLE_RATE,
    _moving_average,
    _to_db,
)

FRAME_MS = 10                          # 对齐 / 门限用的包络帧长
ACTIVE_ABOVE_FLOOR_DB = 8.0            # 比噪声底高这么多才算有人在说话

# 对齐可信度门槛。互相关峰值相对背景的倍数：两段不相关的录音随机峰大约 5~8 倍标准差，
# 真同期录音实测 90 倍；归一化相关系数实测 0.69，不相关的接近 0。
MIN_PEAK_TO_STD = 15.0
MIN_NCC = 0.20
# 漂移：分段测到的时差与全局时差拟合出的斜率，整片累计不到这个量就当没有
MIN_DRIFT_MS = 50.0
# 两条来自不同文件的轨，归一化包络在有效帧上的相关性高于此值 = 同一个声源被录了两遍
SAME_SOURCE_CORR = 0.90
# 设备增益归一化取有效帧的这个分位数（≈ 近讲人声的峰值区，受串音影响小）
GAIN_PERCENTILE = 90


@dataclass
class Alignment:
    """一个文件相对共享时间轴的位置。shared_ms = local_ms * speed + offset_ms。"""

    offset_ms: float = 0.0
    speed: float = 1.0
    ncc: float = 0.0
    peak_ratio: float = 0.0
    confident: bool = True
    windows: list[tuple[float, float]] = field(default_factory=list)   # (局部时刻 s, 局部时差 s)

    def to_shared(self, local_ms: float) -> float:
        return local_ms * self.speed + self.offset_ms


@dataclass
class Track:
    file_idx: int
    channel: int | None            # None = 整个文件混成单声道；0/1 = 左/右
    speaker: int                   # 全局说话人号（0 起）
    label: str                     # 给人看的：「文件1·L」
    merged_into: int | None = None  # 被判为同一声源并入别的轨时记录目标轨


@dataclass
class Plan:
    alignments: list[Alignment]
    tracks: list[Track]
    total_ms: float
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps({
            "alignments": [asdict(a) for a in self.alignments],
            "tracks": [asdict(t) for t in self.tracks],
            "total_ms": self.total_ms,
            "notes": self.notes,
        }, ensure_ascii=False)


# ---------------------------------------------------------------- 读音频


def read_mono(path: Path, channel: int | None):
    """解成 16k 单声道 float32。channel=None 是左右混音，0/1 是只取那一条声道。"""
    import numpy as np

    filt = ["-ac", "1"] if channel is None else ["-af", f"pan=mono|c0=c{channel}"]
    cmd = ["ffmpeg", "-v", "error", "-i", str(path), "-vn", *filt,
           "-ar", str(SAMPLE_RATE), "-f", "s16le", "-acodec", "pcm_s16le", "-"]
    result = subprocess.run(cmd, capture_output=True, timeout=3600)
    if result.returncode != 0 or not result.stdout:
        return None
    return np.frombuffer(result.stdout, dtype="<i2").astype("float32") / 32768.0


def envelope_db(mono, frame_ms: int = FRAME_MS):
    import numpy as np

    frame = int(SAMPLE_RATE * frame_ms / 1000)
    count = len(mono) // frame
    if count == 0:
        return np.zeros(0, dtype="float32")
    power = (mono[: count * frame].reshape(count, frame) ** 2).mean(axis=1)
    return _to_db(power).astype("float32")


# ---------------------------------------------------------------- 对齐


def _prep(env):
    """互相关前的预处理：去掉噪声底、只留「有声」的部分、去均值。"""
    import numpy as np

    x = env - np.median(env)
    x = np.maximum(x, 0.0)
    return (x - x.mean()).astype("float32")


def _xcorr_lag(a, b) -> tuple[int, float, float]:
    """返回 (lag, ncc, peak/std)：a[i] ≈ b[i - lag]，即 b 比 a 晚 lag 帧开始。"""
    import numpy as np

    n = 1 << int(np.ceil(np.log2(len(a) + len(b))))
    spectrum = np.fft.rfft(a, n) * np.conj(np.fft.rfft(b, n))
    xc = np.fft.irfft(spectrum, n)
    k = int(np.argmax(xc))
    lag = k if k < n // 2 else k - n
    denom = float(np.sqrt((a ** 2).sum() * (b ** 2).sum())) or 1.0
    std = float(xc.std()) or 1.0
    return lag, float(xc[k] / denom), float(xc[k] / std)


def align_pair(ref_env, env, window_s: float = 180.0, every_s: float = 600.0) -> Alignment:
    """把 env 对到 ref_env 上：全局时差 + 分窗口测漂移。"""
    import numpy as np

    a, b = _prep(ref_env), _prep(env)
    lag, ncc, ratio = _xcorr_lag(a, b)
    result = Alignment(offset_ms=lag * FRAME_MS, ncc=ncc, peak_ratio=ratio,
                       confident=ncc >= MIN_NCC and ratio >= MIN_PEAK_TO_STD)
    if not result.confident:
        return result

    # 漂移：每隔 every_s 取一个 window_s 的窗口，在全局时差附近 ±30s 内再测一次
    win = int(window_s * 1000 / FRAME_MS)
    step = int(every_s * 1000 / FRAME_MS)
    slack = int(30_000 / FRAME_MS)
    points: list[tuple[float, float]] = []
    for start in range(0, len(a) - win, step):
        lo, hi = start - lag - slack, start - lag + win + slack
        if lo < 0 or hi > len(b):
            continue
        seg_a, seg_b = a[start:start + win], b[lo:hi]
        local, local_ncc, local_ratio = _xcorr_lag(seg_a, seg_b)
        if local_ncc < MIN_NCC or local_ratio < MIN_PEAK_TO_STD:
            continue
        # seg_a[i] ≈ seg_b[i - local]  →  a[start + i] ≈ b[lo + i - local]  →  时差 = start - lo + local
        points.append((start * FRAME_MS / 1000.0, (start - lo + local) * FRAME_MS / 1000.0))
    result.windows = points
    if len(points) >= 3:
        xs = np.array([p[0] for p in points])
        ys = np.array([p[1] for p in points])
        slope, intercept = np.polyfit(xs, ys, 1)
        span_ms = abs(slope) * (len(env) * FRAME_MS)
        if span_ms >= MIN_DRIFT_MS:
            # shared = local + lag(local) = local * (1 + slope) + intercept
            result.speed = 1.0 + float(slope)
            result.offset_ms = float(intercept) * 1000.0
    return result


def align_files(envs: list) -> list[Alignment]:
    """以第一个文件为参考对齐所有文件，再平移成「最早开录的 = 0」。"""
    results = [Alignment(offset_ms=0.0, ncc=1.0, peak_ratio=float("inf"), confident=True)]
    for env in envs[1:]:
        results.append(align_pair(envs[0], env))
    earliest = min(r.offset_ms for r in results if r.confident)
    for r in results:
        r.offset_ms -= earliest
    return results


# ---------------------------------------------------------------- N 轨门限


def _active_mask(env):
    import numpy as np

    floor = float(np.percentile(env, 20))
    return (env > floor + ACTIVE_ABOVE_FLOOR_DB) & (env > -55.0)


def file_gain_db(mix_env) -> float:
    """一个文件（设备）的电平参考：有效帧响度的 p90，≈ 离它最近那个人的近讲电平。

    不取均值/中位数：一个人说得多、另一个说得少时，少说的那台设备的中位数落在串音电平上，
    归一化后串音会被抬到跟本人一样响，两台都保留、全是重复。
    """
    import numpy as np

    active = _active_mask(mix_env)
    if not active.any():
        return float(np.median(mix_env))
    return float(np.percentile(mix_env[active], GAIN_PERCENTILE))


def place_on_timeline(env, alignment: Alignment, total_frames: int):
    """把一个文件的包络按时差搬到共享时间轴上（漂移忽略，几十毫秒对门限无所谓）。"""
    import numpy as np

    out = np.full(total_frames, FLOOR_DB, dtype="float32")
    start = int(round(alignment.offset_ms / FRAME_MS))
    end = min(total_frames, start + len(env))
    if end > start:
        out[start:end] = env[: end - start]
    return out


def same_source_pairs(track_envs: list, tracks: list[Track]) -> list[tuple[int, int, float]]:
    """找出来自**不同文件**、但包络几乎一样的轨——同一个声源被两台设备各录了一遍。"""
    import numpy as np

    pairs = []
    for i in range(len(tracks)):
        for j in range(i + 1, len(tracks)):
            if tracks[i].file_idx == tracks[j].file_idx:
                continue
            a, b = track_envs[i], track_envs[j]
            active = _active_mask(np.maximum(a, b))
            if active.sum() < 1000:
                continue
            corr = float(np.corrcoef(a[active], b[active])[0, 1])
            if corr >= SAME_SOURCE_CORR:
                pairs.append((i, j, corr))
    return pairs


def gate_masks(track_envs: list, keep_margin_db: float = KEEP_MARGIN_DB,
               hold_ms: float = HOLD_MS) -> list:
    """每条轨一个布尔掩码（共享时间轴、FRAME_MS 一帧）：本轨不比最响的别轨低 2 dB 以上就保留。"""
    import numpy as np

    smooth = max(1, int(100 / FRAME_MS))
    hold = max(1, int(hold_ms / FRAME_MS))
    stacked = np.stack([_moving_average(env, smooth) for env in track_envs])
    masks = []
    for k in range(len(track_envs)):
        others = np.delete(stacked, k, axis=0)
        best_other = others.max(axis=0) if len(others) else np.full(stacked.shape[1], FLOOR_DB)
        keep = (stacked[k] - best_other) > keep_margin_db
        masks.append(_dilate(keep, hold))
    return masks


def _dilate(mask, k: int):
    if k <= 0:
        return mask
    out = mask.copy()
    for shift in range(1, k + 1):
        out[shift:] |= mask[:-shift]
        out[:-shift] |= mask[shift:]
    return out


def apply_gate(mono, mask, alignment: Alignment):
    """把共享时间轴上的掩码搬回文件本地时间，硬静音掉不属于本轨的部分。"""
    import numpy as np

    frame = int(SAMPLE_RATE * FRAME_MS / 1000)
    start = int(round(alignment.offset_ms / FRAME_MS))
    local_frames = len(mono) // frame + 1
    local = np.zeros(local_frames, dtype="float32")
    src_lo, src_hi = max(0, start), min(len(mask), start + local_frames)
    if src_hi > src_lo:
        local[src_lo - start: src_hi - start] = mask[src_lo:src_hi].astype("float32")
    gain = _moving_average(local, 3)                    # 3 帧渐变，避免爆音
    expanded = np.repeat(gain, frame)[: len(mono)]
    if len(expanded) < len(mono):
        expanded = np.concatenate([expanded, np.full(len(mono) - len(expanded), expanded[-1] if len(expanded) else 1.0, dtype="float32")])
    return mono * expanded


def pooled_envelopes(track_envs: list) -> dict[int, list[int]]:
    """给串音清理 / 跨轨去重用的粗包络：每 ENVELOPE_MS 一个值，按轨号索引。"""
    import numpy as np

    step = ENVELOPE_MS // FRAME_MS
    out = {}
    for k, env in enumerate(track_envs):
        count = len(env) // step
        pooled = env[: count * step].reshape(count, step).max(axis=1) if count else np.zeros(0)
        out[k] = [int(round(v)) for v in pooled]
    return out


def shift_sentence(item: dict, alignment: Alignment) -> dict:
    """把 ASR 给的文件本地时间换成共享时间轴（含漂移校正）。"""
    item["start"] = alignment.to_shared(float(item.get("start") or 0))
    item["end"] = alignment.to_shared(float(item.get("end") or 0))
    if item.get("words"):
        item["words"] = [{**word, "start": alignment.to_shared(float(word["start"])),
                          "end": alignment.to_shared(float(word["end"]))}
                         for word in item["words"]]
    stamps = item.get("timestamp")
    if stamps:
        item["timestamp"] = [[alignment.to_shared(float(s)), alignment.to_shared(float(e))]
                             for s, e in stamps]
    return item
