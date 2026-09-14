#!/usr/bin/env python3
"""敏感 / 冒犯内容审查字幕生成脚本。

读入正片字幕，把其中可能有发布风险的片段**按原时间码原样摘出来**，输出：
- `<名字>.review.srt`  只含风险片段的字幕，扔进剪辑器就能逐个跳转决定删 / 静音 / 打码
- `<名字>.review.md`   人工复核清单：时间 / 类别 / 严重度 / 原文 / 风险点 / 处理建议

四类风险（用户口径）：
- 敏感信息：个人身份信息、联系方式、住址、证件号、账号密钥、公司内部或客户的未公开信息
- 人身攻击：针对具体个人的贬低、辱骂、羞辱
- 引起不适：暴力血腥、生理不适、疾病细节、令人尴尬的描述
- 冒犯性：脏话、地域/性别/种族/年龄/职业歧视、刻板印象、点名贬低第三方公司或产品

**宁可多报不可漏报**——这是给人复核用的，漏掉的代价远大于误报。

Usage:
    python make_review.py <subtitle.srt> [options]

Environment Variables:
    SUBTITLE_HANDOFF_DIR    handoff 任务根目录 (默认: <字幕所在目录>/.subtitle_tasks)
    SUBTITLE_LLM            是否启用 agent: 0 或 1 (默认: 1)

Exit codes:
    0   正常产出
    1   出错 / 跳过
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
from text_utils import LANGUAGE_RULES
from make_highlights import (  # noqa: E402
    fits_single_pass,
    format_timestamp,
    parse_subtitle,
    short_timestamp,
)


def next_indices(cues: list[dict], cursor: int) -> list[int]:
    """从 cursor 开始取下一段：默认把剩下的全给它；超长时才截成 BATCH_CUES 条。"""
    rest = cues[cursor:]
    size = len(rest) if fits_single_pass(rest) else BATCH_CUES
    return list(range(cursor, cursor + size))

# 审查的输出也很小（命中才几条），所以默认**整份一次过**：
# 通篇看完才判断得准（同一个人名在前面出现过、后面才念手机号这类跨段线索）。
# 整份 / 切片的门槛按**文字量**判，理由见 make_highlights.fits_single_pass
BATCH_CUES = 200
CONTEXT_CUES = 15
CATEGORIES = ("敏感信息", "人身攻击", "引起不适", "冒犯性")
SEVERITIES = ("高", "中", "低")
# 命中比例超过这个值多半是模型跑偏了，给个提醒
SUSPICIOUS_HIT_RATIO = 0.3


# ---------------------------------------------------------------- agent 分析


def _coerce_items(payload) -> list | None:
    """agent 写的可能是裸数组，也可能是 {"scanned_to":.., "items":[..]}。"""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("items", "results", "risks", "data"):
            if isinstance(payload.get(key), list):
                return payload[key]
    return None


def analyze_batch(cues: list[dict], indices: list[int], context: str) -> tuple[list[dict], int | None]:
    payload = {
        "任务": "口播视频字幕的发布前风险审查",
        "视频主题背景": context or "（未提供）",
        "上文_只读参考_不要审查它": " ".join(
            cue["text"] for cue in cues[max(0, indices[0] - CONTEXT_CUES):indices[0]]
        ),
        "待审查字幕": [
            {"cue": i, "t": short_timestamp(cues[i]["start"]), "text": cues[i]["text"]}
            for i in indices
        ],
    }

    instruction = (
        LANGUAGE_RULES + "风险原文必须逐字引用，不翻译、不换语言；分析说明可用当前会话语言。\n" +
        "你是视频发布前的内容审查员。请读取当前目录下的 in.json，"
        "把「待审查字幕」里可能有发布风险的片段挑出来，结果写入当前目录下的 out.json。\n\n"
        "四个类别（category 只能取这四个之一）：\n"
        "- 敏感信息：真实姓名+职务的组合、手机号、邮箱、微信/QQ、住址、身份证/银行卡号、"
        "密码密钥、公司内部数据、客户名单、报价与薪资、尚未公开的计划\n"
        "- 人身攻击：针对具体个人的贬低、辱骂、羞辱、能力否定\n"
        "- 引起不适：暴力血腥、生理不适、疾病细节、令人尴尬或难堪的描述\n"
        "- 冒犯性：脏话、地域/性别/种族/年龄/职业歧视、刻板印象、"
        "点名贬低第三方公司或产品（容易引起纠纷）\n\n"
        "严重度 severity 取「高」「中」「低」：\n"
        "- 高：发出去大概率出事（真实联系方式、明确辱骂、明显歧视）\n"
        "- 中：有争议风险，建议改口或删掉\n"
        "- 低：轻微，提示一下让人自己判断\n\n"
        "硬性要求：\n"
        "1. **宁可多报不可漏报**——这份结果是给人复核的，漏掉的代价远大于误报\n"
        "2. text 必须**原样复制** in.json 里那一条的文字，一个字都不要改写、不要概括\n"
        "3. 一段风险话横跨好几条字幕时，用 cue 标开头那条、cue_end 标结尾那条；"
        "只有一条就不写 cue_end\n"
        "4. cue / cue_end 必须是「待审查字幕」里出现过的 cue 值\n"
        "5. reason 一句话说清风险在哪；action 给处理建议（删除 / 静音 / 打码 / 改口 / 自行判断）\n"
        "6. 正常的技术讨论、产品介绍、对事不对人的评价不要报；"
        "「上文」只是帮你理解语境，不要给它报风险\n"
        "7. 一条风险都没有就写空数组 []，不要为了凑数硬报\n\n"
        "out.json 必须是一个 JSON 对象：\n"
        '{"scanned_to": <你实际审查到的最后一个 cue 值>, "items": [\n'
        '  {"cue": 12, "cue_end": 14, "category": "敏感信息", "severity": "高", '
        '"text": "原样复制的字幕原文", "reason": "风险点", "action": "删除"}\n]}\n'
        "**scanned_to 必须诚实**：字幕很长、你没能全部看完就写你真正看到的位置，"
        "剩下的会由后续调用接着审；谎报会让没审过的内容被当成已审。全部审完就写最后一个 cue 值。\n"
        "一条风险都没有也要写 scanned_to，items 给空数组。只写文件，不要在回复里输出 JSON。"
    )

    payload_out = run_agent_task(instruction, payload, raw=True,
                                 label=f"review-cue{indices[0] + 1}-{indices[-1] + 1}")
    if payload_out is None:
        if last_call_pending():
            return [], None      # handoff：题已出，等下一轮拿答案，别拆批
        if len(indices) > 1:
            mid = len(indices) // 2
            print(f"  Retrying with smaller batches ({mid} + {len(indices) - mid} cues)...")
            head_items, _ = analyze_batch(cues, indices[:mid], context)
            tail_items, tail_scanned = analyze_batch(cues, indices[mid:], context)
            return head_items + tail_items, tail_scanned
        print("  Warning: agent 审查失败，跳过该片段")
        return [], None

    scanned_to = None
    if isinstance(payload_out, dict):
        try:
            scanned_to = int(payload_out.get("scanned_to"))
        except (TypeError, ValueError):
            scanned_to = None
    data = _coerce_items(payload_out)
    if data is None:
        print("  Warning: out.json 里找不到 items 数组")
        return [], scanned_to

    allowed = set(indices)
    items = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        try:
            start_idx = int(entry.get("cue"))
        except (TypeError, ValueError):
            continue
        if start_idx not in allowed:
            continue
        try:
            end_idx = int(entry.get("cue_end", start_idx))
        except (TypeError, ValueError):
            end_idx = start_idx
        end_idx = max(start_idx, min(end_idx, indices[-1]))

        category = str(entry.get("category") or "").strip()
        if category not in CATEGORIES:
            category = "冒犯性"
        severity = str(entry.get("severity") or "").strip()
        if severity not in SEVERITIES:
            severity = "中"

        items.append({
            "cue": start_idx,
            "cue_end": end_idx,
            "category": category,
            "severity": severity,
            "reason": str(entry.get("reason") or "").strip(),
            "action": str(entry.get("action") or "").strip() or "自行判断",
        })
    return items, scanned_to


# ---------------------------------------------------------------- 合并与输出


SEVERITY_ORDER = {"高": 0, "中": 1, "低": 2}


def merge_ranges(items: list[dict]) -> list[dict]:
    """按时间排序，把重叠或紧挨着的区间并成一条，标签合起来。"""
    items = sorted(items, key=lambda it: (it["cue"], it["cue_end"]))
    merged: list[dict] = []
    for item in items:
        if merged and item["cue"] <= merged[-1]["cue_end"] + 1:
            prev = merged[-1]
            prev["cue_end"] = max(prev["cue_end"], item["cue_end"])
            same = next((t for t in prev["tags"] if t["category"] == item["category"]), None)
            if same is None:
                prev["tags"].append({"category": item["category"], "severity": item["severity"]})
            elif SEVERITY_ORDER.get(item["severity"], 9) < SEVERITY_ORDER.get(same["severity"], 9):
                # 同一类别只保留最严重的那档
                same["severity"] = item["severity"]
            if item["reason"] and item["reason"] not in prev["reasons"]:
                prev["reasons"].append(item["reason"])
            if item["action"] and item["action"] not in prev["actions"]:
                prev["actions"].append(item["action"])
            continue
        merged.append({
            "cue": item["cue"],
            "cue_end": item["cue_end"],
            "tags": [{"category": item["category"], "severity": item["severity"]}],
            "reasons": [item["reason"]] if item["reason"] else [],
            "actions": [item["action"]] if item["action"] else [],
        })
    return merged


def build_entries(merged: list[dict], cues: list[dict], tagged: bool) -> list[dict]:
    entries = []
    for item in merged:
        span = cues[item["cue"]:item["cue_end"] + 1]
        # 原文原样拼接，不改写
        body = " ".join(cue["text"] for cue in span).strip()
        tags = sorted(item["tags"], key=lambda t: SEVERITY_ORDER.get(t["severity"], 9))
        label = "｜".join(f"{t['category']}·{t['severity']}" for t in tags)
        entries.append({
            "start": span[0]["start"],
            "end": span[-1]["end"],
            "text": f"【{label}】{body}" if tagged else body,
            "body": body,
            "label": label,
            "top_severity": tags[0]["severity"],
            "categories": sorted({t["category"] for t in tags}),
            "reason": "；".join(item["reasons"]) or "-",
            "action": " / ".join(item["actions"]) or "自行判断",
        })
    return entries


def render_srt(entries: list[dict], vtt: bool = False) -> str:
    blocks = ["WEBVTT\n"] if vtt else []
    for i, entry in enumerate(entries, 1):
        blocks.append(
            f"{i}\n{format_timestamp(entry['start'], vtt)} --> {format_timestamp(entry['end'], vtt)}\n{entry['text']}\n"
        )
    return "\n".join(blocks)


def render_markdown(entries: list[dict], source: Path, total_cues: int) -> str:
    by_severity = {level: sum(1 for e in entries if e["top_severity"] == level) for level in SEVERITIES}
    lines = [
        f"# 发布前风险审查 —— {source.stem}",
        "",
        f"来源字幕：`{source.name}`　全片 {total_cues} 条，命中 {len(entries)} 处"
        f"（高 {by_severity['高']} / 中 {by_severity['中']} / 低 {by_severity['低']}）",
        "",
        "> 这是**机器初筛**，宁可多报不可漏报，**必须人工逐条确认**后再决定删改。",
        "",
        "| 时间 | 类别·严重度 | 原文 | 风险点 | 建议 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for entry in entries:
        body = entry["body"].replace("|", "/")
        reason = entry["reason"].replace("|", "/")
        lines.append(
            f"| {short_timestamp(entry['start'])} - {short_timestamp(entry['end'])} "
            f"| {entry['label']} | {body} | {reason} | {entry['action']} |"
        )
    if not entries:
        lines.append("| - | - | 未发现风险片段 | - | - |")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- 主流程


def build_review(subtitle: Path, context: str = "", use_llm: bool | None = None,
                 tagged: bool = True, output: Path | None = None) -> Path | None:
    cues = parse_subtitle(subtitle)
    if not cues:
        print(f"Error: 字幕解析为空: {subtitle}")
        return None
    print(f"读入 {len(cues)} 条字幕，总时长 {cues[-1]['end'] / 60000:.1f}min")

    if use_llm is None:
        use_llm = check_agent_available() is not None
    if not use_llm:
        print("⚠️  模型任务已关闭：风险审查完全依赖语义判断，规则模式做不了，跳过")
        return None

    # 默认整份丢给 agent；它若只审到一半会在 scanned_to 里说明，这里接着往下审，
    # 避免"看着审完了、其实只扫了前半段"这种假安全感
    items: list[dict] = []
    cursor = 0
    rounds = 0
    while cursor < len(cues) and rounds < 12 and not pending_handoff():
        rounds += 1
        indices = next_indices(cues, cursor)
        scope = "整份" if cursor == 0 and indices[-1] == len(cues) - 1 else \
            f"第 {cursor + 1}-{indices[-1] + 1} 条"
        print(f"  agent 审查{scope}（{len(indices)} 条字幕）...")
        found, scanned_to = analyze_batch(cues, indices, context)
        items += found

        if scanned_to is None:
            scanned_to = indices[-1]          # 没回报就按老行为当作审完了
        scanned_to = max(cursor, min(scanned_to, indices[-1]))
        if scanned_to < indices[-1] - 2:
            print(f"  Note: agent 只审到第 {scanned_to + 1} 条（共 {len(cues)} 条），继续审剩下的...")
        cursor = scanned_to + 1

    if pending_handoff():
        # 出完题就停：审查最怕「看着审完了、其实只扫了前半段」，宁可不出文件也不能出半份
        print(pending_report(resume_command()))
        return None

    if cursor < len(cues):
        reason = "重试次数用尽"
        print(f"  ⚠️  审查在第 {cursor} 条处中止（{reason}），后面 {len(cues) - cursor} 条**未覆盖**——"
              f"这份结果不完整，不能当作全片已审")

    merged = merge_ranges(items)
    entries = build_entries(merged, cues, tagged)

    flagged_cues = sum(item["cue_end"] - item["cue"] + 1 for item in merged)
    hit_ratio = flagged_cues / len(cues)
    if hit_ratio > SUSPICIOUS_HIT_RATIO:
        print(f"  Warning: 命中了 {hit_ratio:.0%} 的字幕，比例偏高，复核时留意是否误报过多")

    stem = subtitle.name[: -len(subtitle.suffix)]
    target = output or subtitle.with_name(f"{stem}.review{subtitle.suffix}")
    target.write_text(render_srt(entries, vtt=subtitle.suffix.lower() == ".vtt"), encoding="utf-8")
    md_path = subtitle.with_name(f"{stem}.review.md")
    md_path.write_text(render_markdown(entries, subtitle, len(cues)), encoding="utf-8")

    if entries:
        high = sum(1 for e in entries if e["top_severity"] == "高")
        print(f"Done! {len(entries)} 处风险片段（严重度高的 {high} 处）")
    else:
        print("Done! 未发现风险片段")
    print(f"审查字幕：{target}")
    print(f"复核清单：{md_path}")
    print("⚠️  这是机器初筛，必须人工逐条确认后再决定删改")
    return target


def main() -> int:
    parser = argparse.ArgumentParser(
        description="从正片字幕里摘出敏感信息 / 人身攻击 / 引起不适 / 冒犯性的片段，单独成一个字幕文件",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例:
  python make_review.py "/path/to/合集.srt"
  python make_review.py "/path/to/合集.srt" --context "客户访谈" --plain
""",
    )
    parser.add_argument("subtitle", help="正片字幕文件（.srt / .vtt）")
    parser.add_argument("-o", "--output", help="审查字幕输出路径（默认 <名字>.review.srt）")
    parser.add_argument("--context", default="", help="视频主题背景，帮助判断什么算敏感")
    parser.add_argument("--plain", action="store_true", help="字幕只放原文，不加【类别·严重度】前缀")
    parser.add_argument("--no-llm", action="store_true", help="不创建模型任务（本功能会直接跳过）")
    args = parser.parse_args()

    subtitle = Path(args.subtitle).expanduser().resolve()
    if not subtitle.is_file():
        print(f"Error: 字幕文件不存在: {subtitle}")
        return 1
    set_handoff_root(subtitle.parent / ".subtitle_tasks")

    result = build_review(
        subtitle,
        context=args.context,
        use_llm=False if args.no_llm else None,
        tagged=not args.plain,
        output=Path(args.output).expanduser().resolve() if args.output else None,
    )
    if result:
        return 0
    return EXIT_HANDOFF_PENDING if pending_handoff() else 1


if __name__ == "__main__":
    raise SystemExit(main())
