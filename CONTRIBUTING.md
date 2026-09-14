# Contributing and maintenance

English | [简体中文](CONTRIBUTING.zh-CN.md)

English skills live in `skills/`, with Simplified Chinese versions in `skills-zh-CN/`. The skill tables and installation notes in `README.md` and `README.zh-CN.md` are not handled by the sync script; update both by hand when a skill is added or its summary changes. After editing the English version, sync to Chinese with the default command:

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
