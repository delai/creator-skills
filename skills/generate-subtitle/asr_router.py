"""Per-track language routing and engine-specific caches; heavy models load lazily."""
from __future__ import annotations

import json

from whisper_asr import WhisperASR, model_identity


class ASRRouter:
    def __init__(self, args, cache_dir, cache_path, resolve_engine, load_model):
        self.args = args
        self.cache_dir = None if args.no_cache else cache_dir
        self.cache_path = cache_path
        self.resolve_engine = resolve_engine
        self.load_model = load_model
        self.models = {}
        self.records = []

    def whisper(self):
        if "whisper" not in self.models:
            self.models["whisper"] = WhisperASR(self.args.whisper_model, self.args.device)
        return self.models["whisper"]

    def cache(self, path, track, engine, language, model, diarize=False):
        if self.cache_dir is None:
            return None
        return self.cache_path(self.cache_dir, path, diarize, track, engine, language, model)

    def detect(self, path, track, make_wav):
        language = self.args.language
        if language != "auto":
            return {"language": language, "chinese": language == "zh", "samples": [], "hint": True}
        cache = self.cache(path, track, "whisper-lid-v1", "auto", model_identity(self.args.whisper_model))
        if cache and cache.exists():
            try:
                data = json.loads(cache.read_text(encoding="utf-8"))
                if isinstance(data["chinese"], bool) and isinstance(data["language"], str):
                    return data
            except (ValueError, KeyError, TypeError):
                pass
        data = self.whisper().detect(make_wav())
        if cache:
            cache.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return data

    def transcribe(self, path, track, make_wav, diarize=False):
        detection = self.detect(path, track, make_wav)
        if not detection["chinese"]:
            if diarize:
                raise ValueError("当前 FunASR paraformer-zh + cam++ 管线仅用于中文；非中文请按声道/设备分轨，或关闭 --speakers。")
            engine = "whisper"
            if self.args.engine not in ("auto", "whisper"):
                print(f"Note: 识别为非中文/不确定语言，{self.args.engine} 改走 whisper")
        else:
            engine = self.resolve_engine(self.args.engine, diarize)
        if diarize and engine != "funasr":
            raise ValueError(f"{engine} 没有声纹分离；中文混音声纹分离请用 --engine auto/funasr")
        model_tag = {"funasr": "paraformer-zh+fsmn-vad+ct-punc", "firered": "fireredasr2-aed"}.get(engine)
        if engine == "whisper":
            model_tag = model_identity(self.args.whisper_model)
        # auto 与显式提示不能互用；检测结论、实际引擎、模型都进入 key。
        language_key = f"{self.args.language}:{detection['language']}"
        cache = self.cache(path, track, engine, language_key, model_tag, diarize)
        sentences = None
        if cache and cache.exists():
            try:
                sentences = json.loads(cache.read_text(encoding="utf-8"))["sentences"]
                if not isinstance(sentences, list):
                    sentences = None
            except (ValueError, KeyError, TypeError):
                pass
        record = {"source": str(path), "track": track, "engine": engine, "model": model_tag,
                  "audio_language": self.args.language, "detection": detection,
                  "detector_model": model_identity(self.args.whisper_model) if self.args.language == "auto" else None}
        self.records.append(record)
        if sentences is not None:
            print(f"命中缓存: {path.name} [{track or 'mono'}] {engine} / {detection['language']}")
            return sentences
        wav = make_wav()
        if engine == "whisper":
            sentences = self.whisper().transcribe(wav, self.args.language)
        else:
            key = (engine, diarize)
            if key not in self.models:
                self.models[key] = self.load_model(self.args.device, diarize, engine)
            model = self.models[key]
            if model is None:
                raise RuntimeError(f"无法加载 {engine}")
            from generate_subtitle import transcribe_audio
            sentences = transcribe_audio(model, wav, self.args.speaker_count if diarize else None)
        for item in sentences:
            item["language"] = item.get("language", detection["language"])
            item["engine"] = engine
        if cache:
            cache.write_text(json.dumps({**record, "sentences": sentences}, ensure_ascii=False), encoding="utf-8")
        return sentences

    def close(self):
        for model in self.models.values():
            if hasattr(model, "close"):
                model.close()
