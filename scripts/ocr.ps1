# 以 Windows 內建 OCR（Windows.Media.Ocr）讀取圖片文字，輸出 JSON：[{ "path": ..., "text": ... }]
# 用法：powershell -NoProfile -ExecutionPolicy Bypass -File ocr.ps1 -Lang zh-Hant-TW img1.png img2.png
[CmdletBinding(PositionalBinding = $false)]
param(
    [string]$Lang = "zh-Hant-TW",
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$Paths
)
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
if (-not $Paths) { throw "沒有給任何圖片路徑" }

Add-Type -AssemblyName System.Runtime.WindowsRuntime
[Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null
[Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime] | Out-Null

# WinRT 非同步呼叫轉同步
$asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
} | Select-Object -First 1
function Wait-WinRt($op, [Type]$resultType) {
    $task = $asTask.MakeGenericMethod($resultType).Invoke($null, @($op))
    $task.Wait()
    return $task.Result
}

$language = New-Object Windows.Globalization.Language($Lang)
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($language)
if ($null -eq $engine) {
    throw "無法建立 OCR 引擎：語言 $Lang 未安裝。可用語言：" + (([Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages | ForEach-Object { $_.LanguageTag }) -join ", ")
}

$results = @()
foreach ($p in $Paths) {
    $full = (Resolve-Path -LiteralPath $p).Path
    $file = Wait-WinRt ([Windows.Storage.StorageFile]::GetFileFromPathAsync($full)) ([Windows.Storage.StorageFile])
    $stream = Wait-WinRt ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
    try {
        $decoder = Wait-WinRt ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
        $bitmap = Wait-WinRt ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
        $ocr = Wait-WinRt ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
        $lines = @($ocr.Lines | ForEach-Object { $_.Text })
        $results += [pscustomobject]@{ path = $p; text = ($lines -join "`n") }
    }
    finally {
        $stream.Dispose()
    }
}
ConvertTo-Json -InputObject @($results) -Depth 3 -Compress
