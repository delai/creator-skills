#!/usr/bin/env python3
"""Sync skills between English and Simplified Chinese (default: English to Chinese)."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
SOURCE = "skills"
TARGET = "skills-zh-CN"
DIRECTIONS = {
    "en-to-zh": (SOURCE, TARGET, "English", "Simplified Chinese"),
    "zh-to-en": (TARGET, SOURCE, "Simplified Chinese", "English"),
}
STATE = ".translation-state.json"
PROMPT = Path(__file__).with_name("translation_prompt.md")
IGNORED_DIRS = {"__pycache__", ".subtitle_cache", ".subtitle_tasks", ".git"}


def inventory(folder):
    result = {}
    for path in folder.rglob("*"):
        relative = path.relative_to(folder)
        if any(part in IGNORED_DIRS for part in relative.parts):
            continue
        if path.name == ".DS_Store" or path.suffix in {".pyc", ".pyo"}:
            continue
        if path.is_symlink():
            raise ValueError(f"Symlinks are not supported in distribution files: {path}")
        if path.is_file():
            result[relative.as_posix()] = path.read_bytes()
    return result


def distribution_inventory(folder):
    """Root ZIPs are language-specific build artifacts, not shared skill resources."""
    return {p: data for p, data in inventory(folder).items()
            if not ("/" not in p and p.lower().endswith(".zip"))}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def group_for(path):
    return path.split("/", 1)[0] if "/" in path else "@root"


def source_hash(files, group, prompt):
    entries = {p: digest(data) for p, data in files.items() if group_for(p) == group}
    return digest(json.dumps({"files": entries, "prompt": digest(prompt)}, sort_keys=True).encode())


def translated_hashes(files, group):
    return {p: digest(data) for p, data in files.items()
            if group_for(p) == group and p.lower().endswith(".md")}


def pair_state(source_dir, source, target_dir, target, group, prompt):
    # A verified bilingual pair remains current when only the direction switches.
    return {"fingerprints": {
        source_dir: source_hash(source, group, prompt),
        target_dir: source_hash(target, group, prompt),
    }}


def read_state(root):
    path = root / STATE
    if not path.exists():
        return {"version": 2, "groups": {}}
    state = json.loads(path.read_text())
    if state.get("version") == 1 and isinstance(state.get("groups"), dict):
        # Legacy one-way fingerprints cannot establish a bidirectional pair.
        return {"version": 2, "groups": {}}
    if state.get("version") != 2 or not isinstance(state.get("groups"), dict):
        raise ValueError(f"Invalid translation state: {path}")
    return state


def structure(text):
    """Check basic completeness without depending on translated wording."""
    headings = []
    fences = []
    inside = None
    for line in text.splitlines():
        match = re.match(r"^\s*(`{3,}|~{3,})(.*)$", line)
        if match:
            marker, suffix = match.groups()
            if inside is None:
                inside = marker[0]
                fences.append(suffix.strip())
            elif marker[0] == inside:
                inside = None
            continue
        if inside is None:
            match = re.match(r"^(#{1,6}) ", line)
            if match:
                headings.append(len(match[1]))
    if inside:
        raise ValueError("Unclosed Markdown code fence")
    return headings, fences


def validate_translation(path, source, target):
    original = source.decode("utf-8")
    translated = target.decode("utf-8")
    if not translated.strip() or source == target:
        raise ValueError(f"Empty or untranslated document: {path}")
    if structure(original) != structure(translated):
        raise ValueError(f"Heading/code-block structure differs from source: {path}")
    # CLI options and environment variables are executable interfaces, not translatable prose.
    pattern = r"(?<![\w-])--[a-z][a-z0-9-]*|\bSUBTITLE_[A-Z_]+\b"
    missing = set(re.findall(pattern, original)) - set(re.findall(pattern, translated))
    if missing:
        raise ValueError(f"Missing command options/variables in {path}: {sorted(missing)}")
    if path.endswith("/SKILL.md"):
        for label, text in [("source", original), ("target", translated)]:
            front = re.match(r"\A---\s*\n(.*?)\n---\s*\n", text, re.S)
            if not front:
                raise ValueError(f"Missing {label} YAML frontmatter: {path}")
            name = re.search(r"^name:\s*['\"]?([^\s'\"]+)", front[1], re.M)
            if not name or name[1] != Path(path).parent.name:
                raise ValueError(f"Invalid {label} skill name: {path}")
            if not re.search(r"^description:\s*\S", front[1], re.M):
                raise ValueError(f"Missing {label} description: {path}")


def atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        handle.write(data)
        temporary = Path(handle.name)
    try:
        if path.exists():
            temporary.chmod(path.stat().st_mode & 0o777)
        else:
            temporary.chmod(0o644)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def run(args, root=ROOT):
    source_dir, target_dir, source_language, target_language = DIRECTIONS[args.direction]
    if not (root / source_dir).is_dir():
        raise ValueError(f"Missing source directory: {root / source_dir}")
    source = distribution_inventory(root / source_dir)
    target = distribution_inventory(root / target_dir)
    prompt = PROMPT.read_bytes()
    state = read_state(root)
    state_before = (root / STATE).read_bytes() if (root / STATE).exists() else None
    groups = sorted({group_for(p) for p in source.keys() | target.keys()})
    if args.skill:
        if args.skill not in groups:
            raise ValueError(f"Unknown skill: {args.skill}")
        groups = [args.skill]
    selected = lambda p: group_for(p) in groups
    extras = sorted(p for p in target.keys() - source.keys() if selected(p))
    if extras:
        raise ValueError(f"Target-only files need explicit review/removal: {', '.join(extras)}")
    shared = [p for p in source if selected(p) and not p.lower().endswith(".md")]
    changed = [p for p in shared if source[p] != target.get(p)]
    stale = []
    for group in groups:
        expected = pair_state(source_dir, source, target_dir, target, group, prompt)
        docs = {p for p in source if group_for(p) == group and p.lower().endswith(".md")}
        if args.force or not docs.issubset(target) or state["groups"].get(group) != expected:
            stale.append(group)
    if args.check:
        for path in changed:
            print(f"Shared file missing or different: {path}")
        for group in stale:
            print(f"Translation needs sync: {group}")
        if changed or stale:
            return 1
        print("OK: shared files and recorded translation fingerprints are current.")
        return 0
    if not changed and not stale:
        print("Already synchronized; no Codex calls needed.")
        return 0
    codex = shutil.which(args.codex)
    if not codex:
        raise ValueError(f"Codex CLI not found: {args.codex}; install it and run codex login first")

    stage = Path(tempfile.mkdtemp(prefix="creator-skills-translate-"))
    print(f"Translation workspace: {stage}", flush=True)
    success = False
    try:
        for folder, files in [(source_dir, source), (target_dir, target)]:
            for path, data in files.items():
                destination = stage / folder / path
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(data)
        for homepage in ("README.md", "README.zh-CN.md"):
            if (root / homepage).exists():
                shutil.copy2(root / homepage, stage / homepage)
        for path in changed:
            destination = stage / target_dir / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(source[path])
        schema = {"type": "object", "properties": {
            "translated": {"type": "array", "items": {"type": "string"}},
            "notes": {"type": "array", "items": {"type": "string"}}},
            "required": ["translated", "notes"], "additionalProperties": False}
        schema_path = stage / "response-schema.json"
        schema_path.write_text(json.dumps(schema))
        for index, group in enumerate(stale):
            docs = sorted(p for p in source if group_for(p) == group and p.lower().endswith(".md"))
            if not docs:
                continue
            before = inventory(stage)
            output = stage / f"result-{index}.json"
            log = stage / f"codex-{index}.log"
            pairs = [{"source": f"{source_dir}/{p}", "target": f"{target_dir}/{p}"} for p in docs]
            context = (f"Direction: {args.direction}\nSource language: {source_language}\n"
                       f"Target language: {target_language}\nSource directory: {source_dir}/\n"
                       f"Target directory: {target_dir}/\n")
            task = context + prompt.decode() + "\nTranslate these files:\n" + json.dumps(pairs, ensure_ascii=False, indent=2)
            command = [codex, "exec", "--cd", str(stage), "--skip-git-repo-check",
                       "--sandbox", "workspace-write", "--ephemeral", "--color", "never",
                       "--output-schema", str(schema_path), "--output-last-message", str(output)]
            if args.model:
                command += ["--model", args.model]
            command.append("-")
            print(f"Translating {group} ({args.direction}): {len(docs)} document(s); log: {log}", flush=True)
            with log.open("w") as stream:
                process = subprocess.run(command, input=task, text=True, stdout=stream,
                                         stderr=subprocess.STDOUT, timeout=args.timeout)
            if process.returncode:
                raise ValueError(f"Codex exited with {process.returncode}; see {log}")
            response = json.loads(output.read_text())
            expected_paths = {f"{target_dir}/{p}" for p in docs}
            if set(response.get("translated", [])) != expected_paths:
                raise ValueError(f"Codex did not confirm every requested document: {output}")
            after = inventory(stage)
            allowed = expected_paths | {output.name, log.name}
            unexpected = [p for p in before.keys() | after.keys()
                          if before.get(p) != after.get(p) and p not in allowed]
            if unexpected:
                raise ValueError(f"Translator changed unlisted files: {unexpected}")
            for path in docs:
                validate_translation(path, source[path], after[f"{target_dir}/{path}"])
            for note in response.get("notes", []):
                print(f"Translation note: {note}", flush=True)
        translated = distribution_inventory(stage / target_dir)
        # Do not overwrite edits made by a person while the model was running.
        current_state = (root / STATE).read_bytes() if (root / STATE).exists() else None
        if (distribution_inventory(root / source_dir) != source or distribution_inventory(root / target_dir) != target
                or current_state != state_before or PROMPT.read_bytes() != prompt):
            raise ValueError("Repository changed during translation; staged results retained. Rerun sync.")
        for path, data in translated.items():
            if selected(path) and target.get(path) != data:
                atomic_write(root / target_dir / path, data)
                if not path.lower().endswith(".md"):
                    shutil.copymode(root / source_dir / path, root / target_dir / path)
        for group in groups:
            state["groups"][group] = pair_state(source_dir, source, target_dir, translated, group, prompt)
        atomic_write(root / STATE, (json.dumps(state, indent=2, sort_keys=True) + "\n").encode())
        success = True
        print(f"Synchronized {len(changed)} shared file(s) and {len(stale)} translation group(s).")
        return 0
    finally:
        if success:
            shutil.rmtree(stage)
        else:
            print(f"Sync did not complete. Inspect staged results and logs: {stage}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--direction", choices=DIRECTIONS, default="en-to-zh",
                        help="Translation direction (default: en-to-zh); use zh-to-en after editing Chinese")
    parser.add_argument("--check", action="store_true", help="Check without writing files or calling Codex")
    parser.add_argument("--skill", help="Sync only the named skill (default: all skills)")
    parser.add_argument("--force", action="store_true", help="Retranslate even when fingerprints match")
    parser.add_argument("--codex", default="codex", help="Codex CLI executable")
    parser.add_argument("--model", help="Optional model override; otherwise use the local Codex default")
    parser.add_argument("--timeout", type=int, default=1800, help="Maximum seconds per translation agent (default: 1800)")
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    try:
        return run(args)
    except (ValueError, OSError, subprocess.TimeoutExpired) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted; sync was not marked complete.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
