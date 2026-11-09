<#
.SYNOPSIS
    一键发版：打 zip → 打 tag → 建 GitHub Release → 传安装包。

.DESCRIPTION
    版本号只从 metadata.yaml 读，避免「代码一个版本、tag 另一个版本」。

    顺序是刻意的：先跑测试、再打 zip、再动 git、最后才发请求。
    任何一步失败都立刻停下并说清楚缺什么，不会留下半个 Release。

    凭据优先级：-Token > 环境变量 GITHUB_TOKEN > git 里已缓存的 GitHub 凭据
    （git credential fill）。token 只在内存里用，不写文件、不打印。

.EXAMPLE
    .\tools\release.ps1 -DryRun
    只演一遍：校验版本、跑测试、打 zip、把要做的动作打出来，不发任何网络请求。

.EXAMPLE
    .\tools\release.ps1
    正式发版：metadata.yaml 里的版本号 → tag vX.Y.Z + Release + 附件。

.EXAMPLE
    .\tools\release.ps1 -NotesFile .\docs\release-notes\0.2.0.md
    用写好的 Markdown 当 Release 说明（不写就按 git log 自动生成）。

.EXAMPLE
    .\tools\release.ps1 -BuildOnly
    只打本地安装包，不碰 git、不碰网络。

.EXAMPLE
    .\tools\release.ps1 -Force
    同一个版本重发：tag 已存在时用它覆盖 Release 说明与附件。
#>
[CmdletBinding()]
param(
    # 目标版本，留空 = 用 metadata.yaml 里的
    [string]$Version = "",
    # Release 说明文件（Markdown），留空 = 按 git log 生成
    [string]$NotesFile = "",
    # 安装包输出目录，默认是仓库的上一级（AstrBot 的 data/plugins）
    [string]$OutDir = "",
    # 直接给 token（不推荐，会留在命令历史里）
    [string]$Token = "",
    # 建成草稿（自己可见，手动点发布）
    [switch]$Draft,
    # 空跑：做完本地检查就停
    [switch]$DryRun,
    # 只打包
    [switch]$BuildOnly,
    # 跳过单测（不建议）
    [switch]$SkipTests,
    # 允许覆盖已存在的 tag / Release
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch { }
try { $OutputEncoding = [Text.Encoding]::UTF8 } catch { }

# 任何一步抛错都只留一行干净的中文提示，不要把 PowerShell 的调用栈糊一脸
trap {
    Write-Host ""
    Write-Host ("✗ " + $_.Exception.Message) -ForegroundColor Red
    exit 1
}

$RepoRoot = Split-Path -Parent $PSScriptRoot
$RepoSlug = 'ttlvip/astrbot_plugin_fish_find_chicken'
$PluginName = 'astrbot_plugin_fish_find_chicken'
$script:GhToken = ""

# ── 小工具 ────────────────────────────────────────────────────────────────

function Say([string]$Text, [string]$Color = 'Gray') {
    Write-Host $Text -ForegroundColor $Color
}

function Fail([string]$Text) {
    throw $Text
}

function Invoke-Git {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Args)
    # PS 5.1 的坑：native 命令的 stderr 一旦重定向（2>&1 / *>）就变成 ErrorRecord，
    # 在 $ErrorActionPreference='Stop' 下会被当成异常抛出 —— 而 git push 正常输出也走 stderr。
    # 所以调用期间临时把 EAP 放回 Continue，靠 $LASTEXITCODE 判断成败。
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = & git @Args 2>&1
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prev
    }
    if ($code -ne 0) {
        Fail ("git " + ($Args -join ' ') + " 失败（退出码 $code）：`n" + ($out -join "`n"))
    }
    return ($out -join "`n").Trim()
}

function Test-GitRef {
    param([string]$Ref)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & git rev-parse --verify --quiet $Ref *> $null
        $ok = ($LASTEXITCODE -eq 0)
    } finally {
        $ErrorActionPreference = $prev
    }
    return $ok
}

function Get-MetaVersion {
    $meta = Join-Path $RepoRoot 'metadata.yaml'
    if (-not (Test-Path $meta)) { Fail "找不到 metadata.yaml：$meta" }
    foreach ($line in [IO.File]::ReadAllLines($meta, [Text.Encoding]::UTF8)) {
        if ($line -match '^version:\s*(\S+)\s*$') { return $Matches[1] }
    }
    Fail 'metadata.yaml 里没有 version 行'
}

function Get-HttpErrorBody($ErrorRecord) {
    # 把 GitHub 返回的 JSON message 抠出来，比「远程服务器返回错误: 422」有用得多
    try {
        $resp = $ErrorRecord.Exception.Response
        if ($null -eq $resp) { return '' }
        $stream = $resp.GetResponseStream()
        if ($null -eq $stream) { return '' }
        $reader = New-Object IO.StreamReader($stream, [Text.Encoding]::UTF8)
        return $reader.ReadToEnd()
    } catch { return '' }
}

function Get-GitHubToken {
    if ($Token) { return $Token.Trim() }
    if ($env:GITHUB_TOKEN) { return $env:GITHUB_TOKEN.Trim() }

    $raw = "protocol=https`nhost=github.com`n`n" | & git credential fill 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $raw) {
        Fail @'
拿不到 GitHub 凭据。三条路选一条：
  1) 先手动 git push 一次，让 Git 凭据管理器记住账号；
  2) 设环境变量 GITHUB_TOKEN=<pat>（scope 至少 repo）；
  3) -Token <pat>。
'@
    }
    foreach ($line in ($raw -split "`r?`n")) {
        if ($line -like 'password=*') { return $line.Substring(9).Trim() }
    }
    Fail 'git credential fill 返回里没有 password 字段'
}

function Invoke-GitHub {
    param(
        [string]$Method,
        [string]$Uri,
        [string]$Body = $null,
        [string]$ContentType = 'application/json',
        [string]$InFile = $null
    )
    $headers = @{
        Authorization          = "Bearer $script:GhToken"
        Accept                 = 'application/vnd.github+json'
        'User-Agent'           = 'fish-find-chicken-release'
        'X-GitHub-Api-Version' = '2022-11-28'
    }
    $params = @{ Method = $Method; Uri = $Uri; Headers = $headers }
    if ($InFile) {
        $params.InFile = $InFile
        $params.ContentType = $ContentType
    } elseif ($null -ne $Body) {
        # 中文说明必须按 UTF-8 字节发，否则 PS 5.1 会按 Latin-1 编码，页面上全是问号
        $params.Body = [Text.Encoding]::UTF8.GetBytes($Body)
        $params.ContentType = "$ContentType; charset=utf-8"
    }
    return Invoke-RestMethod @params
}

# ── 打包 ──────────────────────────────────────────────────────────────────

function New-PluginZip {
    param([string]$ZipPath)

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $parent = Split-Path -Parent $ZipPath
    if (-not (Test-Path $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
    if (Test-Path $ZipPath) { Remove-Item $ZipPath -Force }

    # 不把 .git / 缓存 / 本地配置打进包：旧包 991KB 里一半是 git 对象
    $skipDirs = @('.git', '__pycache__', '.venv', '.dev', '.idea', '.vscode', 'dist', 'data')
    $skipExt = @('.pyc', '.pyo', '.log', '.tmp')

    $files = @()
    foreach ($file in (Get-ChildItem -Path $RepoRoot -Recurse -File -Force)) {
        $rel = $file.FullName.Substring($RepoRoot.Length + 1).Replace('\', '/')
        $parts = $rel.Split('/')
        $dirs = @()
        if ($parts.Length -gt 1) { $dirs = $parts[0..($parts.Length - 2)] }

        $skip = $false
        foreach ($d in $dirs) { if ($skipDirs -contains $d) { $skip = $true } }
        if ($skip) { continue }
        if ($skipExt -contains $file.Extension.ToLower()) { continue }

        $files += , @($file.FullName, "$PluginName/$rel")
    }

    if ($files.Count -eq 0) { Fail '要打包的文件是空的，检查 $RepoRoot 对不对' }

    $zip = [IO.Compression.ZipFile]::Open($ZipPath, 'Create')
    try {
        foreach ($pair in $files) {
            [IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
                $zip, $pair[0], $pair[1], [IO.Compression.CompressionLevel]::Optimal
            ) | Out-Null
        }
    } finally {
        $zip.Dispose()
    }

    $size = [math]::Round((Get-Item $ZipPath).Length / 1KB, 1)
    Say ("  [zip] {0}（{1} 个文件，{2} KB）" -f (Split-Path -Leaf $ZipPath), $files.Count, $size) 'Green'
    return @{ Path = $ZipPath; Count = $files.Count; SizeKB = $size }
}

# ── 1. 版本 ───────────────────────────────────────────────────────────────

$metaVersion = Get-MetaVersion
if (-not $Version) { $Version = $metaVersion }
if ($Version -ne $metaVersion) {
    Fail "版本对不上：metadata.yaml 是 $metaVersion，参数给的是 $Version。先改 metadata.yaml（它是唯一来源）。"
}

$tag = "v$Version"
$assetName = "$PluginName-$Version.zip"
if (-not $OutDir) { $OutDir = Split-Path -Parent $RepoRoot }
$zipVersioned = Join-Path $OutDir $assetName
$zipLocal = Join-Path $OutDir "$PluginName.zip"

Say ""
Say "鱼鱼找鸡 · 发版 $Version" 'Cyan'
Say "  仓库：$RepoRoot"
Say "  标签：$tag"
Say "  输出：$OutDir"

# ── 2. 本地检查 ───────────────────────────────────────────────────────────

if (-not $BuildOnly) {
    $branch = Invoke-Git rev-parse --abbrev-ref HEAD
    if ($branch -ne 'main') { Fail "当前分支是 $branch，发版只从 main 走。" }

    $dirty = Invoke-Git status --porcelain
    if ($dirty) {
        Fail @"
工作区不干净，先把改动提交掉：
$dirty
"@
    }
}

if (-not $SkipTests -and -not $BuildOnly) {
    Say "  [test] unittest ..." 'DarkGray'
    Push-Location $RepoRoot
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & python -m unittest discover -s tests -q
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prevEap
        Pop-Location
    }
    if ($code -ne 0) { Fail "测试没过（退出码 $code）。修好再发，或临时加 -SkipTests。" }
    Say "  [test] 通过" 'Green'
}

$zip = New-PluginZip -ZipPath $zipVersioned
Copy-Item -Path $zipVersioned -Destination $zipLocal -Force
Say ("  [zip] 另存一份给本地安装：{0}" -f (Split-Path -Leaf $zipLocal)) 'Green'

if ($BuildOnly) {
    Say ""
    Say "只打包，结束。" 'Green'
    exit 0
}

# ── 3. git 状态：提交必须已推送 ──────────────────────────────────────────

$hasUpstream = Test-GitRef 'origin/main'
if (-not $hasUpstream) { Fail '本地没有 origin/main，先 git fetch origin。' }

$unpushed = Invoke-Git rev-list --count 'origin/main..HEAD'
if ([int]$unpushed -gt 0) {
    Fail "有 $unpushed 个提交还没推。先 git push，发版脚本只推 tag。"
}

$tagExists = Test-GitRef "refs/tags/$tag"

if ($tagExists -and -not $Force -and -not $DryRun) {
    Fail "tag $tag 已存在。改 metadata.yaml 升版本，或加 -Force 覆盖同一个版本的 Release。"
}

# ── 4. Release 说明 ───────────────────────────────────────────────────────

$compareBase = ''
$prevEap = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
try {
    $prevTag = & git describe --tags --abbrev=0 HEAD 2>$null
    if ($LASTEXITCODE -eq 0 -and $prevTag) { $compareBase = ($prevTag -join '').Trim() }
} finally {
    $ErrorActionPreference = $prevEap
}

if ($NotesFile) {
    if (-not (Test-Path $NotesFile)) { Fail "说明文件不存在：$NotesFile" }
    $notes = [IO.File]::ReadAllText((Resolve-Path $NotesFile), [Text.Encoding]::UTF8)
    Say "  [notes] 用 $NotesFile" 'Green'
} else {
    if ($compareBase) {
        $logRange = "$compareBase..HEAD"
    } else {
        $logRange = 'HEAD~15..HEAD'
    }
    $lines = & git log --pretty=format:'- %s' $logRange
    $notes = @"
## 变更

$($lines -join "`n")

**完整对比**：https://github.com/$RepoSlug/compare/$compareBase...$tag
"@
    Say "  [notes] 按 git log 生成（$logRange）" 'Green'
}

# ── 5. 空跑到此为止 ───────────────────────────────────────────────────────

if ($DryRun) {
    Say ""
    Say "空跑结束，没发任何请求。将要执行：" 'Yellow'
    if ($tagExists) {
        Say "  - tag $tag 已存在（正式跑需要 -Force）" 'Yellow'
    } else {
        Say "  - git tag -a $tag / git push origin $tag"
    }
    Say "  - POST /repos/$RepoSlug/releases（draft=$($Draft.IsPresent)）"
    Say "  - 上传附件 $assetName"
    Say ""
    Say "说明预览（前 12 行）：" 'Yellow'
    ($notes -split "`n" | Select-Object -First 12) | ForEach-Object { Say "    $_" }
    exit 0
}

# ── 6. 打 tag ─────────────────────────────────────────────────────────────

if (-not $tagExists) {
    $null = Invoke-Git tag -a $tag -m "$Version"
    $null = Invoke-Git push origin $tag
    Say "  [tag] $tag 已创建并推送" 'Green'
} elseif ($Force) {
    Say "  [tag] $tag 已存在，-Force：只更新 Release" 'Yellow'
}

# ── 7. 建 Release ─────────────────────────────────────────────────────────

$script:GhToken = Get-GitHubToken
Say "  [api] 凭据就绪（长度 $($script:GhToken.Length)）" 'DarkGray'

$bodyObj = @{
    tag_name         = $tag
    name             = "$Version"
    body             = $notes
    draft            = $Draft.IsPresent
    prerelease       = $false
    generate_release_notes = $false
}
$json = $bodyObj | ConvertTo-Json -Depth 5

$release = $null
try {
    $release = Invoke-GitHub -Method Post -Uri "https://api.github.com/repos/$RepoSlug/releases" -Body $json
    Say "  [release] 已创建 #$($release.id)" 'Green'
} catch {
    $detail = Get-HttpErrorBody $_
    if ($detail -match 'already_exists') {
        $release = Invoke-GitHub -Method Get -Uri "https://api.github.com/repos/$RepoSlug/releases/tags/$tag"
        $release = Invoke-GitHub -Method Patch -Uri "https://api.github.com/repos/$RepoSlug/releases/$($release.id)" -Body $json
        Say "  [release] 已存在，说明已更新 #$($release.id)" 'Yellow'
    } else {
        Fail "建 Release 失败：$detail"
    }
}

# ── 8. 传附件（同名先删，保证可重复执行） ────────────────────────────────

$assets = Invoke-GitHub -Method Get -Uri "https://api.github.com/repos/$RepoSlug/releases/$($release.id)/assets"
foreach ($a in $assets) {
    if ($a.name -eq $assetName) {
        $null = Invoke-GitHub -Method Delete -Uri "https://api.github.com/repos/$RepoSlug/releases/assets/$($a.id)"
        Say "  [asset] 删掉同名旧附件" 'Yellow'
    }
}

$uploadUri = "https://uploads.github.com/repos/$RepoSlug/releases/$($release.id)/assets?name=$assetName"
$asset = Invoke-GitHub -Method Post -Uri $uploadUri -InFile $zip.Path -ContentType 'application/zip'
Say ("  [asset] {0}（{1} bytes）" -f $asset.name, $asset.size) 'Green'

# ── 9. 收尾 ───────────────────────────────────────────────────────────────

Say ""
Say "发版完成 ✔" 'Cyan'
Say "  Release：$($release.html_url)"
Say "  附件：$($asset.browser_download_url)"
Say ""
Say "记得重载插件让新版本生效。" 'Gray'
