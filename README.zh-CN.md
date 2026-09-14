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
