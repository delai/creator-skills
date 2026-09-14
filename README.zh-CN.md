<p align="center">
  <img src="assets/creator-skills-poster.png" alt="creator-skills — AI 内容创作技能海报" width="100%">
</p>

<p align="center">
  <img src="assets/logo/creator-skills-512.png" alt="creator-skills logo" width="320" height="320">
</p>

# creator-skills

[English](README.md) | 简体中文 —— 每个 skill 都有功能完全相同的英文版和中文版，唯一区别是指令与说明文档的语言。本页介绍中文版。

用于音视频字幕、图片、视频、封面等内容创作的可复用 AI Agent skills。

## 技能列表

| Skill | 用途与场景 | 详细介绍 |
| --- | --- | --- |
| [generate-subtitle](skills-zh-CN/generate-subtitle/README.md) | **用途**：通过自动纠错、排版和术语积累，逐步做到**几乎不再需要手动修正字幕**；支持分轨、翻译、章节花字和发布前复核。<br>**典型使用场景**：口播 / 教程字幕、多段视频合集、多人访谈录音、外语字幕、脱敏检查 | [查看详情](skills-zh-CN/generate-subtitle/README.md) |

## 安装

每个 skill 都是一个完整文件夹，包含 `SKILL.md` 和配套脚本，需要整个文件夹一起安装。依赖配置见各 skill 的说明。下面以 `generate-subtitle` 为例，任选一种方式即可。

### 方式一：一句话让 Agent 安装（推荐）

把下面这句话发给你正在用的 Agent（Claude Code、Codex、Cursor 等均可），由它完成下载和安装：

```text
帮我安装 https://github.com/delai/creator-skills/tree/main/skills-zh-CN/generate-subtitle 这个 skill：把整个文件夹放进你的用户级 skills 目录，再按 SKILL.md 检查依赖，缺什么先告诉我。
```

### 方式二：skills CLI（跨 Agent 通用）

[skills](https://github.com/vercel-labs/skills) 可以为 Claude Code、Codex、Cursor、Gemini CLI、GitHub Copilot、OpenCode 等主流 Agent 安装 skill：

```bash
npx skills add delai/creator-skills/skills-zh-CN/generate-subtitle
```

默认安装到当前项目；加 `-g` 安装到用户目录，加 `-a <agent>`（如 `-a claude-code`）指定 Agent。用 `npx skills list` 查看已安装的 skill，用 `npx skills remove generate-subtitle` 卸载。

### 方式三：GitHub CLI

```bash
gh skill install delai/creator-skills skills-zh-CN/generate-subtitle/SKILL.md
```

可用 `--agent`（如 `claude-code`）和 `--scope user|project` 指定安装位置。

### 方式四：Agent 自带的安装命令

- **Codex**：在会话中输入 `$skill-installer install https://github.com/delai/creator-skills/tree/main/skills-zh-CN/generate-subtitle`。
- **Gemini CLI**：运行 `gemini skills install https://github.com/delai/creator-skills.git --path skills-zh-CN/generate-subtitle`，可用 `--scope user|workspace` 选择安装范围。

### 方式五：手动复制

```bash
git clone https://github.com/delai/creator-skills.git
mkdir -p ~/.agents/skills
cp -R creator-skills/skills-zh-CN/generate-subtitle ~/.agents/skills/
```

想随仓库更新时，可把 `cp -R` 换成软链接 `ln -s "$PWD/creator-skills/skills-zh-CN/generate-subtitle" ~/.agents/skills/`，之后在仓库里 `git pull` 即可。

`~/.agents/skills/` 是多数 Agent 共用的目录，Claude Code 则需放到 `~/.claude/skills/`。各 Agent 读取 skill 的目录如下：

| Agent | 用户级目录 | 项目级目录 |
| --- | --- | --- |
| Claude Code | `~/.claude/skills/` | `.claude/skills/` |
| Codex | `~/.agents/skills/` | `.agents/skills/` |
| Gemini CLI | `~/.gemini/skills/` 或 `~/.agents/skills/` | `.gemini/skills/` 或 `.agents/skills/` |
| Cursor | `~/.cursor/skills/` 或 `~/.agents/skills/` | `.cursor/skills/` 或 `.agents/skills/` |
| GitHub Copilot | `~/.copilot/skills/` 或 `~/.agents/skills/` | `.github/skills/` 或 `.agents/skills/` |
| OpenCode | `~/.config/opencode/skills/` 或 `~/.agents/skills/` | `.opencode/skills/` 或 `.agents/skills/` |

## 维护方式

英文版位于 `skills/`，简体中文版位于 `skills-zh-CN/`。本页与 `README.md` 中的技能列表和安装说明不由同步脚本处理，新增 skill 或修改其简介时需手动同时更新两份。修改英文版后，默认同步到中文：

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
