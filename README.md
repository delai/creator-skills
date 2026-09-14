<p align="center">
  <img src="assets/creator-skills-poster.png" alt="creator-skills — reusable skills for AI content creation" width="100%">
</p>

<p align="center">
  <img src="assets/logo/creator-skills-512.png" alt="creator-skills logo" width="320" height="320">
</p>

# creator-skills

English | [简体中文](README.zh-CN.md) — Every skill has functionally identical English and Chinese versions; only the language of the instructions and docs differs. This page covers the English versions.

Reusable skills for AI agents to create and edit subtitles, images, videos, covers, and other content.

## Skills

| Skill | Purpose and use cases | Details |
| --- | --- | --- |
| [generate-subtitle](skills/generate-subtitle/README.md) | **Purpose:** Gradually reach the point where **manual subtitle corrections are almost never needed** through automatic correction, formatting, and glossary building; supports track separation, translation, chapter titles and highlight captions, and review before publishing.<br>**Typical use cases:** Talking-head / tutorial subtitles, multi-video compilations, multi-person interview recordings, foreign-language subtitles, checks for sensitive information to redact | [Read more](skills/generate-subtitle/README.md) |

## Installation

Each skill is a complete folder containing `SKILL.md` and its supporting scripts, so install the whole folder. See each skill's documentation for dependency configuration. The examples below use `generate-subtitle`; pick any one method.

### Option 1: Ask your agent in one sentence (recommended)

Send this sentence to the agent you use (Claude Code, Codex, Cursor, and so on) and let it download and install the skill:

```text
Install the skill at https://github.com/delai/creator-skills/tree/main/skills/generate-subtitle: put the entire folder in your user-level skills directory, then check its dependencies against SKILL.md and tell me what is missing before installing anything.
```

### Option 2: skills CLI (works across agents)

[skills](https://github.com/vercel-labs/skills) installs skills for Claude Code, Codex, Cursor, Gemini CLI, GitHub Copilot, OpenCode, and other mainstream agents:

```bash
npx skills add delai/creator-skills/skills/generate-subtitle
```

It installs into the current project by default; add `-g` to install for your user, or `-a <agent>` (for example, `-a claude-code`) to choose the agent. Use `npx skills list` to see installed skills and `npx skills remove generate-subtitle` to uninstall.

### Option 3: GitHub CLI

```bash
gh skill install delai/creator-skills generate-subtitle
```

Use `--agent` (for example, `claude-code`) and `--scope user|project` to choose where it is installed.

### Option 4: Agent built-in installers

- **Codex**: in a session, enter `$skill-installer install https://github.com/delai/creator-skills/tree/main/skills/generate-subtitle`.
- **Gemini CLI**: run `gemini skills install https://github.com/delai/creator-skills.git --path skills/generate-subtitle`; use `--scope user|workspace` to choose the scope.

### Option 5: Copy manually

```bash
git clone https://github.com/delai/creator-skills.git
mkdir -p ~/.agents/skills
cp -R creator-skills/skills/generate-subtitle ~/.agents/skills/
```

To follow repository updates, replace `cp -R` with a symlink, `ln -s "$PWD/creator-skills/skills/generate-subtitle" ~/.agents/skills/`, then run `git pull` in the repository.

`~/.agents/skills/` is shared by most agents; Claude Code uses `~/.claude/skills/` instead. Skill directories by agent:

| Agent | User directory | Project directory |
| --- | --- | --- |
| Claude Code | `~/.claude/skills/` | `.claude/skills/` |
| Codex | `~/.agents/skills/` | `.agents/skills/` |
| Gemini CLI | `~/.gemini/skills/` or `~/.agents/skills/` | `.gemini/skills/` or `.agents/skills/` |
| Cursor | `~/.cursor/skills/` or `~/.agents/skills/` | `.cursor/skills/` or `.agents/skills/` |
| GitHub Copilot | `~/.copilot/skills/` or `~/.agents/skills/` | `.github/skills/` or `.agents/skills/` |
| OpenCode | `~/.config/opencode/skills/` or `~/.agents/skills/` | `.opencode/skills/` or `.agents/skills/` |

## Maintenance

English skills live in `skills/`, with Simplified Chinese versions in `skills-zh-CN/`. The skill tables and installation notes in this page and `README.zh-CN.md` are not handled by the sync script; update both by hand when a skill is added or its summary changes. After editing the English version, sync to Chinese with the default command:

```bash
python3 scripts/sync_skills.py
```

For the reverse direction after editing the Chinese version, add `--direction zh-to-en`:

```bash
python3 scripts/sync_skills.py --direction zh-to-en
```

The script copies shared code/resources and calls a separate `codex exec` agent to translate Markdown documents. The agent can read scripts, glossaries, and other shared files in a temporary workspace for context. After checks pass, the script writes the target files and records both language versions’ fingerprints in `.translation-state.json`. Unchanged pairs do not call Codex again, even when you switch direction. Commit the state file with the two language versions.

```bash
# Sync only one skill.
python3 scripts/sync_skills.py --skill generate-subtitle

# Check without model calls or file writes (suitable for CI).
python3 scripts/sync_skills.py --check

# Force a new translation after changing translation requirements.
python3 scripts/sync_skills.py --skill generate-subtitle --force
```

Install and sign in to Codex CLI first (`codex login`). By default the script uses your local Codex model configuration; `--model` optionally overrides it, and `--codex` selects the executable. Each agent has a 1,800-second timeout, adjustable with `--timeout`. Model calls use your configured account and may consume its allowance. See the [official non-interactive mode documentation](https://developers.openai.com/codex/noninteractive).

Translation instructions live in [scripts/translation_prompt.md](scripts/translation_prompt.md). The script detects shared-file differences, stale translation fingerprints, missing documents, basic heading/code-block mismatches, and missing command options or environment variables. These checks do not prove semantic translation accuracy; review the diff before publishing. All non-Markdown files inside skill folders are copied unchanged, including script comments, runtime messages, and glossary data. ZIP packages at the language-directory root are separate release artifacts: synchronization leaves them untouched, and they must be rebuilt separately when publishing.

Model failures and validation failures leave the existing target files untouched and retain a temporary workspace/log path for inspection. If source or target files change during translation, synchronization stops to avoid overwriting those edits. Target-only obsolete files are reported for explicit review/removal; the script never deletes them automatically. Only reviewed sharing copies belong in these directories; private originals and audit reports must stay outside the repository and translation context. Synchronization does not commit, push, or publish.
