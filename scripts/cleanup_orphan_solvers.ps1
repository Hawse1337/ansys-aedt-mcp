# Removes orphaned HFSS solver ranks left behind by a force-killed AEDT.
#
# HFSS launches solver ranks through Intel MPI (hydra_pmi_proxy) and each rank is
# a matrix_solution_intel*.exe process. Killing ansysedt/hf3d with Stop-Process
# -Force does NOT kill that tree, so the ranks keep burning CPU with a dead
# parent. Run this only when you are sure no legitimate solve is running:
# it refuses to touch ranks whose parent chain is still alive.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\cleanup_orphan_solvers.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\cleanup_orphan_solvers.ps1 -Kill
# (-ExecutionPolicy Bypass is needed on machines that block unsigned scripts)

param([switch]$Kill)

$rankNames = @('matrix_solution_intel21', 'matrix_solution_intel20', 'matrix_solution_intel19')
$proxies = Get-Process hydra_pmi_proxy -ErrorAction SilentlyContinue
$ranks = foreach ($name in $rankNames) { Get-Process $name -ErrorAction SilentlyContinue }
$aedt = Get-Process ansysedt, hf3d -ErrorAction SilentlyContinue

Write-Host ("AEDT desktops/solvers: {0}" -f ($aedt | Measure-Object).Count)
Write-Host ("MPI proxies: {0}" -f ($proxies | Measure-Object).Count)
Write-Host ("Solver ranks: {0}" -f ($ranks | Measure-Object).Count)
if ($ranks) {
    $cpu = [math]::Round(($ranks | Measure-Object CPU -Sum).Sum, 0)
    Write-Host ("Accumulated rank CPU: {0} s" -f $cpu)
}

if (-not $Kill) {
    Write-Host 'Report only. Re-run with -Kill to remove them.'
    return
}

if ($aedt) {
    Write-Warning 'An AEDT/hf3d process is running; refusing to kill solver ranks.'
    return
}

$killed = 0
foreach ($proc in $ranks) { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue; $killed++ }
foreach ($proc in $proxies) { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue; $killed++ }
Write-Host ("Removed {0} orphan processes." -f $killed)

