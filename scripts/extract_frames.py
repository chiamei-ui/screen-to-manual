"""從螢幕錄影抽出關鍵畫面（本機執行，不花 token）。

流程：
1. ffmpeg 把影片解成低解析灰階（每秒 --sample-fps 張、160x90），用 numpy 計算相鄰取樣間
   「變動像素比例」偵測畫面切換。不用 ffmpeg 內建 scene 分數，因為它是為影片剪接設計，
   對淺色介面的局部變化（開對話框、換頁籤）不敏感。
2. 連續變動（動畫、捲動、載入中）合併成一次切換，取變動結束、畫面穩定後的時間點；第 0 秒必取。
3. 偵測到的切換太少時，改用固定間隔補幀。
4. 每個時間點擷取一張原尺寸 PNG；用同一套變動比例去掉幾乎相同的相鄰幀，超過上限時沿時間軸平均挑選。
5. 另存寬 800px 的 JPG 縮圖給 AI 看；可選擇用 Windows OCR 取圖內文字與每行的文字框。
6. 檢查錄影品質（實際幀率、畫面凍結），有問題只警告不中斷，寫進 frames.json 的 warnings。
7. 輸出 frames.json。

開始前檢查 ffmpeg／ffprobe 是否存在、輸入是不是影片、長度是否超過上限（超過 exit 2）。
輸出資料夾已存在時不覆蓋，自動改用 <名稱>_2、_3…。
任何一步失敗都直接報錯結束，並清掉本次新建的輸出資料夾，不產出殘缺結果。
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
from PIL import Image

SCRIPT_DIR = Path(__file__).resolve().parent


class ExtractError(RuntimeError):
    exit_code = 1


class TooLongError(ExtractError):
    """影片超過長度上限：exit 2，讓呼叫端知道是「需要操作者決定」而不是壞掉。"""
    exit_code = 2


INSTALL_FFMPEG = ("安裝方法：\n"
                  "  Windows：winget install Gyan.FFmpeg（裝完請關掉並重開終端機）\n"
                  "  macOS：brew install ffmpeg")


def require_ffmpeg() -> None:
    missing = [exe for exe in ("ffmpeg", "ffprobe") if not shutil.which(exe)]
    if missing:
        raise ExtractError(f"找不到 {'、'.join(missing)}（不在 PATH 中），無法處理影片。\n{INSTALL_FFMPEG}")


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    except FileNotFoundError as e:
        raise ExtractError(f"找不到執行檔 {cmd[0]}，請確認已安裝並在 PATH 中") from e
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-15:])
        raise ExtractError(f"指令失敗（exit {proc.returncode}）：{' '.join(cmd)}\n{tail}")
    return proc


MIN_SIDE = 320  # 寬或高小於此值，多半不是螢幕錄影


def probe_video(video: Path) -> tuple[float, int, int]:
    """回傳（長度秒數、寬、高），取第一條視訊軌；不是影片就報錯。"""
    try:
        proc = run([
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=codec_type,width,height,duration:format=duration", "-of", "json", str(video),
        ])
    except ExtractError as e:
        raise ExtractError(f"無法讀取，可能不是影片檔或檔案已損毀：{video}\n{e}") from e
    info = json.loads(proc.stdout)
    streams = info.get("streams") or []
    if not streams:
        raise ExtractError(f"沒有視訊軌，不是影片檔：{video}")
    st = streams[0]
    try:
        duration = float(info.get("format", {}).get("duration") or st.get("duration") or 0)
    except ValueError:
        duration = 0.0
    if duration <= 0:
        raise ExtractError(f"讀不到影片長度，可能不是影片檔（例如單張圖片）：{video}")
    w, h = int(st.get("width") or 0), int(st.get("height") or 0)
    if w < MIN_SIDE or h < MIN_SIDE:
        raise ExtractError(f"解析度 {w}x{h} 太小（寬或高小於 {MIN_SIDE}），可能不是螢幕錄影：{video}")
    return duration, w, h


def make_out_dir(requested: Path) -> Path:
    """建立輸出資料夾；已存在（不論是否為空）就不覆蓋，改用 <名稱>_2、_3…第一個不存在的。"""
    candidate, n = requested, 1
    while True:
        try:
            candidate.mkdir(parents=True, exist_ok=False)
            return candidate
        except FileExistsError:
            # 上層路徑是檔案時也會拋 FileExistsError，此時 candidate 本身不存在，改名重試會無限迴圈
            if not candidate.exists():
                raise ExtractError(f"無法建立輸出資料夾（上層路徑可能是檔案而不是資料夾）：{candidate}")
            n += 1
            candidate = requested.with_name(f"{requested.name}_{n}")


SIG_W, SIG_H = 160, 90
PIXEL_DELTA = 12  # 灰階差超過此值才算「這個像素變了」，濾掉壓縮雜訊


def change_ratio(a: np.ndarray, b: np.ndarray) -> float:
    """兩張灰階縮圖之間，變動像素佔全畫面的比例（0–1）。"""
    return float((np.abs(a.astype(np.int16) - b.astype(np.int16)) > PIXEL_DELTA).mean())


def detect_scenes(video: Path, sample_fps: float, threshold: float, settle: float,
                  duration: float) -> tuple[list[float], list[float]]:
    """回傳（畫面切換後穩定下來的時間點（秒，含第 0 秒）、相鄰取樣的變動比例）。"""
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-i", str(video), "-an",
         "-vf", f"fps={sample_fps},scale={SIG_W}:{SIG_H},format=gray", "-f", "rawvideo", "-"],
        capture_output=True,
    )
    if proc.returncode != 0:
        raise ExtractError(f"ffmpeg 解碼失敗（exit {proc.returncode}）：{proc.stderr.decode('utf-8', 'replace')[-1500:]}")
    n = len(proc.stdout) // (SIG_W * SIG_H)
    if n == 0:
        raise ExtractError("ffmpeg 沒有解出任何畫面，影片可能損毀")
    samples = np.frombuffer(proc.stdout[: n * SIG_W * SIG_H], dtype=np.uint8).reshape(n, SIG_H, SIG_W)

    ratios = [change_ratio(samples[i - 1], samples[i]) for i in range(1, n)]
    changed = [r >= threshold for r in ratios]
    times = [0.0]
    i = 0
    while i < len(changed):
        if changed[i]:
            j = i
            while j + 1 < len(changed) and changed[j + 1]:
                j += 1  # 連續變動視為同一次切換
            end = (j + 1) / sample_fps  # 變動結束的取樣時間
            times.append(min(end + settle, max(duration - 0.1, 0)))
            i = j + 1
        else:
            i += 1
    return times, ratios


GAMEBAR_HINT = ("可能原因：Xbox Game Bar 預設不錄滑鼠游標、只錄單一應用程式視窗，"
                "切換視窗或電腦效能不足時畫面會凍結、掉幀。"
                "建議改用剪取工具（Win+Shift+R）或 OBS Studio 重錄。")
FREEZE_MIN_RUN = 3.0  # 秒；短於此的靜止（操作者照建議停 1–2 秒）不列為凍結區段，避免誤報


def parse_rate(text: str | None) -> float:
    """把 ffprobe 的 "60/1"、"30000/1001" 轉成數字；讀不到回傳 0。"""
    if not text or "/" not in text:
        return 0.0
    num, den = text.split("/", 1)
    try:
        return float(num) / float(den) if float(den) else 0.0
    except ValueError:
        return 0.0


def check_fps(video: Path, duration: float) -> tuple[dict | None, str | None]:
    """回傳（low_fps 警告或 None、無法檢查時的說明或 None）。實際 fps＝封包數 ÷ 長度。"""
    proc = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets",
                "-show_entries", "stream=nb_read_packets,r_frame_rate,avg_frame_rate", "-of", "json", str(video)])
    streams = json.loads(proc.stdout).get("streams") or []
    if not streams:
        return None, "ffprobe 沒有回傳視訊軌資訊，無法檢查幀率"
    st = streams[0]
    nominal = parse_rate(st.get("r_frame_rate")) or parse_rate(st.get("avg_frame_rate"))
    try:
        packets = int(st.get("nb_read_packets") or 0)
    except ValueError:
        packets = 0
    if nominal <= 0 or packets <= 0:
        return None, (f"讀不到標稱幀率或封包數（r_frame_rate={st.get('r_frame_rate')}，"
                      f"nb_read_packets={st.get('nb_read_packets')}），無法檢查幀率")
    actual = packets / duration
    if actual >= nominal * 0.5:
        return None, None
    return {
        "type": "low_fps",
        "message": (f"實際幀率只有 {actual:.1f} fps（{packets} 幀 ÷ {duration:.1f} 秒），"
                    f"不到標稱 {nominal:g} fps 的一半，錄影可能掉幀、畫面卡頓。{GAMEBAR_HINT}"),
        "nominal_fps": round(nominal, 3), "actual_fps": round(actual, 2),
        "packets": packets, "duration": round(duration, 2),
    }, None


def check_freeze(ratios: list[float], sample_fps: float, duration: float, freeze_seconds: float) -> dict | None:
    """找變動比例＝0 的連續區段（至少 FREEZE_MIN_RUN 秒）；最長 ≥ freeze_seconds 或總長 ≥ 影片一半就警告。"""
    segments = []
    i = 0
    while i < len(ratios):
        if ratios[i] == 0:
            j = i
            while j + 1 < len(ratios) and ratios[j + 1] == 0:
                j += 1
            start, end = i / sample_fps, (j + 1) / sample_fps  # 取樣 i 到 j+1 之間完全沒變
            if end - start >= FREEZE_MIN_RUN:
                segments.append((start, end))
            i = j + 1
        else:
            i += 1
    if not segments:
        return None
    longest = max(e - s for s, e in segments)
    total = sum(e - s for s, e in segments)
    if longest < freeze_seconds and total < duration * 0.5:
        return None
    listed = "、".join(f"{timecode(s)}–{timecode(e)}" for s, e in segments)
    return {
        "type": "freeze",
        "message": (f"畫面完全靜止的區段共 {total:.1f} 秒（佔影片 {total / duration:.0%}），"
                    f"最長一段 {longest:.1f} 秒：{listed}。錄影可能凍結，或沒有錄到操作與滑鼠游標。{GAMEBAR_HINT}"),
        "longest_seconds": round(longest, 2), "total_seconds": round(total, 2),
        "segments": [{"start": round(s, 2), "end": round(e, 2), "start_tc": timecode(s), "end_tc": timecode(e)}
                     for s, e in segments],
    }


def grab_frame(video: Path, t: float, dest: Path) -> None:
    run(["ffmpeg", "-hide_banner", "-nostdin", "-y", "-ss", f"{t:.3f}", "-i", str(video),
         "-frames:v", "1", str(dest)])
    if not dest.exists():
        raise ExtractError(f"在 {t:.2f}s 擷取不到畫面")


def signature(path: Path) -> np.ndarray:
    with Image.open(path) as im:
        return np.asarray(im.convert("L").resize((SIG_W, SIG_H), Image.BILINEAR), dtype=np.uint8)


def timecode(t: float) -> str:
    m, s = divmod(int(round(t)), 60)
    return f"{m:02d}:{s:02d}"


CJK_SPACE = re.compile(r"(?<=[⺀-鿿＀-￯])\s+(?=[⺀-鿿＀-￯])")


def strip_cjk_spaces(text: str) -> str:
    """中文 OCR 會在每個字之間插空白，去掉中日韓字元之間的空白。"""
    return CJK_SPACE.sub("", text)


def run_ocr(paths: list[Path], lang: str) -> list[tuple[str, list[dict]]]:
    """回傳每張圖的（全文、每行文字與文字框 [{text, x, y, w, h}]，原圖座標）。"""
    ps = shutil.which("powershell") or shutil.which("pwsh")
    if not ps:
        raise ExtractError("找不到 PowerShell，無法使用 --ocr（Windows OCR 僅限 Windows）")
    proc = run([ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT_DIR / "ocr.ps1"),
                "-Lang", lang, *[str(p) for p in paths]])
    data = json.loads(proc.stdout)
    out = []
    for d in data:
        lines = [{"text": strip_cjk_spaces(ln["text"]), "x": int(ln["x"]), "y": int(ln["y"]),
                  "w": int(ln["w"]), "h": int(ln["h"])} for ln in (d.get("lines") or [])]
        out.append((strip_cjk_spaces(d["text"]), lines))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="從螢幕錄影抽出關鍵畫面")
    ap.add_argument("video", help="錄影檔路徑（mp4、mkv、mov、webm 等 ffmpeg 讀得到的格式）")
    ap.add_argument("--out", required=True, help="輸出資料夾（會建立 images/、thumbs/、frames.json）")
    ap.add_argument("--max-frames", type=int, default=20, help="最多保留幾張（預設 20）")
    ap.add_argument("--sample-fps", type=float, default=4, help="偵測切換時每秒取樣幾張（預設 4）")
    ap.add_argument("--scene", type=float, default=0.005,
                    help="變動像素比例超過此值算一次畫面切換（0–1，越小越敏感，預設 0.005＝0.5%%）")
    ap.add_argument("--interval", type=float, default=2.5, help="補幀間隔秒數（預設 2.5）")
    ap.add_argument("--min-scenes", type=int, default=3, help="場景切換少於這個數就用固定間隔補幀（預設 3）")
    ap.add_argument("--settle", type=float, default=0.3, help="畫面停止變動後再等幾秒才擷取（預設 0.3）")
    ap.add_argument("--dedupe", type=float, default=0.003,
                    help="相鄰兩張的變動像素比例小於此值視為重複（0–1，預設 0.003＝0.3%%）")
    ap.add_argument("--thumb-width", type=int, default=800, help="給 AI 看的縮圖寬度（預設 800）")
    ap.add_argument("--ocr", action="store_true", help="用 Windows 內建 OCR 讀取圖內文字，寫進 frames.json")
    ap.add_argument("--ocr-lang", default="zh-Hant-TW", help="OCR 語言（預設 zh-Hant-TW）")
    ap.add_argument("--max-minutes", type=float, default=15,
                    help="影片超過幾分鐘就停下來（exit 2），請操作者決定分段或加 --allow-long（預設 15）")
    ap.add_argument("--allow-long", action="store_true", help="確認要處理超過 --max-minutes 的長影片")
    ap.add_argument("--freeze-seconds", type=float, default=10,
                    help="畫面完全靜止的連續區段達幾秒就發出凍結警告（預設 10）")
    args = ap.parse_args()

    require_ffmpeg()

    video = Path(args.video).resolve()
    if not video.is_file():
        raise ExtractError(f"找不到影片檔：{video}")
    if args.max_frames < 1:
        raise ExtractError("--max-frames 至少要 1")

    duration, width, height = probe_video(video)  # 先確認影片讀得到，再建立輸出資料夾
    if duration > args.max_minutes * 60 and not args.allow_long:
        raise TooLongError(
            f"影片長度 {timecode(duration)}（{duration / 60:.1f} 分鐘），超過上限 {args.max_minutes:g} 分鐘。\n"
            f"長影片抽出的畫面多、步驟容易漏，建議先分段，例如：\n"
            f"  ffmpeg -ss 00:00:00 -to 00:10:00 -i 原檔 -c copy 片段1.mp4\n"
            f"  ffmpeg -ss 00:10:00 -i 原檔 -c copy 片段2.mp4\n"
            f"確定要整段處理，請加 --allow-long 重跑。")

    requested = Path(args.out)
    out_given = make_out_dir(requested.resolve())
    if out_given.name != requested.resolve().name:
        print(f"輸出資料夾已存在，改用：{out_given}")
    out = out_given
    # frames.json 記錄操作者給的寫法（相對或絕對）加上實際序號，不額外展開成本機絕對路徑
    out_record = str(requested.with_name(out.name))
    img_dir, thumb_dir, raw_dir = out / "images", out / "thumbs", out / "_raw"

    # 之後任何一步失敗都清掉本次新建的整個輸出資料夾，不留下殘缺結果
    try:
        for d in (img_dir, thumb_dir, raw_dir):
            d.mkdir()
        scenes, ratios = detect_scenes(video, args.sample_fps, args.scene, args.settle, duration)
        warnings = []
        fps_warning, fps_skip = check_fps(video, duration)
        if fps_warning:
            warnings.append(fps_warning)
        freeze_warning = check_freeze(ratios, args.sample_fps, duration, args.freeze_seconds)
        if freeze_warning:
            warnings.append(freeze_warning)
        candidates = [(t, "scene") for t in scenes]
        if len(scenes) - 1 < args.min_scenes:  # 扣掉第 0 秒
            t = args.interval
            while t < duration:
                if all(abs(t - s) > args.interval / 2 for s in scenes):
                    candidates.append((t, "interval"))
                t += args.interval
        candidates.sort()

        grabbed = []
        for i, (t, source) in enumerate(candidates):
            raw = raw_dir / f"raw_{i:04d}.png"
            grab_frame(video, t, raw)
            grabbed.append({"time": t, "source": source, "raw": raw, "sig": signature(raw)})

        if not grabbed:
            raise ExtractError("抽不到任何畫面")

        # 去重：跟上一張保留的幀比
        kept = [dict(grabbed[0], diff=float("inf"))]
        for g in grabbed[1:]:
            diff = change_ratio(g["sig"], kept[-1]["sig"])
            if diff >= args.dedupe:
                kept.append(dict(g, diff=diff))
        dropped_dupes = len(grabbed) - len(kept)

        # 超過上限：沿時間軸平均挑（頭尾一定留），避免整段後面的步驟被砍掉
        dropped_over_limit = 0
        if len(kept) > args.max_frames:
            if args.max_frames == 1:
                picks = [0]
            else:
                step = (len(kept) - 1) / (args.max_frames - 1)
                picks = sorted({round(i * step) for i in range(args.max_frames)})
            dropped_over_limit = len(kept) - len(picks)
            kept = [kept[i] for i in picks]

        frames = []
        for n, k in enumerate(kept, 1):
            name = f"frame_{n:02d}"
            img_path, thumb_path = img_dir / f"{name}.png", thumb_dir / f"{name}.jpg"
            k["raw"].replace(img_path)
            with Image.open(img_path) as im:
                w, h = im.size
                tw = min(args.thumb_width, w)
                im.convert("RGB").resize((tw, round(h * tw / w)), Image.LANCZOS).save(thumb_path, quality=85)
            frames.append({
                "index": n, "time": round(k["time"], 2), "timecode": timecode(k["time"]),
                "source": k["source"], "image": f"images/{name}.png", "thumb": f"thumbs/{name}.jpg",
                "width": w, "height": h,
            })
        shutil.rmtree(raw_dir)

        if args.ocr:
            ocr_results = run_ocr([out / f["image"] for f in frames], args.ocr_lang)
            if len(ocr_results) != len(frames):
                raise ExtractError(f"OCR 回傳筆數 {len(ocr_results)} 與畫面數 {len(frames)} 不符")
            for f, (text, lines) in zip(frames, ocr_results):
                f["ocr"] = text
                f["ocr_lines"] = lines

        result = {
            "video": args.video, "out": out_record, "duration": round(duration, 2),
            "width": width, "height": height,
            "scene_changes": len(scenes) - 1, "candidates": len(candidates),
            "dropped_duplicates": dropped_dupes, "dropped_over_limit": dropped_over_limit,
            "warnings": warnings, "frames": frames,
        }
        (out / "frames.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    except BaseException:
        try:
            shutil.rmtree(out)
        except OSError as ce:  # 清理失敗要讓操作者知道，但不蓋掉原本的錯誤
            print(f"警告：清不掉本次建立的輸出資料夾，請手動刪除：{out}（{ce}）", file=sys.stderr)
        raise

    print(f"影片長度 {duration:.1f}s，偵測到 {len(scenes) - 1} 次畫面切換，候選 {len(candidates)} 張，"
          f"去重 {dropped_dupes} 張，保留 {len(frames)} 張")
    if dropped_over_limit:
        print(f"注意：超過上限 {args.max_frames} 張，已沿時間軸平均刪掉 {dropped_over_limit} 張；"
              f"需要更完整請加大 --max-frames 重跑")
    for f in frames:
        print(f"  {f['index']:>2}. [{f['timecode']}] {f['thumb']}（{f['source']}）")
    print(f"已輸出：{out / 'frames.json'}")
    if fps_skip:
        print(f"注意：{fps_skip}")
    for w in warnings:
        print()
        print(f"⚠ 錄影品質警告（{w['type']}）：{w['message']}")
    return 0


if __name__ == "__main__":
    # Windows 主控台預設 cp950，統一用 UTF-8 輸出避免亂碼
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    try:
        sys.exit(main())
    except ExtractError as e:
        print(f"錯誤：{e}", file=sys.stderr)
        sys.exit(e.exit_code)
    except OSError as e:
        where = e.filename or "（未知路徑）"
        print(f"錯誤：讀寫檔案失敗：{where}（{e.strerror or e}）。請確認有寫入權限、磁碟空間足夠。", file=sys.stderr)
        sys.exit(1)
