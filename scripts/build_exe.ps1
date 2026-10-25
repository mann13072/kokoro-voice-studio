<#
  Builds the Windows app into dist\KokoroTTS\KokoroTTS.exe

  Usage (from anywhere):
      pip install -r requirements-dev.txt
      .\scripts\build_exe.ps1            # build the folder
      .\scripts\build_exe.ps1 -Zip       # build and also make dist\KokoroVoiceStudio-windows.zip

  The voice model is NOT bundled. The app downloads it on first run (about 190 MB).
#>
param([switch]$Zip)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

# Libraries that other packages can drag in but this app never uses; leaving them out keeps the download small.
$exclude = "torch","torchvision","torchaudio","tensorflow","h5py","pygraphviz","matplotlib","pandas","scipy","sklearn",
           "jedi","IPython","cv2","transformers","pyarrow","polars","duckdb","llvmlite","numba","plotly","sphinx",
           "skimage","grpc","botocore","boto3","babel","sympy"

$arguments = @(
    "--noconfirm", "--clean", "--onedir", "--windowed",
    "--name", "KokoroTTS",
    "--icon", "$root\assets\kokoro.ico",
    "--specpath", "$root\build",
    "--workpath", "$root\build\work",
    "--distpath", "$root\dist",
    # window and drag-and-drop
    "--collect-all", "customtkinter", "--collect-all", "tkinterdnd2",
    "--hidden-import", "tkinter", "--hidden-import", "tkinter.ttk",
    "--hidden-import", "tkinter.filedialog", "--hidden-import", "tkinter.messagebox",
    "--hidden-import", "PIL", "--hidden-import", "PIL.Image", "--hidden-import", "PIL.ImageTk",
    # speech
    "--collect-all", "espeakng_loader", "--collect-data", "kokoro_onnx",
    "--collect-all", "phonemizer", "--collect-all", "language_tags",
    # audio
    "--collect-all", "sounddevice", "--collect-all", "_sounddevice_data",
    # reading documents
    "--collect-submodules", "markdown",
    "--collect-submodules", "pdfminer", "--collect-data", "pdfminer",
    "--collect-data", "docx",
    "--hidden-import", "charset_normalizer", "--hidden-import", "cryptography"
)
foreach ($name in $exclude) { $arguments += @("--exclude-module", $name) }
$arguments += "$root\kokoro_app.py"

python -m PyInstaller @arguments
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

$folder = Join-Path $root "dist\KokoroTTS"
$mb = [math]::Round((Get-ChildItem $folder -Recurse -File | Measure-Object Length -Sum).Sum / 1MB)
Write-Host "Built $folder ($mb MB)"

if ($Zip) {
    $zipPath = Join-Path $root "dist\KokoroVoiceStudio-windows.zip"
    if (Test-Path $zipPath) { Remove-Item $zipPath }
    Compress-Archive -Path $folder -DestinationPath $zipPath
    Write-Host ("Zip: {0} ({1} MB)" -f $zipPath, [math]::Round((Get-Item $zipPath).Length / 1MB))
}
