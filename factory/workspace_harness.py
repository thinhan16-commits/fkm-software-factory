"""
Workspace Harness Engine for AI Software Factory.
Cung cấp khả năng định vị cây thư mục, đọc lát cắt code, và vá mã cục bộ (Search/Replace Diff)
giúp các Agent (ChatGPT, DeepSeek, Dola qua Web Bridge) làm việc trực tiếp trên mã nguồn
mà KHÔNG cần tải/nén các file .zip cồng kềnh.
"""
import os
import re
import difflib
import subprocess
from typing import Dict, List, Optional, Any, Tuple


IGNORE_DIRS = {
    ".git", ".dart_tool", "build", "node_modules", ".gradle", ".idea",
    "__pycache__", ".factory_cache", ".factory_home", "Pods", ".vscode",
    "build_outputs", "director_inbox", "rollback_store", "shipments",
    ".flutter-plugins", ".flutter-plugins-dependencies",
}

IGNORE_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".tar", ".gz",
    ".apk", ".aab", ".ipa", ".dylib", ".so", ".dll", ".exe", ".o", ".a",
    ".class", ".dex", ".pyc", ".lock",
}


def get_workspace_tree(
    workspace_dir: str,
    max_depth: int = 4,
    max_entries: int = 250,
) -> Tuple[str, List[Dict[str, Any]]]:
    """
    Tạo cây thư mục thu gọn (ASCII tree + cấu trúc JSON) cho AI và giao diện.
    Tự động đếm số dòng cho các file mã nguồn.
    """
    if not os.path.exists(workspace_dir):
        return "(Thư mục workspace chưa được khởi tạo)", []

    lines = [os.path.basename(os.path.abspath(workspace_dir)) + "/"]
    json_tree = []
    count = 0

    def walk_dir(current_dir: str, depth: int, prefix: str = ""):
        nonlocal count
        if depth > max_depth or count >= max_entries:
            return

        try:
            entries = sorted(os.scandir(current_dir), key=lambda e: (not e.is_dir(), e.name.lower()))
        except Exception:
            return

        valid_entries = [
            e for e in entries
            if not e.name.startswith(".")
            and e.name not in IGNORE_DIRS
            and not any(e.name.endswith(ext) for ext in IGNORE_EXTS)
        ]

        total = len(valid_entries)
        for i, entry in enumerate(valid_entries):
            if count >= max_entries:
                lines.append(f"{prefix}└── ... (còn nhiều file khác)")
                break

            count += 1
            is_last = (i == total - 1)
            connector = "└── " if is_last else "├── "
            new_prefix = prefix + ("    " if is_last else "│   ")
            rel_path = os.path.relpath(entry.path, workspace_dir)

            if entry.is_dir():
                lines.append(f"{prefix}{connector}{entry.name}/")
                node = {
                    "name": entry.name,
                    "path": rel_path,
                    "type": "directory",
                    "children": []
                }
                json_tree.append(node)
                walk_dir(entry.path, depth + 1, new_prefix)
            else:
                line_info = ""
                line_count = 0
                try:
                    with open(entry.path, "r", encoding="utf-8", errors="ignore") as f:
                        line_count = sum(1 for _ in f)
                        line_info = f" ({line_count} dòng)"
                except Exception:
                    pass

                lines.append(f"{prefix}{connector}{entry.name}{line_info}")
                json_tree.append({
                    "name": entry.name,
                    "path": rel_path,
                    "type": "file",
                    "size": entry.stat().st_size,
                    "lines": line_count,
                })

    walk_dir(workspace_dir, depth=1)
    return "\n".join(lines), json_tree


def read_file_slice(
    workspace_dir: str,
    rel_path: str,
    start_line: int = 1,
    line_count: int = 120,
) -> Dict[str, Any]:
    """
    Đọc một lát cắt mã nguồn của file, đánh số thứ tự từng dòng (L45: ...).
    """
    full_path = os.path.join(workspace_dir, rel_path)
    if not os.path.exists(full_path):
        return {"error": f"Không tìm thấy file: {rel_path}"}

    try:
        with open(full_path, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
    except Exception as e:
        return {"error": f"Lỗi đọc file: {e}"}

    total_lines = len(all_lines)
    start_idx = max(0, start_line - 1)
    end_idx = min(total_lines, start_idx + line_count)

    slice_lines = all_lines[start_idx:end_idx]
    numbered_content = "".join(
        f"L{idx + 1:04d}: {line}" for idx, line in enumerate(slice_lines, start=start_idx)
    )

    return {
        "rel_path": rel_path,
        "start_line": start_idx + 1,
        "end_line": end_idx,
        "total_lines": total_lines,
        "content": numbered_content,
        "raw_content": "".join(slice_lines),
    }


def search_code(
    workspace_dir: str,
    query: str,
    max_results: int = 40,
) -> List[Dict[str, Any]]:
    """
    Tìm kiếm biến, hàm hoặc chuỗi trong mã nguồn.
    """
    results = []
    if not query or not query.strip():
        return results

    q_lower = query.lower().strip()
    for root, dirs, files in os.walk(workspace_dir):
        dirs[:] = [d for d in dirs if d not in IGNORE_DIRS and not d.startswith(".")]
        for file in files:
            if any(file.endswith(ext) for ext in IGNORE_EXTS) or file.startswith("."):
                continue
            file_path = os.path.join(root, file)
            rel_path = os.path.relpath(file_path, workspace_dir)
            try:
                with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                    for line_num, line in enumerate(f, start=1):
                        if q_lower in line.lower():
                            results.append({
                                "file": rel_path,
                                "line": line_num,
                                "text": line.strip()[:160]
                            })
                            if len(results) >= max_results:
                                return results
            except Exception:
                pass
    return results


def apply_search_replace(
    workspace_dir: str,
    rel_path: str,
    search_block: str,
    replace_block: str,
) -> Dict[str, Any]:
    """
    Áp dụng thay thế nguyên tử (Search/Replace Patch) vào file vật lý.
    Hỗ trợ cả khớp chính xác và khớp xấp xỉ khoảng trắng (fuzzy whitespace).
    """
    full_path = os.path.join(workspace_dir, rel_path)
    if not os.path.exists(full_path):
        return {"success": False, "error": f"File không tồn tại: {rel_path}"}

    try:
        with open(full_path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        return {"success": False, "error": f"Không đọc được file {rel_path}: {e}"}

    clean_search = search_block.replace("\r\n", "\n")
    clean_replace = replace_block.replace("\r\n", "\n")
    clean_content = content.replace("\r\n", "\n")

    # 1. Khớp chính xác
    if clean_search in clean_content:
        new_content = clean_content.replace(clean_search, clean_replace, 1)
        with open(full_path, "w", encoding="utf-8") as f:
            f.write(new_content)
        return {
            "success": True,
            "file": rel_path,
            "strategy": "exact",
            "lines_before": len(clean_content.splitlines()),
            "lines_after": len(new_content.splitlines()),
        }

    # 2. Khớp xấp xỉ khoảng trắng đầu/cuối dòng (Fuzzy Whitespace Match)
    search_lines = [l.strip() for l in clean_search.splitlines() if l.strip()]
    if not search_lines:
        return {"success": False, "error": "Khối tìm kiếm rỗng."}

    content_lines = clean_content.splitlines(keepends=True)
    matched_start = -1
    matched_end = -1

    for i in range(len(content_lines) - len(search_lines) + 1):
        window = [content_lines[i + j].strip() for j in range(len(search_lines))]
        if window == search_lines:
            matched_start = i
            matched_end = i + len(search_lines)
            break

    if matched_start != -1:
        formatted_replace_lines = [r_line for r_line in clean_replace.splitlines(keepends=True)]
        new_lines = content_lines[:matched_start] + formatted_replace_lines + content_lines[matched_end:]
        new_content = "".join(new_lines)
        with open(full_path, "w", encoding="utf-8") as f:
            f.write(new_content)
        return {
            "success": True,
            "file": rel_path,
            "strategy": "fuzzy_whitespace",
            "matched_lines": (matched_start + 1, matched_end),
            "lines_before": len(content_lines),
            "lines_after": len(new_lines),
        }

    return {
        "success": False,
        "error": f"Không tìm thấy đoạn mã cần thay thế trong {rel_path}. Vui lòng kiểm tra lại nội dung SEARCH.",
    }


def write_file(
    workspace_dir: str,
    rel_path: str,
    content: str,
) -> Dict[str, Any]:
    """Tạo hoặc ghi đè toàn bộ một file trong workspace."""
    full_path = os.path.join(workspace_dir, rel_path)
    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    clean_content = content.replace("\r\n", "\n")
    with open(full_path, "w", encoding="utf-8") as f:
        f.write(clean_content)
    return {
        "success": True,
        "file": rel_path,
        "lines": len(clean_content.splitlines()),
        "bytes": len(clean_content.encode("utf-8")),
    }


def filter_and_extract_patches(ai_response_text: str) -> List[Dict[str, Any]]:
    """
    Trích xuất và lọc sạch toàn bộ mã nguồn / bản vá từ câu trả lời của AI,
    loại bỏ 100% các đoạn chat, chào hỏi, giải thích linh tinh bên ngoài khung code.
    Mỗi kết quả trả về là một khối code sạch sẵn sàng để sao chép hoặc áp dụng vào đĩa.
    """
    patches = []
    text = ai_response_text or ""

    # Regex nhận diện đường dẫn file ở tiêu đề hoặc dòng chỉ định
    file_header_pattern = re.compile(
        r"(?:^|\n)\s*(?:#+|//|/\*|\*\*|--)?\s*(?:File|Tệp|Tệp tin|Tạo file|CREATE_FILE|NEW_FILE|PATH):\s*[`'\"]?([a-zA-Z0-9_\-\./\\]+\.[a-zA-Z0-9]+)[`'\"]?",
        re.IGNORECASE
    )

    # 1. Tìm các fenced code block ```lang ... ```
    code_block_pattern = re.compile(r"```([a-zA-Z0-9_\-\.\/]*)\n(.*?)```", re.DOTALL)
    matches = list(code_block_pattern.finditer(text))

    for m in matches:
        lang = (m.group(1) or "").strip().lower()
        code_content = m.group(2)
        start_idx = m.start()

        # Kiểm tra xem ngay sau dấu ``` có ghi luôn tên file không (vd: ```dart lib/main.dart)
        detected_file = None
        if " " in lang:
            parts = lang.split()
            lang = parts[0]
            if "." in parts[-1]:
                detected_file = parts[-1].strip()

        # Tìm dòng chỉ định File ngay trước khối code này (trong phạm vi 350 ký tự)
        if not detected_file:
            prefix_text = text[max(0, start_idx - 350):start_idx]
            file_match_before = list(file_header_pattern.finditer(prefix_text))
            if file_match_before:
                detected_file = file_match_before[-1].group(1).strip()

        # Nếu chưa có, tìm bên trong 3 dòng đầu tiên của code_content
        first_lines = code_content.strip().splitlines()[:3]
        for line in first_lines:
            f_match = file_header_pattern.search(line)
            if f_match:
                detected_file = f_match.group(1).strip()
                break

        # Làm sạch tiền tố số dòng dạng L0001: hoặc 1 | do AI sao chép từ giao diện
        cleaned_code = re.sub(r'(?m)^L\d{3,5}:\s?', '', code_content)
        cleaned_code = re.sub(r'(?m)^\s*\d+\s*\|\s?', '', cleaned_code)

        # Kiểm tra xem có chứa khối SEARCH/REPLACE không
        sr_matches = list(re.finditer(
            r"<<<<<<<\s*SEARCH\s*\n(.*?)=======\s*\n(.*?)>>>>>>>\s*REPLACE",
            cleaned_code,
            re.DOTALL
        ))

        if sr_matches:
            for sr in sr_matches:
                search_part = sr.group(1)
                replace_part = sr.group(2)
                raw_patch = f"<<<<<<< SEARCH\n{search_part}=======\n{replace_part}>>>>>>> REPLACE"
                patches.append({
                    "type": "patch",
                    "file": detected_file,
                    "lang": lang or "diff",
                    "search": search_part,
                    "replace": replace_part,
                    "raw_code": raw_patch,
                })
        else:
            # Đây là toàn bộ file mới hoặc code trọn vẹn
            clean_file_body = cleaned_code.strip()
            # Bỏ dòng header // File: ... ở đầu nếu có
            clean_lines = clean_file_body.splitlines()
            if clean_lines and file_header_pattern.search(clean_lines[0]):
                clean_file_body = "\n".join(clean_lines[1:]).lstrip()

            patches.append({
                "type": "file",
                "file": detected_file,
                "lang": lang or "txt",
                "content": clean_file_body,
                "raw_code": clean_file_body,
            })

    # 2. Nếu AI không bọc ``` nhưng gõ trực tiếp các khối <<<<<<< SEARCH ... >>>>>>> REPLACE
    if not any(p["type"] == "patch" for p in patches):
        direct_sr = list(re.finditer(
            r"(?:(?:^|\n)\s*(?:#|//|--)?\s*(?:File|Tệp|Tệp tin):\s*[`'\"]?([a-zA-Z0-9_\-\./\\]+\.[a-zA-Z0-9]+)[`'\"]?\s*\n)?"
            r"<<<<<<<\s*SEARCH\s*\n(.*?)=======\s*\n(.*?)>>>>>>>\s*REPLACE",
            text, re.DOTALL | re.IGNORECASE
        ))
        for sr in direct_sr:
            f = sr.group(1)
            search_part = re.sub(r'(?m)^L\d{3,5}:\s?', '', sr.group(2))
            replace_part = re.sub(r'(?m)^L\d{3,5}:\s?', '', sr.group(3))
            raw_patch = f"<<<<<<< SEARCH\n{search_part}=======\n{replace_part}>>>>>>> REPLACE"
            patches.append({
                "type": "patch",
                "file": f,
                "lang": "diff",
                "search": search_part,
                "replace": replace_part,
                "raw_code": raw_patch,
            })

    return patches


def extract_and_apply_harness_edits(
    workspace_dir: str,
    ai_response_text: str,
) -> Dict[str, Any]:
    """
    Trích xuất và áp dụng tự động tất cả các bản vá do AI (DeepSeek, ChatGPT, Dola)
    sinh ra, loại bỏ mọi đoạn chat linh tinh.
    """
    applied = []
    errors = []

    extracted_patches = filter_and_extract_patches(ai_response_text)

    # Nếu không tìm thấy bằng bộ lọc cấu trúc, fallback kiểm tra các khối CREATE_FILE
    if not extracted_patches:
        cf_pattern = re.compile(
            r"(?:CREATE_FILE|TẠO_FILE|NEW_FILE):\s*([^\s\n\r]+)\s*\n"
            r"(?:```[a-zA-Z0-9_\-]*\n)?(.*?)(?:```|$)",
            re.DOTALL
        )
        for match in cf_pattern.finditer(ai_response_text):
            target_file = match.group(1).strip().strip("`").strip("'").strip('"')
            new_content = match.group(2).strip()
            extracted_patches.append({
                "type": "file",
                "file": target_file,
                "content": new_content,
            })

    for item in extracted_patches:
        target_file = item.get("file")
        if not target_file:
            errors.append("Phát hiện khối code/bản vá nhưng không xác định được đường dẫn File mục tiêu.")
            continue

        target_file = target_file.strip().strip("`").strip("'").strip('"')

        if item["type"] == "patch":
            res = apply_search_replace(workspace_dir, target_file, item["search"], item["replace"])
            if res.get("success"):
                applied.append({"action": "patch", "file": target_file, "strategy": res.get("strategy")})
            else:
                errors.append(f"{target_file}: {res.get('error')}")
        elif item["type"] == "file":
            res = write_file(workspace_dir, target_file, item.get("content", ""))
            if res.get("success"):
                applied.append({"action": "create", "file": target_file, "lines": res.get("lines")})
            else:
                errors.append(f"{target_file}: Lỗi khi ghi file")

    return {
        "applied_count": len(applied),
        "applied": applied,
        "error_count": len(errors),
        "errors": errors,
        "extracted_total": len(extracted_patches),
    }


# =========================================================================
# GIT VERSION CONTROL & BRANCHING HARNESS ENGINE
# =========================================================================

def git_init_workspace(workspace_dir: str) -> Dict[str, Any]:
    """Khởi tạo git repository cho thư mục workspace nếu chưa có."""
    os.makedirs(workspace_dir, exist_ok=True)
    git_dir = os.path.join(workspace_dir, ".git")
    is_new = not os.path.exists(git_dir)
    if is_new:
        subprocess.run(["git", "init", "-b", "main"], cwd=workspace_dir, capture_output=True, text=True)
        subprocess.run(["git", "config", "user.name", "AI Software Factory"], cwd=workspace_dir, capture_output=True)
        subprocess.run(["git", "config", "user.email", "factory@ai.local"], cwd=workspace_dir, capture_output=True)

        gitignore_path = os.path.join(workspace_dir, ".gitignore")
        if not os.path.exists(gitignore_path):
            with open(gitignore_path, "w", encoding="utf-8") as f:
                f.write("node_modules/\nbuild/\n.dart_tool/\n__pycache__/\n*.pyc\n.DS_Store\n*.apk\n*.zip\n.factory_cache/\n")

        subprocess.run(["git", "add", "-A"], cwd=workspace_dir, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init: Khởi tạo mã nguồn gốc [main]"], cwd=workspace_dir, capture_output=True)
    return {"initialized": True, "created": is_new, "workspace": workspace_dir}


def git_get_status(workspace_dir: str) -> Dict[str, Any]:
    """Lấy trạng thái Git hiện tại: nhánh đang đứng, danh sách nhánh, file sửa/thêm, commit gần nhất."""
    os.makedirs(workspace_dir, exist_ok=True)
    git_dir = os.path.join(workspace_dir, ".git")
    if not os.path.exists(git_dir):
        git_init_workspace(workspace_dir)

    branch_res = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=workspace_dir, capture_output=True, text=True)
    current_branch = branch_res.stdout.strip() or "main"

    branches_res = subprocess.run(["git", "branch", "--format=%(refname:short)"], cwd=workspace_dir, capture_output=True, text=True)
    branches = [b.strip() for b in branches_res.stdout.splitlines() if b.strip()]
    if not branches:
        branches = ["main"]

    status_res = subprocess.run(["git", "status", "--porcelain"], cwd=workspace_dir, capture_output=True, text=True)
    modified = []
    untracked = []
    for line in status_res.stdout.splitlines():
        if not line:
            continue
        status_code = line[:2]
        path = line[3:].strip()
        if "?" in status_code:
            untracked.append(path)
        else:
            modified.append(path)

    log_res = subprocess.run(["git", "log", "-1", "--pretty=format:%H|%h|%an|%ar|%s"], cwd=workspace_dir, capture_output=True, text=True)
    last_commit = None
    if log_res.returncode == 0 and log_res.stdout.strip():
        parts = log_res.stdout.strip().split("|")
        if len(parts) >= 5:
            last_commit = {
                "hash": parts[0],
                "short_hash": parts[1],
                "author": parts[2],
                "time": parts[3],
                "message": parts[4],
            }

    return {
        "is_git": True,
        "current_branch": current_branch,
        "branches": branches,
        "modified_files": modified,
        "untracked_files": untracked,
        "is_clean": len(modified) == 0 and len(untracked) == 0,
        "last_commit": last_commit,
    }


def git_create_branch(workspace_dir: str, branch_name: str, checkout: bool = True) -> Dict[str, Any]:
    """Tạo nhánh mới an toàn cho task hoặc vai trò AI."""
    git_init_workspace(workspace_dir)
    clean_name = re.sub(r"[^a-zA-Z0-9_\-\./]", "-", branch_name).strip("-")
    if not clean_name:
        clean_name = "feature-patch"

    cmd = ["git", "checkout", "-b", clean_name] if checkout else ["git", "branch", clean_name]
    res = subprocess.run(cmd, cwd=workspace_dir, capture_output=True, text=True)
    if res.returncode != 0:
        if checkout:
            res2 = subprocess.run(["git", "checkout", clean_name], cwd=workspace_dir, capture_output=True, text=True)
            if res2.returncode == 0:
                return {"success": True, "branch": clean_name, "switched": True}
        return {"success": False, "error": res.stderr.strip() or "Không tạo được nhánh."}
    return {"success": True, "branch": clean_name, "created": True}


def git_switch_branch(workspace_dir: str, branch_name: str) -> Dict[str, Any]:
    """Chuyển nhánh làm việc an toàn."""
    git_init_workspace(workspace_dir)
    res = subprocess.run(["git", "checkout", branch_name], cwd=workspace_dir, capture_output=True, text=True)
    if res.returncode != 0:
        return {"success": False, "error": res.stderr.strip() or f"Không chuyển được sang nhánh {branch_name}."}
    return {"success": True, "current_branch": branch_name}


def git_commit(workspace_dir: str, message: str, author_role: str = "Giám đốc Kỹ thuật") -> Dict[str, Any]:
    """Lưu một mốc thay đổi (Commit) kèm tác giả vai trò AI."""
    git_init_workspace(workspace_dir)
    subprocess.run(["git", "add", "-A"], cwd=workspace_dir, capture_output=True)
    full_msg = f"[{author_role}] {message.strip()}"
    res = subprocess.run(["git", "commit", "-m", full_msg], cwd=workspace_dir, capture_output=True, text=True)
    if res.returncode != 0 and "nothing to commit" in (res.stdout + res.stderr):
        return {"success": True, "no_changes": True, "message": "Không có thay đổi mới để commit."}
    if res.returncode != 0:
        return {"success": False, "error": res.stderr.strip() or "Lỗi khi commit."}

    hash_res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=workspace_dir, capture_output=True, text=True)
    return {"success": True, "commit": hash_res.stdout.strip()[:8], "message": full_msg}


def parse_git_diff(raw_diff: str) -> List[Dict[str, Any]]:
    """Bóc tách git diff dạng unified text thành cấu trúc JSON chi tiết từng dòng xanh/đỏ."""
    files = []
    current_file = None
    current_hunk = None

    for line in raw_diff.splitlines():
        if line.startswith("diff --git"):
            if current_file:
                if current_hunk:
                    current_file["hunks"].append(current_hunk)
                files.append(current_file)
            parts = line.split(" ")
            b_path = parts[-1].lstrip("b/")
            current_file = {
                "file": b_path,
                "additions": 0,
                "deletions": 0,
                "hunks": []
            }
            current_hunk = None
        elif line.startswith("@@"):
            if current_file:
                if current_hunk:
                    current_file["hunks"].append(current_hunk)
                current_hunk = {"header": line, "lines": []}
        elif current_hunk is not None:
            if line.startswith("+") and not line.startswith("+++"):
                current_file["additions"] += 1
                current_hunk["lines"].append({"type": "+", "text": line[1:]})
            elif line.startswith("-") and not line.startswith("---"):
                current_file["deletions"] += 1
                current_hunk["lines"].append({"type": "-", "text": line[1:]})
            elif not line.startswith("+++") and not line.startswith("---") and not line.startswith("index"):
                current_hunk["lines"].append({"type": " ", "text": line[1:] if line.startswith(" ") else line})

    if current_file:
        if current_hunk:
            current_file["hunks"].append(current_hunk)
        files.append(current_file)
    return files


def git_diff(workspace_dir: str, base_branch: str = "main", target: Optional[str] = None) -> Dict[str, Any]:
    """Tính toán diff giữa nhánh hiện tại và nhánh gốc main, hoặc các thay đổi chưa commit."""
    git_init_workspace(workspace_dir)
    status = git_get_status(workspace_dir)
    current_branch = status["current_branch"]

    if current_branch != base_branch and not target:
        cmd = ["git", "diff", f"{base_branch}...HEAD"]
    elif target:
        cmd = ["git", "diff", f"{base_branch}...{target}"]
    else:
        cmd = ["git", "diff", "HEAD"]

    res = subprocess.run(cmd, cwd=workspace_dir, capture_output=True, text=True)
    raw_diff = res.stdout

    if not raw_diff:
        res2 = subprocess.run(["git", "diff"], cwd=workspace_dir, capture_output=True, text=True)
        raw_diff = res2.stdout

    parsed_files = parse_git_diff(raw_diff)
    total_adds = sum(f["additions"] for f in parsed_files)
    total_dels = sum(f["deletions"] for f in parsed_files)

    return {
        "success": True,
        "current_branch": current_branch,
        "base_branch": base_branch,
        "files_count": len(parsed_files),
        "total_additions": total_adds,
        "total_deletions": total_dels,
        "files": parsed_files,
        "raw_diff": raw_diff[:8000],
    }


def git_merge(workspace_dir: str, source_branch: str, target_branch: str = "main") -> Dict[str, Any]:
    """Hợp nhất an toàn một nhánh vào main, tự động hủy nếu xung đột."""
    git_init_workspace(workspace_dir)
    if source_branch == target_branch:
        return {"success": False, "error": "Không thể merge một nhánh vào chính nó."}

    sw = subprocess.run(["git", "checkout", target_branch], cwd=workspace_dir, capture_output=True, text=True)
    if sw.returncode != 0:
        return {"success": False, "error": f"Không thể checkout sang nhánh đích {target_branch}: {sw.stderr}"}

    merge_res = subprocess.run(
        ["git", "merge", "--no-ff", source_branch, "-m", f"merge: Hợp nhất {source_branch} vào {target_branch}"],
        cwd=workspace_dir,
        capture_output=True,
        text=True
    )

    if merge_res.returncode != 0:
        subprocess.run(["git", "merge", "--abort"], cwd=workspace_dir, capture_output=True)
        return {
            "success": False,
            "conflict": True,
            "error": f"Phát hiện xung đột khi merge {source_branch} vào {target_branch}. Đã tự động hủy merge để bảo vệ mã nguồn gốc."
        }

    hash_res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=workspace_dir, capture_output=True, text=True)
    return {
        "success": True,
        "merged": True,
        "source_branch": source_branch,
        "target_branch": target_branch,
        "commit": hash_res.stdout.strip()[:8],
    }


def git_log(workspace_dir: str, limit: int = 20) -> List[Dict[str, Any]]:
    """Lấy danh sách các mốc lịch sử commit (Cỗ máy thời gian)."""
    git_init_workspace(workspace_dir)
    res = subprocess.run(
        ["git", "log", f"-n{limit}", "--pretty=format:%H|%h|%an|%ar|%s|%D"],
        cwd=workspace_dir,
        capture_output=True,
        text=True
    )
    if res.returncode != 0:
        return []

    commits = []
    for line in res.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("|")
        if len(parts) >= 5:
            commits.append({
                "hash": parts[0],
                "short_hash": parts[1],
                "author": parts[2],
                "time": parts[3],
                "message": parts[4],
                "refs": parts[5] if len(parts) > 5 else "",
            })
    return commits


def git_rollback(workspace_dir: str, commit_hash: str) -> Dict[str, Any]:
    """Quay ngược thời gian về mốc commit ổn định chỉ định."""
    git_init_workspace(workspace_dir)
    res = subprocess.run(["git", "reset", "--hard", commit_hash.strip()], cwd=workspace_dir, capture_output=True, text=True)
    if res.returncode != 0:
        return {"success": False, "error": res.stderr.strip() or f"Không khôi phục được về commit {commit_hash}."}
    subprocess.run(["git", "clean", "-fd"], cwd=workspace_dir, capture_output=True)
    return {"success": True, "rollback_to": commit_hash.strip()[:8]}
