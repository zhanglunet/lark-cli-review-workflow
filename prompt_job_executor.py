#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
from pathlib import Path

from docx import Document  # type: ignore
from openpyxl import load_workbook  # type: ignore

try:
    from PyPDF2 import PdfReader  # type: ignore
except Exception:  # pragma: no cover
    PdfReader = None


def read_prompt(prompt_path: Path) -> str:
    text = prompt_path.read_text(encoding="utf-8")
    parts = text.split("\n\n", 1)
    return parts[1].strip() if len(parts) > 1 else text.strip()


def read_text_file(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def read_docx(path: Path) -> str:
    doc = Document(str(path))
    return "\n".join(p.text for p in doc.paragraphs if p.text.strip())


def read_xlsx(path: Path) -> str:
    wb = load_workbook(filename=str(path), data_only=True, read_only=True)
    lines: list[str] = []
    for sheet in wb.worksheets:
        lines.append(f"# Sheet: {sheet.title}")
        for row in sheet.iter_rows(values_only=True):
            values = ["" if value is None else str(value) for value in row]
            if any(values):
                lines.append(" | ".join(values))
        lines.append("")
    return "\n".join(lines).strip()


def read_csv_like(path: Path) -> str:
    lines: list[str] = []
    with path.open("r", encoding="utf-8", errors="ignore", newline="") as fh:
        reader = csv.reader(fh)
        for row in reader:
            lines.append(" | ".join(row))
    return "\n".join(lines)


def read_pdf(path: Path) -> str:
    if PdfReader is None:
        return f"[PDF parser unavailable for {path.name}]"
    reader = PdfReader(str(path))
    parts: list[str] = []
    for page in reader.pages:
        parts.append(page.extract_text() or "")
    return "\n".join(parts).strip()


def read_source(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".txt", ".md"}:
        return read_text_file(path)
    if suffix == ".json":
        return json.dumps(json.loads(read_text_file(path)), ensure_ascii=False, indent=2)
    if suffix in {".csv", ".tsv"}:
        return read_csv_like(path)
    if suffix == ".docx":
        return read_docx(path)
    if suffix in {".xlsx", ".xlsm"}:
        return read_xlsx(path)
    if suffix == ".pdf":
        return read_pdf(path)
    return f"[Unsupported file type: {path.name}]"


def build_context(input_dir: Path, max_chars: int) -> tuple[str, list[dict[str, str]]]:
    sections: list[str] = []
    files_meta: list[dict[str, str]] = []
    used = 0
    for path in sorted(p for p in input_dir.rglob("*") if p.is_file()):
        content = read_source(path).strip()
        files_meta.append({"name": path.name, "path": str(path)})
        if not content:
            continue
        block = f"## Source File: {path.name}\n\n{content}\n"
        if used + len(block) > max_chars:
            remaining = max(0, max_chars - used)
            if remaining > 0:
                sections.append(block[:remaining])
            break
        sections.append(block)
        used += len(block)
    return "\n".join(sections).strip(), files_meta


def run_openai_compatible(api_base: str, api_key: str, model: str, prompt: str, source_context: str) -> str:
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "You are a careful analyst. Use the provided source files as the primary evidence base. Produce a structured, high-quality result in Markdown.",
            },
            {
                "role": "user",
                "content": f"Prompt:\n{prompt}\n\nSource materials:\n{source_context}",
            },
        ],
        "temperature": 0.2,
    }
    proc = subprocess.run(
        [
            "curl",
            "-sS",
            api_base.rstrip("/") + "/chat/completions",
            "-H",
            f"Authorization: Bearer {api_key}",
            "-H",
            "Content-Type: application/json",
            "-d",
            json.dumps(payload, ensure_ascii=False),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "curl failed")
    data = json.loads(proc.stdout)
    if "error" in data:
        raise RuntimeError(json.dumps(data["error"], ensure_ascii=False))
    return data["choices"][0]["message"]["content"].strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="Execute prompt jobs with attached source files")
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--inputs", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--provider", default=os.getenv("PROMPT_EXECUTOR_PROVIDER", "openrouter"))
    parser.add_argument("--model", default=os.getenv("PROMPT_EXECUTOR_MODEL", "anthropic/claude-3.5-sonnet"))
    parser.add_argument("--max-source-chars", type=int, default=30000)
    args = parser.parse_args()

    prompt_path = Path(args.prompt)
    input_dir = Path(args.inputs)
    output_dir = Path(args.output)
    manifest_path = Path(args.manifest)
    output_dir.mkdir(parents=True, exist_ok=True)

    prompt = read_prompt(prompt_path)
    source_context, files_meta = build_context(input_dir, args.max_source_chars)
    if not source_context:
        source_context = "[No readable source files were found.]"

    provider = args.provider.lower()
    api_key = ""
    api_base = ""
    if provider == "openrouter":
        api_key = os.getenv("OPENROUTER_API_KEY", "")
        api_base = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    elif provider == "openai":
        api_key = os.getenv("OPENAI_API_KEY", "")
        api_base = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    else:
        raise RuntimeError(f"unsupported provider: {provider}")

    if not api_key:
        raise RuntimeError(f"missing API key for provider {provider}")

    result_text = run_openai_compatible(api_base, api_key, args.model, prompt, source_context)

    result_md = output_dir / "result.md"
    result_md.write_text(result_text + "\n", encoding="utf-8")

    metadata = {
        "provider": provider,
        "model": args.model,
        "prompt_path": str(prompt_path),
        "manifest_path": str(manifest_path),
        "source_files": files_meta,
        "result_path": str(result_md),
    }
    (output_dir / "result.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
