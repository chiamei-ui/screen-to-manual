"""依 steps.json 組出 manual.md 與 manual.html（本機執行，不花 token）。

steps.json 格式：
{
  "title": "手冊標題",
  "author": "製作者名稱",             # 必填，顯示在標題下並寫進 PDF 檔案屬性
  "created_at": "2026-11-27 14:30",   # 選填，YYYY-MM-DD HH:MM；沒填用產出當下的本地時間
  "audience": "同事",                 # 選填，顯示在標題下
  "intro": "這份手冊說明……",         # 選填
  "steps": [
    {"text": "點選右上角的「新增」", "image": "images/frame_01.png"},
    {"text": "沒有圖的步驟也可以"}
  ],
  "notes": ["注意事項一", "注意事項二"],  # 選填
  "privacy_checked": true              # 必填：操作者已明確回答過敏感資訊遮蔽確認
}
image 路徑相對於 steps.json 所在資料夾。
privacy_checked 不是 true、或遮蔽狀態不一致（有 images_original/ 卻沒有 masks_applied.json）時報錯，不產出任何檔案。

manual.md 用相對路徑引用 images/；manual.html 把圖片以 base64 內嵌，單一檔案即可傳給同事。
manual.pdf 由本機 Edge 或 Chrome 以 headless 模式列印 manual.html 產生；加 --no-pdf 可略過。
瀏覽器列印不會帶入作者，產出後以「增量更新」在 PDF 尾端附加新的 Info 物件（Title、Author）與 trailer（/Prev 指回原 xref）。
"""
from __future__ import annotations

import argparse
import base64
import html
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path


class BuildError(RuntimeError):
    pass


CSS = """
:root { --brand:#FF5000; --ink:#1A1A1A; --bg:#F4F4F4; --card:#FFFFFF; --muted:#5A5A5A; }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--ink);
  font-family:"Noto Sans TC","Inter","Microsoft JhengHei","PingFang TC",system-ui,sans-serif; line-height:1.7; }
main { max-width:960px; margin:0 auto; padding:32px 16px 64px; }
header { border-left:6px solid var(--brand); padding:4px 0 4px 16px; margin-bottom:24px; }
h1 { margin:0; font-size:1.8rem; }
.meta { color:var(--muted); margin:4px 0 0; }
.intro, .notes { background:var(--card); border-radius:8px; padding:16px 20px; margin-bottom:24px; }
.step { background:var(--card); border-radius:8px; padding:20px; margin-bottom:20px;
  box-shadow:0 1px 3px rgba(0,0,0,.06); }
.step h2 { margin:0 0 12px; font-size:1.1rem; display:flex; gap:12px; align-items:baseline; }
.num { flex:none; background:var(--brand); color:#fff; border-radius:999px; padding:0 12px;
  font-family:"Inter",system-ui,sans-serif; font-size:.95rem; }
.step img { display:block; max-width:100%; height:auto; border:1px solid #E2E2E2; border-radius:6px; }
.notes h2 { margin:0 0 8px; font-size:1.1rem; color:var(--brand); }
@page { size: A4; margin: 12mm; }
@media print {
  html, body { background:#fff; -webkit-print-color-adjust:exact; print-color-adjust:exact; }
  main { max-width:none; padding:0; }
  h1, h2, .step h2 { break-after:avoid; page-break-after:avoid; }
  .step { box-shadow:none; border:1px solid #E2E2E2; break-inside:avoid; page-break-inside:avoid; }
  .step img { max-height:200mm; width:auto; max-width:100%; margin:0 auto; }
}
"""


def load_steps(path: Path) -> dict:
    if not path.is_file():
        raise BuildError(f"找不到 steps 檔：{path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise BuildError(f"steps 檔不是合法 JSON：{path}（{e}）") from e
    if not isinstance(data, dict):
        raise BuildError("steps 檔最外層必須是物件 {...}，不是陣列或其他型別")
    if not str(data.get("title", "")).strip():
        raise BuildError("steps 檔缺少 title")
    author = data.get("author")
    if not isinstance(author, str) or not author.strip():
        raise BuildError("steps 檔缺少 author（製作者名稱），請先問操作者後填入，例如 \"author\": \"王小明\"")
    created_at = data.get("created_at")
    if created_at is None:
        data["created_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    elif not isinstance(created_at, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", created_at):
        raise BuildError(f"created_at 格式必須是 YYYY-MM-DD HH:MM，目前是：{created_at!r}（不填則用產出當下時間）")
    steps = data.get("steps")
    if not isinstance(steps, list) or not steps:
        raise BuildError("steps 檔沒有任何步驟（steps 為空），不產出空手冊")
    notes = data.get("notes")
    if notes is not None and not (isinstance(notes, list) and all(isinstance(n, str) for n in notes)):
        raise BuildError("notes 必須是字串陣列，例如 [\"注意事項一\"]")
    if data.get("privacy_checked") is not True:
        raise BuildError("尚未完成敏感資訊遮蔽確認，請先問操作者截圖裡有沒有要遮蔽的敏感資訊，"
                         "操作者明確回答後在 steps.json 設 \"privacy_checked\": true 再重跑")
    base = path.parent
    has_orig, has_applied = (base / "images_original").is_dir(), (base / "masks_applied.json").is_file()
    if has_orig and not has_applied:
        raise BuildError(f"遮蔽狀態不一致：有 images_original 資料夾但沒有 masks_applied.json（{base}）。"
                         "請重跑 mask_regions.py 套用遮蔽，或用 --restore 還原後再組手冊")
    if has_applied and not has_orig:
        raise BuildError(f"遮蔽狀態不一致：有 masks_applied.json 但沒有 images_original 資料夾（{base}）。"
                         "原圖備份不見了，請確認 images 資料夾裡的圖是否已遮蔽，處理後再組手冊")
    for i, s in enumerate(steps, 1):
        if not isinstance(s, dict):
            raise BuildError(f"第 {i} 步必須是物件 {{\"text\": ..., \"image\": ...}}，不是 {type(s).__name__}")
        if not str(s.get("text", "")).strip():
            raise BuildError(f"第 {i} 步缺少說明文字 text")
        if s.get("image") and not (base / s["image"]).is_file():
            raise BuildError(f"第 {i} 步的圖片不存在：{base / s['image']}")
    return data


def byline(data: dict) -> str:
    return f"製作：{data['author']}｜製作時間：{data['created_at']}"


def build_md(data: dict) -> str:
    lines = [f"# {data['title']}", ""]
    lines += [f"> {byline(data)}", ""]
    if data.get("audience"):
        lines += [f"> 讀者：{data['audience']}", ""]
    if data.get("intro"):
        lines += [data["intro"], ""]
    for i, s in enumerate(data["steps"], 1):
        lines += [f"## 第 {i} 步：{s['text']}", ""]
        if s.get("image"):
            lines += [f"![第 {i} 步]({s['image']})", ""]
    if data.get("notes"):
        lines += ["## 注意事項", ""] + [f"- {n}" for n in data["notes"]] + [""]
    return "\n".join(lines)


def data_uri(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


def build_html(data: dict, base: Path) -> str:
    e = html.escape
    parts = [
        "<!doctype html>", '<html lang="zh-Hant">', "<head>", '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{e(data['title'])}</title>", f"<style>{CSS}</style>", "</head>", "<body>", "<main>",
        f"<header><h1>{e(data['title'])}</h1>",
    ]
    parts.append(f'<p class="meta">{e(byline(data))}</p>')
    if data.get("audience"):
        parts.append(f'<p class="meta">讀者：{e(data["audience"])}</p>')
    parts.append("</header>")
    if data.get("intro"):
        parts.append(f'<section class="intro">{e(data["intro"])}</section>')
    for i, s in enumerate(data["steps"], 1):
        parts.append(f'<section class="step"><h2><span class="num">第 {i} 步</span><span>{e(s["text"])}</span></h2>')
        if s.get("image"):
            parts.append(f'<img src="{data_uri(base / s["image"])}" alt="第 {i} 步畫面">')
        parts.append("</section>")
    if data.get("notes"):
        items = "".join(f"<li>{e(n)}</li>" for n in data["notes"])
        parts.append(f'<section class="notes"><h2>注意事項</h2><ul>{items}</ul></section>')
    parts += ["</main>", "</body>", "</html>"]
    return "\n".join(parts)


def find_browser() -> str | None:
    """依序找 Edge、Chrome（Windows 安裝路徑 → PATH → macOS 應用程式）。"""
    candidates = []
    for env, sub in (("ProgramFiles(x86)", r"Microsoft\Edge\Application\msedge.exe"),
                     ("ProgramFiles", r"Microsoft\Edge\Application\msedge.exe"),
                     ("ProgramFiles", r"Google\Chrome\Application\chrome.exe"),
                     ("ProgramFiles(x86)", r"Google\Chrome\Application\chrome.exe"),
                     ("LOCALAPPDATA", r"Google\Chrome\Application\chrome.exe")):
        base = os.environ.get(env)
        if base:
            candidates.append(Path(base) / sub)
    for c in candidates:
        if c.is_file():
            return str(c)
    for name in ("msedge", "chrome", "google-chrome", "chromium"):
        found = shutil.which(name)
        if found:
            return found
    for mac in ("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"):
        if Path(mac).is_file():
            return mac
    return None


PDF_TIMEOUT = 120  # 秒


def locked_error(pdf_path: Path) -> BuildError:
    return BuildError(f"manual.pdf 正在被其他程式開啟，請先關閉 PDF 再重跑：{pdf_path}")


def is_locked(pdf_path: Path) -> bool:
    """檔案存在但無法以寫入方式開啟，視為被其他程式鎖定。"""
    if not pdf_path.exists():
        return False
    try:
        with pdf_path.open("ab"):
            return False
    except PermissionError:
        return True


def pdf_string(text: str) -> bytes:
    """PDF 文字字串：一律用 UTF-16BE（含 BOM）的十六進位寫法，中文與特殊字元都不必跳脫。"""
    return b"<FEFF" + text.encode("utf-16-be").hex().upper().encode("ascii") + b">"


def read_literal(buf: bytes, i: int) -> int:
    """buf[i] 是 "("，回傳對應 ")" 之後的位置（處理巢狀括號與反斜線跳脫）。"""
    depth = 0
    while i < len(buf):
        c = buf[i:i + 1]
        if c == b"\\":
            i += 2
            continue
        if c == b"(":
            depth += 1
        elif c == b")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise BuildError("PDF Info 字串沒有結尾的右括號")


def parse_simple_dict(body: bytes) -> list[tuple[bytes, bytes]]:
    """解析只含「/Key 值」的平面字典（Info 物件），值可為 (文字)、<十六進位>、名稱或數字。"""
    items, i = [], 0
    while True:
        m = re.compile(rb"\s*/([^\s/<>()\[\]]+)\s*").match(body, i)
        if not m:
            if body[i:].strip():
                raise BuildError(f"無法解析 PDF Info 字典：{body[i:i + 80]!r}")
            return items
        key, i = m.group(1), m.end()
        if body[i:i + 1] == b"(":
            end = read_literal(body, i)
        elif body[i:i + 1] == b"<":
            end = body.index(b">", i) + 1
        else:
            vm = re.compile(rb"[^\s/]+(?:\s+\d+\s+R)?").match(body, i)
            if not vm:
                raise BuildError(f"無法解析 PDF Info 字典的值：/{key.decode('latin-1')}")
            end = vm.end()
        items.append((key, body[i:end]))
        i = end


def set_pdf_info(pdf_path: Path, title: str, author: str) -> None:
    """以增量更新寫入 Title／Author：附加新 Info 物件、只含該物件的 xref 段與帶 /Prev 的 trailer。

    Edge／Chrome（Skia）產出的是傳統 xref 表（"xref" 關鍵字＋"trailer" 字典），只支援這種；
    遇到 xref stream（PDF 1.5 交叉參照串流）直接報錯，不默默略過。
    """
    pdf = pdf_path.read_bytes()
    m = re.search(rb"startxref\s+(\d+)\s+%%EOF\s*$", pdf)
    if not m:
        raise BuildError(f"PDF 結尾找不到 startxref／%%EOF，無法寫入作者與標題：{pdf_path}")
    prev = int(m.group(1))
    if pdf[prev:prev + 4] != b"xref":
        raise BuildError(f"PDF 使用 xref stream（不是傳統 xref 表），目前不支援寫入作者與標題：{pdf_path}")
    t = pdf.rfind(b"trailer", prev, m.start())
    if t < 0:
        raise BuildError(f"PDF 找不到 trailer 字典：{pdf_path}")
    trailer = pdf[t:m.start()]
    size_m = re.search(rb"/Size\s+(\d+)", trailer)
    root_m = re.search(rb"/Root\s+(\d+\s+\d+\s+R)", trailer)
    if not size_m or not root_m:
        raise BuildError(f"PDF trailer 缺少 /Size 或 /Root：{pdf_path}")
    size = int(size_m.group(1))
    id_m = re.search(rb"/ID\s*(\[[^\]]*\])", trailer)

    # 保留原 Info 的其他欄位（Creator、Producer、CreationDate…），只換掉 Title／Author
    kept: list[tuple[bytes, bytes]] = []
    info_m = re.search(rb"/Info\s+(\d+)\s+(\d+)\s+R", trailer)
    if info_m:
        om = re.search(rb"(?<!\d)" + info_m.group(1) + rb"\s+" + info_m.group(2) + rb"\s+obj\s*<<(.*?)>>\s*endobj",
                       pdf, re.S)
        if not om:
            raise BuildError(f"PDF 找不到原本的 Info 物件 {info_m.group(1).decode()} 0 obj：{pdf_path}")
        kept = [(k, v) for k, v in parse_simple_dict(om.group(1)) if k not in (b"Title", b"Author")]
    entries = [(b"Title", pdf_string(title)), (b"Author", pdf_string(author))] + kept
    info_body = b"<<" + b"\n".join(b"/" + k + b" " + v for k, v in entries) + b">>"

    sep = b"" if pdf.endswith(b"\n") else b"\n"
    obj_offset = len(pdf) + len(sep)
    obj = f"{size} 0 obj\n".encode() + info_body + b"\nendobj\n"
    xref_offset = obj_offset + len(obj)
    xref = f"xref\n{size} 1\n{obj_offset:010d} 00000 n \n".encode()  # 每筆固定 20 bytes
    new_trailer = (f"trailer\n<</Size {size + 1}\n/Root ".encode() + root_m.group(1)
                   + f"\n/Info {size} 0 R\n/Prev {prev}".encode()
                   + ((b"\n/ID " + id_m.group(1)) if id_m else b"")
                   + f">>\nstartxref\n{xref_offset}\n%%EOF\n".encode())
    try:
        with pdf_path.open("ab") as f:
            f.write(sep + obj + xref + new_trailer)
    except PermissionError as e:
        raise locked_error(pdf_path) from e


def build_pdf(html_path: Path, pdf_path: Path, title: str, author: str) -> str:
    if pdf_path.exists():
        try:
            pdf_path.unlink()  # 先刪舊檔，避免找不到瀏覽器或列印失敗時留下內容過時的 PDF
        except PermissionError as e:
            raise locked_error(pdf_path) from e
    browser = find_browser()
    if not browser:
        raise BuildError(f"找不到 Edge 或 Chrome，無法產生 PDF。manual.md／manual.html 已產出：{html_path.parent}\n"
                         "要 PDF 請先安裝 Microsoft Edge 或 Google Chrome 後重跑；只要 HTML 可加 --no-pdf。")
    # 一定要用獨立的暫存設定檔：使用者已開著同一個瀏覽器時，headless 會附掛到既有程序而不產檔
    profile = tempfile.mkdtemp(prefix="screen-to-manual-pdf-")
    cmd = [browser, "--headless=new", "--disable-gpu", "--no-pdf-header-footer", f"--user-data-dir={profile}",
           f"--print-to-pdf={pdf_path}", html_path.as_uri()]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=PDF_TIMEOUT)
    except subprocess.TimeoutExpired as e:
        raise BuildError(f"瀏覽器列印 PDF 超過 {PDF_TIMEOUT} 秒沒有完成：{browser}") from e
    finally:
        try:
            shutil.rmtree(profile)
        except OSError as ce:  # 只是暫存設定檔沒刪掉，不影響結果，但要讓人知道
            print(f"警告：暫存資料夾沒刪乾淨，可手動刪除：{profile}（{ce}）", file=sys.stderr)
    if proc.returncode != 0:
        if is_locked(pdf_path):
            raise locked_error(pdf_path)
        tail = "\n".join((proc.stderr or "").strip().splitlines()[-10:])
        raise BuildError(f"瀏覽器列印 PDF 失敗（exit {proc.returncode}）：{browser}\n{tail}")
    if not pdf_path.is_file() or pdf_path.stat().st_size == 0:
        if is_locked(pdf_path):
            raise locked_error(pdf_path)
        raise BuildError(f"瀏覽器執行完畢但沒有產出 PDF：{pdf_path}\n{(proc.stderr or '').strip()[-800:]}")
    with pdf_path.open("rb") as f:
        if f.read(4) != b"%PDF":
            raise BuildError(f"產出的檔案不是 PDF：{pdf_path}")
    try:
        set_pdf_info(pdf_path, title, author)
    except BaseException:
        pdf_path.unlink(missing_ok=True)  # 寫入屬性失敗就不留下缺標題／作者的 PDF，原錯誤照常拋出
        raise
    return browser


def main() -> int:
    ap = argparse.ArgumentParser(description="依 steps.json 產生 manual.md、manual.html 與 manual.pdf")
    ap.add_argument("steps", help="steps.json 路徑（輸出寫在同一個資料夾）")
    ap.add_argument("--no-pdf", action="store_true", help="不產生 manual.pdf（只要 md 與 html）")
    args = ap.parse_args()

    steps_path = Path(args.steps).resolve()
    data = load_steps(steps_path)
    out = steps_path.parent
    md_path, html_path, pdf_path = out / "manual.md", out / "manual.html", out / "manual.pdf"
    if not args.no_pdf and pdf_path.exists():
        # 先刪舊 PDF：被其他程式開啟（鎖定）時在寫 md／html 之前就停下，避免新舊檔混在一起
        try:
            pdf_path.unlink()
        except PermissionError as e:
            raise locked_error(pdf_path) from e
    md_path.write_text(build_md(data), encoding="utf-8")
    html_path.write_text(build_html(data, out), encoding="utf-8")
    size_kb = html_path.stat().st_size / 1024
    print(f"已輸出 {len(data['steps'])} 個步驟：")
    print(f"  {md_path}")
    print(f"  {html_path}（單檔，圖片已內嵌，{size_kb:.0f} KB）")
    if args.no_pdf:
        if pdf_path.exists():
            print(f"注意：已指定 --no-pdf，資料夾內舊的 {pdf_path.name} 沒有更新，內容可能過時")
        return 0
    browser = build_pdf(html_path, pdf_path, data["title"], data["author"])
    print(f"  {pdf_path}（{pdf_path.stat().st_size / 1024:.0f} KB，以 {Path(browser).name} 列印）")
    return 0


if __name__ == "__main__":
    # Windows 主控台預設 cp950，統一用 UTF-8 輸出避免亂碼
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    try:
        sys.exit(main())
    except BuildError as e:
        print(f"錯誤：{e}", file=sys.stderr)
        sys.exit(1)
    except OSError as e:
        where = e.filename or "（未知路徑）"
        print(f"錯誤：讀寫檔案失敗：{where}（{e.strerror or e}）。請確認有寫入權限、磁碟空間足夠。", file=sys.stderr)
        sys.exit(1)
