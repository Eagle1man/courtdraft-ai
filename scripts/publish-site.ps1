<#
.SYNOPSIS
  Опубликовать сайт в отдельный публичный репозиторий.

.DESCRIPTION
  Основной репозиторий приватный, а GitHub Pages на приватных
  репозиториях не работает без платного плана. Поэтому сайт
  публикуется отдельно: в каталоге site/ лежит всё, что нужно
  показать, и ничего больше — ни кода, ни материалов дел, ни
  документов для ректора.

  Скрипт собирает демо-данные, проверяет вёрстку и отправляет
  содержимое site/ в указанный репозиторий отдельной веткой.

  ЧТО НУЖНО ОТ ВАС (один раз)
    Создать на GitHub ПУСТОЙ публичный репозиторий, например
    courtdraft-demo, ничего в него не загружать.
    Скрипт сам создаст ветку и запушит сайт.

.EXAMPLE
  powershell -File scripts\publish-site.ps1 -Repository Eagle1man/courtdraft-demo

.EXAMPLE
  powershell -File scripts\publish-site.ps1 -Repository Eagle1man/courtdraft-demo -Branch main
#>

param(
    [Parameter(Mandatory)][string]$Repository,
    [string]$Branch = 'gh-pages',
    [switch]$SkipBuild
)

. (Join-Path $PSScriptRoot 'lib.ps1')

Write-Step 'CourtDraft AI — публикация сайта'

$siteDir = Join-Path $script:ProjectRoot 'site'
if (-not (Test-Path $siteDir)) {
    Write-Err "Не найден каталог сайта: $siteDir"
    exit 1
}

$python = Get-PythonExecutable
if (-not $python) {
    Write-Err 'Python не найден'
    exit 1
}

if (-not $SkipBuild) {
    Write-Step 'Пересборка данных демо и проверка вёрстки'
    & $python (Join-Path $script:ProjectRoot 'scripts\build_demo_site.py')
    if ($LASTEXITCODE -ne 0) {
        Write-Err 'Сборка данных демо не удалась.'
        Write-Info 'Если сборщик отказался публиковать данные — так и должно быть:'
        Write-Info 'в сайт попадают только синтетические материалы.'
        exit 1
    }

    & $python (Join-Path $script:ProjectRoot 'scripts\build_real_site.py')
    if ($LASTEXITCODE -ne 0) {
        Write-Warn 'Данные по реальным делам не обновились, публикуем как есть.'
    }

    & $python (Join-Path $script:ProjectRoot 'scripts\check_site.py')
    if ($LASTEXITCODE -ne 0) {
        Write-Err 'Проверка вёрстки не прошла. Публикация остановлена.'
        exit 1
    }
}

Write-Step "Отправка сайта в $Repository (ветка $Branch)"

$staging = Join-Path $env:TEMP ("courtdraft-site-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $staging -Force | Out-Null

try {
    Copy-Item (Join-Path $siteDir '*') $staging -Recurse -Force
    if (Test-Path (Join-Path $siteDir '.nojekyll')) {
        New-Item -Path (Join-Path $staging '.nojekyll') -ItemType File -Force | Out-Null
    }

    Push-Location $staging
    try {
        & git init -q
        & git checkout -q -b $Branch
        & git add -A
        & git commit -q -m 'Сайт и демонстрация ИИ помощника судьи'
        & git remote add origin "https://github.com/$Repository.git"
        & git push -u origin $Branch --force
        if ($LASTEXITCODE -ne 0) {
            Write-Err 'Отправка не удалась.'
            Write-Info 'Проверьте, что репозиторий создан, пустой и доступен для записи.'
            exit 1
        }
    } finally {
        Pop-Location
    }
} finally {
    Remove-Item $staging -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Step 'Сайт отправлен'
Write-Ok "Репозиторий: https://github.com/$Repository"
Write-Info 'Осталось включить Pages в нём:'
Write-Info "  https://github.com/$Repository/settings/pages"
Write-Info '  Build and deployment → Source: Deploy from a branch'
Write-Info "  Branch: $Branch · Folder: / (root)"
Write-Info ''
Write-Info 'После этого сайт будет доступен по адресу:'
$owner, $repoName = $Repository -split '/'
Write-Info "  https://$($owner.ToLower()).github.io/$repoName/"