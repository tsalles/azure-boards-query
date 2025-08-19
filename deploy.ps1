
$resourceGroup = $args[0]
$functionAppName = $args[1]


$FolderPath = Get-Location
$FuncIgnorePath = Join-Path $FolderPath ".funcignore"
$Destination = Join-Path $FolderPath "app.zip"
if (Test-Path $Destination) {
    Remove-Item $Destination -Force
}
$Exclude = Import-Csv -Path $FuncIgnorePath -Header "Exclude"
$Files = Get-ChildItem -Path $FolderPath -Exclude $Exclude.Exclude
Compress-Archive -Path $Files -DestinationPath $Destination -CompressionLevel Fastest

az functionapp deployment source config-zip -g $resourceGroup -n $functionAppName --src $Destination