# -*- coding:utf-8 -*-
# ============================================================
# 铁路工机具清点上位机（Kivy）— 优化版
# 修复：
#   1. 黑屏/中文方框：字体优先注册 .ttf（Kivy对.ttc兼容差），
#      并兼容安卓系统字体；注册失败自动回退默认字体不黑屏
#   2. 崩溃：修复 TOOL_LIST 未定义；pyserial 未安装不闪退
#   3. 线程安全：串口初始化不再在子线程调 Clock，改用 @mainthread 回调
#   4. 异常可见：所有异常打印 traceback，不再静默吞掉
#   5. UI增强：右侧清点进度总览，左侧按钮锁定变绿/等待变黄
# ============================================================
import os

def _register_cn_font():
    from kivy.core.text import LabelBase
    candidates = [
        r"C:\Windows\Fonts\simhei.ttf",           # 黑体(.ttf) 兼容性最好
        r"C:\Windows\Fonts\msyh.ttc",             # 微软雅黑(.ttc) 备用
        r"C:\Windows\Fonts\simsun.ttc",           # 宋体 备用
        r"C:\Windows\Fonts\Deng.ttf",             # 等线(.ttf) 备用
        r"/system/fonts/DroidSansFallback.ttf",   # 安卓
        r"/system/fonts/NotoSansCJK-Regular.ttc", # 安卓
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                LabelBase.register(name="CNFont", fn_regular=path)
                print("[字体] 注册成功:", path)
                return True
            except Exception as e:
                print("[字体] 注册失败 %s: %s" % (path, e))
    print("[字体] 未找到中文字体，回退默认字体")
    return False

HAS_CN_FONT = _register_cn_font()

def font_opts(**kwargs):
    """给 Label/Button 注入中文字体；字体缺失时自动回退默认，避免黑屏"""
    if HAS_CN_FONT:
        kwargs.setdefault("font_name", "CNFont")
    return kwargs

# ===================== 配置区（仅修改此处） =====================
# 工机具清单（必须和K230下位机工具名完全一致）
TOOL_DICT = {
    1: "GSM手持终端机",
    2: "对讲机",
    3: "防护包",
    4: "钢尺",
    5: "红旗",
    6: "黄旗",
    7: "活口",
    8: "检查小锤",
    9: "镜子",
    10: "喇叭",
    11: "烟斗扳手"
}
BAUD = 115200
SERIAL_DEV = "COM10"   # 电脑调试填COM号；安卓固定 /dev/ttyUSB0

# ===================== 依赖与全局状态 =====================
try:
    import serial
    HAS_SERIAL = True
except ImportError:
    serial = None
    HAS_SERIAL = False
    print("[警告] 未安装 pyserial，串口功能不可用。安装：pip install pyserial")

# 安卓 USB 串口支持（打包 Android 时才有这两个库；电脑调试自动跳过）
try:
    import usb4a
    from usb4a import usb
    from usbserial4a import serial4a
    HAS_ANDROID_USB = True
except Exception:
    usb4a = None
    usb = None
    serial4a = None
    HAS_ANDROID_USB = False

import json
import threading
import time
import traceback

from kivy.app import App
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.gridlayout import GridLayout
from kivy.uix.textinput import TextInput
from kivy.uix.scrollview import ScrollView
from kivy.uix.spinner import Spinner, SpinnerOption
from kivy.clock import Clock, mainthread
from kivy.uix.popup import Popup
from kivy.core.window import Window
from kivy.utils import platform
from kivy.metrics import dp, sp

# ===================== 界面字号与尺寸（统一用 dp，不随系统字体缩放，真机高DPI也不溢出） =====================
FS_TITLE = dp(22)     # 页面主标题
FS_SECTION = dp(19)   # 区块小标题
FS_BODY = dp(18)      # 正文/按钮
FS_STATUS = dp(19)    # 状态提示（醒目）
FS_ROW = dp(18)       # 清单行文字
FS_NUM = dp(20)       # 数量 + - 按钮
FS_PROG = dp(17)      # 右侧进度区文字
# 行高 / 间距 / 内边距（固定尺寸，文字在框内自适应）
ROW_H_CHECK = dp(60)  # 已选清单行高（固定）
ROW_H_PROG = dp(52)   # 进度总览行高（固定）
H_CFG_ROW = dp(62)    # 选择操作行高（固定：工具下拉/数量/-+/添加）
H_STATE = dp(64)      # 底部状态栏高度（固定）
SPACING = dp(10)      # 块间距
PADDING = dp(12)      # 页面内边距

# 深灰底色；仅电脑调试时固定窗口尺寸（安卓由 fullscreen=1 全屏自适应，否则会只占屏幕一部分）
Window.clearcolor = (0.13, 0.14, 0.16, 1)
if platform != "android":
    Window.size = (1080, 2340)   # 电脑调试用手机比例，便于预览真实布局

uart_handle = None
rx_loop_running = False

class CheckRecord:
    def __init__(self):
        self.target_now = None       # 当前待识别工具
        self.target_count = 1        # 当前待识别工具的目标数量
        self.check_list = {}         # 已添加清点清单 {工具名: 目标数量}
        self.finished = set()        # 已识别锁定的工具
        self.all_complete = False    # 是否全部清点完毕

global_check = CheckRecord()

# -------------------------- 串口底层通信函数 --------------------------
def open_uart():
    global uart_handle
    # 安卓：通过 USB Host API 枚举并打开串口设备（usbserial4a 自动匹配 CH340/CP210x/FTDI/CDC）
    if HAS_ANDROID_USB:
        try:
            dev_list = usb.get_usb_device_list()
            if not dev_list:
                return False, "未检测到USB设备，请确认K230已通过OTG线连接手机"
            dev = dev_list[0]
            device_name = dev.getDeviceName()
            # 首次连接需要 USB 权限；在后台线程等待用户在主线程弹出的授权框
            if not usb.has_usb_permission(dev):
                usb.request_usb_permission(dev)
                for _ in range(50):   # 最多等 5 秒
                    time.sleep(0.1)
                    if usb.has_usb_permission(dev):
                        break
                if not usb.has_usb_permission(dev):
                    return False, "等待USB授权超时，请重新打开App并点击允许"
            uart_handle = serial4a.get_serial_port(
                device_name, BAUD, 8, "N", 1, timeout=0.1)
            if uart_handle.is_open:
                return True, "安卓USB串口连接成功"
            return False, "安卓USB串口打开失败：设备被占用或驱动不支持"
        except Exception as err:
            return False, "安卓USB串口打开失败：%s" % err
    # 电脑：pyserial 直接打开 COM 口
    if not HAS_SERIAL:
        return False, "未安装pyserial（pip install pyserial）"
    try:
        uart_handle = serial.Serial(SERIAL_DEV, BAUD, timeout=0.1)
        return True, "串口连接K230开发板成功"
    except Exception as err:
        return False, "串口打开失败：%s" % err

def send_cmd_to_k230(cmd_dict):
    global uart_handle
    if not (HAS_SERIAL and uart_handle and uart_handle.is_open):
        return
    try:
        send_str = json.dumps(cmd_dict, ensure_ascii=False) + "\n"
        uart_handle.write(send_str.encode("utf-8"))
    except Exception:
        traceback.print_exc()

def sync_list_to_k230():
    """把整份清点清单同步给K230（增删工具时调用），K230屏幕全量显示"""
    tools = [{"tool": t, "count": c} for t, c in global_check.check_list.items()]
    send_cmd_to_k230({"cmd": "sync_list", "tools": tools})

def receive_data_loop():
    """后台持续接收K230回传识别结果，独立线程不阻塞UI"""
    global uart_handle, rx_loop_running
    rx_loop_running = True
    buffer = b""
    while rx_loop_running:
        if not (uart_handle and uart_handle.is_open):
            break
        try:
            recv_bytes = uart_handle.read(128)
        except Exception:
            traceback.print_exc()
            break
        if recv_bytes:
            buffer += recv_bytes
            while b"\n" in buffer:
                line_data, buffer = buffer.split(b"\n", 1)
                try:
                    json_msg = json.loads(line_data.decode("utf-8", errors="ignore"))
                    handle_k230_message(json_msg)
                except Exception:
                    traceback.print_exc()
    rx_loop_running = False

# ====================== USB 热插拔支持（安卓） ======================
def _hotplug_poll_loop():
    """后台轮询USB设备插拔：打开App后再插USB，能自动请求授权并打开串口"""
    global uart_handle, rx_loop_running
    if not HAS_ANDROID_USB:
        return
    try:
        dev_list = usb.get_usb_device_list()
        had_device = bool(dev_list)
    except Exception:
        had_device = False
    while True:
        try:
            dev_list = usb.get_usb_device_list()
            present = bool(dev_list)
            if present and not had_device:
                had_device = True
                Clock.schedule_once(lambda dt, d=dev_list[0]: _hotplug_attach(d), 0)
            elif not present and had_device:
                had_device = False
                Clock.schedule_once(lambda dt: _hotplug_detach(), 0)
        except Exception:
            traceback.print_exc()
        time.sleep(1.5)

@mainthread
def _hotplug_attach(dev):
    """USB设备插入：主线程请求授权（后台线程请求不弹框），授权后打开串口"""
    global rx_loop_running
    app = App.get_running_app()
    root = app.root if app else None
    if root:
        root.state_label.text = "串口状态：检测到USB设备，请求授权..."
    def worker():
        try:
            # 等待用户授权（最多5秒），授权后打开串口
            if not usb.has_usb_permission(dev):
                usb.request_usb_permission(dev)
                for _ in range(50):
                    time.sleep(0.1)
                    if usb.has_usb_permission(dev):
                        break
            ok, tip = open_uart()
            if ok and not rx_loop_running:
                threading.Thread(target=receive_data_loop, daemon=True).start()
            Clock.schedule_once(lambda dt, o=ok, t=tip: _hotplug_result(o, t), 0)
        except Exception:
            traceback.print_exc()
    threading.Thread(target=worker, daemon=True).start()

@mainthread
def _hotplug_result(ok, tip):
    app = App.get_running_app()
    if app and app.root:
        app.root.state_label.text = "串口状态：%s" % tip

@mainthread
def _hotplug_detach():
    """USB设备拔出：关闭串口，等待下次插入"""
    global uart_handle, rx_loop_running
    rx_loop_running = False
    if uart_handle and uart_handle.is_open:
        try:
            uart_handle.close()
        except Exception:
            pass
    uart_handle = None
    app = App.get_running_app()
    if app and app.root:
        app.root.state_label.text = "串口状态：USB已拔出，重新插入可自动连接"

# -------------------------- Android 唤醒锁（黑屏保活） --------------------------
_wake_lock = None
def acquire_wakelock():
    """获取Android部分唤醒锁：息屏时CPU不休眠，串口读取线程持续运行不丢数据"""
    global _wake_lock
    try:
        from jnius import autoclass
        PythonActivity = autoclass("org.kivy.android.PythonActivity")
        activity = PythonActivity.mActivity
        Context = autoclass("android.content.Context")
        PowerManager = autoclass("android.os.PowerManager")
        pm = activity.getSystemService(Context.POWER_SERVICE)
        _wake_lock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "toolhost:serial")
        _wake_lock.acquire()
        return True
    except Exception:
        traceback.print_exc()
        return False

# -------------------------- K230消息处理 --------------------------
@mainthread
def handle_k230_message(msg):
    """处理K230返回数据，主线程刷新UI（子线程禁止操作控件）"""
    try:
        status_type = msg.get("status")
        app = App.get_running_app()
        root = app.root if app else None
        if status_type == "detect_success":
            match_tool = msg["matched_tool"]
            if match_tool in global_check.finished:
                return
            global_check.finished.add(match_tool)
            global_check.target_now = None
            # 判断是否全部清点完成（基于已添加清单）
            if global_check.check_list and len(global_check.finished) == len(global_check.check_list):
                global_check.all_complete = True
                pop_kwargs = {
                    "title": "清点完成",
                    "content": Label(**font_opts(text="全部工机具识别锁定完毕！", font_size=FS_BODY)),
                    "size_hint": (0.75, 0.4),
                }
                if HAS_CN_FONT:
                    pop_kwargs["title_font"] = "CNFont"
                pop_win = Popup(**pop_kwargs)
                pop_win.open()
            if root:
                root.update_progress()   # 全部完成时状态栏自动变绿"全部工具清点完毕"
            if not global_check.all_complete:
                refresh_ui_text("✅【%s】识别锁定，请选择下一项工具" % match_tool)
        elif status_type == "ready":
            if root:
                root.update_progress()
            refresh_ui_text("等待识别：对准【%s】拍摄" % msg["target"])
        elif status_type == "reset_done":
            if root:
                root.update_progress()
            refresh_ui_text("已重置所有清点记录，可重新开始")
        elif status_type == "list_ok":
            # K230已成功同步整份清单（屏幕只显示选择的工具）
            refresh_ui_text("K230清单已同步：屏幕显示 %d 种工具" % msg.get("total", 0))
        elif status_type == "status_sync":
            # 亮屏/重连后的全量同步：用K230当前锁定状态覆盖本地（补黑屏期间丢失的回传）
            if root:
                root.apply_status_sync(msg)
        elif status_type == "error":
            refresh_ui_text("⚠️ K230返回错误：%s" % msg.get("msg", ""))
    except Exception:
        traceback.print_exc()

@mainthread
def refresh_ui_text(content):
    """统一刷新底部状态提示文字"""
    app = App.get_running_app()
    if app and app.root:
        app.root.state_label.text = content

# 自定义下拉选项：使用中文字体（否则下拉列表选项显示为方框）
class CNSpinnerOption(SpinnerOption):
    def __init__(self, **kwargs):
        if HAS_CN_FONT:
            kwargs.setdefault("font_name", "CNFont")
        kwargs.setdefault("font_size", FS_BODY)
        super().__init__(**kwargs)

# -------------------------- UI界面布局 --------------------------
class MainUI(BoxLayout):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.orientation = "horizontal"
        self.spacing = SPACING
        self.padding = PADDING
        self._build_left()
        self._build_right()
        # 安卓：必须在主线程请求USB设备授权（后台线程调用不会弹授权框）
        if HAS_ANDROID_USB:
            try:
                usb4a.setup()
                # 主动请求第一个USB设备的访问权限，用户点允许后串口即可打开
                dev_list = usb.get_usb_device_list()
                if dev_list and not usb.has_usb_permission(dev_list[0]):
                    usb.request_usb_permission(dev_list[0])
            except Exception:
                traceback.print_exc()
        # UI 完全就绪后再初始化串口（后台线程只做阻塞打开，不碰Clock）
        Clock.schedule_once(self._start_serial, 0.2)
        # 安卓：启动USB热插拔监听（打开App后再插USB也能自动授权连接）
        if HAS_ANDROID_USB:
            threading.Thread(target=_hotplug_poll_loop, daemon=True).start()

    # ---------- 左侧：手动配置清点清单 ----------
    def _build_left(self):
        left_area = BoxLayout(orientation="vertical", size_hint=(0.46, 1), spacing=SPACING)
        t1 = Label(**font_opts(text="清点清单", font_size=FS_TITLE, bold=True,
                               halign="center", valign="middle", size_hint_y=0.06))
        t1.bind(size=lambda w, s: setattr(w, "text_size", s))
        left_area.add_widget(t1)
        # 选择行：工具下拉 + 数量(-/+) + 添加（整行固定高度，元素等高管）
        cfg_row = BoxLayout(orientation="horizontal", size_hint_y=None,
                            height=H_CFG_ROW, spacing=dp(5))
        self.tool_spinner = Spinner(
            text="选择工具", values=list(TOOL_DICT.values()),
            size_hint_x=0.44, font_size=FS_BODY, size_hint_y=1,
            option_cls=CNSpinnerOption, **font_opts())
        btn_minus = Button(
            text="－", font_size=FS_NUM, bold=True, size_hint_x=0.13, size_hint_y=1,
            background_color=(0.35, 0.35, 0.42, 1))
        btn_minus.bind(on_press=lambda w: self._change_count(-1))
        self.cnt_input = TextInput(
            text="1", input_filter="int", multiline=False,
            size_hint_x=0.16, size_hint_y=1, halign="center", font_size=FS_NUM,
            hint_text="数量", **font_opts())
        btn_plus = Button(
            text="＋", font_size=FS_NUM, bold=True, size_hint_x=0.13, size_hint_y=1,
            background_color=(0.35, 0.35, 0.42, 1))
        btn_plus.bind(on_press=lambda w: self._change_count(1))
        btn_add = Button(**font_opts(
            text="添加", font_size=FS_BODY, bold=True, size_hint_x=0.14, size_hint_y=1,
            background_color=(0.2, 0.5, 0.85, 1)))
        btn_add.bind(on_press=self.on_add_tool)
        cfg_row.add_widget(self.tool_spinner)
        cfg_row.add_widget(btn_minus)
        cfg_row.add_widget(self.cnt_input)
        cfg_row.add_widget(btn_plus)
        cfg_row.add_widget(btn_add)
        left_area.add_widget(cfg_row)
        # 已添加清单（点选可优先识别）
        t2 = Label(**font_opts(text="已选清单（点选优先识别）", font_size=FS_SECTION, bold=True,
                               halign="center", valign="middle", size_hint_y=0.05))
        t2.bind(size=lambda w, s: setattr(w, "text_size", s))
        left_area.add_widget(t2)
        self.button_map = {}
        self.count_map = {}
        self.checklist_grid = GridLayout(cols=1, size_hint_y=None, spacing=dp(6))
        self.checklist_grid.bind(minimum_height=self.checklist_grid.setter("height"))
        self.tool_scroll = ScrollView(size_hint=(1, 1), do_scroll_x=False)
        self.tool_scroll.add_widget(self.checklist_grid)
        left_area.add_widget(self.tool_scroll)
        btn_reset = Button(**font_opts(
            text="重置全部清点", font_size=FS_BODY, bold=True, size_hint_y=0.13,
            background_color=(0.85, 0.2, 0.2, 1)))
        btn_reset.bind(on_press=self.reset_all_record)
        left_area.add_widget(btn_reset)
        self.add_widget(left_area)

    # ---------- 右侧：清点进度总览（只显示已添加工具） ----------
    def _build_right(self):
        right_area = BoxLayout(orientation="vertical", size_hint=(0.54, 1), spacing=SPACING)
        t3 = Label(**font_opts(text="清点进度总览", font_size=FS_TITLE, bold=True,
                               halign="center", valign="middle", size_hint_y=0.08))
        t3.bind(size=lambda w, s: setattr(w, "text_size", s))
        right_area.add_widget(t3)
        self.progress_grid = BoxLayout(orientation="vertical", size_hint_y=None, spacing=dp(6))
        self.progress_grid.bind(minimum_height=self.progress_grid.setter("height"))
        self.progress_map = {}
        # 进度总览放进 ScrollView，工具多时可滑动查看
        self.progress_scroll = ScrollView(size_hint=(1, 1), do_scroll_x=False)
        self.progress_scroll.add_widget(self.progress_grid)
        right_area.add_widget(self.progress_scroll)
        self.state_label = Label(**font_opts(
            text="正在初始化串口，连接K230设备...",
            font_size=FS_STATUS, bold=True, color=(1, 1, 0.9, 1), size_hint_y=None,
            height=H_STATE, halign="center", valign="middle"))
        self.state_label.bind(size=lambda w, s: setattr(w, "text_size", s))
        right_area.add_widget(self.state_label)
        self.add_widget(right_area)

    # ---------- 清单增删 ----------
    def _change_count(self, delta):
        """+/- 快速调整数量（最少1）"""
        try:
            cur = int(self.cnt_input.text.strip()) if self.cnt_input.text.strip() else 1
        except ValueError:
            cur = 1
        cur = max(1, cur + delta)
        self.cnt_input.text = str(cur)

    def on_add_tool(self, instance):
        tool = self.tool_spinner.text
        if tool in (None, "", "选择工具"):
            refresh_ui_text("⚠️ 请先在顶部选择工具")
            return
        cnt_text = self.cnt_input.text.strip()
        try:
            cnt = int(cnt_text) if cnt_text else 1
        except ValueError:
            cnt = 1
        if cnt < 1:
            cnt = 1
        self.cnt_input.text = str(cnt)
        # 加入/更新清单；若改数量则解锁该项重新清点
        global_check.check_list[tool] = cnt
        if tool in global_check.finished:
            global_check.finished.discard(tool)
        global_check.all_complete = False
        self.rebuild_checklist_rows()
        self.rebuild_progress()
        self.update_progress()
        # 同步整份清单到K230（屏幕全量显示）；默认全部开启识别（K230全量清点），不设优先
        sync_list_to_k230()
        if HAS_SERIAL:
            refresh_ui_text("✅ 全部识别中：对准任一工具拍摄，达标自动锁定" )
        else:
            refresh_ui_text("已添加：%s × %d（串口库不可用，未下发）" % (tool, cnt))

    def remove_from_checklist(self, tool):
        if tool in global_check.check_list:
            del global_check.check_list[tool]
        global_check.finished.discard(tool)
        if global_check.target_now == tool:
            global_check.target_now = None
        self.rebuild_checklist_rows()
        self.rebuild_progress()
        self.update_progress()
        sync_list_to_k230()   # 删除后同步整份清单到K230
        refresh_ui_text("已移除：%s" % tool)

    def rebuild_checklist_rows(self):
        self.checklist_grid.clear_widgets()
        self.button_map.clear()
        self.count_map.clear()
        for tool, cnt in global_check.check_list.items():
            row = BoxLayout(orientation="horizontal", size_hint_y=None, height=ROW_H_CHECK, spacing=dp(6))
            # 工具名按钮：文字限制在按钮内换行居中，不撑高不溢出
            btn = Button(**font_opts(text=tool, font_size=FS_ROW))
            btn.halign = "center"
            btn.valign = "middle"
            btn.bind(size=lambda w, s: setattr(w, "text_size", (s[0] - dp(6), s[1] - dp(2))))
            btn.bind(on_press=self.click_tool_btn)
            row.add_widget(btn)
            cnt_lab = Label(**font_opts(text="×%d" % cnt, font_size=FS_ROW, size_hint_x=0.14))
            row.add_widget(cnt_lab)
            # 删除按钮：加大，醒目易点
            del_btn = Button(text="✕", font_size=FS_ROW, bold=True, size_hint_x=0.16,
                             background_color=(0.75, 0.2, 0.2, 1))
            del_btn.bind(on_press=lambda w, t=tool: self.remove_from_checklist(t))
            row.add_widget(del_btn)
            self.button_map[tool] = btn
            self.count_map[tool] = cnt_lab
            self.checklist_grid.add_widget(row)

    def rebuild_progress(self):
        self.progress_grid.clear_widgets()
        self.progress_map = {}
        for tool, cnt in global_check.check_list.items():
            # 每行 = 名称(0.55) + 状态(0.45)，横排不挤压
            row = BoxLayout(orientation="horizontal", size_hint_y=None,
                            height=ROW_H_PROG, spacing=dp(6))
            n_lab = Label(**font_opts(text=tool, font_size=FS_PROG, halign="left", valign="middle"))
            n_lab.bind(size=lambda w, s: setattr(w, "text_size", (s[0] - dp(4), s[1] - dp(2))))
            n_lab.size_hint_x = 0.55
            st_lab = Label(**font_opts(
                text="待清点×%d" % cnt, font_size=FS_PROG,
                color=(1, 0.3, 0.3, 1), size_hint_x=0.45))
            row.add_widget(n_lab)
            row.add_widget(st_lab)
            self.progress_grid.add_widget(row)
            self.progress_map[tool] = st_lab

    # ---------- 串口初始化（线程安全） ----------
    def _start_serial(self, dt):
        def worker():
            ok, tip = open_uart()
            self._serial_result(ok, tip)
        threading.Thread(target=worker, daemon=True).start()

    @mainthread
    def _serial_result(self, ok, tip):
        self.state_label.text = "串口状态：%s" % tip
        if ok:
            threading.Thread(target=receive_data_loop, daemon=True).start()

    # ---------- 交互 ----------
    def click_tool_btn(self, instance):
        select_tool = instance.text
        if select_tool not in global_check.check_list:
            return
        target_cnt = global_check.check_list[select_tool]
        if select_tool in global_check.finished:
            # 已锁定项也允许重新识别：弹窗提示"单项重置"
            is_relock = True
            prompt = "该项已锁定，是否重置并优先识别：\n%s × %d" % (select_tool, target_cnt)
        else:
            is_relock = False
            if global_check.all_complete:
                refresh_ui_text("⚠️ 所有工具已清点完成，请点击重置")
                return
            prompt = "是否优先识别：\n%s × %d" % (select_tool, target_cnt)
        # 弹窗确认：是否优先识别该项（已锁定项=单项重置后重新识别）
        content = BoxLayout(orientation="vertical", padding=dp(8), spacing=dp(8))
        content.add_widget(Label(**font_opts(
            text=prompt,
            font_size=FS_BODY, halign="center", valign="middle")))
        btn_row = BoxLayout(orientation="horizontal", spacing=dp(10), size_hint_y=0.4)
        btn_yes = Button(**font_opts(text="优先识别", font_size=FS_BODY, bold=True,
                                     background_color=(0.2, 0.6, 0.3, 1)))
        btn_no = Button(**font_opts(text="继续全量", font_size=FS_BODY,
                                    background_color=(0.4, 0.4, 0.45, 1)))
        btn_row.add_widget(btn_yes)
        btn_row.add_widget(btn_no)
        content.add_widget(btn_row)
        pop_kwargs = {
            "title": "清点模式",
            "content": content,
            "size_hint": (0.8, 0.42),
        }
        if HAS_CN_FONT:
            pop_kwargs["title_font"] = "CNFont"
        pop = Popup(**pop_kwargs)
        btn_yes.bind(on_press=lambda w: (self._prioritize_tool(select_tool, target_cnt), pop.dismiss()))
        btn_no.bind(on_press=lambda w: (refresh_ui_text("继续全量识别：%s" % select_tool), pop.dismiss()))
        pop.open()

    def _prioritize_tool(self, select_tool, target_cnt):
        """优先识别：下发 set_target 给 K230（K230会单项重置该项锁定后重新识别）"""
        global_check.target_now = select_tool
        global_check.target_count = target_cnt
        # 本地同步单项重置：若该项已锁定，先移除锁定状态，等待重新识别
        if select_tool in global_check.finished:
            global_check.finished.discard(select_tool)
        global_check.all_complete = False
        self.update_progress()
        if HAS_SERIAL:
            send_cmd_to_k230({"cmd": "set_target", "tool": select_tool, "count": target_cnt})
            refresh_ui_text("已优先：%s × %d，对准工具拍摄" % (select_tool, target_cnt))
        else:
            refresh_ui_text("⚠️ 串口库不可用，无法下发指令")

    def reset_all_record(self, instance):
        global_check.target_now = None
        global_check.target_count = 1
        global_check.finished.clear()
        global_check.check_list.clear()
        global_check.all_complete = False
        if HAS_SERIAL:
            send_cmd_to_k230({"cmd": "reset_all"})
        self.rebuild_checklist_rows()
        self.rebuild_progress()
        self.update_progress()
        refresh_ui_text("已重置，清点清单已清空")

    # ---------- 刷新左右两侧状态 ----------
    def update_progress(self):
        # 与K230屏幕一致：未清点红色、识别中黄色、已锁定绿色、全部完毕绿色
        done_all = bool(global_check.check_list) and len(global_check.finished) == len(global_check.check_list)
        if done_all:
            global_check.all_complete = True
            self.state_label.text = "✅ 全部工具清点完毕"
            self.state_label.color = (0.2, 1, 0.3, 1)
        elif global_check.check_list:
            self.state_label.color = (1, 1, 0.9, 1)
        for tool in global_check.check_list:
            need = global_check.check_list[tool]
            st = self.progress_map.get(tool)
            btn = self.button_map.get(tool)
            if st is None or btn is None:
                continue
            if tool in global_check.finished:
                st.text = "✅ 已锁定"
                st.color = (0.2, 1, 0.3, 1)
                btn.background_color = (0.15, 0.6, 0.2, 1)
            elif tool == global_check.target_now:
                st.text = "识别中×%d" % need
                st.color = (1, 0.85, 0.2, 1)
                btn.background_color = (0.8, 0.7, 0.1, 1)
            else:
                st.text = "待清点×%d" % need
                st.color = (1, 0.3, 0.3, 1)   # 未清点：红色
                btn.background_color = (0.2, 0.2, 0.2, 1)

    # ---------- K230状态全量同步（亮屏/重连/周期心跳补差） ----------
    def apply_status_sync(self, msg):
        """收到K230 status_sync（0.2s周期心跳或GET_STATUS）：只同步锁定状态，不重建清单"""
        k_locked = msg.get("locked") or {}
        new_finished = set()
        for k in k_locked:
            try:
                new_finished.add(TOOL_DICT[int(k)])
            except Exception:
                continue
        # 本地清单为准（清单已通过sync_list同步给K230），只更新锁定集合
        changed = (new_finished != global_check.finished)
        global_check.finished = new_finished
        done_all = bool(global_check.check_list) and len(new_finished) == len(global_check.check_list)
        global_check.all_complete = done_all
        if done_all:
            global_check.target_now = None
        # 仅状态变化时才刷新UI（心跳0.2s一次，避免无谓重绘卡顿）
        if changed or done_all:
            self.update_progress()
        if done_all:
            refresh_ui_text("✅ 全部工具清点完毕")
# -------------------------- 应用入口 --------------------------
class ToolHostApp(App):
    def build(self):
        self.title = "铁路工机具清点上位机"
        try:
            ui = MainUI()
            # 安卓：黑屏保活 + 回到前台时主动向K230拉取完整状态（补黑屏期间丢失的回传）
            if platform == "android":
                acquire_wakelock()
                Window.bind(on_resume=self._on_resume)
            return ui
        except Exception:
            # 构建失败兜底：显示错误信息，而不是黑屏/闪退
            traceback.print_exc()
            err_box = BoxLayout(orientation="vertical", padding=20, spacing=10)
            err_box.add_widget(Label(**font_opts(
                text="界面初始化失败，请查看命令行错误信息", font_size=FS_TITLE,
                color=(1, 0.3, 0.3, 1))))
            err_text = Label(**font_opts(
                text=traceback.format_exc()[-800:], font_size=FS_BODY,
                halign="left", valign="top", color=(1, 0.9, 0.9, 1)))
            err_text.bind(size=lambda w, s: setattr(w, "text_size", s))
            err_box.add_widget(err_text)
            return err_box

    def _on_resume(self, *args):
        """App回到前台/亮屏：主动拉取K230完整状态，修复黑屏期间未收到的事件"""
        if HAS_SERIAL:
            send_cmd_to_k230({"cmd": "GET_STATUS"})
            refresh_ui_text("回到前台，正在同步K230清点状态...")

    def on_stop(self):
        """APP退出时关闭串口释放资源"""
        global rx_loop_running, uart_handle, _wake_lock
        rx_loop_running = False
        if uart_handle and uart_handle.is_open:
            try:
                uart_handle.close()
            except Exception:
                pass
        if _wake_lock is not None:
            try:
                _wake_lock.release()
            except Exception:
                pass
            _wake_lock = None

if __name__ == "__main__":
    ToolHostApp().run()