# Repository maintenance

- The root `README.md` is the default English homepage; `README.zh-CN.md` is its Simplified Chinese counterpart. Keep their content aligned and include language-switch links in both files. They also hold the skill list and installation notes (there is no per-language index README); the sync script does not touch them, so update both by hand when a skill is added or its summary changes.
- `skills/` is the default English distribution; `skills-zh-CN/` contains Simplified Chinese versions. The sync direction defaults to English → Chinese. Each skill keeps the same folder name and frontmatter `name` in both directories.
- After English edits, run `python3 scripts/sync_skills.py --skill <name>`. After Chinese edits, run `python3 scripts/sync_skills.py --direction zh-to-en --skill <name>`. Use the command without `--skill` for a full sync.
- The script copies non-Markdown resources and invokes Codex CLI to translate Markdown with shared-file context. Translation instructions are in `scripts/translation_prompt.md`. Keep non-Markdown implementation files identical across both languages; the chosen direction determines which copy is the source.
- Run `python3 scripts/sync_skills.py --check` afterward. Include `.translation-state.json` with both language versions. Fingerprint and structure checks do not establish semantic translation accuracy; inspect the translation diff before publishing.
- Only reviewed public-sharing copies belong in either skill directory. Keep private originals, audit reports, and approval records outside this repository and translation workspaces.
- If translation fails, report the incomplete target-language synchronization and retained log/workspace path. Do not mark the bilingual update complete. Review obsolete target-only files before removing them.
- Preserve unrelated working-tree edits. Synchronization alone does not authorize committing, pushing, or publishing.
