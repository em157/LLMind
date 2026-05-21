<#
.SYNOPSIS
    Windows built-in OCR via Windows.Media.Ocr (WinRT). No Tesseract required.
    Requires Windows 10/11.
.PARAMETER ImagePath
    Absolute path to the PNG/BMP image to OCR. Must be an absolute path.
.OUTPUT
    JSON array of OCR blocks: [{text, confidence, bbox:[x1,y1,x2,y2]}, ...]
#>
param(
    [Parameter(Mandatory=$true)]
    [string]$ImagePath
)

Set-StrictMode -Off
$ErrorActionPreference = 'Stop'

try {
    Add-Type -AssemblyName System.Runtime.WindowsRuntime

    # Load WinRT types
    $null = [Windows.Media.Ocr.OcrEngine,          Windows.Foundation, ContentType=WindowsRuntime]
    $null = [Windows.Storage.StorageFile,           Windows.Foundation, ContentType=WindowsRuntime]
    $null = [Windows.Storage.FileAccessMode,        Windows.Foundation, ContentType=WindowsRuntime]
    $null = [Windows.Graphics.Imaging.BitmapDecoder,Windows.Foundation, ContentType=WindowsRuntime]
    $null = [Windows.Graphics.Imaging.SoftwareBitmap,Windows.Foundation,ContentType=WindowsRuntime]

    # Helper: convert IAsyncOperation -> .NET Task -> Result
    function Await {
        param($WinRtTask, [Type]$ResultType)
        $methods = [System.WindowsRuntimeSystemExtensions].GetMethods()
        $asTask  = ($methods | Where-Object {
            $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.IsGenericMethod
        })[0]
        $netTask = $asTask.MakeGenericMethod($ResultType).Invoke($null, @($WinRtTask))
        $netTask.Wait(-1) | Out-Null
        $netTask.Result
    }

    # Resolve absolute path (WinRT GetFileFromPathAsync requires absolute)
    $absPath = [System.IO.Path]::GetFullPath($ImagePath)
    if (-not [System.IO.File]::Exists($absPath)) {
        Write-Error "File not found: $absPath"
        exit 2
    }

    $file    = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($absPath))             ([Windows.Storage.StorageFile])
    $stream  = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read))                   ([Windows.Storage.Streams.IRandomAccessStream])
    $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream))            ([Windows.Graphics.Imaging.BitmapDecoder])
    $bitmap  = Await ($decoder.GetSoftwareBitmapAsync())                                         ([Windows.Graphics.Imaging.SoftwareBitmap])

    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
    if (-not $engine) {
        Write-Error "WinRT OCR engine unavailable (no language pack?)"
        exit 3
    }

    $ocrResult = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])

    $blocks = @()
    foreach ($line in $ocrResult.Lines) {
        $words  = @()
        $minX   = [int]::MaxValue
        $minY   = [int]::MaxValue
        $maxX   = 0
        $maxY   = 0
        foreach ($word in $line.Words) {
            $words += $word.Text
            $r = $word.BoundingRect
            $x1 = [int]$r.X
            $y1 = [int]$r.Y
            $x2 = [int]($r.X + $r.Width)
            $y2 = [int]($r.Y + $r.Height)
            if ($x1 -lt $minX) { $minX = $x1 }
            if ($y1 -lt $minY) { $minY = $y1 }
            if ($x2 -gt $maxX) { $maxX = $x2 }
            if ($y2 -gt $maxY) { $maxY = $y2 }
        }
        $lineText = ($words -join ' ').Trim()
        if ($lineText) {
            $blocks += [PSCustomObject]@{
                text       = $lineText
                confidence = 0.88
                bbox       = @($minX, $minY, $maxX, $maxY)
            }
        }
    }

    $stream.Dispose()

    if ($blocks.Count -eq 0) {
        Write-Output '[]'
    } else {
        $blocks | ConvertTo-Json -Compress -Depth 5
    }
    exit 0
}
catch {
    Write-Error $_.Exception.Message
    exit 1
}
