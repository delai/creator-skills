"""Unicode subtitle boundaries; offsets always refer to the original Python string."""
from __future__ import annotations

import unicodedata

LANGUAGE_RULES = (
    "字幕和引用必须保留音频/输入原语言，包括混合语言、专名和组合字符；"
    "Skill、提示词或背景资料使用哪种语言都不决定字幕语言，禁止自动翻译。\n"
    "中文年份、中文重复/语气词清理、中英文空格规则只适用于明确的中文片段；"
    "其他语言不套用这些规则，不把相同汉字的日文当中文。"
    "非中文只修有把握的识别错误，真实重复和语气词保留。\n"
)


def grapheme_spans(text: str) -> list[tuple[int, int]]:
    try:
        import regex
    except ImportError as exc:
        raise RuntimeError("多语言断句需要 regex：请在运行环境安装 regex wcwidth") from exc
    return [m.span() for m in regex.finditer(r"\X", text)]


def is_han(ch: str) -> bool:
    return bool(ch) and (0x3400 <= ord(ch) <= 0x9fff or 0x20000 <= ord(ch) <= 0x323af)


def is_unspaced(ch: str) -> bool:
    n = ord(ch)
    return is_han(ch) or 0x3040 <= n <= 0x30ff or 0x0e00 <= n <= 0x0eff or 0x1780 <= n <= 0x17ff or 0x1000 <= n <= 0x109f


def chinese_rules(text: str, language: str = "auto") -> bool:
    # 汉字本身不能证明是中文。auto/未知时关闭自动中文改写，交给模型判断。
    if language != "zh":
        return False
    return not any(0x3040 <= ord(c) <= 0x30ff or 0xac00 <= ord(c) <= 0xd7af for c in text)


def tokenize_spans(text: str) -> list[tuple[int, int]]:
    clusters = grapheme_spans(text)
    out = []
    for a, b in clusters:
        cluster = text[a:b]
        if cluster.isspace() or all(unicodedata.category(c).startswith(("P", "C")) for c in cluster):
            continue
        # 有空格语言保全词；无空格语言最少保全字素（泰文等不声称有词典分词）。
        word = any(unicodedata.category(c).startswith(("L", "N", "M")) for c in cluster)
        if (word and not is_unspaced(cluster[0]) and out
                and not is_unspaced(text[out[-1][0]])
                and (out[-1][1] == a or text[out[-1][1]:a] in ("'", "’", "-"))
                and any(unicodedata.category(c).startswith(("L", "N")) for c in text[out[-1][0]:out[-1][1]])):
            out[-1] = (out[-1][0], b)
        else:
            out.append((a, b))
    return out


def display_width(text: str) -> float:
    try:
        from wcwidth import wcswidth
    except ImportError as exc:
        raise RuntimeError("多语言字宽需要 wcwidth：请在运行环境安装 regex wcwidth") from exc
    return sum(max(0, wcswidth(text[a:b])) / 2 for a, b in grapheme_spans(text.strip()))


def sentence_boundary(text: str, i: int, soft: bool = True) -> bool:
    ch = text[i]
    if ch in "。！？!?…؟۔।॥":
        return True
    if ch == ".":
        # 小数、域名、缩写内部不切；句末句点及后接空白的句点可切。
        return i + 1 == len(text) or text[i + 1].isspace()
    return soft and ch in "，、,;；:：،؛"


def map_graphemes(text: str, a: int, b: int, start: float, end: float, times: list) -> None:
    clusters = [(x, y) for x, y in grapheme_spans(text) if a <= x and y <= b]
    for i, (x, y) in enumerate(clusters):
        stamp = (start + (end - start) * i / len(clusters),
                 start + (end - start) * (i + 1) / len(clusters))
        times[x:y] = [stamp] * (y - x)


def char_time_map(text: str, timestamps, start: float, end: float, words=None) -> list[tuple[float, float]]:
    if not text:
        return []
    times = [None] * len(text)
    if words:
        # Whisper 的真实词文本/字符区间，不能用本地 token 数强配词时间戳。
        cursor = 0
        for word in words:
            token = str(word.get("word", "")).strip()
            a = text.find(token, cursor) if token else -1
            if a < 0:
                continue
            b = a + len(token)
            map_graphemes(text, a, b, min(end, max(start, float(word["start"]))),
                          min(end, max(start, float(word["end"]))), times)
            cursor = b
    else:
        spans = tokenize_spans(text)
        if timestamps and len(timestamps) == len(spans):
            for (a, b), stamp in zip(spans, timestamps):
                map_graphemes(text, a, b, min(end, max(start, float(stamp[0]))), min(end, max(start, float(stamp[1]))), times)
    if all(t is None for t in times):
        map_graphemes(text, 0, len(text), start, end, times)
    # 未对齐的文字在相邻已知时间之间插值，不跨越句子范围。
    clusters = grapheme_spans(text)
    i = 0
    last = start
    while i < len(clusters):
        a, b = clusters[i]
        if times[a] is not None:
            ts, te = times[a]
            stamp = (max(last, ts), min(end, max(last, ts, te)))
            times[a:b] = [stamp] * (b - a)
            last = stamp[1]
            i += 1
            continue
        j = i
        while j < len(clusters) and times[clusters[j][0]] is None:
            j += 1
        next_start = max(last, times[clusters[j][0]][0]) if j < len(clusters) else end
        map_graphemes(text, a, clusters[j - 1][1], last, next_start, times)
        last = next_start
        i = j
    return times
