#!/usr/bin/env python3
"""补齐 Android 16 / 6.12.58 上 SUSFS 补丁 fs/proc/task_mmu.c 的 3 个失败 hunk。

背景：
  SUSFS 主补丁 (50_add_susfs_in_gki-android16-6.12.patch) 的 SUS_MAP 相关 hunk
  基于旧版 task_mmu.c（使用 vma_data_pages、旧 show_smaps_rollup 结构）。
  6.12.58 已重构为 vma_pages + 新的 rollup 循环，导致 Hunk #4/#5/#6 落空(rej)。

本脚本按新版结构等价补齐：
  1. show_smap(): 函数开头跳过被 sus_map 标记的 vma
  2. show_smaps_rollup(): 循环内跳过 + 末尾 susfs_sus_map_skip 标签

幂等：已插入过的内容会被跳过。
用法：python3 fix_task_mmu_61258.py [fs/proc/task_mmu.c]
"""
import os
import sys

PATH = sys.argv[1] if len(sys.argv) > 1 else 'fs/proc/task_mmu.c'

if not os.path.exists(PATH):
    print(f'[fix_task_mmu] 跳过：{PATH} 不存在')
    sys.exit(0)

src = open(PATH, encoding='utf-8', errors='surrogateescape').read()
orig = src

# ---------- (1) show_smap：函数开头跳过 sus_map 的 vma ----------
OLD1 = (
    'static int show_smap(struct seq_file *m, void *v)\n'
    '{\n'
    '\tstruct vm_area_struct *vma = v;\n'
    '\tstruct mem_size_stats mss = {};\n'
    '\n'
    '\tif (!vma_pages(vma))'
)
NEW1 = (
    'static int show_smap(struct seq_file *m, void *v)\n'
    '{\n'
    '\tstruct vm_area_struct *vma = v;\n'
    '\tstruct mem_size_stats mss = {};\n'
    '\n'
    '#ifdef CONFIG_KSU_SUSFS_SUS_MAP\n'
    '\tif (vma->vm_file && SUSFS_IS_INODE_SUS_MAP(file_inode(vma->vm_file)))\n'
    '\t\treturn 0;\n'
    '#endif\n'
    '\n'
    '\tif (!vma_pages(vma))'
)
MARK1 = 'if (vma->vm_file && SUSFS_IS_INODE_SUS_MAP(file_inode(vma->vm_file)))\n\t\treturn 0;'

if MARK1 in src:
    print('[fix_task_mmu] show_smap 已含 SUS_MAP 检查，跳过')
elif OLD1 in src:
    src = src.replace(OLD1, NEW1, 1)
    print('[fix_task_mmu] show_smap 已补齐 SUS_MAP 检查')
else:
    print('[fix_task_mmu] 警告：show_smap 上下文未匹配，未修改')

# ---------- (2) show_smaps_rollup：循环内跳过 + 末尾标签 ----------
OLD2 = (
    '\tvma_start = vma->vm_start;\n'
    '\tdo {\n'
    '\t\tsmap_gather_stats(vma, &mss, 0);\n'
    '\t\tlast_vma_end = vma->vm_end;'
)
NEW2 = (
    '\tvma_start = vma->vm_start;\n'
    '\tdo {\n'
    '#ifdef CONFIG_KSU_SUSFS_SUS_MAP\n'
    '\t\tif (vma->vm_file && SUSFS_IS_INODE_SUS_MAP(file_inode(vma->vm_file)))\n'
    '\t\t\tgoto susfs_sus_map_skip;\n'
    '#endif\n'
    '\t\tsmap_gather_stats(vma, &mss, 0);\n'
    '\t\tlast_vma_end = vma->vm_end;'
)

if 'susfs_sus_map_skip' in src:
    print('[fix_task_mmu] show_smaps_rollup 已含 SUS_MAP 逻辑，跳过')
elif OLD2 in src:
    src = src.replace(OLD2, NEW2, 1)
    print('[fix_task_mmu] show_smaps_rollup 循环头已插入 goto')
    OLD3 = '\t} for_each_vma(vmi, vma);\n\nempty_set:'
    NEW3 = (
        '#ifdef CONFIG_KSU_SUSFS_SUS_MAP\n'
        'susfs_sus_map_skip:\n'
        '#endif\n'
        '\t} for_each_vma(vmi, vma);\n\nempty_set:'
    )
    if OLD3 in src:
        src = src.replace(OLD3, NEW3, 1)
        print('[fix_task_mmu] show_smaps_rollup 末尾已插入 susfs_sus_map_skip 标签')
    else:
        print('[fix_task_mmu] 警告：rollup 循环尾部未匹配，goto 可能悬空')
else:
    print('[fix_task_mmu] 警告：show_smaps_rollup 上下文未匹配，未修改')

if src != orig:
    open(PATH, 'w', encoding='utf-8', errors='surrogateescape').write(src)
    print('[fix_task_mmu] 已写回', PATH)
else:
    print('[fix_task_mmu] 无改动')
