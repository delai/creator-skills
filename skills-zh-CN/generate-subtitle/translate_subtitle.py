#!/usr/bin/env python3
"""Translate a corrected SRT/VTT through resumable handoff, without retranscription."""
from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
from pathlib import Path

from llm import (EXIT_HANDOFF_PENDING, check_agent_available, pending_report,
                 run_agent_task, set_handoff_root, reject_task_result)

TIME_LINE = re.compile(r"(?P<a>(?:\d{2,}:)?\d{2}:\d{2}[,.]\d{3}) --> (?P<b>(?:\d{2,}:)?\d{2}:\d{2}[,.]\d{3})(?:[^\r\n]*)$")
# Preserve literal spkN labels and VTT voice annotations. Other labels stay in
# immutable markup or must be kept by the handoff (e.g. a person's name).
MARKERS = re.compile(r"spk\d+\s*[:：]\s*|<[^>]+>|\{[^}]+\}|^\[[^\]\n]+\]\s*|^[^\W\d_][\w .'-]{0,40}[:：]\s*", re.MULTILINE)


def target_code(value: str) -> str:
    aliases = {"english": "en", "英文": "en", "chinese": "zh", "中文": "zh", "japanese": "ja"}
    value = aliases.get(value.strip().lower(), value.strip().lower())
    if value == "auto" or not re.fullmatch(r"[a-z]{2,3}(?:-[a-z0-9]{2,8})*", value):
        raise ValueError("--translate-to 需明确目标语言代码（如 en、ja、zh-hant），不能用 auto")
    return value


def stamp_ms(value):
    fields = list(map(int, re.split(r"[:,.]", value)))
    h, m, s, ms = fields if len(fields) == 4 else [0, *fields]
    if m >= 60 or s >= 60:
        raise ValueError(f"无效时间码：{value}")
    return ((h * 60 + m) * 60 + s) * 1000 + ms


def parse_document(raw: str):
    # Keep separators, cue identifiers, timing settings, NOTE/STYLE and line breaks.
    parts = re.split(r"(\r?\n[ \t]*\r?\n)", raw)
    cues = []
    for pos in range(0, len(parts), 2):
        lines = parts[pos].splitlines(keepends=True)
        if lines and lines[0].lstrip('\ufeff').startswith(("NOTE", "STYLE", "REGION", "WEBVTT")):
            continue
        indices = [i for i, line in enumerate(lines) if TIME_LINE.fullmatch(line.rstrip('\r\n'))]
        if not indices:
            if "-->" in parts[pos]:
                raise ValueError("无法解析的时间码行；请先修复字幕格式")
            continue
        if len(indices) != 1 or indices[0] > 1:
            raise ValueError("每个字幕条目必须只有一条时间码，前面至多一行编号")
        index = indices[0]
        match = TIME_LINE.fullmatch(lines[index].rstrip('\r\n'))
        if stamp_ms(match['b']) <= stamp_ms(match['a']):
            raise ValueError("字幕必须有正时长")
        header = ''.join(lines[:index + 1])
        text = ''.join(lines[index + 1:])
        if not text.strip():
            raise ValueError("字幕有空白条目，请先确认原文")
        cues.append({"id": len(cues), "part": pos, "header": header, "text": text,
                     "markers": MARKERS.findall(text)})
    if not cues:
        raise ValueError("字幕中没有可翻译的条目")
    return parts, cues


def translate_file(source: Path, language: str, output: Path | None = None, dry_run=False) -> int:
    language = target_code(language)
    output = output or source.with_name(f"{source.stem}.{language}{source.suffix}")
    if source.resolve() == output.resolve():
        raise ValueError("译文不能覆盖原字幕，请使用另一个文件名")
    if output.suffix.lower() != source.suffix.lower():
        raise ValueError("翻译保持源字幕格式，输出扩展名必须与源文件一致")
    uncertain = source.with_name(f"{source.stem}.uncertain{source.suffix}")
    if uncertain.exists() and uncertain.read_text(encoding="utf-8-sig").strip():
        print(f"⏸ 请先人工确认 {uncertain}，改好 {source.name}，将已处理的存疑清单移走或清空，再重跑本命令。")
        return 2
    raw = source.read_bytes().decode("utf-8")
    parts, cues = parse_document(raw)
    if dry_run:
        print(f"翻译 {len(cues)} 条 → {output}（保留原文及全部时间码/编号/标记）")
        return 0
    if not check_agent_available():
        raise ValueError("翻译需要 handoff；不能使用 --no-llm 或 SUBTITLE_LLM=0")
    set_handoff_root(source.parent / ".subtitle_tasks")
    instruction = (
        f"将 in.json 的字幕文字翻译为 {language}，只输出 out.json。原语言纠错已完成，不再润色原文。\n"
        "返回一个数组，逐条包含 id、text；保留全部 id，不增删、合并或拆分条目。\n"
        "text 只翻译台词，逐字保留说话人标记（spkN:、姓名:、[姓名]）、HTML/VTT 标签及各说话人的换行。\n"
        "专有名词和代码按原文保留；背景或指令的语言不改变翻译目标。\n"
        "遇到原文含糊、无法确定原意时不要猜，返回该条原文并加 uncertain 字段说明原因，等待人工确认。\n"
        "不要编造时间码、编号或额外说明，不要在 text 中插入空行。\n"
    )
    # Chunked tasks retain complete structure in the script; content hashes enable resume.
    translated = []
    waiting = False
    for offset in range(0, len(cues), 100):
        batch = cues[offset:offset + 100]
        payload = {"target_language": language, "cues": [{"id": c['id'], "text": c['text']} for c in batch]}
        label = f"translate-{language}-{offset + 1}-{offset + len(batch)}"
        data = run_agent_task(instruction, payload, label=label)
        if data is None:
            waiting = True
            continue
        error = None
        if (not isinstance(data, list) or len(data) != len(batch)
                or any(not isinstance(x, dict) for x in data)
                or [x.get('id') for x in data] != [c['id'] for c in batch]):
            error = "条目数量、id 或顺序有误"
        else:
            for cue, item in zip(batch, data):
                text = item.get('text')
                if item.get('uncertain'):
                    report = source.with_name(f"{source.stem}.translation-{language}.uncertain.json")
                    report.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
                    print(f"⏸ 翻译存疑，先人工确认：{report}；确认后修正该任务 out.json 并移除 uncertain，再续跑。")
                    return 2
                if (not isinstance(text, str) or not text.strip() or re.search(r'\n\s*\n', text)
                        or '-->' in text or MARKERS.findall(text) != cue['markers']
                        or len(text.strip().splitlines()) != len(cue['text'].strip().splitlines())):
                    error = "文字为空、增加条目/时间码、换行或说话人/格式标记被更改"
                    break
        if error:
            reject_task_result(instruction, payload, label, error)
            waiting = True
        else:
            translated.extend(data)
    if waiting:
        # Resume translation directly against the corrected source, never re-run ASR.
        resume = shlex.join([sys.executable, str(Path(__file__).resolve()), str(source.resolve()),
                             "--translate-to", language, "-o", str(output.resolve())])
        print(pending_report(resume))
        return EXIT_HANDOFF_PENDING
    for cue, item in zip(cues, translated):
        newline = '\r\n' if '\r\n' in cue['header'] else '\n'
        ending = newline if cue['text'].endswith(('\n', '\r')) else ''
        parts[cue['part']] = cue['header'] + newline.join(item['text'].strip().splitlines()) + ending
    result = ''.join(parts)
    _, check = parse_document(result)
    if [(c['header'], c['markers']) for c in cues] != [(c['header'], c['markers']) for c in check]:
        raise ValueError("译文结构校验失败，未写出")
    # Source bytes remain untouched; use replace so partial writes are not delivered.
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_name(output.name + '.tmp')
    tmp.write_bytes(result.encode('utf-8'))
    tmp.replace(output)
    source.with_name(f"{source.stem}.translation-{language}.uncertain.json").unlink(missing_ok=True)
    print(f"翻译完成：{output}（{len(cues)} 条，原字幕保留，编号/时间码/说话人标记校验通过）")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('--translate-to', required=True)
    parser.add_argument('-o', '--output', type=Path)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    try:
        return translate_file(args.source.expanduser(), args.translate_to, args.output, args.dry_run)
    except (ValueError, OSError) as exc:
        print(f"Error: {exc}")
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
