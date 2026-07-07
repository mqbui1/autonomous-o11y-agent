"""
Source code access tools for the performance specialist.

Access priority:
  1. SOURCE_ROOT env var — mounted directory (e.g. /src in container, ~/code locally)
     Expected layout: SOURCE_ROOT/<service-name>/... or SOURCE_ROOT/... (monorepo)
  2. GITHUB_TOKEN + GITHUB_REPO — GitHub API (fetches file contents by path)
  3. Neither set → returns source_available=False with profiling-only guidance

When source is available, the performance specialist can:
  - Read the exact function causing a hotspot
  - See the full call context around a slow line
  - Generate a concrete diff / suggested fix
  - Reference exact file:line in the remediation payload

Without source, the specialist still provides file:line:function from profiling frames
and pattern-based fix descriptions (e.g. "replace per-item query at repository.py:247
with bulk SELECT WHERE id IN (...)") — actionable enough for a developer to apply.
"""

import json
import logging
import os
import urllib.error
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

_SOURCE_ROOT = os.environ.get("SOURCE_ROOT", "")
_GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
_GITHUB_REPO = os.environ.get("GITHUB_REPO", "")   # e.g. "myorg/myrepo"
_GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")

# Max lines to return from a single read to avoid flooding the LLM context
_MAX_LINES = 120


def _source_mode() -> str:
    if _SOURCE_ROOT and Path(_SOURCE_ROOT).exists():
        return "local"
    if _GITHUB_TOKEN and _GITHUB_REPO:
        return "github"
    return "none"


def get_source_status() -> str:
    """
    Report whether source code access is configured and which mode is active.
    Call this before using other source tools to know what's available.
    """
    mode = _source_mode()
    if mode == "local":
        root = Path(_SOURCE_ROOT)
        # List top-level service directories
        try:
            entries = [p.name for p in sorted(root.iterdir()) if p.is_dir()][:20]
        except Exception:
            entries = []
        return json.dumps({
            "source_available": True,
            "mode": "local",
            "source_root": _SOURCE_ROOT,
            "top_level_dirs": entries,
            "note": "Read source files with get_source_context(file_path, line, service).",
        }, indent=2)
    if mode == "github":
        return json.dumps({
            "source_available": True,
            "mode": "github",
            "repo": _GITHUB_REPO,
            "branch": _GITHUB_BRANCH,
            "note": "GitHub API access configured. Use get_source_context with the file path from profiling frames.",
        }, indent=2)
    return json.dumps({
        "source_available": False,
        "mode": "none",
        "note": (
            "No source code access configured. Set SOURCE_ROOT (mounted repo path) or "
            "GITHUB_TOKEN + GITHUB_REPO env vars to enable code-level fix generation. "
            "Profiling data (file:line:function) and pattern-based fixes are still available."
        ),
    }, indent=2)


def get_source_context(
    file_path: str,
    line: int = 0,
    service: str = "",
    context_lines: int = 30,
) -> str:
    """
    Read source code around a specific line — the function containing a hotspot.

    Provide the file_path as returned by get_cpu_flamegraph (e.g. 'src/cart/repository.py').
    The tool resolves the full path via SOURCE_ROOT or GitHub API.

    Args:
        file_path: Relative file path from profiling frame (e.g. 'src/cart/repository.py').
        line: Line number from profiling frame (0 = read full file head).
        service: Service name — used to scope path resolution in monorepos.
        context_lines: Lines before/after the target line to include (default: 30).
    """
    mode = _source_mode()

    if mode == "local":
        return _read_local(file_path, line, service, context_lines)
    if mode == "github":
        return _read_github(file_path, line, service, context_lines)

    return json.dumps({
        "source_available": False,
        "file_path": file_path,
        "note": (
            "Source code access not configured. "
            "Set SOURCE_ROOT or GITHUB_TOKEN + GITHUB_REPO to enable code reads. "
            "You can still describe the likely fix based on the operation pattern and profiling frame."
        ),
    }, indent=2)


def search_source_for_function(
    function_name: str,
    service: str = "",
    file_hint: str = "",
) -> str:
    """
    Find a function definition in the source code by name.

    Useful when you have a function name from a profiling frame but need to locate
    exactly which file it lives in. Returns the file path and surrounding code.

    Args:
        function_name: Function or method name (e.g. 'get_cart_items', 'processPayment').
        service: Service name to scope the search (optional).
        file_hint: Partial file path from profiling frame to narrow the search (optional).
    """
    mode = _source_mode()
    if mode == "none":
        return json.dumps({
            "source_available": False,
            "function_name": function_name,
            "note": "Source access not configured.",
        }, indent=2)

    # Build candidate paths to search
    root = Path(_SOURCE_ROOT) if mode == "local" else None
    if root is None:
        return json.dumps({"source_available": False, "note": "GitHub function search not yet implemented."}, indent=2)

    # Resolve service directory
    search_root = root
    if service:
        svc_path = root / service
        if svc_path.exists():
            search_root = svc_path

    results = []
    patterns = [f"def {function_name}", f"function {function_name}", f"fun {function_name}",
                f"async def {function_name}", f"public.*{function_name}(", f"private.*{function_name}("]

    try:
        for ext in ("*.py", "*.js", "*.ts", "*.java", "*.go", "*.rb", "*.cs"):
            for fpath in search_root.rglob(ext):
                if any(skip in str(fpath) for skip in ("node_modules", ".git", "__pycache__", "vendor", "dist")):
                    continue
                if file_hint and file_hint not in str(fpath):
                    continue
                try:
                    lines = fpath.read_text(encoding="utf-8", errors="replace").splitlines()
                    for i, ln in enumerate(lines, 1):
                        if any(p.lower() in ln.lower() for p in patterns[:2]):  # def/function
                            # Return 20 lines of context from this match
                            start = max(0, i - 2)
                            end = min(len(lines), i + 20)
                            results.append({
                                "file": str(fpath.relative_to(root)),
                                "line": i,
                                "match": ln.strip(),
                                "context": "\n".join(f"{start+j+1}: {lines[start+j]}" for j in range(end - start)),
                            })
                            if len(results) >= 5:
                                break
                except Exception:
                    pass
                if len(results) >= 5:
                    break

        if not results:
            return json.dumps({
                "source_available": True,
                "function_name": function_name,
                "found": False,
                "note": f"Function '{function_name}' not found in source. Check the function name spelling from the profiling frame.",
            }, indent=2)

        return json.dumps({
            "source_available": True,
            "function_name": function_name,
            "found": True,
            "matches": results,
        }, indent=2)

    except Exception as exc:
        return json.dumps({"source_available": True, "error": str(exc)}, indent=2)


def list_service_files(service: str, extension: str = "*.py") -> str:
    """
    List source files for a service — use to understand the project structure
    before reading specific files.

    Args:
        service: Service name (matches top-level directory in SOURCE_ROOT).
        extension: File extension glob (e.g. '*.py', '*.java', '*.go'). Default: '*.py'.
    """
    mode = _source_mode()
    if mode == "none":
        return json.dumps({"source_available": False, "note": "Source access not configured."}, indent=2)
    if mode != "local":
        return json.dumps({"source_available": False, "note": "list_service_files only works in local mode."}, indent=2)

    root = Path(_SOURCE_ROOT)
    svc_root = root / service if (root / service).exists() else root

    try:
        files = []
        for p in sorted(svc_root.rglob(extension)):
            if any(skip in str(p) for skip in ("node_modules", ".git", "__pycache__", "vendor", "dist", ".pyc")):
                continue
            rel = str(p.relative_to(root))
            size = p.stat().st_size
            files.append({"path": rel, "size_bytes": size})
            if len(files) >= 50:
                break

        return json.dumps({
            "source_available": True,
            "service": service,
            "root": str(svc_root.relative_to(root)),
            "file_count": len(files),
            "files": files,
        }, indent=2)
    except Exception as exc:
        return json.dumps({"source_available": True, "error": str(exc)}, indent=2)


# ── Internal helpers ───────────────────────────────────────────────────────────

def _read_local(file_path: str, line: int, service: str, context_lines: int) -> str:
    root = Path(_SOURCE_ROOT)

    # Try multiple path resolutions: exact, under service dir, anywhere under root
    candidates = [
        root / file_path,
        root / service / file_path if service else None,
        root / Path(file_path).name,  # just filename match
    ]
    # Also try stripping leading path components
    parts = Path(file_path).parts
    for i in range(1, len(parts)):
        candidates.append(root / Path(*parts[i:]))
        if service:
            candidates.append(root / service / Path(*parts[i:]))

    resolved = None
    for c in candidates:
        if c and c.exists():
            resolved = c
            break

    if not resolved:
        # Fuzzy: find any file with matching name
        name = Path(file_path).name
        for found in root.rglob(name):
            if service and service not in str(found):
                continue
            resolved = found
            break

    if not resolved:
        return json.dumps({
            "source_available": True,
            "file_path": file_path,
            "found": False,
            "note": f"File not found under SOURCE_ROOT={_SOURCE_ROOT}. "
                    f"The profiling frame path may be relative to the build directory.",
        }, indent=2)

    try:
        all_lines = resolved.read_text(encoding="utf-8", errors="replace").splitlines()
        total = len(all_lines)

        if line > 0:
            start = max(0, line - context_lines - 1)
            end = min(total, line + context_lines)
        else:
            start = 0
            end = min(total, _MAX_LINES)

        snippet = "\n".join(f"{start+i+1:4d}: {all_lines[start+i]}" for i in range(end - start))
        return json.dumps({
            "source_available": True,
            "file_path": str(resolved.relative_to(root)),
            "absolute_path": str(resolved),
            "target_line": line,
            "lines_shown": f"{start+1}-{end}",
            "total_lines": total,
            "code": snippet,
        }, indent=2)

    except Exception as exc:
        return json.dumps({"source_available": True, "file_path": file_path, "error": str(exc)}, indent=2)


def _read_github(file_path: str, line: int, service: str, context_lines: int) -> str:
    """Fetch file content from GitHub API."""
    # Try common path structures
    candidates = [file_path]
    if service:
        candidates.insert(0, f"{service}/{file_path}")

    for path in candidates:
        url = f"https://api.github.com/repos/{_GITHUB_REPO}/contents/{path}?ref={_GITHUB_BRANCH}"
        headers = {
            "Authorization": f"Bearer {_GITHUB_TOKEN}",
            "Accept": "application/vnd.github.v3.raw",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                content = resp.read().decode("utf-8", errors="replace")
                all_lines = content.splitlines()
                total = len(all_lines)

                if line > 0:
                    start = max(0, line - context_lines - 1)
                    end = min(total, line + context_lines)
                else:
                    start = 0
                    end = min(total, _MAX_LINES)

                snippet = "\n".join(f"{start+i+1:4d}: {all_lines[start+i]}" for i in range(end - start))
                return json.dumps({
                    "source_available": True,
                    "file_path": path,
                    "repo": _GITHUB_REPO,
                    "branch": _GITHUB_BRANCH,
                    "target_line": line,
                    "lines_shown": f"{start+1}-{end}",
                    "total_lines": total,
                    "code": snippet,
                }, indent=2)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                continue
            return json.dumps({"source_available": True, "error": f"GitHub API {e.code}: {e.read().decode()[:200]}"}, indent=2)
        except Exception as exc:
            return json.dumps({"source_available": True, "error": str(exc)}, indent=2)

    return json.dumps({
        "source_available": True,
        "file_path": file_path,
        "found": False,
        "note": f"File not found in {_GITHUB_REPO}@{_GITHUB_BRANCH}. "
                f"Tried paths: {candidates}",
    }, indent=2)


# ── Tool registry ──────────────────────────────────────────────────────────────

SCHEMAS = [
    {
        "toolSpec": {
            "name": "get_source_status",
            "description": (
                "Check whether source code access is configured and which mode (local/github/none). "
                "Call this first before any other source tool."
            ),
            "inputSchema": {"json": {"type": "object", "properties": {}}},
        }
    },
    {
        "toolSpec": {
            "name": "get_source_context",
            "description": (
                "Read source code around a specific file and line number from a profiling frame. "
                "Returns the function body and surrounding context so you can identify the exact "
                "code pattern causing the hotspot and generate a concrete fix."
            ),
            "inputSchema": {"json": {
                "type": "object",
                "required": ["file_path"],
                "properties": {
                    "file_path": {"type": "string", "description": "File path from profiling frame (e.g. 'src/cart/repository.py')."},
                    "line": {"type": "integer", "description": "Line number from profiling frame (0 = start of file)."},
                    "service": {"type": "string", "description": "Service name to scope path resolution."},
                    "context_lines": {"type": "integer", "description": "Lines before/after to include (default: 30)."},
                },
            }},
        }
    },
    {
        "toolSpec": {
            "name": "search_source_for_function",
            "description": (
                "Find a function definition in source code by name. "
                "Use when you have a function name from a profiling frame but need to locate it."
            ),
            "inputSchema": {"json": {
                "type": "object",
                "required": ["function_name"],
                "properties": {
                    "function_name": {"type": "string"},
                    "service": {"type": "string"},
                    "file_hint": {"type": "string", "description": "Partial path to narrow search."},
                },
            }},
        }
    },
    {
        "toolSpec": {
            "name": "list_service_files",
            "description": "List source files for a service to understand project structure.",
            "inputSchema": {"json": {
                "type": "object",
                "required": ["service"],
                "properties": {
                    "service": {"type": "string"},
                    "extension": {"type": "string", "description": "File glob e.g. '*.py', '*.java'. Default: '*.py'."},
                },
            }},
        }
    },
]

TOOL_FNS = {
    "get_source_status": get_source_status,
    "get_source_context": get_source_context,
    "search_source_for_function": search_source_for_function,
    "list_service_files": list_service_files,
}
