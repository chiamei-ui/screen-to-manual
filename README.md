# screen-to-manual

Claude Code skill：把操作網站或工具的**螢幕錄影**（不需口述）轉成有截圖的**步驟式操作手冊**。
輸出 `manual.md`、單檔 `manual.html`（圖片內嵌，直接傳給同事即可開啟）與 A4 的 `manual.pdf`。

- 還沒有錄影檔時，先偵測本機的錄影工具（剪取工具、Xbox Game Bar、Clipchamp、OBS Studio、ShareX），列出錄影步驟與存放位置。
- 抽幀、去重、縮圖、OCR、組 HTML、印 PDF 全在本機執行，不花 token。
- AI 只負責看縮圖寫每一步的說明；看不懂會停下來問操作者，不猜。
- 組手冊前一定先問操作者要不要遮蔽敏感資訊；可用色塊（或馬賽克）遮住截圖局部，遮完給操作者看縮圖確認。
- 錄影掉幀、畫面凍結（常見於 Xbox Game Bar）時會發出警告，讓操作者決定要不要重錄。

## 安裝

需求：Windows、`ffmpeg`／`ffprobe` 在 PATH、Python 3.11（`py -3.11`）並安裝 Pillow 與 numpy；產生 PDF 需要 Microsoft Edge 或 Google Chrome（Windows 10／11 內建 Edge）。

沒有 ffmpeg 時：Windows `winget install Gyan.FFmpeg`（裝完重開終端機），macOS `brew install ffmpeg`。

```powershell
py -3.11 -m pip install pillow numpy
git clone https://github.com/chiamei-ui/screen-to-manual.git "$env:USERPROFILE\.claude\skills\screen-to-manual"
```

安裝後在 Claude Code 說「幫我把這段錄影做成操作手冊：D:\錄影\xxx.mp4」即會觸發。

## 安裝後第一步

第一次使用時（skill 資料夾裡還沒有 `.onboarded` 標記檔），不論有沒有錄影檔，skill 都會先顯示「建議錄影方式」，
內容依 `detect_recorders.ps1` 的偵測結果調整（例如沒有裝 Xbox Game Bar 就不提第 2 點）：

1. 首選剪取工具錄影：Win+Shift+R，框選要錄的範圍；會錄到滑鼠，不受切換視窗影響。
2. 用 Xbox Game Bar 的話：先到「設定 → 遊戲 → 擷取」開啟「在錄製中擷取滑鼠游標」，錄影中不要切換到其他視窗（否則畫面會凍結）。
3. 錄網頁前按 Ctrl+Shift+B 隱藏書籤列。
4. 操作放慢、每步停 1–2 秒、點選前先讓游標停在要點的按鈕上（避免截圖看起來點錯位置）、開勿打擾。
5. 用測試帳號或測試資料，避免錄到個資。

完成第一份手冊後會寫入 `.onboarded`（已列在 `.gitignore`）。之後只有在還沒錄影、或抽幀出現 `low_fps`／`freeze` 警告時，才會再顯示這段建議。
想再看一次首次引導，刪掉 skill 資料夾裡的 `.onboarded` 即可。

## 用法（手動執行腳本）

```powershell
# 0. （沒有錄影檔時）偵測本機錄影工具，輸出 JSON
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\detect_recorders.ps1

# 1. 抽關鍵畫面（輸出 images/、thumbs/、frames.json）
py -3.11 scripts\extract_frames.py 錄影.mp4 --out 輸出資料夾 --ocr

# 2. 寫好 輸出資料夾\steps.json（格式見 SKILL.md 或 examples\output\steps.json）

# 3. 遮蔽敏感資訊（需要時；先看建議，再套用，不要遮就跳過）
py -3.11 scripts\mask_regions.py 輸出資料夾 --suggest
py -3.11 scripts\mask_regions.py 輸出資料夾 輸出資料夾\masks.json
#    確認過遮蔽（或確定不用遮）後，在 steps.json 設 "privacy_checked": true

# 4. 組手冊（manual.md、manual.html、manual.pdf；加 --no-pdf 不產 PDF）
py -3.11 scripts\build_manual.py 輸出資料夾\steps.json
```

`extract_frames.py` 開始前會先檢查：ffmpeg／ffprobe 是否存在、檔案是不是影片（有視訊軌、讀得到長度、寬高至少 320）、長度是否超過 `--max-minutes`（超過以 exit 2 結束並建議分段）。
`--out` 指定的資料夾已存在時不覆蓋，自動改用 `<名稱>_2`、`_3`…，並在第一行印出實際路徑。

`extract_frames.py` 主要參數：

| 參數 | 預設 | 說明 |
|---|---|---|
| `--max-frames` | 20 | 最多保留幾張 |
| `--scene` | 0.005 | 變動像素比例超過此值算一次畫面切換（越小越敏感） |
| `--interval` | 2.5 | 切換次數少於 `--min-scenes`（預設 3）時，用此間隔秒數補幀 |
| `--settle` | 0.3 | 畫面停止變動後再等幾秒才擷取，避開載入中的畫面 |
| `--dedupe` | 0.003 | 相鄰兩張變動比例低於此值視為重複並刪除 |
| `--ocr` | 關 | 用 Windows 內建 OCR 讀圖內文字，寫進 `frames.json` |
| `--max-minutes` | 15 | 影片超過此長度就停下來（exit 2），請操作者決定分段或整段處理 |
| `--allow-long` | 關 | 確認要整段處理超過 `--max-minutes` 的長影片 |
| `--freeze-seconds` | 10 | 畫面完全靜止的連續區段達此秒數就發出凍結警告 |

`build_manual.py` 參數：

| 參數 | 預設 | 說明 |
|---|---|---|
| `--no-pdf` | 關 | 不產生 `manual.pdf`（找不到瀏覽器、或只需要 HTML 時由操作者明確選擇） |

`steps.json` 必須有 `author`（製作者名稱）；`created_at` 選填（`YYYY-MM-DD HH:MM`，不填用產出當下的本地時間）。
md／html／pdf 的標題下方都會顯示「製作：<author>｜製作時間：<created_at>」。

PDF 檔案屬性的標題與作者：Edge／Chrome 的 `--print-to-pdf` 不會帶入作者，所以列印後由 `build_manual.py` 用純 Python 對 PDF 做**增量更新**——
在檔尾附加一個新的 Info 物件（Title、Author，保留原本的 Creator、Producer、日期）、只含該物件的 xref 段，以及帶 `/Prev` 指回原 xref 的新 trailer，不重寫原本內容。
Edge 產出的是傳統 xref 表（`xref` 關鍵字＋`trailer`），只支援這種；遇到 xref stream 會報錯，不會默默略過。

`manual.pdf` 正在被其他程式開啟（例如瀏覽器分頁或 PDF 閱讀器開著）而無法刪除或寫入時，會報錯「manual.pdf 正在被其他程式開啟，請先關閉 PDF 再重跑」並附路徑。

`steps.json` 必須有 `"privacy_checked": true`（操作者已明確回答過遮蔽確認），否則報錯「尚未完成敏感資訊遮蔽確認」且不產出任何檔案。
輸出資料夾有 `images_original\` 卻沒有 `masks_applied.json`（或反過來）時，視為遮蔽狀態不一致，也會報錯。

## 遮蔽敏感資訊與標示正確按鈕（`mask_regions.py`）

```powershell
py -3.11 scripts\mask_regions.py 輸出資料夾 masks.json [--style box|mosaic]   # 套用遮蔽／紅框
py -3.11 scripts\mask_regions.py 輸出資料夾 --suggest                         # 依 OCR 文字框印出建議（不改圖）
py -3.11 scripts\mask_regions.py 輸出資料夾 --restore                         # 還原成未遮蔽的原圖
```

`masks.json` 格式：

```json
{
  "coord": "thumb",
  "masks": [
    {"image": "images/frame_02.png", "boxes": [[520, 40, 180, 30]], "reason": "客戶 Email"},
    {"image": "images/frame_04.png", "boxes": [{"box": [610, 340, 150, 50], "type": "highlight"}], "reason": "正確按鈕"}
  ]
}
```

- `coord`：`thumb` 表示座標以 `thumbs\` 縮圖為準（AI 看的就是縮圖），依寬度比例換算回原圖；`original` 表示原圖像素座標。
- `boxes`：每個區塊寫 `[x, y, w, h]`（左上角為原點，等同遮蔽），或 `{"box": [x, y, w, h], "type": "mask" | "highlight"}`；`reason` 選填，記錄原因。
- `highlight`：紅色外框（`#E00000`，粗細約原圖寬度 0.4%、至少 3px），不填滿，用來標示正確按鈕。
  用途是點錯位置防呆：截圖裡游標停的按鈕和下一張實際打開的畫面對不上時，AI 會先問操作者實際點的是哪個，確認後依縮圖提出座標加紅框，看預覽確認。
  同一張圖先畫遮蔽再畫紅框；座標一樣可用 `thumb` 換算、一樣從 `images_original\` 重畫、可 `--restore`。
- 第一次套用會把 `images\` 整個備份到 `images_original\`，之後每次都從備份重新遮：可以反覆調整座標重跑，上次遮過但這次沒列的圖會還原。
- 套用後重產對應的 `thumbs\` 縮圖給操作者確認，並寫 `masks_applied.json`（套用內容、換算後的原圖座標、時間）。
- 先驗證全部內容再動手：JSON 格式錯、圖檔不存在、座標超出圖片或寬高 ≤ 0 時直接報錯，不寫任何檔。
- `--restore` 把 `images_original\` 複製回 `images\`、重產縮圖，再刪除 `images_original\` 與 `masks_applied.json`；沒有套用過遮蔽時報錯。

**色塊 vs 馬賽克**：預設 `--style box` 用實心深色色塊（`#1A1A1A`），內容完全不可還原。
`--style mosaic` 把區塊縮小到約 1/12 再放大，看起來比較柔和，但對小字、數字仍可能認得出來（實測 56px 大字打完馬賽克，數字「3」還看得出輪廓），所以不是預設。

**OCR 文字框輔助定位**：抽幀加 `--ocr` 時，`frames.json` 每張會有 `ocr_lines: [{"text", "x", "y", "w", "h"}]`（每行文字與原圖座標的文字框，由 `Windows.Media.Ocr` 的字詞框合併）。
`--suggest` 依這些文字用正規式找 Email、台灣手機（09xx-xxx-xxx）、市話（02-xxxx-xxxx、(02)xxxx-xxxx）、身分證字號，印出建議的 masks.json（縮圖座標）。限制：
- 只有格式固定的資料抓得到；姓名、密碼、客戶名稱、金額等要靠人看縮圖補。
- OCR 會漏字、認錯（例如把「-」認成「一」，已處理；其他錯字仍可能漏抓），也可能把小字整行漏掉。
- 文字框是「整行」，同一行的標籤文字（例如「Email」）會一起被遮。
- 只是建議，不會自動套用；一定要看縮圖、問過操作者再套用。

## 錄影品質警告

`extract_frames.py` 會檢查兩件事，有問題只警告、不中斷（exit 0），警告以「⚠ 錄影品質警告」開頭印出，並寫進 `frames.json` 的 `warnings`（每筆有 `type`、`message` 與數據）：

| type | 判斷方式 | 觸發條件 |
|---|---|---|
| `low_fps` | `ffprobe -count_packets` 取封包數與標稱幀率（`r_frame_rate`），實際 fps＝封包數 ÷ 長度 | 實際 fps 低於標稱的 50% |
| `freeze` | 偵測切換時每秒 4 張的取樣中，相鄰變動比例＝0 的連續區段（至少 3 秒才列入） | 最長一段 ≥ `--freeze-seconds`（預設 10 秒），或凍結總長 ≥ 影片長度 50% |

常見原因是 Xbox Game Bar：預設不錄滑鼠游標、只錄單一應用程式視窗，切換視窗或電腦效能不足時畫面會凍結、掉幀（實例：標稱 60fps、57 秒只有 899 幀，約 15.8fps，沒有游標、畫面卡住）。建議改用剪取工具（Win+Shift+R）或 OBS Studio 重錄。
短於 3 秒的靜止不算凍結，避免把操作者照建議「每步停 1–2 秒」誤判；但畫面本來就長時間不動（例如閱讀說明）仍可能觸發，由操作者判斷。
讀不到標稱幀率或封包數時，會印出「注意：…無法檢查幀率」，不會默默略過。

## 錄影建議

同「安裝後第一步」的五點。補充：書籤列沒隱藏時，OCR 常只讀到書籤文字（實測案例）；Game Bar 沒開游標擷取時，手冊看不出點了哪裡。

## 運作方式

1. **偵測畫面切換**：ffmpeg 把影片解成每秒 4 張、160×90 灰階，numpy 計算相鄰取樣的「變動像素比例」。
   沒用 ffmpeg 內建的 `scene` 分數，因為它是為影片剪接設計，實測對淺色介面的局部變化（開對話框、換頁）偵測不到。
   連續變動（動畫、捲動、載入）合併成一次，取畫面穩定後的時間點。游標移動面積太小不會被當成切換。
2. **補幀**：切換少於 3 次時，以固定間隔補幀。
3. **去重與上限**：相鄰幾乎相同的畫面刪除；超過上限時沿時間軸平均挑選（頭尾必留），並在輸出提示刪了幾張。
4. **縮圖**：原圖 PNG 留給手冊；另存寬 800px JPG 給 AI 看。
5. **OCR（選用）**：經 PowerShell 呼叫 `Windows.Media.Ocr`（`scripts/ocr.ps1`），預設繁體中文；每行另外輸出文字框座標（`ocr_lines`），給遮蔽定位用。
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
├─ frames.json          # 抽幀結果（時間碼、檔名、OCR 文字與文字框、錄影品質警告）
├─ images/              # 原尺寸截圖（手冊用；套用遮蔽後是遮過的版本）
├─ images_original/     # 套用遮蔽時才有：未遮蔽的原圖備份（--restore 後刪除；不要給外部）
├─ masks_applied.json   # 套用遮蔽／紅框時才有：套用的區塊與時間
├─ thumbs/              # 寬 800px 縮圖（AI 看）
├─ steps.json           # AI 寫的步驟說明（可手改後重組；需有 privacy_checked）
├─ manual.md            # Markdown，圖片相對路徑（需連同 images/ 一起給）
├─ manual.html          # 單檔 HTML，圖片 base64 內嵌，直接傳給同事
└─ manual.pdf           # A4 PDF，用本機 Edge／Chrome 以 headless 模式列印 manual.html，再寫入標題與作者
```

範例見 `examples/`：`make_test_video.sh` 用 ffmpeg 產生 5 個畫面的測試影片，`output/` 是跑完的結果（非真實系統畫面）。

## 限制

- 任何一步失敗都會清掉本次新建的整個輸出資料夾，不留殘缺結果，可用同一個 `--out` 重跑。
- PDF 依賴本機的 Edge 或 Chrome；都沒有時 `build_manual.py` 會報錯（md、html 已產出），可安裝瀏覽器或加 `--no-pdf`。
- 錄影工具偵測只涵蓋剪取工具、Xbox Game Bar、Clipchamp、OBS Studio、ShareX，其他工具不會列出；macOS 不跑偵測腳本，直接用系統內建的 Cmd+Shift+5。
- 只處理畫面，不處理聲音；看不出操作意圖時需要操作者口頭補充。
- 不追蹤滑鼠點選位置，截圖上不會自動標示點了哪裡。
- 畫面只有很小區域變化（例如只改一個小欄位）時可能偵測不到，可調低 `--scene`；捲動很多的錄影可能抽出較多相似畫面，可調高 `--dedupe` 或在第 3 步刪減。
- OCR 僅限 Windows，且需安裝對應語言的 OCR 套件（繁中為 `zh-Hant-TW`）。
- `manual.html` 內嵌圖片，20 張 1080p 截圖約 5–15 MB。
- 遮蔽只做操作者確認過的區塊，`--suggest` 只能抓格式固定的資料，不保證找出所有敏感資訊；給外部前仍需人工檢查。
- 遮蔽後 `images_original\` 仍保留未遮蔽的原圖，整個輸出資料夾不要直接給外部；給外部只傳 `manual.html`／`manual.pdf`。
