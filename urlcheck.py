#!/usr/bin/env python3
"""urlcheck: 在 Markdown 文件里找出死链。

用法:
    python -m urlcheck README.md
    python -m urlcheck ./docs --jobs 8 --json

纯标准库。退出码: 0 全部通过 / 1 有死链 / 2 用法错误。
"""
import argparse
import concurrent.futures as cf
import fnmatch
import json
import os
import re
import socket
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

VERSION = "0.1.0"
TIMEOUT = 10

# ---------- Markdown 解析 ----------

FENCE_RE = re.compile(r"^(\s*)(```|~~~)")
INLINE_LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
REF_DEF_RE = re.compile(r"^\s*\[([^\]]+)\]:\s*(\S+)", re.MULTILINE)
REF_USE_RE = re.compile(r"\[([^\]]+)\]\[([^\]]*)\]")
BARE_URL_RE = re.compile(r"(?<![(\[`\"'<])https?://[^\s)>\]`\"']+")
INLINE_CODE_RE = re.compile(r"`[^`]*`")


def strip_code_spans(line):
    return INLINE_CODE_RE.sub("", line)


def slugify(heading):
    """GitHub 风格的锚点 slug。"""
    text = heading.strip().lower()
    text = re.sub(r"[^\w\s\-]", "", text, flags=re.UNICODE)
    text = re.sub(r"\s+", "-", text)
    return text


def extract_headings(text):
    headings = set()
    for m in re.finditer(r"^#{1,6}\s+(.+?)\s*#*\s*$", text, re.MULTILINE):
        headings.add(slugify(m.group(1)))
    return headings


def extract_links(text):
    """返回 [(url, line_no)]。跳过代码块和行内代码。"""
    links = []
    ref_defs = {k.lower(): v for k, v in REF_DEF_RE.findall(text)}
    in_fence = False
    for i, line in enumerate(text.splitlines(), 1):
        if FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        clean = strip_code_spans(line)
        if REF_DEF_RE.match(line):
            continue  # 引用定义行本身不算链接出现
        for m in INLINE_LINK_RE.finditer(clean):
            links.append((m.group(1), i))
        for m in REF_USE_RE.finditer(clean):
            ref = (m.group(2) or m.group(1)).lower()
            if ref in ref_defs:
                links.append((ref_defs[ref], i))
        for m in BARE_URL_RE.finditer(clean):
            url = m.group(0).rstrip(".,;:!?")
            links.append((url, i))
    # 去重（保留首次出现行号）
    seen = {}
    for url, ln in links:
        seen.setdefault(url, ln)
    return [(u, ln) for u, ln in seen.items()]


# ---------- 链接检查 ----------

class HopCounter(urllib.request.HTTPRedirectHandler):
    """统计跳转次数并记录最终 URL。"""

    def __init__(self):
        self.hops = 0
        self.final_url = None

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.hops += 1
        self.final_url = newurl
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def check_http(url, timeout=TIMEOUT, retries=0):
    """返回 (status, detail)。status in OK/BROKEN/WARN。"""
    attempt = 0
    last = ("BROKEN", "未知错误")
    while attempt <= retries:
        counter = HopCounter()
        opener = urllib.request.build_opener(counter)
        req = urllib.request.Request(
            url, method="HEAD",
            headers={"User-Agent": "urlcheck/0.1.0 (link checker)"},
        )
        try:
            with opener.open(req, timeout=timeout) as resp:
                code = resp.status
                final = counter.final_url or url
                note = " -> %s" % final if counter.hops else ""
                if counter.hops > 3:
                    return ("WARN", "跳转 %d 次%s" % (counter.hops, note))
                return ("OK", "HTTP %d%s" % (code, note))
        except urllib.error.HTTPError as e:
            if e.code in (405, 501) and req.get_method() == "HEAD":
                # 有些站点不支持 HEAD，降级为 GET（只读 1 字节）
                try:
                    req2 = urllib.request.Request(
                        url,
                        headers={"User-Agent": "urlcheck/0.1.0 (link checker)",
                                 "Range": "bytes=0-0"},
                    )
                    with opener.open(req2, timeout=timeout) as resp2:
                        code = resp2.status
                        final = counter.final_url or url
                        note = " -> %s" % final if counter.hops else ""
                        if counter.hops > 3:
                            return ("WARN", "跳转 %d 次%s" % (counter.hops, note))
                        return ("OK", "HTTP %d (GET)%s" % (code, note))
                except Exception:
                    pass
            if e.code == 404:
                return ("BROKEN", "HTTP 404 未找到")
            if e.code == 403:
                return ("WARN", "HTTP 403（站点可能屏蔽爬虫，不一定是死链）")
            if e.code == 429:
                return ("WARN", "HTTP 429 被限流")
            if 500 <= e.code < 600:
                last = ("BROKEN", "HTTP %d 服务器错误" % e.code)
            else:
                last = ("BROKEN", "HTTP %d" % e.code)
        except urllib.error.URLError as e:
            reason = e.reason
            if isinstance(reason, socket.gaierror):
                return ("BROKEN", "DNS 解析失败")
            if isinstance(reason, socket.timeout) or "timed out" in str(reason):
                last = ("BROKEN", "连接超时")
            elif isinstance(reason, ConnectionRefusedError) or "refused" in str(reason).lower():
                return ("BROKEN", "连接被拒绝")
            else:
                last = ("BROKEN", "URL 错误: %s" % reason)
        except (socket.timeout, TimeoutError):
            last = ("BROKEN", "连接超时")
        except ssl.SSLError as e:
            last = ("BROKEN", "TLS 错误: %s" % e)
        except Exception as e:
            last = ("BROKEN", "%s: %s" % (type(e).__name__, e))
        attempt += 1
    return last


def check_local(url, base_dir, headings):
    """处理 #锚点 和相对路径。返回 (status, detail) 或 None（不归它管）。"""
    if url.startswith("#"):
        anchor = slugify(urllib.parse.unquote(url[1:]))
        if anchor in headings:
            return ("OK", "锚点存在")
        return ("BROKEN", "锚点 #%s 在本文中不存在" % url[1:])
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme in ("http", "https"):
        return None
    if parsed.scheme and parsed.scheme not in ("", "file"):
        return ("WARN", "不支持的协议: %s" % parsed.scheme)
    # 相对路径 / file:
    path = parsed.path or url
    if not path or path.startswith("#"):
        return None
    target = os.path.normpath(os.path.join(base_dir, urllib.parse.unquote(path)))
    anchor = parsed.fragment
    if os.path.exists(target):
        if anchor:
            try:
                with open(target, encoding="utf-8", errors="replace") as f:
                    h = extract_headings(f.read())
                if slugify(urllib.parse.unquote(anchor)) in h:
                    return ("OK", "文件与锚点都存在")
                return ("BROKEN", "文件存在但锚点 #%s 不存在" % anchor)
            except OSError:
                return ("OK", "文件存在（非文本，跳过锚点检查）")
        return ("OK", "文件存在")
    return ("BROKEN", "本地文件不存在: %s" % path)


def check_one(url, base_dir, headings, timeout, retries):
    local = check_local(url, base_dir, headings)
    if local is not None:
        return local
    return check_http(url, timeout=timeout, retries=retries)


# ---------- CLI ----------

def collect_md_files(paths):
    files = []
    for p in paths:
        if os.path.isdir(p):
            for root, dirs, names in os.walk(p):
                dirs[:] = [d for d in dirs if d not in (".git", "node_modules", "__pycache__", ".venv")]
                for n in names:
                    if n.endswith(".md"):
                        files.append(os.path.join(root, n))
        elif os.path.isfile(p):
            files.append(p)
        else:
            sys.stderr.write("error: 路径不存在: %s\n" % p)
            sys.exit(2)
    return sorted(files)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="urlcheck", description="检查 Markdown 文件中的死链")
    ap.add_argument("paths", nargs="+", help="Markdown 文件或目录")
    ap.add_argument("--jobs", type=int, default=8, help="并发数（默认 8）")
    ap.add_argument("--timeout", type=int, default=TIMEOUT, help="单个请求超时秒数（默认 10）")
    ap.add_argument("--retry", type=int, default=0, help="失败重试次数（默认 0）")
    ap.add_argument("--exclude", action="append", default=[], help="跳过匹配该 glob 的 URL（可多次）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--version", action="store_true", help="显示版本")
    args = ap.parse_args(argv)

    if args.version:
        print("urlcheck " + VERSION)
        return 0

    files = collect_md_files(args.paths)
    if not files:
        sys.stderr.write("error: 没有找到 Markdown 文件\n")
        return 2

    # 收集所有链接
    jobs = []  # (file, url, line)
    for f in files:
        with open(f, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        headings = extract_headings(text)
        base_dir = os.path.dirname(os.path.abspath(f))
        for url, ln in extract_links(text):
            if any(fnmatch.fnmatch(url, pat) for pat in args.exclude):
                continue
            jobs.append((f, url, ln, base_dir, headings))

    results = []
    with cf.ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        future_map = {
            pool.submit(check_one, url, base_dir, headings, args.timeout, args.retry): (f, url, ln)
            for f, url, ln, base_dir, headings in jobs
        }
        for fut in cf.as_completed(future_map):
            f, url, ln = future_map[fut]
            try:
                status, detail = fut.result()
            except Exception as e:
                status, detail = "BROKEN", "%s: %s" % (type(e).__name__, e)
            results.append({"file": f, "url": url, "line": ln,
                            "status": status, "detail": detail})

    results.sort(key=lambda r: (r["file"], r["line"], r["url"]))
    n_ok = sum(1 for r in results if r["status"] == "OK")
    n_warn = sum(1 for r in results if r["status"] == "WARN")
    n_broken = sum(1 for r in results if r["status"] == "BROKEN")

    if args.json:
        print(json.dumps({"summary": {"total": len(results), "ok": n_ok,
                                      "warn": n_warn, "broken": n_broken},
                          "results": results}, ensure_ascii=False, indent=2))
    else:
        icon = {"OK": "✅", "WARN": "⚠️", "BROKEN": "❌"}
        for r in results:
            if r["status"] == "OK":
                continue
            print("%s %s:%d\n   %s\n   %s: %s" % (
                icon[r["status"]], r["file"], r["line"], r["url"],
                r["status"], r["detail"]))
        print("\n共检查 %d 个链接：%d 通过，%d 警告，%d 死链" % (
            len(results), n_ok, n_warn, n_broken))

    return 1 if n_broken else 0


if __name__ == "__main__":
    sys.exit(main())
