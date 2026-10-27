#!/usr/bin/env bash
# Run explicitly on YOUR Linux host. Creates a new swap file; never replaces one.
set -euo pipefail
osa_swap_file=/swapfile-osa
if [[ ${EUID} -ne 0 ]]; then
    echo '请使用 sudo bash scripts/setup_swap.sh 执行。' >&2
    exit 1
fi
if [[ $(uname -s) != Linux ]]; then
    echo '此脚本仅适用于 Linux。' >&2
    exit 1
fi
osa_swap_kb=$(awk '/^SwapTotal:/ {print $2}' /proc/meminfo)
if (( osa_swap_kb >= 4 * 1024 * 1024 )); then
    echo '已有至少 4 GiB Swap，无需新增。'
    exit 0
fi
if [[ -e "$osa_swap_file" || -L "$osa_swap_file" ]]; then
    echo '/swapfile-osa 已存在，未覆盖。请检查现有文件及 swapon --show。' >&2
    exit 1
fi
osa_fs=$(findmnt -n -o FSTYPE -T /)
case "$osa_fs" in
    ext4|xfs) ;;
    *) echo "当前文件系统 $osa_fs 需要专用 Swap 配置方法；脚本未修改系统。" >&2; exit 1 ;;
esac
osa_free_kb=$(df -Pk / | awk 'NR==2 {print $4}')
if (( osa_free_kb < 5 * 1024 * 1024 )); then
    echo '根分区至少需要 5 GiB 可用空间来创建 4 GiB Swap。' >&2
    exit 1
fi
umask 077
# noclobber also refuses a file created between the existence check and this line.
(set -C; : > "$osa_swap_file")
osa_swap_active=0
osa_cleanup() {
    if [[ $osa_swap_active == 0 ]]; then rm -f -- "$osa_swap_file"; fi
}
trap osa_cleanup EXIT
dd if=/dev/zero of="$osa_swap_file" bs=1M count=4096 status=progress conv=notrunc
chmod 600 "$osa_swap_file"
mkswap "$osa_swap_file"
swapon "$osa_swap_file"
osa_swap_active=1
if ! awk '$1=="/swapfile-osa" {found=1} END {exit !found}' /etc/fstab; then
    printf '\n/swapfile-osa none swap sw 0 0\n' >> /etc/fstab
fi
echo '已启用 4 GiB Swap 并写入 /etc/fstab。Swap 较慢，不能替代物理内存。'
free -h
