#!/usr/bin/env python3
"""关键重点提示字幕（花字）生成脚本。

读入正片字幕，分析内容后输出：
- `<名字>.highlights.srt`  叠在画面上的提示字幕：关键信息提示 + 章节开始提示（`【N】…`）

Usage:
    python make_highlights.py <subtitle.srt> [options]

Environment Variables:
    SUBTITLE_HANDOFF_DIR    handoff 任务根目录 (默认: <字幕所在目录>/.subtitle_tasks)
    SUBTITLE_LLM            是否启用 LLM: 0 或 1 (默认: 1)

Exit codes:
    0   正常产出
    1   出错
    10  handoff：有任务等当前会话处理（见输出里的清单），做完重跑同一条命令
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from llm import (  # noqa: E402
    EXIT_HANDOFF_PENDING,
    check_agent_available,
    last_call_pending,
    pending_handoff,
    pending_report,
    resume_command,
    run_agent_task,
    set_handoff_root,
)

from text_utils import LANGUAGE_RULES, chinese_rules

TIME_RE = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})"
)

CHAPTER_DURATION = 3.5
HIGHLIGHT_DURATION = 2.6
MIN_GAP = 8.0
MAX_PER_MINUTE = 1.2
# 花字的输出很小（整片也就几十条），输入才是便宜的那头：agent 自己读 in.json，
# 没有命令行长度限制，1700 条字幕也才 ~1 万 tokens。所以默认**整份一次过**——
# 分批会让每批只看见自己那一段，章节被批次边界撑爆（实测 76 分钟切 9 批出了 21 个章节）。
# 真正的约束是**文字量**，不是条数。开说话人分离后，同样长度的视频会被切出更多更短的条目
# （实测 76 分钟从 1765 条变成 2483 条，正文却还是 2.4 万字 ≈ 3 万 tokens）。
# 按条数卡门槛会把它误判成「太长」而退回切片，而切片会让每批只看得见自己那一段，
# 章节被批次边界撑爆。所以按字数判，条数只留一个宽松的兜底。
SINGLE_PASS_CHARS = 60000
SINGLE_PASS_LIMIT = 4000
BATCH_CUES = 200
CONTEXT_CUES = 15


def fits_single_pass(cues: list[dict]) -> bool:
    return len(cues) <= SINGLE_PASS_LIMIT and sum(len(c["text"]) for c in cues) <= SINGLE_PASS_CHARS


# ---------------------------------------------------------------- 字幕解析


def parse_subtitle(path: Path) -> list[dict]:
    """解析 srt / vtt，返回 [{start, end, text}]（时间单位毫秒）。"""
    raw = path.read_text(encoding="utf-8-sig")
    cues: list[dict] = []
    for block in re.split(r"\n\s*\n", raw.strip()):
        lines = [line for line in block.strip().splitlines() if line.strip()]
        if not lines:
            continue
        match = None
        text_start = 0
        for i, line in enumerate(lines):
            match = TIME_RE.search(line)
            if match:
                text_start = i + 1
                break
        if not match:
            continue
        h1, m1, s1, ms1, h2, m2, s2, ms2 = (int(g) for g in match.groups())
        text = " ".join(lines[text_start:]).strip()
        text = re.sub(r"</?[a-zA-Z][^>]*>", "", text).strip()
        if not text:
            continue
        cues.append({
            "start": ((h1 * 60 + m1) * 60 + s1) * 1000 + ms1,
            "end": ((h2 * 60 + m2) * 60 + s2) * 1000 + ms2,
            "text": text,
        })
    return cues


def format_timestamp(ms: float, vtt: bool = False) -> str:
    ms = max(int(round(ms)), 0)
    hours, ms = divmod(ms, 3600_000)
    minutes, ms = divmod(ms, 60_000)
    seconds, millis = divmod(ms, 1000)
    sep = "." if vtt else ","
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}{sep}{millis:03d}"


def short_timestamp(ms: float) -> str:
    total = int(ms // 1000)
    return f"{total // 60:02d}:{total % 60:02d}"


# ---------------------------------------------------------------- LLM 分析


def clean_label(text: str, language: str = "auto") -> str:
    """去掉 agent 可能自带的章节编号/书名号包装（避免和渲染出的【N】重复），
    并按正片字幕同样的规则补中英文空格。"""
    if not chinese_rules(text, language):
        return text.strip()
    text = re.sub(r"^\s*[【\[（(]?第?\s*[0-9一二三四五六七八九十]+\s*(?:章|节|部分|段)[】\])）]?[：:、.\s-]*", "", text.strip())
    text = re.sub(r"^[【\[]|[】\]]$", "", text.strip())
    text = re.sub(r"[。，、；：,.;:]+$", "", text.strip()).strip()
    text = re.sub(r"([一-鿿])([A-Za-z0-9])", r"\1 \2", text)
    text = re.sub(r"([A-Za-z0-9])([一-鿿])", r"\1 \2", text)
    return text.strip()


def batch_indices(cues: list[dict], size: int) -> list[list[int]]:
    return [list(range(i, min(i + size, len(cues)))) for i in range(0, len(cues), size)]


def plan_batches(cues: list[dict]) -> list[list[int]]:
    """默认整份一次过；超长字幕才退回切片。"""
    if fits_single_pass(cues):
        return [list(range(len(cues)))]
    return batch_indices(cues, BATCH_CUES)


def analyze_batch(cues: list[dict], indices: list[int], context: str,
                  known_chapters: list[str], per_minute: float = MAX_PER_MINUTE) -> list[dict]:
    minutes = max((cues[indices[-1]]["end"] - cues[indices[0]]["start"]) / 60000, 0.1)
    budget = max(round(minutes * per_minute), 1)
    # 整份一次过时按全片长度给章节预算；只有被迫切片时才用「每片最多 3 个」
    full_pass = len(indices) == len(cues)
    chapter_budget = min(8, max(3, round(minutes / 8))) if full_pass else 3

    payload = {
        "任务": "为口播视频挑花字（关键重点提示 + 章节分段提示）",
        "视频主题背景": context or "（未提供）",
        "前面已划出的章节": known_chapters or "（还没有，这是视频开头）",
        # 上一批的结尾只读带进来，判断「这是新话题还是上一段的延续」
        "上文_只读参考_不要给它挑花字": " ".join(
            cue["text"] for cue in cues[max(0, indices[0] - CONTEXT_CUES):indices[0]]
        ),
        "本片段时长分钟": round(minutes, 1),
        "本片段花字数量上限": budget,
        "本片段章节数量上限": chapter_budget,
        "是否整片一次给你": "是，这就是全片字幕" if full_pass else "否，这只是其中一段",
        "字幕": [
            {"cue": i, "t": short_timestamp(cues[i]["start"]), "text": cues[i]["text"]}
            for i in indices
        ],
    }

    if full_pass:
        chapter_rule = ("这是全片字幕，请通篇看完再决定章节怎么切——"
                        "章节是观众理解全片结构的骨架，宁可少而准，别把同一话题的小转折也当成新章。")
    elif known_chapters:
        chapter_rule = "只有话题真的换了才新增章节；同一话题的延续不要再开新章节。"
    else:
        chapter_rule = "这是视频的开头部分，第一条章节提示应当标出视频真正开始讲正题的位置。"

    instruction = (
        LANGUAGE_RULES + "花字和章节标题沿用对应片段原语言，混合语言按原文保留；不要加中文章节前缀。\n" +
        "你在为一条口播视频挑「花字」——叠在画面上的提示文字，不是字幕本身。\n"
        "目的有两个：讲到关键信息时给观众一个提示；进入新话题时给一个分段提示。\n\n"
        "请读取当前目录下的 in.json（里面有视频背景、已划出的章节、上文参考和本片段字幕），"
        "挑好花字后把结果写入当前目录下的 out.json。\n\n"
        "两种类型：\n"
        "- chapter：新章节开始，数量不超过 in.json 里「本片段章节数量上限」，没有就一个都别给。"
        f"文字是这一段的小标题，6~12 字，概括这一章讲什么。{chapter_rule}\n"
        "- highlight：关键重点提示。只挑「观众会想记笔记」的内容：核心概念定义、有洞察的结论金句、"
        "关键步骤/命令/路径、容易混淆的对比关系、实用技巧与排查方法。8~14 字，短促有力\n\n"
        "数量控制：不超过 in.json 里「本片段花字数量上限」给的条数，宁缺毋滥。\n\n"
        "硬性要求：\n"
        "1. 不复述原话，要提炼；也不要写「现在知道怎么做了吧」这种无信息量的总结\n"
        "2. 内容必须能从 in.json 的字幕原文推导出来，禁止臆造文件名、路径、数字、产品实现细节\n"
        "3. 不挑琐碎操作细节（点某个菜单、临时起的示例名）\n"
        "4. cue 必须是 in.json「字幕」里出现过的 cue 值，指向这个信息**开始讲**的那一条\n"
        "5. 相邻两条提示至少隔 8 秒，不要扎堆\n"
        "6. 并列要点可以用 1️⃣2️⃣3️⃣ 开头，其余不要加表情\n"
        "7. 数字和年份写成阿拉伯数字（19年、2025年、3 个步骤），不要写成中文数字\n\n"
        "out.json 必须是一个 JSON 数组，元素格式：\n"
        # reason 不落盘，只是逼模型对每条给出理由——挑不出理由的条目通常也不该挑
        '{"cue": 12, "type": "chapter", "text": "花字内容", "reason": "为什么挑它"}\n'
        "一条都没挑中就写空数组 []。只写文件，不要在回复里输出 JSON。"
    )

    data = run_agent_task(instruction, payload,
                          label=f"highlights-cue{indices[0] + 1}-{indices[-1] + 1}")
    if data is None:
        if last_call_pending():
            return []            # handoff：题已出，等下一轮拿答案，别拆批
        if len(indices) > 1:
            mid = len(indices) // 2
            print(f"  Retrying with smaller batches ({mid} + {len(indices) - mid} cues)...")
            first = analyze_batch(cues, indices[:mid], context, known_chapters, per_minute)
            titles = known_chapters + [item["text"] for item in first if item["type"] == "chapter"]
            return first + analyze_batch(cues, indices[mid:], context, titles, per_minute)
        print("  Warning: agent 分析失败，跳过该片段")
        return []

    allowed = set(indices)
    items = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        try:
            cue_idx = int(entry.get("cue"))
        except (TypeError, ValueError):
            continue
        text = clean_label(str(entry.get("text") or ""))
        if cue_idx not in allowed or not text:
            continue
        kind = "chapter" if str(entry.get("type") or "").strip() == "chapter" else "highlight"
        items.append({"cue": cue_idx, "type": kind, "text": text})
    return items


def rule_chapters(cues: list[dict], gap_seconds: float = 20.0) -> list[dict]:
    """无 LLM 时的兜底：只按长停顿切章节，不猜重点。"""
    items = []
    for i in range(1, len(cues)):
        if cues[i]["start"] - cues[i - 1]["end"] >= gap_seconds * 1000:
            items.append({"cue": i, "type": "chapter", "text": ""})
    return items


# ---------------------------------------------------------------- 时间轴与输出


def dedupe(items: list[dict], cues: list[dict], min_gap: float) -> list[dict]:
    """按 cue 排序，去重复文案，压掉扎堆的条目（章节优先于重点）。"""
    items = sorted(items, key=lambda it: it["cue"])
    kept: list[dict] = []
    seen: set[str] = set()
    for item in items:
        key = re.sub(r"[\s\W_]+", "", item["text"])
        if key and key in seen:
            continue
        if kept:
            prev = kept[-1]
            gap = (cues[item["cue"]]["start"] - cues[prev["cue"]]["start"]) / 1000
            if gap < min_gap:
                # 靠得太近：章节提示优先保留
                if item["type"] == "chapter" and prev["type"] != "chapter":
                    seen.discard(re.sub(r"[\s\W_]+", "", prev["text"]))
                    kept[-1] = item
                    seen.add(key)
                continue
        kept.append(item)
        if key:
            seen.add(key)
    return kept


def assign_timeline(items: list[dict], cues: list[dict]) -> list[dict]:
    chapter_no = 0
    result = []
    for i, item in enumerate(items):
        anchor = cues[item["cue"]]
        start = anchor["start"]
        duration = (CHAPTER_DURATION if item["type"] == "chapter" else HIGHLIGHT_DURATION) * 1000
        end = start + duration
        if i + 1 < len(items):
            end = min(end, cues[items[i + 1]["cue"]]["start"] - 100)
        end = min(end, cues[-1]["end"])
        end = max(end, start + 1200)
        if item["type"] == "chapter":
            chapter_no += 1
            label = f"【{chapter_no}】{item['text']}"
        else:
            label = item["text"]
        result.append({**item, "start": start, "end": end, "label": label, "chapter_no": chapter_no})
    return result


def render_srt(items: list[dict], vtt: bool = False) -> str:
    blocks = ["WEBVTT\n"] if vtt else []
    for i, item in enumerate(items, 1):
        blocks.append(
            f"{i}\n{format_timestamp(item['start'], vtt)} --> {format_timestamp(item['end'], vtt)}\n{item['label']}\n"
        )
    return "\n".join(blocks)


# ---------------------------------------------------------------- 主流程


def build_highlights(subtitle: Path, context: str = "", use_llm: bool | None = None,
                     min_gap: float = MIN_GAP, per_minute: float = MAX_PER_MINUTE,
                     output: Path | None = None) -> Path | None:
    cues = parse_subtitle(subtitle)
    if not cues:
        print(f"Error: 字幕解析为空: {subtitle}")
        return None
    print(f"读入 {len(cues)} 条字幕，总时长 {cues[-1]['end'] / 60000:.1f}min")

    if use_llm is None:
        use_llm = check_agent_available() is not None

    items: list[dict] = []
    if use_llm:
        batches = plan_batches(cues)
        chapters_so_far: list[str] = []
        for i, indices in enumerate(batches):
            scope = "整份" if len(batches) == 1 else f"片段 {i + 1}/{len(batches)}"
            print(f"  agent 分析{scope}（{len(indices)} 条字幕）...")
            found = analyze_batch(cues, indices, context, chapters_so_far, per_minute)
            chapters_so_far += [item["text"] for item in found if item["type"] == "chapter"]
            items += found
        if pending_handoff():
            # 出完题就停：这时 items 是残缺的，写出去等于用半份分析覆盖上一轮的成果
            print(pending_report(resume_command()))
            return None
    else:
        print("⚠️  模型任务已关闭：只能按长停顿切章节，无法生成重点提示")
        items = rule_chapters(cues)

    if not items:
        if use_llm:
            print("Warning: 没有产出任何提示条目，可调大 --per-minute 再试")
        else:
            print("Warning: 规则模式下没找到长停顿（连续口播很常见）——启用 handoff 模型任务才能挑重点")
        return None

    items = assign_timeline(dedupe(items, cues, min_gap), cues)

    stem = subtitle.name[: -len(subtitle.suffix)]
    target = output or subtitle.with_name(f"{stem}.highlights{subtitle.suffix}")

    # 章节提示和重点提示**必须写在同一个文件里**：剪辑器（实测剪映）一个视频只认
    # 3 个字幕文件，正片 + 审查 + 花字已经占满，再多一个就退化成「以默认时间戳展示」。
    # 章节靠 `【N】` 前缀区分，需要单独用时按前缀 grep 出来即可。
    target.write_text(render_srt(items, vtt=subtitle.suffix.lower() == ".vtt"), encoding="utf-8")

    chapters = sum(1 for item in items if item["type"] == "chapter")
    print(f"Done! {len(items)} 条提示（章节 {chapters} 条，重点 {len(items) - chapters} 条）")
    print(f"提示字幕：{target}")
    return target


def main() -> int:
    parser = argparse.ArgumentParser(
        description="分析字幕内容，生成关键重点提示 + 章节分段提示字幕（花字）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例:
  python make_highlights.py "/path/to/合集.srt"
  python make_highlights.py "/path/to/合集.srt" --context "Claude Code skill 教程" --min-gap 12
""",
    )
    parser.add_argument("subtitle", help="正片字幕文件（.srt / .vtt）")
    parser.add_argument("-o", "--output", help="提示字幕输出路径（默认 <名字>.highlights.srt）")
    parser.add_argument("--context", default="", help="视频主题背景，帮助判断重点")
    parser.add_argument("--min-gap", type=float, default=MIN_GAP, help=f"相邻提示最小间隔秒数 (默认: {MIN_GAP})")
    parser.add_argument("--per-minute", type=float, default=MAX_PER_MINUTE,
                        help=f"每分钟提示条数上限 (默认: {MAX_PER_MINUTE})")
    parser.add_argument("--no-llm", action="store_true", help="不创建模型任务（只按停顿切章节）")
    args = parser.parse_args()

    subtitle = Path(args.subtitle).expanduser().resolve()
    if not subtitle.is_file():
        print(f"Error: 字幕文件不存在: {subtitle}")
        return 1
    set_handoff_root(subtitle.parent / ".subtitle_tasks")

    result = build_highlights(
        subtitle,
        context=args.context,
        use_llm=False if args.no_llm else None,
        min_gap=args.min_gap,
        per_minute=args.per_minute,
        output=Path(args.output).expanduser().resolve() if args.output else None,
    )
    if result:
        return 0
    return EXIT_HANDOFF_PENDING if pending_handoff() else 1


if __name__ == "__main__":
    raise SystemExit(main())
