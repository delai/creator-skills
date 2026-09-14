#!/usr/bin/env python3
"""字幕结构校验：确认 agent 只改了文字，没动时间轴。

整份字幕交给 agent 直接改文件，速度和上下文都远好于分批，但风险集中在一处——
**时间码被动到**。所以必须有一道不依赖模型的确定性校验，这就是它。

检查项：
1. 时间码完整性——输出的每个时间码都必须在输入里存在，逐字符比对，且顺序递增
2. 条目账目——保留 / 删除 / 凭空出现（凭空出现 = 时间码被篡改或捏造）
3. 文字只减不增——润色只允许删字改错字，变长即可疑
4. 改动密度分布——全片切 10 段看每段改了多少，检测「前面认真后面摆烂」

单独用：
    python validate_subtitle.py 润色前.srt 润色后.srt
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

TIME_RE = re.compile(r"(\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})\s*-->\s*(\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})")

# 尾段改动密度低于全片均值的这个比例，就认为 agent 后半程摆烂了
LAZY_TAIL_RATIO = 0.35


def parse_text(raw: str) -> list[tuple[str, str]]:
    """解析字幕文本 → [(时间码原文, 文字)]。时间码保留原始字符串以便逐字对比。"""
    out = []
    for block in re.split(r"\n\s*\n", raw.strip()):
        lines = [x for x in block.strip().splitlines() if x.strip()]
        if not lines:
            continue
        idx = next((i for i, line in enumerate(lines) if TIME_RE.search(line)), None)
        if idx is None:
            continue
        out.append((TIME_RE.search(lines[idx]).group(0), " ".join(lines[idx + 1:]).strip()))
    return out


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text)


def check_structure(before: list[tuple[str, str]], after: list[tuple[str, str]],
                    grow_chars: int = 12, grow_total: float = 0.08) -> tuple[bool, str]:
    """返回 (是否通过, 人类可读的报告)。

    纠错会让文字**变长**（`cos` → `Cursor`、补中英文空格），所以不能一律禁止变长：
    单条允许长 grow_chars 个字符以内，全片总字数增幅不超过 grow_total
    （实测纯纠错一轮总增幅约 +0.3%，8% 已是很宽的头寸）。
    超出就说明 agent 在加内容，不是在纠错。
    """
    lines: list[str] = []
    b_map = dict(before)
    b_order = [t for t, _ in before]
    a_order = [t for t, _ in after]

    fabricated = [t for t in a_order if t not in b_map]
    deleted = [t for t in b_order if t not in set(a_order)]
    kept = [t for t in a_order if t in b_map]
    lines.append(f"  条目 {len(before)} → {len(after)}：保留 {len(kept)}、删除 {len(deleted)}、"
                 f"凭空出现或时间码被改 {len(fabricated)}")
    if fabricated[:3]:
        lines.append(f"  可疑时间码: {fabricated[:3]}")

    rank = {t: i for i, t in enumerate(b_order)}
    seq = [rank[t] for t in a_order if t in rank]
    ordered = all(x < y for x, y in zip(seq, seq[1:]))

    changed, bloated = [], []
    chars_before = chars_after = 0
    for t, txt in after:
        if t not in b_map:
            continue
        src, dst = _norm(b_map[t]), _norm(txt)
        chars_before += len(src)
        chars_after += len(dst)
        if dst != src:
            changed.append(t)
            # 纠错允许小幅变长（cos→Cursor），大幅变长说明在加内容
            if len(dst) - len(src) > grow_chars:
                bloated.append((b_map[t], txt))
    ratio = len(changed) / max(len(kept), 1)
    growth = (chars_after - chars_before) / max(chars_before, 1)
    lines.append(f"  文字改动 {len(changed)} 条（{ratio:.0%}），总字数 {growth:+.1%}，"
                 f"单条超量变长 {len(bloated)} 条，顺序{'递增' if ordered else '被打乱'}")
    for src, dst in bloated[:3]:
        lines.append(f"  超量变长: {src!r} → {dst!r}")
    over_total = growth > grow_total

    # 改动密度：尾段明显低于均值说明后半程没认真处理
    n = len(before)
    touched = set(changed) | set(deleted)
    deciles = []
    for k in range(10):
        seg = b_order[n * k // 10: n * (k + 1) // 10]
        deciles.append(sum(1 for t in seg if t in touched) / max(len(seg), 1))
    mean = sum(deciles) / len(deciles) if deciles else 0
    tail = sum(deciles[-3:]) / 3 if deciles else 0
    # 只在改动本来就密集时才判摆烂：纯纠错模式改动天然稀疏，尾段为 0 很正常
    lazy = mean > 0.15 and tail < mean * LAZY_TAIL_RATIO
    lines.append("  改动密度: " + " ".join(f"{d:.0%}" for d in deciles) +
                 (f"　⚠️ 尾段仅 {tail:.0%}，疑似后半程未处理" if lazy else ""))

    if over_total:
        lines.append(f"  ⚠️ 全片总字数增长 {growth:.1%}，超过阈值 {grow_total:.0%}，疑似在加内容而不是纠错")
    ok = not fabricated and not bloated and not over_total and ordered and not lazy
    return ok, "\n".join(lines)


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    before = parse_text(Path(sys.argv[1]).read_text(encoding="utf-8-sig"))
    after = parse_text(Path(sys.argv[2]).read_text(encoding="utf-8-sig"))
    ok, report = check_structure(before, after)
    print(report)
    print("结论：", "结构完好 ✅" if ok else "存在结构性损伤 ❌")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
