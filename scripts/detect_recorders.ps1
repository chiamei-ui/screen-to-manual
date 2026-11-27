# 偵測本機已安裝的螢幕錄影工具，輸出 JSON 陣列：
# [{ name, found, evidence, how_to_record, save_location, note }]
# 用法：powershell -NoProfile -ExecutionPolicy Bypass -File detect_recorders.ps1
# 偵測本身失敗（例如 Get-AppxPackage 拋錯）時直接報錯結束，不輸出不完整的結果。
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$build = [Environment]::OSVersion.Version.Build
$videos = [Environment]::GetFolderPath("MyVideos")
$documents = [Environment]::GetFolderPath("MyDocuments")
if (-not $videos) { throw "取不到「影片」資料夾路徑（GetFolderPath MyVideos 回傳空值）" }
if (-not $documents) { throw "取不到「文件」資料夾路徑（GetFolderPath MyDocuments 回傳空值）" }

# Appx 套件：Get-AppxPackage 一次只查一個名稱
function Find-Appx([string]$name) {
    $pkg = Get-AppxPackage -Name $name | Sort-Object Version -Descending | Select-Object -First 1
    if ($pkg) { return "Appx 套件 $($pkg.Name) 版本 $($pkg.Version)" }
    return $null
}

# 解除安裝登錄檔（HKLM／HKCU，含 WOW6432Node）中 DisplayName 符合的項目
$uninstallRoots = @(
    "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
    "HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
    "HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
)
$uninstallEntries = @()
foreach ($root in $uninstallRoots) {
    if (-not (Test-Path -LiteralPath $root)) { continue }
    foreach ($key in Get-ChildItem -LiteralPath $root) {
        $display = $key.GetValue("DisplayName")
        if ($display) {
            $uninstallEntries += [pscustomobject]@{ name = [string]$display; version = $key.GetValue("DisplayVersion"); key = $key.Name }
        }
    }
}
function Find-Uninstall([string]$pattern) {
    $hit = $uninstallEntries | Where-Object { $_.name -match $pattern } | Select-Object -First 1
    if ($hit) { return "登錄檔 $($hit.key)（DisplayName：$($hit.name)，版本 $($hit.version)）" }
    return $null
}

# 開始功能表捷徑（補充依據）
$startMenuRoots = @(
    (Join-Path $env:ProgramData "Microsoft\Windows\Start Menu\Programs"),
    (Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs")
)
$shortcuts = @()
foreach ($root in $startMenuRoots) {
    if (Test-Path -LiteralPath $root) {
        $shortcuts += Get-ChildItem -LiteralPath $root -Recurse -Filter *.lnk
    }
}
function Find-Shortcut([string]$pattern) {
    $hit = $shortcuts | Where-Object { $_.BaseName -match $pattern } | Select-Object -First 1
    if ($hit) { return "開始功能表捷徑 $($hit.FullName)" }
    return $null
}

function Find-Exe([string[]]$paths) {
    foreach ($p in $paths) {
        if ($p -and (Test-Path -LiteralPath $p -PathType Leaf)) { return "執行檔 $p" }
    }
    return $null
}

function New-Result($name, [string[]]$evidence, $how, $save, $note) {
    $ev = @($evidence | Where-Object { $_ })
    [pscustomobject]@{
        name          = $name
        found         = ($ev.Count -gt 0)
        evidence      = $(if ($ev.Count -gt 0) { $ev -join "；" } else { $null })
        how_to_record = $how
        save_location = $save
        note          = $note
    }
}

$results = @()

# 剪取工具（Snipping Tool）
$snipNote = "Windows 11（build 22000 以上）的剪取工具才有螢幕錄製功能；可錄整個螢幕或框選區域。若版本較舊，畫面上沒有錄影按鈕，需先到 Microsoft Store 更新剪取工具。若沒有開啟自動儲存，錄完要在預覽視窗按「另存新檔」手動存檔。"
if ($build -lt 22000) { $snipNote = "此電腦為 build $build（低於 22000，不是 Windows 11），剪取工具不支援螢幕錄製。" + $snipNote }
$results += New-Result "剪取工具（螢幕錄製）" @(Find-Appx "Microsoft.ScreenSketch") `
    "按 Win+Shift+R（較新版本支援）直接進入錄影；或從開始功能表開啟「剪取工具」，切換到錄影模式（攝影機圖示）→ 按「新增」→ 框選要錄的範圍 → 按「開始」；錄完按停止。" `
    (Join-Path $videos "Screen Recordings") `
    $snipNote

# Xbox Game Bar
$results += New-Result "Xbox Game Bar" @(Find-Appx "Microsoft.XboxGamingOverlay") `
    "先點一下要錄的應用程式視窗，按 Win+Alt+R 開始錄影，再按一次 Win+Alt+R 停止；也可按 Win+G 開啟 Game Bar 後在「擷取」面板按錄製。" `
    (Join-Path $videos "Captures") `
    "只能錄單一應用程式視窗，不能錄桌面與檔案總管，切換視窗後不會跟著錄；若要錄跨視窗操作請改用剪取工具或 OBS Studio。"

# Clipchamp
$results += New-Result "Clipchamp" @(Find-Appx "Clipchamp.Clipchamp") `
    "開啟 Clipchamp → 建立影片 → 左側「錄製與建立」→「螢幕」→ 選擇要分享的螢幕或視窗後開始錄製；錄完後按「儲存並編輯」，再按右上角「匯出」輸出 mp4。" `
    (Join-Path ([Environment]::GetFolderPath("UserProfile")) "Downloads") `
    "錄完的影片在 Clipchamp 專案內，一定要「匯出」才會產生影片檔；若匯出時沒有另選位置，通常存到「下載」資料夾，請以匯出完成時顯示的位置為準。首次使用可能需要登入 Microsoft 帳號。"

# OBS Studio
$results += New-Result "OBS Studio" @(
    $(if (Test-Path -LiteralPath "HKLM:\SOFTWARE\OBS Studio") { "登錄檔 HKLM:\SOFTWARE\OBS Studio" }),
    (Find-Uninstall "^OBS Studio"),
    (Find-Exe @("C:\Program Files\obs-studio\bin\64bit\obs64.exe")),
    (Find-Shortcut "^OBS Studio")
) `
    "開啟 OBS Studio → 在「來源」按「+」新增「螢幕擷取」或「視窗擷取」→ 按右下角「開始錄製」，錄完按「停止錄製」。" `
    $videos `
    "存放位置可在「設定 → 輸出 → 錄影路徑」變更，以該設定為準；第一次使用需先設定擷取來源。"

# ShareX
$results += New-Result "ShareX" @(
    (Find-Uninstall "^ShareX"),
    (Find-Exe @("C:\Program Files\ShareX\ShareX.exe")),
    (Find-Shortcut "^ShareX")
) `
    "按 Shift+Print Screen 開始錄影 → 框選要錄的範圍 → 再按一次 Shift+Print Screen（或按畫面上的停止）結束。" `
    (Join-Path $documents "ShareX\Screenshots") `
    "影片會放在 Screenshots 底下依年月分的子資料夾；第一次錄影時 ShareX 可能會提示下載 ffmpeg。"

ConvertTo-Json -InputObject @($results) -Depth 3
