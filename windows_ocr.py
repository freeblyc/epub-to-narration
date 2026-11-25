"""Use Windows' installed OCR languages without uploading book pages."""

import base64
import json
import os
from pathlib import Path
import subprocess


WINDOWS_OCR = r'''
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Storage.StorageFile,Windows.Storage,ContentType=WindowsRuntime]
$null = [Windows.Storage.Streams.IRandomAccessStream,Windows.Storage.Streams,ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder,Windows.Graphics.Imaging,ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.SoftwareBitmap,Windows.Graphics.Imaging,ContentType=WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime]
$null = [Windows.Media.Ocr.OcrResult,Windows.Foundation,ContentType=WindowsRuntime]
$null = [Windows.Globalization.Language,Windows.Globalization,ContentType=WindowsRuntime]
$asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -like 'IAsyncOperation*'
} | Select-Object -First 1
function Await-Result($operation, [Type]$type) {
    $task = $asTask.MakeGenericMethod($type).Invoke($null, @($operation))
    $task.GetAwaiter().GetResult()
}
$engine = $null
foreach ($language in [Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages) {
    if ($language.LanguageTag -like ($env:EPUB_OCR_LANGUAGE + '*')) {
        $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($language)
        if ($engine) { break }
    }
}
if (-not $engine) {
    [Console]::WriteLine((@{missing=$env:EPUB_OCR_LANGUAGE} | ConvertTo-Json -Compress))
    exit 2
}
[Console]::WriteLine((@{language=$engine.RecognizerLanguage.LanguageTag} | ConvertTo-Json -Compress))
$files = Get-Content -LiteralPath $env:EPUB_OCR_MANIFEST -Raw -Encoding UTF8 | ConvertFrom-Json
foreach ($page in $files) {
    $stream = $null
    $bitmap = $null
    try {
        $file = Await-Result ([Windows.Storage.StorageFile]::GetFileFromPathAsync($page.file)) ([Windows.Storage.StorageFile])
        $stream = Await-Result ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
        $decoder = Await-Result ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
        $limit = [Windows.Media.Ocr.OcrEngine]::MaxImageDimension
        $scale = [Math]::Min(1.0, $limit / [Math]::Max($decoder.PixelWidth, $decoder.PixelHeight))
        $transform = [Windows.Graphics.Imaging.BitmapTransform]::new()
        $transform.ScaledWidth = [uint32][Math]::Max(1, [Math]::Floor($decoder.PixelWidth * $scale))
        $transform.ScaledHeight = [uint32][Math]::Max(1, [Math]::Floor($decoder.PixelHeight * $scale))
        $bitmap = Await-Result ($decoder.GetSoftwareBitmapAsync(
            [Windows.Graphics.Imaging.BitmapPixelFormat]::Bgra8,
            [Windows.Graphics.Imaging.BitmapAlphaMode]::Ignore,
            $transform, [Windows.Graphics.Imaging.ExifOrientationMode]::IgnoreExifOrientation,
            [Windows.Graphics.Imaging.ColorManagementMode]::DoNotColorManage
        )) ([Windows.Graphics.Imaging.SoftwareBitmap])
        $result = Await-Result ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
        $lines = [System.Collections.Generic.List[object]]::new()
        foreach ($line in $result.Lines) {
            $left = [double]::MaxValue
            $right = $bottom = 0
            $top = [double]::MaxValue
            foreach ($word in $line.Words) {
                $r = $word.BoundingRect
                $left = [Math]::Min($left, $r.X)
                $top = [Math]::Min($top, $r.Y)
                $right = [Math]::Max($right, $r.X + $r.Width)
                $bottom = [Math]::Max($bottom, $r.Y + $r.Height)
            }
            if ($line.Text.Trim() -and $right -gt $left -and $bottom -gt $top) {
                $lines.Add(@{text=$line.Text; left=$left; top=$top; right=$right; bottom=$bottom})
            }
        }
        [Console]::WriteLine((@{number=$page.number; width=$bitmap.PixelWidth; height=$bitmap.PixelHeight;
            lines=$lines.ToArray()} | ConvertTo-Json -Compress -Depth 4))
    } catch {
        [Console]::WriteLine((@{number=$page.number; error=$_.Exception.GetType().Name} | ConvertTo-Json -Compress))
    } finally {
        if ($bitmap) { $bitmap.Dispose() }
        if ($stream) { $stream.Dispose() }
    }
}
'''


def run_windows_ocr(manifest: Path, language: str):
    system_root = os.environ.get("SystemRoot")
    if os.name != "nt" or not system_root:
        raise ValueError("图片版 EPUB 需要 Windows 10/11 的系统 OCR。")
    powershell = Path(system_root) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    if not powershell.is_file():
        raise ValueError("找不到 Windows PowerShell，无法调用系统 OCR。")
    encoded = base64.b64encode(WINDOWS_OCR.encode("utf-16-le")).decode("ascii")
    env = dict(os.environ, EPUB_OCR_MANIFEST=str(manifest), EPUB_OCR_LANGUAGE=language)
    command = [str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]
    # Stderr goes to a file so the OCR process can always keep writing page results.
    errors = manifest.with_suffix(".stderr")
    with errors.open("wb") as error_log, subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=error_log, text=True, encoding="utf-8",
        errors="replace", env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ) as worker:
        try:
            for row in worker.stdout:
                if row.strip():
                    record = json.loads(row)
                    if "missing" in record:
                        raise ValueError(f"Windows 未安装 {language} OCR 语言。请在系统语言功能中安装对应的 OCR 组件。")
                    yield record
            status = worker.wait()
            if status:
                detail = errors.read_text(encoding="utf-8", errors="replace").strip()
                raise ValueError(f"Windows OCR 无法运行，退出码 {status}。{detail[:240]}")
        finally:
            if worker.poll() is None:
                worker.kill()
