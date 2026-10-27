#!/usr/bin/env python3
"""宽松模块校验补丁：忽略 vendor 模块的 modversions CRC 与 vermagic 不匹配。

背景（第 34-39 轮 ABI 审计实测）：
  设备 stock 内核为小米定制树（CONFIG_MODULE_SIG_PROTECT、MI_SCHED_EXT、
  XIAOMI_*、F2FS_VIP_FILE 等 AOSP common 树中不存在），struct module 布局
  必然与 AOSP 构建产物不同。实测 module_layout 的 CRC：

      stock vendor 模块（设备期望值）    0x797f2b3e
      prague 配置(LTO_NONE/VSIZE 1024)   0xea3c5f1b
      gki_defconfig(LTO_THIN/VSIZE 1280) 0x3b2a3e3f

  CRC 由 struct module 布局决定，AOSP 树上无法收敛。加载模块时逐个比对
  符号 CRC，module_layout 不符即 "disagrees about version of symbol
  module_layout" -> -ENOEXEC，全部 vendor 模块被拒 -> 卡开机 logo。

注入点（已逐字核对 linux v6.12.58 源码，两函数均在 kernel/module/version.c）：
    int check_version(...)  返回 1=CRC 匹配 / 0=不匹配，仅做 CRC 比对
    int same_magic(...)     返回 1=vermagic 匹配，仅做 vermagic 比较
  两者都不是 static（第 39 轮首次尝试按 "static ..." 匹配导致失配，退化到
  check_modinfo 注入，从而跳过 set_license() —— GPL-only 符号会被误拒）。

实现方式：把函数体整体替换为 `return 1;`（等价于 #if 0 包裹，但不引入
预处理指令，也不会产生 unreachable / unused-but-set 警告；内核 CONFIG_WERROR=y
时这类警告会直接变成编译错误）。

保持不变：
  * 符号解析仍严格：内核里没有的符号照样 "Unknown symbol" 失败；
  * check_modinfo() 不动，set_license()/license_gplok 逻辑完好；
  * CONFIG_MODULE_SIG_FORCE 未开启，签名不符只 taint 不阻止加载。

用法：patch_modcheck.py <kernel_common_dir>
"""
import re
import sys
from pathlib import Path

MARK = "MODCHECK_LENIENT_PATCH"
_MODIFIERS = {
    "static", "inline", "noinline", "__always_inline", "__maybe_unused",
    "__init", "__visible", "extern", "__cold",
}


def _scan_to_close(src: str, start: int):
    """从 src[start]=='{' 开始配对花括号，跳过字符串/字符字面量与注释。

    返回匹配的 '}' 下标；失败返回 -1。
    """
    i, depth, n = start, 0, len(src)
    while i < n:
        ch = src[i]
        if ch == "/" and i + 1 < n and src[i + 1] == "*":
            end = src.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        if ch == "/" and i + 1 < n and src[i + 1] == "/":
            end = src.find("\n", i + 2)
            i = n if end < 0 else end + 1
            continue
        if ch in "\"'":
            quote = ch
            i += 1
            while i < n:
                if src[i] == "\\":
                    i += 2
                    continue
                if src[i] == quote:
                    i += 1
                    break
                i += 1
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def find_func(src: str, name: str):
    """定位函数定义，返回 (body_open_idx, body_close_idx, header_text)。"""
    for m in re.finditer(r"\b" + re.escape(name) + r"\s*\(", src):
        i, depth = m.end(), 1
        while i < len(src):
            ch = src[i]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        if depth != 0:
            continue
        j = i + 1
        while j < len(src) and src[j] not in "{;":
            j += 1
        if j >= len(src) or src[j] != "{":
            continue  # 调用或声明，不是定义
        close = _scan_to_close(src, j)
        if close < 0:
            continue
        # 函数头：前 250 字符窗口去注释后，回溯到上一个 ; / } / 空行之后
        win = src[max(0, m.start() - 250) : m.end()]
        win = re.sub(r"/\*.*?\*/", " ", win, flags=re.S)
        win = re.sub(r"//[^\n]*", " ", win)
        cut = max(win.rfind(";"), win.rfind("}"))
        if cut >= 0:
            win = win[cut + 1 :]
        cut2 = win.rfind("\n\n")
        if cut2 >= 0:
            win = win[cut2 + 2 :]
        return j, close, win
    return None, None, None


def patch_func(path: Path, func: str, why: str) -> bool:
    """把 func 的函数体替换为 return 1;（表示校验通过）。"""
    try:
        src = path.read_text(errors="surrogateescape")
    except OSError as exc:
        print(f"[error] 无法读取 {path}: {exc}")
        return False

    open_i, close_i, header = find_func(src, func)
    if open_i is None:
        print(f"[error] {path.name}: 未定位到 {func}() 的函数定义")
        return False

    body = src[open_i + 1 : close_i]
    if MARK in body:
        print(f"[skip] {path.name}: {func}() 已打过补丁")
        return True

    words = re.findall(r"[A-Za-z_]\w*", header)
    ret_type = next((w for w in words if w not in _MODIFIERS and w != func), None)
    if ret_type in (None, "void"):
        print(f"[error] {path.name}: {func}() 返回类型非数值（{ret_type}），无法注入")
        return False
    stmt = "return true;" if ret_type == "bool" else "return 1;"

    new_body = f"\n\t/* {MARK}: {why} */\n\t{stmt}\n"
    path.write_text(src[: open_i + 1] + new_body + src[close_i:],
                    errors="surrogateescape")
    print(f"[ok] {path.name}: {func}() 函数体(返回 {ret_type})替换为 {stmt}")
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
    if not version_c.is_file():
        print(f"[error] 缺少 {version_c}")
        return 1

    ok_crc = patch_func(version_c, "check_version",
                        "ignore modversions CRC mismatch")
    ok_magic = patch_func(version_c, "same_magic", "ignore vermagic mismatch")

    if not (ok_crc and ok_magic):
        print("[error] 关键校验未能关闭，产物必然无法加载 vendor 模块，中止构建")
        return 1
    print("[done] 宽松模块校验补丁全部应用成功")
    return 0


if __name__ == "__main__":
    sys.exit(main())
