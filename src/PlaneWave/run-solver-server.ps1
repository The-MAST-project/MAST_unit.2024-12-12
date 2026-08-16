
param (
    [string]$StdoutLogFilePath,  # The path to the file where stdout will be redirected
    [string]$StderrLogFilePath   # The path to the file where stderr will be redirected
)

# Change to the desired directory
cd "C:\Program Files (x86)\PlaneWave Instruments\ps3cli"

# Run the executable with the specified parameters
$process = Start-Process `
    -NoNewWindow `
    -FilePath "ps3cli-20240829.exe" `
    -ArgumentList "--server", "--port=9896" `
    -PassThru `
    -RedirectStandardOutput $StdoutLogFilePath `
    -RedirectStandardError  $StderrLogFilePath

Write-Output "Started process with pid: $($process.Id)"

exit
