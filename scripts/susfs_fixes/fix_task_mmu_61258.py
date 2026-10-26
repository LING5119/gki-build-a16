#!/usr/bin/env python3
"""补齐 Android 16 / 6.12.58 上 SUSFS 补丁 fs/proc/task_mmu.c 的失败 hunk。

背景：
  SUSFS 主补丁 (50_add_susfs_in_gki-android16-6.12.patch) 的 SUS_MAP 相关 hunk
  基于旧版 task_mmu.c（使用 vma_data_pages、旧 show_smaps_rollup 结构）。
  GKI android16-6.12-2025-12 (SUBLEVEL=58) 已重构为 vma_pages + 新的 rollup 循环，
  且相对 patch 预期有 ~77 行偏移，导致 show_smap / show_smaps_rollup 相关 hunk 落空(rej)。

本脚本按 6.12.58 实际结构等价补齐，严格对齐上游补丁的语义与标签名：
  1. show_smap():            函数开头跳过被 sus_map 标记的 vma
  2. show_smaps_rollup():    do 循环头插入 goto，循环体内 last_vma_end 赋值前放
                             bypass_orig_flow 标签（标签后仍有语句，避免 C23 
                             "label at end of compound statement" 报错）

幂等：已插入过的内容会被跳过；旧的 susfs_sus_map_skip 标记会被就地纠正。
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

ROLLUP_HEAD = 'static int show_smaps_rollup(struct seq_file *m, void *v)'


def rollup_span(text):
    """返回 show_smaps_rollup 函数体在 text 中的 [start, end) 区间，找不到返回 None。"""
    i = text.find(ROLLUP_HEAD)
    if i < 0:
        return None
    # 函数体以行首的 '}' 结束
    j = text.find('\n}\n', i)
    if j < 0:
        return None
    return i, j + 3


def rollup_text(text):
    sp = rollup_span(text)
    return text[sp[0]:sp[1]] if sp else ''


# ---------- (1) show_smap：函数开头跳过 sus_map 的 vma ----------
MARK1 = ('if (vma->vm_file && SUSFS_IS_INODE_SUS_MAP(file_inode(vma->vm_file)))\n'
         '\t\treturn 0;')

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
    '#endif // #ifdef CONFIG_KSU_SUSFS_SUS_MAP\n'
    '\n'
    '\tif (!vma_pages(vma))'
)

if MARK1 in src:
    print('[fix_task_mmu] show_smap 已含 SUS_MAP 检查，跳过')
elif OLD1 in src:
    src = src.replace(OLD1, NEW1, 1)
    print('[fix_task_mmu] show_smap 已补齐 SUS_MAP 检查')
else:
    print('[fix_task_mmu] 警告：show_smap 上下文未匹配，未修改')

# ---------- (2) show_smaps_rollup：goto + bypass_orig_flow 标签 ----------
# 与上游补丁一致：goto 在 smap_gather_stats 之前，
# 标签紧贴 last_vma_end = vma->vm_end; 之前（其后必有语句）
OLD2 = (
    '\tdo {\n'
    '\t\tsmap_gather_stats(vma, &mss, 0);\n'
    '\t\tlast_vma_end = vma->vm_end;'
)
NEW2 = (
    '\tdo {\n'
    '#ifdef CONFIG_KSU_SUSFS_SUS_MAP\n'
    '\t\tif (vma->vm_file && SUSFS_IS_INODE_SUS_MAP(file_inode(vma->vm_file)))\n'
    '\t\t\tgoto bypass_orig_flow;\n'
    '#endif // #ifdef CONFIG_KSU_SUSFS_SUS_MAP\n'
    '\t\tsmap_gather_stats(vma, &mss, 0);\n'
    '#ifdef CONFIG_KSU_SUSFS_SUS_MAP\n'
    'bypass_orig_flow:\n'
    '#endif // #ifdef CONFIG_KSU_SUSFS_SUS_MAP\n'
    '\t\tlast_vma_end = vma->vm_end;'
)

rs = rollup_span(src)
if rs is None:
    print('[fix_task_mmu] 警告：未找到 show_smaps_rollup，跳过')
else:
    body = src[rs[0]:rs[1]]

    # 清理历史上误插的 susfs_sus_map_skip 标签（位置错误会导致 C23 报错）
    if 'susfs_sus_map_skip' in body:
        body = body.replace(
            '#ifdef CONFIG_KSU_SUSFS_SUS_MAP\nsusfs_sus_map_skip:\n#endif\n', '')
        body = body.replace(
            '#ifdef CONFIG_KSU_SUSFS_SUS_MAP\nsusfs_sus_map_skip:\n'
            '#endif // #ifdef CONFIG_KSU_SUSFS_SUS_MAP\n', '')
        body = body.replace('susfs_sus_map_skip:\n', '')
        body = body.replace('goto susfs_sus_map_skip;', 'goto bypass_orig_flow;')
        print('[fix_task_mmu] 已清理误插的 susfs_sus_map_skip 标记')

    if 'bypass_orig_flow:' in body:
        print('[fix_task_mmu] show_smaps_rollup 已含 bypass_orig_flow 标签，跳过')
        # 仍需把可能残留的 goto 名称统一
    elif OLD2 in body:
        body = body.replace(OLD2, NEW2, 1)
        print('[fix_task_mmu] show_smaps_rollup 已插入 goto + bypass_orig_flow 标签')
    else:
        # 兜底：结构有额外包裹时，只补标签到 last_vma_end 之前
        LM = '\t\tlast_vma_end = vma->vm_end;'
        if LM in body and 'bypass_orig_flow:' not in body:
            body = body.replace(
                LM,
                '#ifdef CONFIG_KSU_SUSFS_SUS_MAP\n'
                'bypass_orig_flow:\n'
                '#endif // #ifdef CONFIG_KSU_SUSFS_SUS_MAP\n' + LM, 1)
            print('[fix_task_mmu] show_smaps_rollup 兜底：仅补 bypass_orig_flow 标签')
        else:
            print('[fix_task_mmu] 警告：show_smaps_rollup 上下文未匹配，未修改')

    src = src[:rs[0]] + body + src[rs[1]:]

if src != orig:
    open(PATH, 'w', encoding='utf-8', errors='surrogateescape').write(src)
    print('[fix_task_mmu] 已写回', PATH)
else:
    print('[fix_task_mmu] 无改动')
