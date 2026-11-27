"""依 steps.json 組出 manual.md 與 manual.html（本機執行，不花 token）。

steps.json 格式：
{
  "title": "手冊標題",
  "audience": "同事",                 # 選填，顯示在標題下
  "intro": "這份手冊說明……",         # 選填
  "steps": [
    {"text": "點選右上角的「新增」", "image": "images/frame_01.png"},
    {"text": "沒有圖的步驟也可以"}
  ],
  "notes": ["注意事項一", "注意事項二"]  # 選填
}
image 路徑相對於 steps.json 所在資料夾。

manual.md 用相對路徑引用 images/；manual.html 把圖片以 base64 內嵌，單一檔案即可傳給同事。
manual.pdf 由本機 Edge 或 Chrome 以 headless 模式列印 manual.html 產生；加 --no-pdf 可略過。
"""
from __future__ import annotations

import argparse
import base64
import html
import json
import mimetypes
import os
import shutil
import subprocess
import sys
import tempfile
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
    steps = data.get("steps")
    if not isinstance(steps, list) or not steps:
        raise BuildError("steps 檔沒有任何步驟（steps 為空），不產出空手冊")
    notes = data.get("notes")
    if notes is not None and not (isinstance(notes, list) and all(isinstance(n, str) for n in notes)):
        raise BuildError("notes 必須是字串陣列，例如 [\"注意事項一\"]")
    base = path.parent
    for i, s in enumerate(steps, 1):
        if not isinstance(s, dict):
            raise BuildError(f"第 {i} 步必須是物件 {{\"text\": ..., \"image\": ...}}，不是 {type(s).__name__}")
        if not str(s.get("text", "")).strip():
            raise BuildError(f"第 {i} 步缺少說明文字 text")
        if s.get("image") and not (base / s["image"]).is_file():
            raise BuildError(f"第 {i} 步的圖片不存在：{base / s['image']}")
    return data


def build_md(data: dict) -> str:
    lines = [f"# {data['title']}", ""]
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


def build_pdf(html_path: Path, pdf_path: Path) -> str:
    if pdf_path.exists():
        pdf_path.unlink()  # 先刪舊檔，避免找不到瀏覽器或列印失敗時留下內容過時的 PDF
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
        tail = "\n".join((proc.stderr or "").strip().splitlines()[-10:])
        raise BuildError(f"瀏覽器列印 PDF 失敗（exit {proc.returncode}）：{browser}\n{tail}")
    if not pdf_path.is_file() or pdf_path.stat().st_size == 0:
        raise BuildError(f"瀏覽器執行完畢但沒有產出 PDF：{pdf_path}\n{(proc.stderr or '').strip()[-800:]}")
    with pdf_path.open("rb") as f:
        if f.read(4) != b"%PDF":
            raise BuildError(f"產出的檔案不是 PDF：{pdf_path}")
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
    browser = build_pdf(html_path, pdf_path)
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
