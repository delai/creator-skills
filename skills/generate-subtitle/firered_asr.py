#!/usr/bin/env python3
"""FireRedASR2S 转写引擎（FunASR 之外的第二个可选 ASR）。

为什么是 2S 而不是初代 FireRedASR：
初代（FireRedASR-AED/LLM）只吐一整串文字——**没有词级时间戳、没有 VAD、没有标点**，
而且单次输入上限 60 秒。本脚本的每一步（按字宽拆条、拆条后重算起止、串音判定）都建立在
「每个 token 都有 (start_ms, end_ms)」之上，初代给不了，硬接只能整句均分时间，字幕会飘。
FireRedASR2S 是同一个团队的第二代，把 VAD + ASR + 标点串成了一条流水线，
`return_timestamp=True` 时还给词级时间戳，正好对上本脚本要的输入。

**跑在另一个 venv 里。** FireRedASR2S 依赖 transformers / peft / kaldi_native_fbank，
torch 版本也和 funasr 那个 venv 对不上，装一起会把 funasr 掀翻。所以这里起一个**常驻子进程**：
主进程（funasr venv）按 JSON 行发文件路径，子进程（fireredasr2s venv）加载一次模型后
一直复用——不常驻的话每个文件都要重新读 4.4GB 权重，多轨录音光加载就要一分钟。

环境变量：
    FIRERED_PYTHON      fireredasr2s venv 的解释器（默认 ~/.local/venvs/fireredasr2s/bin/python）
    FIRERED_MODEL_DIR   权重目录（默认 ~/.cache/fireredasr2s/pretrained_models）
    FIRERED_BEAM_SIZE   beam search 宽度（默认 3）
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

DEFAULT_PYTHON = Path.home() / ".local" / "venvs" / "fireredasr2s" / "bin" / "python"
DEFAULT_MODEL_DIR = Path.home() / ".cache" / "fireredasr2s" / "pretrained_models"

# LID（语种识别）单独 3.5GB，而本脚本面对的都是中文口播，识别出来也没人用，不下也不加载
NEEDED = ("FireRedASR2-AED", "FireRedVAD", "FireRedPunc")

SETUP_HINT = """\
FireRedASR2S 还没装好。一次性装法（约 5.6GB 权重）：

  uv venv --python 3.13 ~/.local/venvs/fireredasr2s
  VIRTUAL_ENV=~/.local/venvs/fireredasr2s uv pip install \\
      torch torchaudio transformers numpy cn2an kaldiio kaldi_native_fbank \\
      sentencepiece soundfile textgrid peft modelscope
  VIRTUAL_ENV=~/.local/venvs/fireredasr2s uv pip install --no-deps \\
      git+https://github.com/FireRedTeam/FireRedASR2S.git

  mkdir -p ~/.cache/fireredasr2s/pretrained_models
  cd ~/.cache/fireredasr2s/pretrained_models
  for m in FireRedASR2-AED FireRedVAD FireRedPunc; do
      ~/.local/venvs/fireredasr2s/bin/modelscope download --model "xukaituo/$m" --local_dir "./$m"
  done
"""


def interpreter() -> Path:
    return Path(os.environ.get("FIRERED_PYTHON") or DEFAULT_PYTHON).expanduser()


def model_dir() -> Path:
    return Path(os.environ.get("FIRERED_MODEL_DIR") or DEFAULT_MODEL_DIR).expanduser()


def missing_pieces() -> list[str]:
    """返回缺什么，空列表表示装好了。"""
    gaps = []
    if not interpreter().exists():
        gaps.append(f"解释器不存在: {interpreter()}")
    root = model_dir()
    for name in NEEDED:
        if not (root / name).is_dir():
            gaps.append(f"权重缺失: {root / name}")
    return gaps


# ---------------------------------------------------------------- 结果格式转换


def to_sentences(result: dict) -> list[dict]:
    """FireRedASR2S 的输出 → 本脚本通用的句子结构。

    `sentences` 是加过标点的句子（含绝对毫秒起止），`words` 是**整个文件**一条平铺的
    词级时间戳流（中文按字、英文按词），两者时间基准相同。这里按时间把 words 顺次分给
    各句，凑成 `timestamp`——下游 `char_time_map` 靠它把时间戳摊到每个字上。
    """
    words = list(result.get("words") or [])
    sentences = []
    cursor = 0
    for sent in result.get("sentences") or []:
        text = str(sent.get("text") or "").strip()
        if not text:
            continue
        start = float(sent.get("start_ms") or 0)
        end = float(sent.get("end_ms") or start)
        # 落在本句开始之前的词（理论上没有，VAD 段不重叠）先丢掉，保证指针单调
        while cursor < len(words) and float(words[cursor].get("end_ms", 0)) <= start:
            cursor += 1
        stamps = []
        while cursor < len(words):
            w = words[cursor]
            ws, we = float(w.get("start_ms", 0)), float(w.get("end_ms", 0))
            if (ws + we) / 2 >= end:
                break
            stamps.append([ws, we])
            cursor += 1
        sentences.append({
            "text": text,
            "start": start,
            "end": end,
            # 对不上时给 None，下游按字数均分——比塞一份错位的时间戳强
            "timestamp": stamps or None,
        })
    return sentences


# ---------------------------------------------------------------- 常驻子进程（客户端）


class FireRedEngine:
    """跑在 fireredasr2s venv 里的常驻转写进程。接口对齐 FunASR 那条路径。"""

    ENGINE = "firered"
    supports_diarization = False        # FireRedASR2S 没有声纹分离，只能靠分声道/分设备区分说话人

    def __init__(self, device: str):
        self.device = device
        self.proc: subprocess.Popen | None = None

    def _start(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            return
        env = dict(os.environ)
        env["FIRERED_MODEL_DIR"] = str(model_dir())
        if self.device == "mps":
            # forced_align（词级时间戳那步）在 MPS 上没实现，不开回退会直接抛
            env["PYTORCH_ENABLE_MPS_FALLBACK"] = "1"
        self.proc = subprocess.Popen(
            [str(interpreter()), str(Path(__file__).resolve()), "--worker", "--device", self.device],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None,
            text=True, bufsize=1, env=env,
        )
        ready = self.proc.stdout.readline()
        if not ready.strip():
            raise RuntimeError("FireRedASR2S 子进程启动即退出，见上方日志")
        payload = json.loads(ready)
        if payload.get("error"):
            raise RuntimeError(f"FireRedASR2S 加载失败: {payload['error']}")

    def transcribe(self, wav) -> list[dict]:
        self._start()
        assert self.proc and self.proc.stdin and self.proc.stdout
        self.proc.stdin.write(json.dumps({"wav": str(wav)}) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line.strip():
            raise RuntimeError("FireRedASR2S 子进程中途退出，见上方日志")
        payload = json.loads(line)
        if payload.get("error"):
            raise RuntimeError(f"FireRedASR2S 转写失败: {payload['error']}")
        return payload["sentences"]

    def close(self) -> None:
        if self.proc is None:
            return
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
            self.proc.wait(timeout=10)
        except Exception:  # noqa: BLE001 - 收尾失败就强杀
            self.proc.kill()
        self.proc = None


def load(device: str) -> FireRedEngine | None:
    gaps = missing_pieces()
    if gaps:
        print("Error: " + "；".join(gaps))
        print(SETUP_HINT)
        return None
    engine = FireRedEngine(device)
    print(f"Loading FireRedASR2S models... (device={device})")
    try:
        engine._start()
    except Exception as exc:  # noqa: BLE001 - 起不来就退回让调用方决定
        print(f"Error: {exc}")
        return None
    return engine


# ---------------------------------------------------------------- 常驻子进程（服务端）


def worker_main(device: str) -> int:
    """在 fireredasr2s venv 里跑：加载一次模型，然后按行处理请求。

    注意所有日志都往 stderr 走——stdout 是和主进程之间的 JSON 通道，混进一行别的就解析失败。
    """
    out = sys.stdout
    sys.stdout = sys.stderr                      # 挡住第三方库的 print

    try:
        import torch
        use_gpu = device in ("mps", "cuda")
        if device == "mps":
            # FireRedASR2S 里 .cuda() 是写死的，这里把它整体重定向到 mps
            mps = torch.device("mps")
            torch.nn.Module.cuda = lambda self, *a, **k: self.to(mps)
            torch.Tensor.cuda = lambda self, *a, **k: self.to(mps)
            torch.cuda.is_bf16_supported = lambda *a, **k: False
            torch.cuda.is_available = lambda *a, **k: True

        from fireredasr2s import FireRedAsr2System, FireRedAsr2SystemConfig
        from fireredasr2s.fireredasr2 import FireRedAsr2Config
        from fireredasr2s.fireredpunc import FireRedPuncConfig
        from fireredasr2s.fireredvad import FireRedVadConfig

        root = model_dir()
        config = FireRedAsr2SystemConfig(
            vad_model_dir=str(root / "FireRedVAD" / "VAD"),
            lid_model_dir="",
            asr_type="aed",
            asr_model_dir=str(root / "FireRedASR2-AED"),
            punc_model_dir=str(root / "FireRedPunc"),
            vad_config=FireRedVadConfig(use_gpu=use_gpu),
            asr_config=FireRedAsr2Config(
                use_gpu=use_gpu, use_half=False,
                beam_size=int(os.environ.get("FIRERED_BEAM_SIZE", 3)), nbest=1,
                decode_max_len=0, softmax_smoothing=1.25,
                aed_length_penalty=0.6, eos_penalty=1.0,
                return_timestamp=True,
            ),
            punc_config=FireRedPuncConfig(use_gpu=use_gpu),
            enable_vad=1, enable_lid=0, enable_punc=1,
        )
        system = FireRedAsr2System(config)
    except Exception as exc:  # noqa: BLE001 - 加载失败要把原因回给主进程
        out.write(json.dumps({"error": f"{type(exc).__name__}: {exc}"}) + "\n")
        out.flush()
        return 1

    out.write(json.dumps({"ready": True}) + "\n")
    out.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            result = system.process(json.loads(line)["wav"])
            reply = {"sentences": to_sentences(result)}
        except Exception as exc:  # noqa: BLE001 - 单个文件失败不该拖垮整个进程
            reply = {"error": f"{type(exc).__name__}: {exc}"}
        out.write(json.dumps(reply, ensure_ascii=False) + "\n")
        out.flush()
    return 0


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--wav", help="单独试跑一个 wav，直接打印 JSON")
    ns = parser.parse_args()
    if ns.worker:
        sys.exit(worker_main(ns.device))
    if ns.wav:
        engine = load(ns.device)
        if engine is None:
            sys.exit(1)
        print(json.dumps(engine.transcribe(ns.wav), ensure_ascii=False, indent=1))
        engine.close()
        sys.exit(0)
    parser.error("要么 --worker，要么 --wav")
