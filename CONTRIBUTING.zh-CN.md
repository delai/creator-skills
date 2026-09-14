# 贡献与维护

[English](CONTRIBUTING.md) | 简体中文

英文版位于 `skills/`，简体中文版位于 `skills-zh-CN/`。`README.md` 与 `README.zh-CN.md` 中的技能列表和安装说明不由同步脚本处理，新增 skill 或修改其简介时需手动同时更新两份。修改英文版后，默认同步到中文：

```bash
python3 scripts/sync_skills.py
```

修改中文版后，如需反向同步到英文，加上 `--direction zh-to-en`：

```bash
python3 scripts/sync_skills.py --direction zh-to-en
```

脚本复制共享代码与资源，并调用独立的 `codex exec` Agent 翻译 Markdown 文档。Agent 可读取临时工作区里的脚本、术语表及其他共享文件理解上下文。检查通过后写回目标文件，并将两个语言版本的指纹记录到 `.translation-state.json`。文件没有变化时，即使切换同步方向也不会重复调用 Codex。状态文件应与两种语言版本一起提交。

```bash
# 只同步指定 skill。
python3 scripts/sync_skills.py --skill generate-subtitle

# 仅检查，不调用模型、不写文件，可用于 CI。
python3 scripts/sync_skills.py --check

# 需要重新翻译时强制运行。
python3 scripts/sync_skills.py --skill generate-subtitle --force
```

先安装并登录 Codex CLI（`codex login`）。默认沿用本机模型配置，可用 `--model` 指定模型、`--codex` 指定可执行文件。每个 Agent 默认超时 1,800 秒，可用 `--timeout` 调整。模型调用使用你的已配置账号，并可能消耗相应额度。参见[官方非交互模式文档](https://developers.openai.com/codex/noninteractive)。

翻译规则见 [scripts/translation_prompt.md](scripts/translation_prompt.md)。脚本检查共享文件差异、翻译指纹是否过期、缺失文档、基本标题与代码块结构、命令参数和环境变量遗漏。这些检查不能证明语义翻译准确，发布前仍应审阅 diff。Skill 文件夹内的非 Markdown 文件都原样复制，包括脚本注释、运行提示和术语数据。语言目录根部的 ZIP 安装包属于单独的发布产物，同步脚本保留它们不变；发布时需另外重新打包。

模型调用或校验失败时保留现有目标文件，并报告临时工作区与日志路径供检查。翻译期间若源文件或目标文件被修改，会停止同步，避免覆盖新改动。目标目录独有的旧文件需审阅后手动删除，脚本不会自动删除。目录中只放已审查的共享副本；私有原件和审查报告应保留在仓库及翻译上下文之外。同步不会自动提交、推送或发布。
