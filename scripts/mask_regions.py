"""遮蔽截圖中的敏感資訊，或用紅框標示正確按鈕（本機執行，不花 token）。

用法：
  py -3.11 mask_regions.py <輸出資料夾> <masks.json> [--style box|mosaic]   套用遮蔽
  py -3.11 mask_regions.py <輸出資料夾> --restore                           還原成未遮蔽的原圖
  py -3.11 mask_regions.py <輸出資料夾> --suggest                           依 OCR 文字框建議遮蔽區塊（只印不套用）

masks.json 格式：
{
  "coord": "thumb",            # thumb：座標以 thumbs/ 縮圖為準（依寬度比例換算回原圖）；original：原圖座標
  "masks": [
    {"image": "images/frame_02.png", "boxes": [[x, y, w, h]], "reason": "客戶 Email"},
    {"image": "images/frame_03.png", "boxes": [{"box": [x, y, w, h], "type": "highlight"}], "reason": "正確按鈕"}
  ]
}
boxes 的每個區塊可寫成 [x, y, w, h]（＝遮蔽），或 {"box": [x, y, w, h], "type": "mask" 或 "highlight"}。
mask（預設）依 --style 遮蔽；highlight 畫紅色外框（#E00000，粗細約原圖寬度 0.4%、至少 3px）標示正確按鈕，不填滿。

行為：
- 第一次套用時把整個 images/ 複製到 images_original/；之後每次套用一律從 images_original 重新遮，
  可反覆調整座標重跑，結果只取決於這次的 masks.json。
- 預設 --style box：實心深色色塊（#1A1A1A）。mosaic：區塊縮小到約 1/12 再放大，小字仍可能辨識得出來。
- 同一張圖先畫遮蔽、再畫紅框，紅框一定在最上層。
- 套用後重新產生對應的 thumbs/ 縮圖給操作者確認，並寫 masks_applied.json。
- 先驗證全部內容（JSON 格式、圖檔、座標範圍）再動手；任何一項不對就報錯，不寫任何檔。
"""
from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw

BOX_COLOR = (0x1A, 0x1A, 0x1A)
HIGHLIGHT_COLOR = (0xE0, 0x00, 0x00)
HIGHLIGHT_RATIO = 0.004  # 紅框粗細＝原圖寬度 × 0.4%
HIGHLIGHT_MIN = 3
MOSAIC_FACTOR = 12
THUMB_QUALITY = 85  # 與 extract_frames.py 的縮圖設定一致
DEFAULT_THUMB_WIDTH = 800
SUGGEST_PAD = 4  # 建議區塊在原圖座標四周多留的像素

# --suggest 用的格式；比對前先去掉文字中的空白（OCR 常在字元間插空白），
# 並把 OCR 常認錯的橫線（例如「一」「－」「—」）統一成半形 -
DASHES = re.compile(r"[一－—–‐−]")
PATTERNS = [
    ("Email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")),
    ("手機號碼", re.compile(r"(?<!\d)09\d{2}-?\d{3}-?\d{3}(?!\d)")),
    ("市話號碼", re.compile(r"(?:\(0\d{1,2}\)|(?<!\d)0\d{1,2}-)\d{3,4}-?\d{4}(?!\d)")),
    ("身分證字號", re.compile(r"(?<![A-Za-z0-9])[A-Z][12]\d{8}(?!\d)")),
]


class MaskError(RuntimeError):
    pass


def thumb_path_for(out: Path, image_rel: str) -> Path:
    return out / "thumbs" / f"{Path(image_rel).stem}.jpg"


def make_thumb(img_path: Path, thumb_path: Path) -> None:
    """重產縮圖：沿用既有縮圖的寬度（extract_frames 的 --thumb-width），沒有既有縮圖時用 800。"""
    width = DEFAULT_THUMB_WIDTH
    if thumb_path.is_file():
        with Image.open(thumb_path) as t:
            width = t.size[0]
    with Image.open(img_path) as im:
        w, h = im.size
        tw = min(width, w)
        im.convert("RGB").resize((tw, round(h * tw / w)), Image.LANCZOS).save(thumb_path, quality=THUMB_QUALITY)


def load_json(path: Path, what: str):
    if not path.is_file():
        raise MaskError(f"找不到 {what}：{path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise MaskError(f"{what} 不是合法 JSON：{path}（{e}）") from e


def is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def validate(out: Path, spec) -> list[dict]:
    """驗證 masks.json，回傳 [{image, boxes（原圖座標整數）, reason}]；任何錯誤都在動手前拋出。"""
    if not isinstance(spec, dict):
        raise MaskError("masks.json 最外層必須是物件 {\"coord\": ..., \"masks\": [...]}")
    coord = spec.get("coord")
    if coord not in ("thumb", "original"):
        raise MaskError(f"coord 必須是 \"thumb\" 或 \"original\"，目前是：{coord!r}")
    masks = spec.get("masks")
    if not isinstance(masks, list) or not masks:
        raise MaskError("masks 必須是非空陣列；不需要遮蔽或標示時請不要跑本工具（要取消用 --restore）")

    src_dir = out / ("images_original" if (out / "images_original").is_dir() else "images")
    result = []
    for i, m in enumerate(masks, 1):
        where = f"masks 第 {i} 筆"
        if not isinstance(m, dict):
            raise MaskError(f"{where} 必須是物件 {{\"image\", \"boxes\", \"reason\"}}")
        image = m.get("image")
        if not isinstance(image, str) or not re.fullmatch(r"images/[^/\\]+", image.replace("\\", "/")):
            raise MaskError(f"{where} 的 image 必須是 \"images/檔名\"，目前是：{image!r}")
        image = image.replace("\\", "/")
        reason = m.get("reason", "")
        if not isinstance(reason, str):
            raise MaskError(f"{where} 的 reason 必須是字串")
        src = src_dir / Path(image).name
        if not src.is_file():
            raise MaskError(f"{where} 的圖檔不存在：{src}")
        with Image.open(src) as im:
            ow, oh = im.size
        if coord == "thumb":
            tp = thumb_path_for(out, image)
            if not tp.is_file():
                raise MaskError(f"{where} 用縮圖座標，但找不到對應縮圖：{tp}")
            with Image.open(tp) as t:
                cw, ch = t.size
        else:
            cw, ch = ow, oh
        boxes = m.get("boxes")
        if not isinstance(boxes, list) or not boxes:
            raise MaskError(f"{where} 的 boxes 必須是非空陣列 [[x, y, w, h], ...]")
        scale = ow / cw
        converted = []
        for j, b in enumerate(boxes, 1):
            bw = f"{where}（{image}）第 {j} 個區塊 {b!r}"
            kind = "mask"
            if isinstance(b, dict):
                kind = b.get("type", "mask")
                if kind not in ("mask", "highlight"):
                    raise MaskError(f"{bw} 的 type 必須是 \"mask\" 或 \"highlight\"，目前是：{kind!r}")
                if set(b) - {"box", "type"}:
                    raise MaskError(f"{bw} 只能有 box 與 type 兩個欄位")
                b = b.get("box")
            if not (isinstance(b, list) and len(b) == 4 and all(is_number(v) for v in b)):
                raise MaskError(f"{bw} 格式錯誤，必須是 4 個數字 [x, y, w, h]")
            x, y, w, h = b
            if w <= 0 or h <= 0:
                raise MaskError(f"{bw} 的寬高必須大於 0")
            if x < 0 or y < 0 or x + w > cw or y + h > ch:
                raise MaskError(f"{bw} 超出圖片範圍（{coord} 座標，圖片大小 {cw}x{ch}）")
            x0, y0 = math.floor(x * scale), math.floor(y * scale)
            x1, y1 = min(math.ceil((x + w) * scale), ow), min(math.ceil((y + h) * scale), oh)
            converted.append((kind, x0, y0, x1, y1))
        result.append({"image": image, "boxes": converted, "reason": reason})
    return result


def mask_image(src: Path, dest: Path, boxes: list[tuple[str, int, int, int, int]], style: str) -> None:
    with Image.open(src) as im:
        im.load()
        img = im if im.mode in ("RGB", "RGBA") else im.convert("RGB")
        alpha = (255,) if img.mode == "RGBA" else ()
        fill = BOX_COLOR + alpha
        for kind, x0, y0, x1, y1 in boxes:
            if kind != "mask":
                continue
            if style == "box":
                img.paste(fill, (x0, y0, x1, y1))
            else:
                region = img.crop((x0, y0, x1, y1))
                small = region.resize((max(1, round((x1 - x0) / MOSAIC_FACTOR)),
                                       max(1, round((y1 - y0) / MOSAIC_FACTOR))), Image.BOX)
                img.paste(small.resize(region.size, Image.NEAREST), (x0, y0))
        line = max(HIGHLIGHT_MIN, round(img.size[0] * HIGHLIGHT_RATIO))
        draw = ImageDraw.Draw(img)
        for kind, x0, y0, x1, y1 in boxes:  # 紅框最後畫，蓋在遮蔽之上；框線往區塊內側畫，不填滿
            if kind == "highlight":
                draw.rectangle((x0, y0, x1 - 1, y1 - 1), outline=HIGHLIGHT_COLOR + alpha, width=line)
        img.save(dest)


def previously_masked(out: Path) -> set[str]:
    applied = out / "masks_applied.json"
    if not applied.is_file():
        return set()
    data = load_json(applied, "masks_applied.json")
    return {m["image"] for m in data.get("masks", [])}


def apply(out: Path, masks_path: Path, style: str) -> None:
    spec = load_json(masks_path, "masks.json")
    plan = validate(out, spec)
    prev = previously_masked(out)

    orig_dir, img_dir = out / "images_original", out / "images"
    if not orig_dir.is_dir():
        shutil.copytree(img_dir, orig_dir)
        print(f"已備份原圖：{orig_dir}")

    # 從原圖重新遮：先把上次遮過的圖還原，再套這次的區塊，結果只取決於這次的 masks.json
    by_image: dict[str, list] = {}
    for m in plan:
        by_image.setdefault(m["image"], []).extend(m["boxes"])
    for image in prev - by_image.keys():
        shutil.copy2(orig_dir / Path(image).name, img_dir / Path(image).name)
        make_thumb(img_dir / Path(image).name, thumb_path_for(out, image))
        print(f"已還原（這次沒有遮）：{img_dir / Path(image).name}")
    print(f"已套用（遮蔽用{'色塊' if style == 'box' else '馬賽克'}，標示用紅框）：")
    for image, boxes in by_image.items():
        name = Path(image).name
        mask_image(orig_dir / name, img_dir / name, boxes, style)
        tp = thumb_path_for(out, image)
        make_thumb(img_dir / name, tp)
        n_mask = sum(1 for b in boxes if b[0] == "mask")
        print(f"  {img_dir / name}（遮蔽 {n_mask} 個、紅框 {len(boxes) - n_mask} 個）")
        print(f"    預覽縮圖：{tp}")

    record = {
        "applied_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "style": style,
        "coord": spec["coord"],
        "masks": spec["masks"],
        "boxes_original": [{"image": i, "boxes": [{"type": k, "box": [x0, y0, x1 - x0, y1 - y0]}
                                                  for k, x0, y0, x1, y1 in b]}
                           for i, b in by_image.items()],
    }
    (out / "masks_applied.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已記錄：{out / 'masks_applied.json'}")
    print("請看預覽縮圖確認遮蔽與紅框位置；要調整就改 masks.json 重跑（會從原圖重新遮），要取消用 --restore。")


def restore(out: Path) -> None:
    orig_dir, img_dir = out / "images_original", out / "images"
    if not orig_dir.is_dir():
        raise MaskError(f"沒有 images_original\\，目前沒有套用遮蔽，無法還原：{orig_dir}")
    files = sorted(p for p in orig_dir.iterdir() if p.is_file())
    if not files:
        raise MaskError(f"images_original\\ 是空的，無法還原：{orig_dir}")
    for f in files:
        shutil.copy2(f, img_dir / f.name)
        make_thumb(img_dir / f.name, thumb_path_for(out, f"images/{f.name}"))
    shutil.rmtree(orig_dir)
    applied = out / "masks_applied.json"
    if applied.exists():
        applied.unlink()
    print(f"已還原 {len(files)} 張原圖到 {img_dir}，並重產縮圖；已刪除 images_original\\ 與 masks_applied.json")


def suggest(out: Path) -> None:
    frames = load_json(out / "frames.json", "frames.json")
    entries = frames.get("frames") if isinstance(frames, dict) else None
    if not isinstance(entries, list):
        raise MaskError("frames.json 格式不對：缺少 frames 陣列")
    if not any("ocr_lines" in f for f in entries):
        raise MaskError("frames.json 沒有 ocr_lines（抽幀時沒加 --ocr，或是舊版抽幀結果），無法建議；"
                        "請加 --ocr 重新抽幀，或直接看縮圖指定位置")
    masks = []
    for f in entries:
        tp = out / f["thumb"]
        if not tp.is_file():
            raise MaskError(f"找不到縮圖：{tp}")
        with Image.open(tp) as t:
            tw, th = t.size
        scale = tw / f["width"]
        boxes, reasons = [], []
        for ln in f.get("ocr_lines", []):
            compact = DASHES.sub("-", re.sub(r"\s+", "", ln["text"]))
            kinds = [name for name, pat in PATTERNS if pat.search(compact)]
            if not kinds:
                continue
            x0 = max(0, ln["x"] - SUGGEST_PAD)
            y0 = max(0, ln["y"] - SUGGEST_PAD)
            x1 = min(f["width"], ln["x"] + ln["w"] + SUGGEST_PAD)
            y1 = min(f["height"], ln["y"] + ln["h"] + SUGGEST_PAD)
            bx, by = math.floor(x0 * scale), math.floor(y0 * scale)
            boxes.append([bx, by, min(math.ceil(x1 * scale), tw) - bx, min(math.ceil(y1 * scale), th) - by])
            reasons.append(f"{'／'.join(kinds)}：{ln['text']}")
        if boxes:
            masks.append({"image": f["image"], "boxes": boxes, "reason": "；".join(reasons)})
    if not masks:
        print("OCR 文字中沒有找到 Email、手機、市話、身分證字號格式的內容。"
              "OCR 可能漏字或認錯，仍請看縮圖、問操作者是否有其他要遮的資訊（姓名、密碼、客戶資料等）。")
        return
    print("建議的 masks.json（縮圖座標，整行文字框；只是建議，尚未套用，請看縮圖並和操作者確認後再存檔套用）：")
    print(json.dumps({"coord": "thumb", "masks": masks}, ensure_ascii=False, indent=2))


def main() -> int:
    ap = argparse.ArgumentParser(description="遮蔽截圖中的敏感資訊")
    ap.add_argument("out", help="extract_frames.py 的輸出資料夾（含 images/、thumbs/）")
    ap.add_argument("masks", nargs="?", help="masks.json 路徑（套用遮蔽時必填）")
    ap.add_argument("--style", choices=("box", "mosaic"), default="box",
                    help="box：實心色塊（預設，最安全）；mosaic：馬賽克（小字可能仍可辨識）")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--restore", action="store_true", help="還原成未遮蔽的原圖")
    mode.add_argument("--suggest", action="store_true", help="依 frames.json 的 OCR 文字框印出建議的 masks.json")
    args = ap.parse_args()

    out = Path(args.out).resolve()
    if not (out / "images").is_dir():
        raise MaskError(f"找不到 images\\ 資料夾，請確認是 extract_frames.py 的輸出資料夾：{out}")
    if args.restore or args.suggest:
        if args.masks:
            raise MaskError("--restore／--suggest 不需要 masks.json 參數")
        restore(out) if args.restore else suggest(out)
        return 0
    if not args.masks:
        raise MaskError("請給 masks.json 路徑；或用 --suggest 看建議、--restore 還原")
    apply(out, Path(args.masks).resolve(), args.style)
    return 0


if __name__ == "__main__":
    # Windows 主控台預設 cp950，統一用 UTF-8 輸出避免亂碼
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    try:
        sys.exit(main())
    except MaskError as e:
        print(f"錯誤：{e}", file=sys.stderr)
        sys.exit(1)
    except OSError as e:
        where = e.filename or "（未知路徑）"
        print(f"錯誤：讀寫檔案失敗：{where}（{e.strerror or e}）。請確認有寫入權限、磁碟空間足夠。", file=sys.stderr)
        sys.exit(1)
