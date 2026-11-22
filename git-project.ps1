# This environment protects Desktop .git writes; use the workspace metadata store.
$env:GIT_DIR = 'C:\Users\Tabeed\Documents\Codex\2026-11-22\hi\work\blast-radius-git'
$env:GIT_WORK_TREE = $PSScriptRoot
& git @args
exit $LASTEXITCODE
