# HP 34401A: COM3, 9600 8N2; native Windows SerialPort, no extra packages.
# Ctrl-C device clear aborts a pending operation and clears interface buffers.
# No *RST, measurement configuration or measurement-trigger commands are sent.
# User manual: https://www.keysight.com/gb/en/assets/9018-01063/user-manuals/9018-01063.pdf
# Two separate sessions verify repeated IDN replies and an empty error queue.
$ErrorActionPreference = 'Stop'
$recoveryLog = [System.Collections.Generic.List[string]]::new()
$meterPort = $null

function Invoke-MeterQuery([string]$command)
{
    $deadline = [DateTime]::UtcNow.AddSeconds(3)
    while (-not $meterPort.DsrHolding)
    {
        if ([DateTime]::UtcNow -gt $deadline)
        {
            throw 'Instrument DTR / PC DSR not ready; no command sent'
        }
        Start-Sleep -Milliseconds 20
    }
    $meterPort.Write($command + "`n")
    $reply = ''
    $deadline = [DateTime]::UtcNow.AddSeconds(4)
    while ([DateTime]::UtcNow -lt $deadline)
    {
        $reply += $meterPort.ReadExisting()
        if ($reply.Contains("`n"))
        {
            $line = $command + ' -> ' + $reply.Trim()
            Write-Host $line
            $recoveryLog.Add($line)
            return $reply.Trim()
        }
        Start-Sleep -Milliseconds 20
    }
    throw ('No complete response to ' + $command + '; received ' + $reply.Length + ' characters')
}

try
{
    foreach ($session in 1..2)
    {
        $meterPort = [System.IO.Ports.SerialPort]::new('COM3', 9600, [System.IO.Ports.Parity]::None, 8, [System.IO.Ports.StopBits]::Two)
        $meterPort.Handshake = [System.IO.Ports.Handshake]::None
        $meterPort.DtrEnable = $true
        $meterPort.RtsEnable = $true
        $meterPort.ReadTimeout = 1000
        $meterPort.WriteTimeout = 2000
        $meterPort.Open()
        $meterPort.DtrEnable = $false
        Start-Sleep -Milliseconds 200
        Write-Host "Session $session : PC DTR low, DSR=$($meterPort.DsrHolding)"
        # The manual specifies Ctrl-C with instrument DSR low for device clear.
        $meterPort.Write([string][char]3)
        Start-Sleep -Milliseconds 200
        $meterPort.DtrEnable = $true
        Start-Sleep -Milliseconds 300
        $old = $meterPort.ReadExisting()
        Write-Host "PC DTR high, DSR=$($meterPort.DsrHolding); drained bytes=$($old.Length)"
        $meterPort.Write("SYST:REM`n")
        Start-Sleep -Milliseconds 150
        foreach ($attempt in 1..5)
        {
            $identity = Invoke-MeterQuery '*IDN?'
            if ($identity -notmatch '^HEWLETT-PACKARD,34401A,')
            {
                throw ('Unexpected identity: ' + $identity)
            }
        }
        $queueEmpty = $false
        foreach ($attempt in 1..25)
        {
            $reply = Invoke-MeterQuery 'SYST:ERR?'
            if ($reply -match '^\+?0,')
            {
                $queueEmpty = $true
                break
            }
        }
        if (-not $queueEmpty)
        {
            throw 'Error queue did not reach zero'
        }
        $function = Invoke-MeterQuery 'FUNC?'
        $meterPort.Write("SYST:LOC`n")
        Start-Sleep -Milliseconds 150
        $meterPort.Close()
        $meterPort.Dispose()
        $meterPort = $null
        Start-Sleep -Milliseconds 300
    }
}
finally
{
    if ($null -ne $meterPort)
    {
        if ($meterPort.IsOpen)
        {
            $meterPort.DtrEnable = $false
            $meterPort.Write([string][char]3)
            Start-Sleep -Milliseconds 200
            $meterPort.DtrEnable = $true
            Start-Sleep -Milliseconds 200
            $meterPort.Write("SYST:LOC`n")
            Start-Sleep -Milliseconds 150
            $meterPort.Close()
        }
        $meterPort.Dispose()
    }
    $recoveryLog | Add-Content -LiteralPath (Join-Path $PSScriptRoot '..\data\rf-inventory\34401A-recovery.txt') -Encoding utf8
}
