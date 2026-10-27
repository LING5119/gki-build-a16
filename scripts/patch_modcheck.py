#!/usr/bin/env python3
"""宽松模块校验补丁：忽略 vendor 模块的 modversions CRC 与 vermagic 不匹配。

背景（第 34-36 轮 ABI 审计实测数据）：
  设备 stock 内核是小米定制树（新增 CONFIG_MODULE_SIG_PROTECT、MI_SCHED_EXT、
  XIAOMI_*、F2FS_VIP_FILE 等 AOSP common 树里不存在的改动），其 struct module
  布局与 AOSP 构建产物必然不同。实测 module_layout 的 CRC：

      stock vendor 模块（设备期望值）    module_layout = 0x797f2b3e
      prague 配置(LTO_NONE/VSIZE 1024)   module_layout = 0xea3c5f1b
      gki_defconfig(LTO_THIN/VSIZE 1280) module_layout = 0x3b2a3e3f

  CRC 由 struct module 布局决定，在 AOSP 树上无法收敛到同一值。内核加载模块时
  逐个校验导出符号 CRC，module_layout 不匹配即
  "disagrees about version of symbol module_layout" -> -ENOEXEC，
  导致全部 vendor 模块被拒绝加载，设备卡在开机 logo。

本补丁只关闭两处"版本一致性"校验，且刻意选择副作用最小的注入点：
  1. kernel/module/version.c : check_version() 入口恒返回 0（CRC 视为匹配）
     —— 该函数只做 CRC 比对，不承担 license/GPL 判定。
  2. kernel/module/main.c    : same_magic() 入口恒返回 1（vermagic 视为匹配）
     —— 只影响 vermagic 比较，check_modinfo() 里 license_gplok 的赋值逻辑
        完全保留（否则 GPL-only 符号会被误拒，进而导致大量模块加载失败）。
        若该版本没有 same_magic，才退化为 check_modinfo() 入口返回 0。

保持不变：符号解析仍严格（内核里没有的符号照样 "Unknown symbol" 失败）；
CONFIG_MODULE_SIG_FORCE 未开启，模块签名不匹配只 taint 不阻止加载。

用法：patch_modcheck.py <kernel_common_dir>
"""
import re
import sys
from pathlib import Path

MARK = "MODCHECK_LENIENT_PATCH"


def inject_entry_return(path: Path, proto: str, int_value: int, why: str) -> bool:
    """在匹配函数体开头插入"校验通过"返回语句。

    返回类型含 bool 时用 true，否则用调用方指定的 int_value
    （check_version: 0 = 通过；same_magic: 1 = 匹配）。
    """
    try:
        src = path.read_text(errors="surrogateescape")
    except OSError as exc:
        print(f"[warn] 无法读取 {path}: {exc}")
        return False
    if MARK in src:
        print(f"[skip] {path.name}: 已打过补丁")
        return True

    m = re.search(proto, src)
    if not m:
        print(f"[warn] {path.name}: 未匹配到原型 /{proto}/")
        return False
    brace = src.find("{", m.end() - 1)
    if brace < 0:
        print(f"[warn] {path.name}: 原型后未找到函数体左花括号")
        return False

    is_bool = bool(re.search(r"\bbool\b", m.group(0)))
    stmt = "return true;" if is_bool else f"return {int_value};"
    inject = f"\t/* {MARK}: {why} */\n\t{stmt}\n"
    path.write_text(src[: brace + 1] + "\n" + inject + src[brace + 1 :],
                    errors="surrogateescape")
    print(f"[ok] {path.name}: 在 {m.group(0).strip()} 入口注入 {stmt}")
    return True


def main() -> int:
    if len(sys.argv) < 2:
        print("用法: patch_modcheck.py <kernel_common_dir>")
        return 2
    common = Path(sys.argv[1])
    if not common.is_dir():
        print(f"[error] 不是目录: {common}")
        return 2

    version_c = common / "kernel/module/version.c"
    main_c = common / "kernel/module/main.c"

    ok_version = False
    if version_c.is_file():
        ok_version = inject_entry_return(
            version_c, r"static\s+[^;{()\n]*check_version\s*\(",
            0, "ignore modversions CRC mismatch")
    else:
        print(f"[warn] 缺少 {version_c}（内核版本差异？）")

    ok_vermagic = False
    if main_c.is_file():
        ok_vermagic = inject_entry_return(
            main_c, r"static\s+[^;{()\n]*same_magic\s*\(",
            1, "ignore vermagic mismatch")
        if not ok_vermagic:
            ok_vermagic = inject_entry_return(
                main_c, r"static\s+[^;{()\n]*check_modinfo\s*\(",
                0, "ignore modinfo/vermagic check")
    else:
        print(f"[warn] 缺少 {main_c}（内核版本差异？）")

    if ok_version and ok_vermagic:
        print("[done] 宽松模块校验补丁全部应用成功")
        return 0
    if ok_version or ok_vermagic:
        print("[done] 部分补丁应用成功，继续构建（缺失项会在日志中标注）")
        return 0
    print("[error] 两处校验均未打补丁，产物将因模块校验失败而无法开机")
    return 1


if __name__ == "__main__":
    sys.exit(main())
