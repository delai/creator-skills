#!/usr/bin/env python3
"""字幕 LLM 任务只通过 handoff 交给当前会话处理。

任务写入 <输出目录>/.subtitle_tasks/，脚本以退出码 10 交接。
当前会话完成 TASK.md 指定的工作后，重跑命令读取结果并续跑。
本模块不会探测或启动外部模型命令，也不依赖会话环境的自动识别。

Environment Variables:
    SUBTITLE_HANDOFF_DIR    handoff 任务根目录
    SUBTITLE_LLM            是否启用模型任务：0 或 1（默认 1）
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import sys
from pathlib import Path

EXIT_HANDOFF_PENDING = 10


def llm_enabled() -> bool:
    return os.environ.get("SUBTITLE_LLM", "1") != "0"


def handoff_mode() -> bool:
    """模型任务启用时，始终使用 handoff。"""
    return llm_enabled()


def check_agent_available() -> str | None:
    """handoff 不依赖外部程序；仅检查模型任务是否启用。"""
    return "handoff" if llm_enabled() else None


# ---------------------------------------------------------------- handoff 后端
#
# 思路：脚本不当 LLM 的调用方，而是当**出题人**。把每个 LLM 任务摊成一个自包含的
# 任务目录（in.json + TASK.md），然后退出；正在调 skill 的模型读题、做题、把答案写回
# 同一个目录，再原样重跑同一条命令——已经有答案的任务直接复用，没答案的重新出题。
#
# 任务目录名带内容哈希，所以「重跑 = 续跑」是天然的：输入没变就命中旧目录，
# 输入变了（比如你手改了字幕）就是一道新题，不会拿旧答案糊弄。

_handoff_root: Path | None = None
_pending: list[dict] = []
_last_pending = False


def set_handoff_root(path) -> None:
    """脚本定完输出目录后调一次，把任务放到产物旁边而不是当前工作目录。"""
    global _handoff_root
    _handoff_root = Path(path)


def handoff_root() -> Path:
    env = os.environ.get("SUBTITLE_HANDOFF_DIR", "").strip()
    if env:
        return Path(env).expanduser()
    if _handoff_root is not None:
        return _handoff_root
    return Path.cwd() / ".subtitle_tasks"


def pending_handoff() -> bool:
    """这一轮有没有摊出去、还没人做的任务。"""
    return bool(_pending)


def last_call_pending() -> bool:
    """最近一次 run_agent_* 是不是因为「题刚出、还没答案」才返回 None。

    调用方靠它区分两种 None：出题（不该拆批重试，等下一轮就有答案了）
    和真失败（结果回来了但不合格，该拆批重问）。
    """
    return _last_pending


def pending_tasks() -> list[dict]:
    return list(_pending)


def resume_command(extra: list[str] | tuple[str, ...] = ()) -> str:
    """续跑命令：原样重跑本次调用，必要时补上几个参数（如 -o 锁定同一个项目目录）。"""
    parts = [shlex.quote(sys.executable)] + [shlex.quote(a) for a in sys.argv]
    parts += [shlex.quote(a) for a in extra]
    # 带上 cd：argv 里可能有相对路径，而调用方（模型）的 shell 每次都从别处起
    return f"cd {shlex.quote(os.getcwd())} && " + " ".join(parts)


def pending_report(resume: str | None = None) -> str:
    """给调用方（模型）看的待办清单。脚本退出前打出来，这就是交接的全部信息。"""
    lines = [
        "",
        "=" * 68,
        f"⏸  HANDOFF：{len(_pending)} 个任务等你处理（LLM 部分不外调，由当前会话现场做）",
        f"任务根目录：{handoff_root()}",
        "",
    ]
    for i, task in enumerate(_pending, 1):
        lines.append(f"  {i}. {task['label']}")
        lines.append(f"     {task['dir']}")
        if task.get("note"):
            lines.append(f"     ⚠️  {task['note']}")
    lines += [
        "",
        "怎么做：",
        "  1. 逐个读任务目录里的 TASK.md，按里面写的要求处理，结果写回**同一个目录**",
        "  2. 任务之间互不依赖；超过 2 个就并行分发给 subagent（一条消息里发多个），",
        "     每个 subagent 只需要拿到任务目录路径，让它自己读 TASK.md",
        "  3. 全部写完后重跑下面这条命令续跑",
        "     —— 已经有答案的任务会直接复用，不会重做；答案不合格的会重新出题",
    ]
    if resume:
        lines += ["", "续跑命令：", f"  {resume}"]
    lines += ["=" * 68, ""]
    return "\n".join(lines)


def _register_pending(task_dir: Path, label: str, note: str | None) -> None:
    global _last_pending
    _pending.append({"dir": str(task_dir), "label": label, "note": note})
    _last_pending = True
    suffix = f"（{note}）" if note else ""
    print(f"  📝 出题：{label} → {task_dir}{suffix}")


def _task_dir(label: str, instruction: str, body: str) -> Path:
    digest = hashlib.sha1(f"{instruction}\x00{body}".encode("utf-8")).hexdigest()[:8]
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", label).strip("-") or "task"
    return handoff_root() / f"{safe}-{digest}"


_TASK_HEADER = """# 任务：{label}

> 由 generate-subtitle skill 出题。**你（当前会话的模型或它派出的 subagent）就是执行者**，
> 不要再去调别的 CLI，也不要问用户，直接干活。

- 工作目录：`{dir}`
- 输入：{inputs}
- 输出：{outputs}
- 完成判定：{done}

规矩：
- 只在这个目录里读写，别碰目录外的文件；输入文件不要改（除非上面说了「就地改」）
- 做完就停，不用汇报中间过程；这个目录不要删，脚本重跑时要读它
- 拿不准的地方按下面的规则处理，宁可保守也不要臆造

---

{instruction}
"""


def _write_task_md(task_dir: Path, label: str, instruction: str,
                   inputs: str, outputs: str, done: str) -> None:
    (task_dir / "TASK.md").write_text(
        _TASK_HEADER.format(label=label, dir=task_dir, inputs=inputs,
                            outputs=outputs, done=done, instruction=instruction),
        encoding="utf-8",
    )


def _handoff_json_task(instruction: str, payload, raw: bool, label: str):
    """in.json → out.json 的数据协议任务。有答案就读回来，没答案就出题并返回 None。"""
    global _last_pending
    _last_pending = False

    body = json.dumps(payload, ensure_ascii=False, indent=2)
    task_dir = _task_dir(label, instruction, body)
    out_path = task_dir / "out.json"
    note = None

    if out_path.exists():
        parse = parse_json_payload if raw else parse_json_list
        data = parse(out_path.read_text(encoding="utf-8"))
        if data is not None:
            print(f"  ✅ 复用已完成任务：{label}")
            return data
        note = "已有的 out.json 解析不了（不是合法 JSON 或结构不对），请重写"

    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "in.json").write_text(body, encoding="utf-8")
    shape = ('一个 JSON 对象（形如 {"scanned_to": .., "items": [..]}）' if raw else "一个 JSON 数组")
    _write_task_md(
        task_dir, label, instruction,
        inputs="`in.json`（只读，别改它）",
        outputs=f"`out.json` —— {shape}，写在这个目录里",
        done="`out.json` 存在且是合法 JSON",
    )
    _register_pending(task_dir, label, note)
    return None


def _handoff_file_task(instruction: str, files: dict[str, str], read_back: str,
                       extra_reads: tuple[str, ...], label: str):
    """就地改文件的任务。用 DONE 标记判完成——文件改到一半和改完了，从外面是分不出来的。"""
    global _last_pending
    _last_pending = False

    body = json.dumps({k: files[k] for k in sorted(files)}, ensure_ascii=False)
    task_dir = _task_dir(label, instruction, body)
    target = task_dir / read_back
    done_flag = task_dir / "DONE"

    def read_result():
        content = target.read_text(encoding="utf-8") if target.exists() else None
        if not extra_reads:
            return content
        extras = tuple(
            (task_dir / name).read_text(encoding="utf-8") if (task_dir / name).exists() else None
            for name in extra_reads
        )
        return (content,) + extras

    if done_flag.exists() and target.exists():
        print(f"  ✅ 复用已完成任务：{label}")
        return read_result()

    note = None
    if target.exists():
        orig = task_dir / ".orig" / read_back
        if orig.exists() and orig.read_text(encoding="utf-8") != target.read_text(encoding="utf-8"):
            note = f"{read_back} 已经改过了但缺 DONE 标记——确认改完的话补写一个 DONE 文件即可"

    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / ".orig").mkdir(exist_ok=True)
    for name, content in files.items():
        (task_dir / ".orig" / name).write_text(content, encoding="utf-8")
        # 已经存在就别覆盖——那多半是上一轮改了一半的成果
        if not (task_dir / name).exists():
            (task_dir / name).write_text(content, encoding="utf-8")

    extra_line = ("；另外写 " + "、".join(f"`{n}`" for n in extra_reads)) if extra_reads else ""
    _write_task_md(
        task_dir, label, instruction,
        inputs=f"`{read_back}`（**就地改它**；原始副本在 `.orig/` 里，仅供对照，别动）",
        outputs=f"改好的 `{read_back}`{extra_line}",
        done="写一个 `DONE` 文件（内容随便，比如一句改动摘要）——没有它脚本会认为你还没做完",
    )
    _register_pending(task_dir, label, note)
    return (None,) * (1 + len(extra_reads)) if extra_reads else None


def _coerce_list(data) -> list | None:
    """agent 可能把数组包在对象里，这里兜一下常见的几种键名。"""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("cues", "items", "results", "result", "data", "highlights", "output"):
            value = data.get(key)
            if isinstance(value, list):
                return value
    return None


def _scan_json_lists(text: str) -> list | None:
    """逐个 '[' 试着解析，挑出最像载荷的那个 JSON 数组。

    agent 会把思考过程一起打到 stdout，里面可能出现方括号，
    所以不能简单地从第一个 '[' 贪婪匹配到最后一个 ']'。
    """
    decoder = json.JSONDecoder()
    best: list | None = None
    best_score = (-1, -1)
    for i, ch in enumerate(text):
        if ch != "[":
            continue
        try:
            data, _ = decoder.raw_decode(text[i:])
        except ValueError:
            continue
        if not isinstance(data, list):
            continue
        # 我们要的载荷元素全是对象；思考文字里的 [3]、[1,2] 一律排在后面
        score = (1 if data and all(isinstance(item, dict) for item in data) else 0, len(data))
        if score > best_score:
            best, best_score = data, score
    return best


def extract_json_block(text: str) -> str | None:
    """从输出中提取 ```json 代码块的内容；没有代码块时返回原文。"""
    if not text:
        return None
    match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return text.strip()


def parse_json_list(output: str | None) -> list | None:
    """把一段文本解析成 JSON 数组；失败返回 None。"""
    block = extract_json_block(output or "")
    if not block:
        return None
    try:
        data = _coerce_list(json.loads(block))
        if data is not None:
            return data
    except ValueError:
        pass
    return _scan_json_lists(block)


def parse_json_payload(output: str | None):
    """解析成 JSON 对象或数组，原样返回（不做数组归一）。"""
    block = extract_json_block(output or "")
    if not block:
        return None
    try:
        return json.loads(block)
    except ValueError:
        return _scan_json_lists(block)


def run_agent_task(instruction: str, payload, raw: bool = False,
                   label: str = "task"):
    """创建 JSON handoff 任务，或复用已完成的 out.json。

    raw=True 保留对象结构；否则解析成列表。待处理时返回 None，
    调用方通过 last_call_pending() 区分等待与结果不合格。
    """
    global _last_pending
    _last_pending = False
    if not llm_enabled():
        return None
    return _handoff_json_task(instruction, payload, raw=raw, label=label)


def run_agent_file_task(instruction: str, files: dict[str, str], read_back: str,
                        extra_reads: tuple[str, ...] = (),
                        label: str = "file-task") -> str | None | tuple:
    """创建就地编辑 handoff 任务，或在 DONE 后读回文件。

    调用方仍需验证字幕结构；等待时保持 extra_reads 对应的返回元数。
    """
    global _last_pending
    _last_pending = False
    if not llm_enabled():
        return (None,) * (1 + len(extra_reads)) if extra_reads else None
    return _handoff_file_task(instruction, files, read_back, extra_reads, label)


def load_glossary(value: str) -> list[str]:
    """术语表：可传文件路径（每行一个），也可传逗号/顿号分隔的字符串。"""
    value = (value or "").strip()
    if not value:
        return []
    try:
        path = Path(value).expanduser()
        if path.is_file():
            return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except OSError:
        pass
    return [term.strip() for term in re.split(r"[,，、\n]", value) if term.strip()]


def reject_task_result(instruction: str, payload, label: str, reason: str) -> None:
    """Keep invalid answers for diagnosis and reopen the exact content-addressed task."""
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    task_dir = _task_dir(label, instruction, body)
    answer = task_dir / "out.json"
    if answer.exists():
        answer.replace(task_dir / "out.invalid.json")
    _register_pending(task_dir, label, reason)
    with (task_dir / "TASK.md").open("a", encoding="utf-8") as stream:
        stream.write(f"\n校验未通过，请重写 out.json：{reason}\n")
