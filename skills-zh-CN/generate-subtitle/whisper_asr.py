"""Local OpenAI Whisper adapter. Always transcribe; translation belongs to handoff."""
from __future__ import annotations

from math import isfinite
from pathlib import Path


def model_identity(name: str) -> str:
    path = Path(name).expanduser()
    if path.is_file():
        stat = path.stat()
        return f"{path.resolve()}:{stat.st_size}:{stat.st_mtime_ns}"
    return name


def normalize_language(language: str) -> str:
    value = language.strip().lower()
    aliases = {"chinese": "zh", "中文": "zh", "zh-cn": "zh", "zh-tw": "zh",
               "english": "en", "japanese": "ja", "korean": "ko"}
    return aliases.get(value, value)


def supported_languages(model) -> set[str]:
    if not model.is_multilingual:
        raise ValueError("--whisper-model 必须是多语言模型，不能使用 .en / 英语单语 checkpoint")
    from whisper.tokenizer import get_tokenizer
    tokenizer = get_tokenizer(True, num_languages=model.num_languages, task="transcribe")
    return set(tokenizer.all_language_codes)


class WhisperASR:
    ENGINE = "whisper"

    def __init__(self, name: str, device: str):
        try:
            import whisper
        except ImportError as exc:
            raise RuntimeError("需要 openai-whisper（不是同名 whisper 包）；请在当前 Python 环境安装 openai-whisper") from exc
        # Whisper 对齐使用 sparse tensors / DTW；当前适配器只验证 CPU/CUDA。
        self.device = "cpu" if device == "mps" else device
        if device == "mps":
            print("Note: Whisper 词时间戳路径未验证 MPS，使用 CPU/FP32；CUDA 可用时建议 --device cuda")
        self.api = whisper
        self.model = whisper.load_model(str(Path(name).expanduser()) if Path(name).expanduser().is_file() else name,
                                        device=self.device)
        self.languages = supported_languages(self.model)
        print(f"Whisper 模型实际支持 {len(self.languages)} 种语言，设备 {self.device}")

    def validate_language(self, language: str):
        if language != "auto" and language not in self.languages:
            raise ValueError(f"当前 Whisper checkpoint 不支持音频语言 {language}；可用代码：{', '.join(sorted(self.languages))}")

    def detect(self, wav: Path) -> dict:
        audio = self.api.load_audio(str(wav))
        size = 30 * 16000
        # 首/中/尾抽样，跳过静音；抽样不保证发现每次语言切换。
        positions = sorted({0, max(0, (len(audio) - size) // 2), max(0, len(audio) - size)})
        samples = []
        for pos in positions:
            chunk = audio[pos:pos + size]
            if len(chunk) == 0 or float((chunk ** 2).mean()) < 1e-7:
                continue
            mel = self.api.log_mel_spectrogram(self.api.pad_or_trim(chunk), self.model.dims.n_mels).to(self.model.device)
            _, probs = self.model.detect_language(mel)
            code = max(probs, key=probs.get)
            samples.append({"language": code, "probability": float(probs[code]), "offset_s": pos / 16000})
        # 任一窗口非中文或低置信度时用多语言模型；不能把未知默认成中文。
        chinese = bool(samples) and all(x["language"] == "zh" and x["probability"] >= 0.7 for x in samples)
        language = "zh" if chinese else (max(samples, key=lambda x: x["probability"])["language"] if samples else "auto")
        return {"language": language, "chinese": chinese, "samples": samples}

    def transcribe(self, wav: Path, language: str = "auto") -> list[dict]:
        self.validate_language(language)
        result = self.model.transcribe(str(wav), task="transcribe", language=None if language == "auto" else language,
                                       word_timestamps=True, fp16=self.device.startswith("cuda"), verbose=False)
        return to_sentences(result)


def to_sentences(result: dict) -> list[dict]:
    out = []
    for segment in result.get("segments", []):
        words = [{"word": w["word"], "start": float(w["start"]) * 1000,
                  "end": float(w["end"]) * 1000} for w in segment.get("words", [])]
        text = segment.get("text", "").strip()
        if not text:
            continue
        # timestamp 使用公共 token 区间；另存 words 以便精确按文本映射。
        from text_utils import char_time_map, tokenize_spans
        start, end = float(segment["start"]) * 1000, float(segment["end"]) * 1000
        if not (isfinite(start) and isfinite(end) and 0 <= start < end):
            raise ValueError("Whisper 返回了无效或零时长的句子时间戳，请检查音频/模型结果")
        times = char_time_map(text, None, start, end, words)
        timestamps = [[times[a][0], times[b - 1][1]] for a, b in tokenize_spans(text)]
        out.append({"text": text, "start": start, "end": end, "words": words, "timestamp": timestamps,
                    "language": result.get("language", "auto"), "engine": "whisper"})
    return out
