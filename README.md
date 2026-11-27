# screen-to-manual

Claude Code skill：把操作網站或工具的**螢幕錄影**（不需口述）轉成有截圖的**步驟式操作手冊**。
輸出 `manual.md` 與單檔 `manual.html`（圖片內嵌，直接傳給同事即可開啟）。

- 抽幀、去重、縮圖、OCR、組 HTML 全在本機執行，不花 token。
- AI 只負責看縮圖寫每一步的說明；看不懂會停下來問操作者，不猜。

## 安裝

需求：Windows、`ffmpeg`／`ffprobe` 在 PATH、Python 3.11（`py -3.11`）並安裝 Pillow 與 numpy。

```powershell
py -3.11 -m pip install pillow numpy
git clone https://github.com/chiamei-ui/screen-to-manual.git "$env:USERPROFILE\.claude\skills\screen-to-manual"
```

安裝後在 Claude Code 說「幫我把這段錄影做成操作手冊：D:\錄影\xxx.mp4」即會觸發。

## 用法（手動執行腳本）

```powershell
# 1. 抽關鍵畫面（輸出 images/、thumbs/、frames.json）
py -3.11 scripts\extract_frames.py 錄影.mp4 --out 輸出資料夾 --ocr

# 2. 寫好 輸出資料夾\steps.json（格式見 SKILL.md 或 examples\output\steps.json）

# 3. 組手冊
py -3.11 scripts\build_manual.py 輸出資料夾\steps.json
```

`extract_frames.py` 主要參數：

| 參數 | 預設 | 說明 |
|---|---|---|
| `--max-frames` | 20 | 最多保留幾張 |
| `--scene` | 0.005 | 變動像素比例超過此值算一次畫面切換（越小越敏感） |
| `--interval` | 2.5 | 切換次數少於 `--min-scenes`（預設 3）時，用此間隔秒數補幀 |
| `--settle` | 0.3 | 畫面停止變動後再等幾秒才擷取，避開載入中的畫面 |
| `--dedupe` | 0.003 | 相鄰兩張變動比例低於此值視為重複並刪除 |
| `--ocr` | 關 | 用 Windows 內建 OCR 讀圖內文字，寫進 `frames.json` |

## 運作方式

1. **偵測畫面切換**：ffmpeg 把影片解成每秒 4 張、160×90 灰階，numpy 計算相鄰取樣的「變動像素比例」。
   沒用 ffmpeg 內建的 `scene` 分數，因為它是為影片剪接設計，實測對淺色介面的局部變化（開對話框、換頁）偵測不到。
   連續變動（動畫、捲動、載入）合併成一次，取畫面穩定後的時間點。游標移動面積太小不會被當成切換。
2. **補幀**：切換少於 3 次時，以固定間隔補幀。
3. **去重與上限**：相鄰幾乎相同的畫面刪除；超過上限時沿時間軸平均挑選（頭尾必留），並在輸出提示刪了幾張。
4. **縮圖**：原圖 PNG 留給手冊；另存寬 800px JPG 給 AI 看。
5. **OCR（選用）**：經 PowerShell 呼叫 `Windows.Media.Ocr`（`scripts/ocr.ps1`），預設繁體中文。
   AI 寫說明時先讀 OCR 文字，文字足以判斷時就不看圖，能再省一些 token。
   取捨：OCR 對小字、低對比文字會有錯字（例如「示」認成「卞」），只當輔助，不直接貼進手冊。

## Token 成本估算

只有「寫步驟說明」花 token（以 Claude 讀圖約「寬×高÷750」tokens 估算）：

| 項目 | 估計 |
|---|---|
| 一張 800×450 縮圖 | 約 500 tokens |
| 20 張全看 | 約 10,000 tokens |
| SKILL.md、frames.json、步驟文字、與操作者往返 | 約 4,000–6,000 tokens |
| **每份手冊合計** | **約 6,000–16,000 tokens**（有 OCR 可少看幾張圖時落在低端） |

抽幀與組手冊不花 token。若用 Claude Code 訂閱方案，不另產生 API 費用。

## 輸出

```
輸出資料夾/
├─ frames.json   # 抽幀結果（時間碼、檔名、OCR 文字）
├─ images/       # 原尺寸截圖（手冊用）
├─ thumbs/       # 寬 800px 縮圖（AI 看）
├─ steps.json    # AI 寫的步驟說明（可手改後重組）
├─ manual.md     # Markdown，圖片相對路徑（需連同 images/ 一起給）
└─ manual.html   # 單檔 HTML，圖片 base64 內嵌，直接傳給同事
```

範例見 `examples/`：`make_test_video.sh` 用 ffmpeg 產生 5 個畫面的測試影片，`output/` 是跑完的結果（非真實系統畫面）。

## 限制

- 任何一步失敗都會清掉本次建立的 `images/`、`thumbs/`，不留殘缺結果，可用同一個 `--out` 重跑。
- 只處理畫面，不處理聲音；看不出操作意圖時需要操作者口頭補充。
- 不追蹤滑鼠點擊位置，截圖上不會自動標示點了哪裡。
- 畫面只有很小區域變化（例如只改一個小欄位）時可能偵測不到，可調低 `--scene`；捲動很多的錄影可能抽出較多相似畫面，可調高 `--dedupe` 或在第 3 步刪減。
- OCR 僅限 Windows，且需安裝對應語言的 OCR 套件（繁中為 `zh-Hant-TW`）。
- `manual.html` 內嵌圖片，20 張 1080p 截圖約 5–15 MB。
- 錄影中若有密碼、個資、客戶資料，需操作者自行刪除或遮蔽該畫面，skill 不會自動遮蔽。
