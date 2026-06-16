# -*- coding: utf-8 -*-
"""
LaTeX to MathType 批量转换脚本 (修复版 v6)
功能：
  1. 支持 .docx 和 .doc (自动转换为 docx)
  2. 支持 安全模式(另存为) 和 覆盖模式(自动备份)
  3. 公式原地替换 (基于 Selection 插入，确保不会移到文档开头)
  4. 修复了覆盖模式 .doc 文件路径错误
  5. 运行时计数，进度显示准确
  6. 改用键盘指令操控 MathType
  7. [v4] 改用 Selection.InlineShapes.AddOLEObject 替代
     Range-based 插入，解决 OLE 对象插入到文档开头的问题
  8. [v6] 修复: 剪贴板资源泄漏、文档打开异常处理、
     空格过度清除、早期退出资源清理

依赖：
    pip install pywin32
"""

import win32com.client
import win32gui
import win32clipboard
import ctypes
import time
import sys
import os
import re
import shutil

user32 = ctypes.windll.user32

VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12       # Alt
VK_ESCAPE = 0x1B
VK_V = 0x56
VK_F4 = 0x73
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002

_MODIFIER_KEYS = (
    VK_SHIFT, VK_CONTROL, VK_MENU,
    0xA0, 0xA1,  # left/right Shift
    0xA2, 0xA3,  # left/right Ctrl
    0xA4, 0xA5,  # left/right Alt
)

# ===========================
# 基础工具函数
# ===========================

def find_windows(class_contains=None, title_contains=None):
    """查找符合条件的可见窗口。"""
    results = []
    def callback(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        title = win32gui.GetWindowText(hwnd)
        cls = win32gui.GetClassName(hwnd)
        if class_contains and class_contains in cls:
            results.append((hwnd, title, cls))
        elif title_contains and title_contains in title:
            results.append((hwnd, title, cls))
        return True
    win32gui.EnumWindows(callback, None)
    return results

def set_clipboard(text):
    """
    可靠地写入剪贴板，带重试机制。
    返回 True 表示成功，False 表示失败。
    """
    for _ in range(10):
        try:
            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT)
                return True
            finally:
                win32clipboard.CloseClipboard()
        except Exception:
            time.sleep(0.1)
    return False

def _key_down(vk):
    user32.keybd_event(vk, 0, 0, 0)

def _key_up(vk):
    user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)

def _release_modifier_keys():
    """释放 Ctrl/Alt/Shift，清掉 SendKeys 或 Alt 抢焦点留下的键盘状态。"""
    for vk in _MODIFIER_KEYS:
        user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
    time.sleep(0.05)

def _tap_key(vk):
    _key_down(vk)
    time.sleep(0.03)
    _key_up(vk)

def _send_ctrl_key(vk):
    """用 Win32 key down/up 发送 Ctrl+指定键，避免 WScript.SendKeys 状态漂移。"""
    _release_modifier_keys()
    _key_down(VK_CONTROL)
    time.sleep(0.04)
    _key_down(vk)
    time.sleep(0.04)
    _key_up(vk)
    time.sleep(0.04)
    _key_up(VK_CONTROL)
    _release_modifier_keys()

def _mathtype_foreground_active(mt_title=""):
    try:
        hwnd = user32.GetForegroundWindow()
        title = win32gui.GetWindowText(hwnd)
        cls = win32gui.GetClassName(hwnd)
        return (
            "EQNWINCLASS" in cls
            or "MathType" in title
            or (mt_title and title == mt_title)
        )
    except Exception:
        return False

def safe_paste_to_mathtype(shell, mt_title, retries=3):
    """
    安全粘贴到 MathType。
    保留 Alt 抢焦点机制，但在 Ctrl+V 前释放修饰键并用 Esc 退出菜单激活状态，
    避免第二个公式开始把 V 解释成 MathType 的 View/视图菜单助记键。
    """
    for _ in range(retries):
        try:
            shell.AppActivate(mt_title)
        except Exception:
            pass
        time.sleep(0.2)

        if not _mathtype_foreground_active(mt_title):
            mt_wins = find_windows(class_contains="EQNWINCLASS")
            if not mt_wins:
                mt_wins = find_windows(title_contains="MathType")
            if mt_wins:
                try:
                    _force_foreground(mt_wins[0][0])
                except Exception:
                    pass
                time.sleep(0.2)

        _release_modifier_keys()
        _tap_key(VK_ESCAPE)
        time.sleep(0.1)
        _release_modifier_keys()

        if _mathtype_foreground_active(mt_title):
            _send_ctrl_key(VK_V)
            time.sleep(0.8)
            return True

    return False

def safe_close_mathtype(shell, mt_title):
    """安全发送 Ctrl+F4 关闭并更新 MathType 对象。"""
    try:
        shell.AppActivate(mt_title)
    except Exception:
        pass
    time.sleep(0.2)
    _release_modifier_keys()
    _send_ctrl_key(VK_F4)

def _force_foreground(hwnd):
    """
    强制将窗口拉到前台，即使当前进程不是前台进程。

    Windows 限制：只有前台进程才能调用 SetForegroundWindow。
    绕法：模拟一次 Alt 键按下/释放，让系统认为本进程刚收到用户输入，
    从而获得 SetForegroundWindow 权限。
    """
    user32.keybd_event(VK_MENU, 0, KEYEVENTF_EXTENDEDKEY, 0)
    user32.SetForegroundWindow(hwnd)
    user32.keybd_event(VK_MENU, 0, KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP, 0)
    _release_modifier_keys()

def activate_mathtype_window(shell, max_wait=10):
    """等待并激活 MathType 编辑器窗口。"""
    for _ in range(max_wait * 2):
        mt_wins = find_windows(class_contains="EQNWINCLASS")
        if not mt_wins:
            mt_wins = find_windows(title_contains="MathType")

        if mt_wins:
            hwnd, mt_title, _ = mt_wins[0]
            try:
                _force_foreground(hwnd)
                return True, mt_title
            except Exception:
                pass
        time.sleep(0.5)
    return False, ""

def wait_mathtype_closed(timeout=10):
    """等待 MathType 编辑器窗口完全关闭。"""
    for _ in range(timeout):
        mt_wins = find_windows(class_contains="EQNWINCLASS")
        if not mt_wins:
            mt_wins = find_windows(title_contains="MathType")
        if not mt_wins:
            return True
        time.sleep(1)
    return False

# ===========================
# 核心转换逻辑 (修复版 v6)
# ===========================

def _remove_ole_and_restore(word, insert_pos, original_text):
    """删除刚插入的空 OLE 对象，并恢复原始公式文本。"""
    try:
        word.Selection.Start = insert_pos
        word.Selection.End = insert_pos + 1
        word.Selection.Delete()
        word.Selection.TypeText(original_text)
    except Exception as restore_err:
        print(f"    ⚠️ 恢复原始文本时出错: {restore_err}")
    word.Selection.Start = insert_pos
    word.Selection.End = insert_pos

def scan_all_formulas(word, doc):
    """预扫描文档中所有 $...$ 和 $$...$$ 公式，返回公式列表（不修改文档）。"""
    formulas = []
    saved_start = word.Selection.Start
    saved_end = word.Selection.End
    search_start = 0

    while len(formulas) < 5000:
        search_range = doc.Range(search_start, doc.Content.End)
        find_obj = search_range.Find
        find_obj.ClearFormatting()
        find_obj.Text = r"\$[!\$]@\$"
        find_obj.MatchWildcards = True
        find_obj.Forward = True
        find_obj.Wrap = 0

        if not find_obj.Execute():
            break

        # 检测 $$...$$
        is_display = False
        try:
            if search_range.Start > 0 and search_range.End < doc.Content.End:
                prev_ch = doc.Range(search_range.Start - 1, search_range.Start).Text
                next_ch = doc.Range(search_range.End, search_range.End + 1).Text
                if prev_ch == "$" and next_ch == "$":
                    search_range.MoveStart(Unit=1, Count=-1)
                    search_range.MoveEnd(Unit=1, Count=1)
                    is_display = True
        except Exception:
            pass

        sel_text = search_range.Text
        latex = sel_text.strip("$").strip()
        if latex:
            formulas.append({
                'index': len(formulas) + 1,
                'latex': latex,
                'is_display': is_display,
            })

        search_start = search_range.End

    # 恢复光标位置
    word.Selection.Start = saved_start
    word.Selection.End = saved_end
    return formulas


def prompt_formula_selection(formulas):
    """显示所有公式并让用户选择要排除的公式，返回排除的编号集合。"""
    print(f"\n{'='*60}")
    print(f"  📋 文档中共找到 {len(formulas)} 个公式:")
    print(f"{'='*60}")

    for f in formulas:
        tag = "📐块" if f['is_display'] else "📝行"
        preview = f['latex'][:60] + ('...' if len(f['latex']) > 60 else '')
        print(f"  [{f['index']:3d}] {tag}  {preview}")

    print(f"{'='*60}")
    print(f"请选择要 排除（不转换）的公式编号:")
    print(f"  - 输入编号，多个用逗号分隔，如: 1,3,5")
    print(f"  - 输入范围，如: 2-8")
    print(f"  - 混合使用，如: 1,3-5,8")
    print(f"  - 直接回车 = 全部转换")

    user_input = input("\n排除编号: ").strip()
    if not user_input:
        return set()

    excluded = set()
    valid_indices = {f['index'] for f in formulas}
    for part in user_input.split(","):
        part = part.strip()
        if '-' in part:
            try:
                a, b = part.split('-', 1)
                for i in range(int(a), int(b) + 1):
                    if i in valid_indices:
                        excluded.add(i)
            except ValueError:
                print(f"  ⚠️ 无法解析: {part}，已忽略")
        else:
            try:
                v = int(part)
                if v in valid_indices:
                    excluded.add(v)
            except ValueError:
                print(f"  ⚠️ 无法解析: {part}，已忽略")

    if excluded:
        print(f"\n  ✅ 将排除 {len(excluded)} 个公式: {sorted(excluded)}")
    else:
        print(f"\n  ✅ 不排除任何公式，全部转换")
    return excluded


def _ping(progress_callback, stage: str, formula_index: int, total: int):
    """安全转发进度回调（异常不阻塞主流程）。"""
    if progress_callback is None:
        return
    try:
        progress_callback(formula_index, total, stage)
    except Exception:
        pass


def convert_one_formula(word, doc, shell, formula_index, skip=False,
                         progress_callback=None, total_formulas=0):
    """
    转换单个公式。
    使用 Range-based Find + 位置锚定实现原地替换。

    Args:
        progress_callback: 可选，签名 (current_index, total, stage_str)。
            在关键节点被调用，用于驱动 Agent 看门狗心跳。
        total_formulas: 本次待转换公式总数（仅用于进度计算）。

    返回: (should_continue, was_converted, is_nonempty)
      - should_continue: 是否继续搜索下一个公式
      - was_converted:   本次是否成功转换了一个公式
      - is_nonempty:     找到的公式是否非空（用于排除编号计数）
    """
    _ping(progress_callback, "find", formula_index, total_formulas)
    # 1. 从当前光标位置到文档末尾创建搜索范围
    #    使用 Range.Find 而非 Selection.Find，避免选区干扰
    cursor_pos = word.Selection.Start
    search_range = doc.Range(cursor_pos, doc.Content.End)

    find_obj = search_range.Find
    find_obj.ClearFormatting()
    find_obj.Text = r"\$[!\$]@\$"  # Word 通配符：匹配 $...$
    find_obj.MatchWildcards = True
    find_obj.Forward = True
    find_obj.Wrap = 0  # wdFindStop

    found = find_obj.Execute()

    if not found:
        return False, False, False

    # 2. 检测是否为 $$...$$ 块公式
    is_display_math = False
    try:
        if search_range.Start > 0 and search_range.End < doc.Content.End:
            prev_char = doc.Range(search_range.Start - 1, search_range.Start).Text
            next_char = doc.Range(search_range.End, search_range.End + 1).Text
            if prev_char == "$" and next_char == "$":
                search_range.MoveStart(Unit=1, Count=-1)
                search_range.MoveEnd(Unit=1, Count=1)
                is_display_math = True
    except Exception:
        pass

    # 2.5 不再清除公式前后的空格，保留原始排版
    #     之前的实现会把公式两侧所有空格都删掉，导致文字粘连

    # 3. 提取 LaTeX 并清洗 (使用正则精确提取，防止跨公式匹配)
    sel_text = search_range.Text
    cleaned = sel_text.strip()

    # 用 Python 非贪婪正则提取第一个完整公式
    m = (re.match(r'^(\$\$(.+?)\$\$)', cleaned, re.DOTALL) or
         re.match(r'^(\$(.+?)\$)', cleaned, re.DOTALL))
    if m:
        latex = m.group(2).strip()
        matched_formula = m.group(1)
        # 如果正则匹配比 Word Find 结果短，说明有跨公式匹配，修正范围
        if len(matched_formula) < len(cleaned):
            # 计算前导空格偏移
            leading_spaces = len(sel_text) - len(sel_text.lstrip())
            new_end = search_range.Start + leading_spaces + len(matched_formula)
            search_range.End = new_end
            sel_text = search_range.Text
            is_display_math = matched_formula.startswith('$$')
            print(f"    ℹ️ 检测到匹配范围过大，已自动修正")
    else:
        latex = sel_text.strip("$").strip()  # 回退到原始方式

    if not latex:
        # 空公式，跳过并将光标移到其后
        word.Selection.Start = search_range.End
        word.Selection.End = search_range.End
        return True, False, False

    # 用户排除的公式，跳过
    if skip:
        print(f"\n  [{formula_index}] 已排除(用户选择): {latex[:50]}{'...' if len(latex) > 50 else ''}")
        word.Selection.Start = search_range.End
        word.Selection.End = search_range.End
        return True, False, True

    print(f"\n  [{formula_index}] 处理: {latex[:50]}{'...' if len(latex) > 50 else ''}")

    # 重新包裹 $ 符号 (MathType 需要 $ 才能识别为 LaTeX)
    if is_display_math or sel_text.startswith("$$"):
        latex_for_clip = f"$${latex}$$"
    else:
        latex_for_clip = f"${latex}$"

    # 4. 写入剪贴板
    if not set_clipboard(latex_for_clip):
        print(f"    ⚠️ 剪贴板写入失败，跳过此公式")
        word.Selection.Start = search_range.End
        word.Selection.End = search_range.End
        return True, False, True

    time.sleep(0.2)

    # 5. [关键] 原地替换 (Selection-based，确保插入到正确位置)：
    #    a) 选中找到的公式文本
    #    b) 通过 Selection 删除并保持光标在原位
    #    c) 用 Selection.InlineShapes.AddOLEObject 在光标位置插入
    #    注意：doc.InlineShapes.AddOLEObject(Range=...) 不可靠，
    #          经常插入到文档开头。改用 Selection 方式可确保位置正确。
    insert_pos = search_range.Start

    # 先选中公式文本
    word.Selection.Start = search_range.Start
    word.Selection.End = search_range.End
    time.sleep(0.1)

    # 删除选中的公式文本，光标自动停留在原位
    word.Selection.Delete()
    time.sleep(0.1)

    # [修复] 删除公式后，清除前后紧邻的空格（让公式与文字紧凑排列）
    try:
        cur = word.Selection.Start
        # 先删后面的空格（可能有多个）
        while cur < doc.Content.End:
            ch = doc.Range(cur, cur + 1).Text
            if ch == ' ':
                doc.Range(cur, cur + 1).Delete()
            else:
                break
        # 再删前面的空格（可能有多个）
        cur = word.Selection.Start
        while cur > 0:
            ch = doc.Range(cur - 1, cur).Text
            if ch == ' ':
                doc.Range(cur - 1, cur).Delete()
                cur = word.Selection.Start
            else:
                break
        insert_pos = word.Selection.Start
    except Exception:
        pass

    _ping(progress_callback, "ole_insert", formula_index, total_formulas)
    try:
        # 通过当前 Selection 位置插入 OLE，不指定 Range 参数
        word.Selection.InlineShapes.AddOLEObject(
            ClassType="Equation.DSMT4"
        )
    except Exception as e:
        print(f"    ❌ 插入 OLE 失败: {e}")
        # 恢复原始公式文本，防止数据丢失
        word.Selection.TypeText(sel_text)
        word.Selection.Start = insert_pos
        word.Selection.End = insert_pos
        return True, False, True

    # 6. 自动化 MathType：等待窗口打开 -> 粘贴 LaTeX -> 关闭
    _ping(progress_callback, "mathtype_open", formula_index, total_formulas)
    activated, mt_title = activate_mathtype_window(shell)
    if not activated:
        print(f"    ❌ MathType 未响应，删除空 OLE 并恢复原始文本")
        _remove_ole_and_restore(word, insert_pos, sel_text)
        return True, False, True

    # 再次写入剪贴板 (窗口切换可能导致剪贴板被覆盖)
    set_clipboard(latex_for_clip)

    try:
        if not safe_paste_to_mathtype(shell, mt_title):
            print("    [ERROR] MathType paste failed; restoring original formula text")
            _remove_ole_and_restore(word, insert_pos, sel_text)
            return True, False, True
        safe_close_mathtype(shell, mt_title)
        _ping(progress_callback, "mathtype_close", formula_index, total_formulas)
        if not wait_mathtype_closed():
            print("    [ERROR] MathType did not close normally; restoring original formula text")
            _remove_ole_and_restore(word, insert_pos, sel_text)
            return True, False, True

        word.Selection.Start = insert_pos + 1
        word.Selection.End = insert_pos + 1
        return True, True, True

    except Exception as e:
        print(f"    ❌ 自动化错误: {e}，删除空 OLE 并恢复原始文本")
        _remove_ole_and_restore(word, insert_pos, sel_text)
        return True, False, True

    # 7. 将光标移到 OLE 对象之后，准备搜索下一个
    word.Selection.Start = insert_pos + 1
    word.Selection.End = insert_pos + 1
    return True, True, True


# ===========================
# 主程序
# ===========================

def select_file_dialog():
    """弹出文件选择框"""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError:
        print("❌ 需要 tkinter 模块，请确认 Python 安装时勾选了 tcl/tk 选项")
        return None

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    file_path = filedialog.askopenfilename(
        title="选择要转换的 Word 文档",
        filetypes=[("Word 文档", "*.docx;*.doc"), ("所有文件", "*.*")],
    )
    root.destroy()
    return file_path


def main(progress_callback=None, excluded_indices=None):
    """
    主转换流程。

    Args:
        progress_callback: 可选，签名 (current_index, total, stage_str)。
            非空时视为“被程序调用”（Agent 工具场景）：
            - 自动跳过所有 input() 交互（模式选择 / 排除编号）
            - 在扫描、每个公式转换、定期保存节点回调以驱动看门狗心跳
        excluded_indices: 可选，要排除的公式编号集合（仅在 callback 模式下生效）。
            默认空集（即全部转换）。CLI 交互模式下忽略此参数，使用用户输入。
    """
    MAX_FORMULAS = 5000  # 安全上限，防止死循环
    is_programmatic = progress_callback is not None

    # 1. 参数解析与文件选择
    if len(sys.argv) >= 2 and not sys.argv[1].startswith("--"):
        input_path = os.path.abspath(sys.argv[1])
    elif is_programmatic:
        print("❌ 程序调用模式下必须通过 sys.argv 传入 file_path")
        return
    else:
        print("📂 请在弹出的对话框中选择要处理的 Word 文档...")
        input_path = select_file_dialog()
        if not input_path:
            return

    if not os.path.exists(input_path):
        print(f"❌ 文件不存在: {input_path}")
        return

    # 2. 模式选择
    overwrite_mode = "--overwrite" in sys.argv
    if not overwrite_mode and "--safe" not in sys.argv:
        if is_programmatic:
            # 程序调用：默认安全模式（避免覆盖原文件的意外）
            overwrite_mode = False
        else:
            print(f"\n📄 已选择: {os.path.basename(input_path)}")
            print(f"   路径: {input_path}")
            print(f"\n请选择转换模式:")
            print(f"  1. 安全模式 — 生成新文件 xxx_converted.docx（推荐）")
            print(f"  2. 覆盖模式 — 修改原文件（自动备份为 xxx_backup）")
            choice = input("\n请输入 1 或 2 (默认 1): ").strip()
            overwrite_mode = choice == "2"

    # 3. 启动信息
    print("=" * 60)
    print("  LaTeX → MathType 批量转换工具 (Fix v6)")
    print("=" * 60)

    # 4. 文件路径处理 (支持 .doc -> .docx)
    #    注意：.doc 判断需排除 .docx
    is_doc = input_path.lower().endswith(".doc") and not input_path.lower().endswith(".docx")
    base, ext = os.path.splitext(input_path)

    if overwrite_mode:
        # 覆盖模式：备份原文件
        backup_path = base + "_backup" + ext
        shutil.copy2(input_path, backup_path)
        print(f"📁 已备份原文件至: {os.path.basename(backup_path)}")

        if is_doc:
            output_path = base + ".docx"
            print(f"ℹ️  .doc 文件将在转换后保存为: {os.path.basename(output_path)}")
        else:
            output_path = input_path
    else:
        # 安全模式：生成 _converted.docx
        output_path = base + "_converted.docx"

    print(f"🚀 启动 Word...")
    word = None
    doc = None
    try:
        word = win32com.client.Dispatch("Word.Application")
        word.Visible = True
    except Exception as e:
        print(f"❌ 无法启动 Word: {e}")
        print(f"   请确认已安装 Microsoft Word，且没有被其他进程锁定。")
        return

    shell = win32com.client.Dispatch("WScript.Shell")

    try:
        # [修复] 始终先打开原文件
        abs_input = os.path.abspath(input_path)
        try:
            doc = word.Documents.Open(abs_input)
        except Exception as e:
            print(f"❌ 无法打开文档: {abs_input}")
            print(f"   错误详情: {e}")
            print(f"   请确认文件未被其他程序占用，且格式正确。")
            return

        # 无论哪种模式，如果需要另存为 .docx
        if is_doc:
            # .doc -> .docx：先另存为再重新打开
            abs_output = os.path.abspath(output_path)
            doc.SaveAs2(abs_output, FileFormat=16)  # wdFormatXMLDocument
            doc.Close()
            doc = word.Documents.Open(abs_output)
        elif not overwrite_mode:
            # 安全模式下 .docx：另存为副本
            doc.SaveAs2(os.path.abspath(output_path), FileFormat=16)

        time.sleep(1)
        word.Selection.HomeKey(Unit=6)  # 移到文档开头

        # ---- 预扫描阶段 ----
        print(f"\n🔍 正在扫描文档中的公式...")
        _ping(progress_callback, "scan", 0, 0)
        formulas = scan_all_formulas(word, doc)
        _ping(progress_callback, "scan_done", 0, len(formulas))

        if not formulas:
            print(f"\n⚠️ 未找到任何 $...$ 或 $$...$$ 公式。")
            print(f"📄 文档已正常打开: {output_path}")
            return

        # 排除编号：程序调用模式使用传入值，否则交互询问
        if is_programmatic:
            excluded = set(excluded_indices) if excluded_indices else set()
        else:
            excluded = prompt_formula_selection(formulas)
        to_convert = len(formulas) - len(excluded)
        if to_convert == 0:
            print(f"\n⚠️ 所有公式均已排除，无需转换。")
            print(f"📄 文档已正常打开: {output_path}")
            return

        print(f"\n🚀 开始转换 {to_convert} 个公式...")
        word.Selection.HomeKey(Unit=6)  # 回到文档开头

        # ---- 转换阶段 ----
        formula_number = 0   # 非空公式序号 (与预扫描编号对应)
        count = 0
        skipped = 0
        user_excluded = 0
        total_iter = 0
        total_formulas = len(formulas)

        while total_iter < MAX_FORMULAS:
            total_iter += 1
            should_skip = (formula_number + 1) in excluded

            should_continue, converted, is_nonempty = convert_one_formula(
                word, doc, shell, formula_number + 1, skip=should_skip,
                progress_callback=progress_callback, total_formulas=total_formulas,
            )
            if not should_continue:
                break

            if is_nonempty:
                formula_number += 1
                stage = "converted" if converted else ("excluded" if should_skip else "skipped")
                _ping(progress_callback, stage, formula_number, total_formulas)
                if converted:
                    count += 1
                elif should_skip:
                    user_excluded += 1
                else:
                    skipped += 1

            # 定期保存
            if count > 0 and count % 5 == 0:
                _ping(progress_callback, "save", formula_number, total_formulas)
                doc.Save()

        doc.Save()
        _ping(progress_callback, "done", formula_number, total_formulas)
        print(f"\n✅ 全部完成！共转换 {count} 个公式。")
        if user_excluded > 0:
            print(f"   (用户排除 {user_excluded} 个)")
        if skipped > 0:
            print(f"   (跳过 {skipped} 个失败项)")
        print(f"💾 文件已保存: {output_path}")

    except Exception as e:
        print(f"\n❌ 发生错误: {e}")
        import traceback
        traceback.print_exc()
    finally:
        print("\n脚本运行结束。Word 保持打开状态，请手动检查结果。")
        try:
            if word is not None:
                word.Quit()
        except Exception:
            pass


if __name__ == "__main__":
    main()
