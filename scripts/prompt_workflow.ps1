param(
    [ValidateSet('run', 'share', 'commit')]
    [string]$Action = 'run',

    [ValidateSet('xai', 'openai', 'anthropic', 'gemini')]
    [string]$Provider = 'xai',

    [string]$Model = '',
    [int]$KeyIndex = 1,
    [string]$PromptFile = 'prompts/PROMPT_1_WINDOWS_UI_TRIAGE.md',
    [string]$SystemInstructions = '',
    [string]$Temperature = '',
    [string]$MaxTokens = '',

    [string]$ShareOutput = 'shared',
    [string]$CommitMessage = 'Update prompt workflow assets'
)

$ErrorActionPreference = 'Stop'

function Get-ProviderUrl {
    param(
        [string]$ProviderName,
        [string]$ModelName = ''
    )
    switch ($ProviderName) {
        'xai' { return 'https://api.x.ai/v1/chat/completions' }
        'openai' { return 'https://api.openai.com/v1/chat/completions' }
        'anthropic' { return 'https://api.anthropic.com/v1/messages' }
        'gemini' {
            $effectiveModel = if ([string]::IsNullOrWhiteSpace($ModelName)) { 'gemini-2.0-flash' } else { $ModelName }
            return "https://generativelanguage.googleapis.com/v1beta/models/$effectiveModel`:generateContent"
        }
        default { throw "Unsupported provider: $ProviderName" }
    }
}

function Get-DefaultModel {
    param([string]$ProviderName)
    switch ($ProviderName) {
        'xai' { return 'grok-4.3' }
        'openai' { return 'gpt-4.1-mini' }
        'anthropic' { return 'claude-opus-4-5' }
        'gemini' { return 'gemini-2.0-flash' }
        default { throw "Unsupported provider: $ProviderName" }
    }
}

function Get-NormalizedPromptText {
    param([string]$FilePath)

    if (-not (Test-Path -LiteralPath $FilePath)) {
        throw "Prompt file not found: $FilePath"
    }

    $raw = Get-Content -LiteralPath $FilePath -Raw
    if ([string]::IsNullOrWhiteSpace($raw)) {
        throw "Prompt file is empty: $FilePath"
    }

    # Keep content compatible with single-line interactive prompt input.
    $singleLine = $raw -replace '\r\n|\n|\r', ' '
    $singleLine = $singleLine -replace '\s{2,}', ' '
    return $singleLine.Trim()
}

function Invoke-Run {
    param(
        [string]$RepoRoot,
        [string]$ProviderName,
        [string]$ModelName,
        [int]$ApiKeyIndex,
        [string]$PromptPath,
        [string]$SystemPrompt,
        [string]$Temp,
        [string]$MaxOut
    )

    $pythonExe = if ($env:PYTHON_EXE) { $env:PYTHON_EXE } else { 'python' }
    $promptText = Get-NormalizedPromptText -FilePath $PromptPath
    if ([string]::IsNullOrWhiteSpace($ModelName)) {
        $ModelName = Get-DefaultModel -ProviderName $ProviderName
    }
    $url = Get-ProviderUrl -ProviderName $ProviderName -ModelName $ModelName

    $inputLines = @(
        '2',
        $url,
        'POST',
        [string]$ApiKeyIndex,
        $ModelName,
        $promptText,
        $SystemPrompt,
        $Temp,
        $MaxOut,
        'q'
    )

    $payload = ($inputLines -join "`n") + "`n"

    Write-Host "Running provider=$ProviderName model=$ModelName keyIndex=$ApiKeyIndex prompt=$(Resolve-Path -LiteralPath $PromptPath)"
    $payload | & $pythonExe "$RepoRoot\main\LLMind.py"
}

function Invoke-Share {
    param(
        [string]$RepoRoot,
        [string]$OutputDir
    )

    $outputPath = Join-Path $RepoRoot $OutputDir
    New-Item -ItemType Directory -Path $outputPath -Force | Out-Null

    $timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $zipPath = Join-Path $outputPath "prompt-pack-$timestamp.zip"

    $includePaths = @(
        (Join-Path $RepoRoot 'prompts\*.md'),
        (Join-Path $RepoRoot 'scripts\prompt_workflow.ps1'),
        (Join-Path $RepoRoot 'main\PROMPT_WORKFLOW.cmd')
    )

    Compress-Archive -Path $includePaths -DestinationPath $zipPath -Force
    Write-Host "Share bundle created: $zipPath"
}

function Invoke-Commit {
    param(
        [string]$RepoRoot,
        [string]$Message
    )

    $branch = git -C $RepoRoot rev-parse --abbrev-ref HEAD
    if ($LASTEXITCODE -ne 0) {
        throw 'Unable to resolve git branch. Ensure this folder is a git repository.'
    }

    if ($branch -in @('main', 'master')) {
        throw "Refusing to auto-commit on $branch. Switch to a feature branch, then retry."
    }

    git -C $RepoRoot add -- prompts/*.md scripts/prompt_workflow.ps1 main/PROMPT_WORKFLOW.cmd
    if ($LASTEXITCODE -ne 0) {
        throw 'git add failed.'
    }

    $staged = git -C $RepoRoot diff --cached --name-only
    if (-not $staged) {
        Write-Host 'No staged changes found for prompt workflow files.'
        return
    }

    git -C $RepoRoot commit -m $Message
    if ($LASTEXITCODE -ne 0) {
        throw 'git commit failed.'
    }

    Write-Host "Committed prompt workflow files on branch: $branch"
}

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$resolvedPromptPath = (Resolve-Path (Join-Path $repoRoot $PromptFile)).Path

switch ($Action) {
    'run' {
        Invoke-Run -RepoRoot $repoRoot -ProviderName $Provider -ModelName $Model -ApiKeyIndex $KeyIndex -PromptPath $resolvedPromptPath -SystemPrompt $SystemInstructions -Temp $Temperature -MaxOut $MaxTokens
    }
    'share' {
        Invoke-Share -RepoRoot $repoRoot -OutputDir $ShareOutput
    }
    'commit' {
        Invoke-Commit -RepoRoot $repoRoot -Message $CommitMessage
    }
}
