# -*- coding: utf-8 -*-
import gc
import json
import uos
import time
import urandom
from media.sensor import *
from media.display import *
from media.media import *
import ybUtils.YbKey as YbKey
from ybUtils.YbUart import YbUart
from libs.PlatTasks import DetectionApp
from libs.Utils import *
# ====================== 【常量配置区 - 统一管理】 ======================
DATA_ROOT = "/data/"
ROOT_MODEL_PATH = "/sdcard/mp_deployment_source/"
IMG_PREFIX = "img"        # 手动截图前缀
LOCK_PREFIX = "lock"      # 自动锁定截图前缀
RANDOM_DIGIT_LEN = 6      # 随机后缀位数
# 工具定义（编号-名称解耦，修改顺序不影响编号）
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
ALL_TOOL_NUMS = set(TOOL_DICT.keys())
# 屏幕渲染配置
SCREEN_W = 640
SCREEN_H = 480
COLOR_RED = (255, 0, 0)
COLOR_GREEN = (0, 255, 0)
COLOR_YELLOW = (255, 255, 0)
COLOR_WHITE = (255, 255, 255)
# ---- 面板分层布局（性能优化核心）----
PANEL_TOP_H = 78          # 顶部面板高度
PANEL_BOT_Y = 300         # 底部面板起始y
PANEL_BOT_H = 180         # 底部面板高度
BOT_TITLE_FONT = 22       # 底部面板标题字号
BOT_LIST_FONT = 18        # 清单字号
BOT_LIST_Y = 28           # 底部面板内清单起始y
BOT_ROW_H = 21            # 清单行距
BOT_MAX_ROW = 6           # 最大行数（双列=12项，11种工具全显示）
BOT_HINT_Y = 156          # 底部提示行y（面板内坐标）
# ---- IOU目标跟踪配置（替代原25帧计数防抖）----
IOU_THRESH = 0.3          # 同类目标IOU匹配阈值
TRACK_MAX_AGE = 8         # 跟踪丢失容忍帧数（短暂漏检补全）
TRACK_MIN_HITS = 4        # 连续命中帧数才确认为稳定目标
# ---- 检测框绘制配置 ----
BOX_COLOR = COLOR_GREEN   # 检测框颜色
BOX_THICKNESS = 2         # 检测框线宽
BOX_LABEL_FONT = 16       # 框上标签字号
# 按键配置
LONG_PRESS_MS = 300
# 模型参数
CONF_THRESH = 0.43
NMS_THRESH = 0.05
# 业务防抖配置
ALERT_INTERVAL_MS = 60
GC_INTERVAL_FRAME = 20
# =====================================================================
# 全局状态
class AppState:
    def __init__(self):
        self.selected_nums = set()
        self.standard_tool_count = {}
        self.real_time_count = {}
        self.locked_count = {}
        self.current_target = None  # 联动模式：当前识别目标工具编号
        self.time_save_dir = ""
        self.img_index = 0
        self.last_alert_tick = 0
        for num in ALL_TOOL_NUMS:
            self.standard_tool_count[num] = 1
        self.sync_selected()
    def sync_selected(self):
        self.selected_nums = set(self.standard_tool_count.keys())
    def reset_all_lock(self):
        self.locked_count.clear()
        self.real_time_count.clear()
        tracker.reset()   # 重置IOU跟踪器，开启新一轮清点
# 硬件实例
uart = YbUart()
key = YbKey.YbKey()
state = AppState()
det_app = None
sensor = None
labels_global = []
canvas = None      # 合成显示画布
ui_top = None      # 顶部面板（黑底+文字，低频重绘）
ui_bot = None      # 底部面板
last_render_key = None
# ====================== 工具函数封装 ======================
def gc_clean():
    gc.collect()

def get_random_suffix(digit=6):
    max_num = 10 ** digit - 1
    rand_num = urandom.randint(0, max_num)
    return f"{rand_num:0{digit}d}"

def get_time_random_folder_name():
    t = time.localtime()
    time_str = f"{t[0]:04d}{t[1]:02d}_{t[3]:02d}{t[4]:02d}"
    rand_str = get_random_suffix(RANDOM_DIGIT_LEN)
    return f"{time_str}_{rand_str}"

def create_time_dir():
    try:
        uos.stat(DATA_ROOT)
    except OSError:
        uos.mkdir(DATA_ROOT)
    folder_name = get_time_random_folder_name()
    full_dir = DATA_ROOT + folder_name + "/"
    try:
        uos.stat(full_dir)
    except OSError:
        uos.mkdir(full_dir)
    state.time_save_dir = full_dir
    print(f"截图保存目录：{full_dir}")

def check_model_dir():
    try:
        uos.stat(ROOT_MODEL_PATH)
    except OSError:
        raise Exception("模型目录不存在，请检查SD卡文件")

def get_select_names():
    return {TOOL_DICT[n] for n in state.selected_nums}

def uart_print(msg):
    uart.send(f"{msg}\r\n")

def uart_json(obj):
    """发送JSON给上位机（联动模式），每行一个完整JSON"""
    try:
        uart.send(json.dumps(obj) + "\n")
    except Exception as e:
        print("JSON发送失败:", e)

# 联动模式标志：收到上位机JSON指令后开启；收到文本指令自动切回自主清点模式
JSON_MODE = False

def reset_det_model():
    global det_app
    if det_app is not None:
        det_app.deinit()
        del det_app
        det_app = None
        gc_clean()

def load_model():
    global det_app, labels_global
    gc_clean()
    deploy_cfg_path = ROOT_MODEL_PATH + "deploy_config.json"
    deploy_conf = read_json(deploy_cfg_path)
    kmodel_path = ROOT_MODEL_PATH + deploy_conf["kmodel_path"]
    labels = deploy_conf["categories"]
    labels_global = labels
    model_input_size = deploy_conf["img_size"]
    nms_option = deploy_conf["nms_option"]
    model_type = deploy_conf["model_type"]
    anchors = []
    if model_type == "AnchorBaseDet":
        anchors = deploy_conf["anchors"][0] + deploy_conf["anchors"][1] + deploy_conf["anchors"][2]
    del deploy_conf
    gc_clean()
    det_app = DetectionApp(
        "image", kmodel_path, labels, model_input_size,
        anchors, model_type, CONF_THRESH, NMS_THRESH,
        [SCREEN_W, SCREEN_H], [SCREEN_W, SCREEN_H], debug_mode=0
    )
    det_app.config_preprocess()
    return labels

def check_and_lock_all_pass(stable_curr):
    missing_list = []
    newly_locked = []
    for num, std_cnt in state.standard_tool_count.items():
        tool_name = TOOL_DICT[num]
        if num in state.locked_count:
            continue
        curr = stable_curr.get(tool_name, 0)
        if curr >= std_cnt:
            state.locked_count[num] = curr
            newly_locked.append(num)
        else:
            missing_list.append(f"{tool_name}(需{std_cnt},当前{curr})")
    all_complete = len(missing_list) == 0
    return all_complete, missing_list, newly_locked
# ====================== IOU目标跟踪器（简化版SORT，纯IOU匹配） ======================
class Track:
    """单个被跟踪目标"""
    def __init__(self, track_id, cls_id, bbox, score):
        self.id = track_id
        self.cls_id = cls_id
        self.bbox = bbox          # [x1, y1, x2, y2]
        self.score = score
        self.hits = 1             # 连续命中帧数
        self.age = 0              # 连续未命中帧数
        self.confirmed = False    # 是否达到min_hits确认为稳定目标

class IOUTracker:
    """基于IOU的多目标跟踪器：同类目标按最大IOU关联，支持短暂漏检补全"""
    def __init__(self):
        self.tracks = []
        self.next_id = 1

    def reset(self):
        self.tracks = []
        self.next_id = 1

    @staticmethod
    def iou(box1, box2):
        """计算两个[ x1,y1,x2,y2 ]框的交并比"""
        x1 = max(box1[0], box2[0])
        y1 = max(box1[1], box2[1])
        x2 = min(box1[2], box2[2])
        y2 = min(box1[3], box2[3])
        w = max(0, x2 - x1)
        h = max(0, y2 - y1)
        inter = w * h
        a1 = max(0, box1[2] - box1[0]) * max(0, box1[3] - box1[1])
        a2 = max(0, box2[2] - box2[0]) * max(0, box2[3] - box2[1])
        union = a1 + a2 - inter
        return inter / union if union > 0 else 0.0

    def update(self, detections):
        """
        detections: list of (cls_id, bbox_xyxy, score)
        返回稳定计数 {工具名称: 数量}（仅统计confirmed目标）
        """
        matched_det = set()
        for track in self.tracks:
            best_iou = IOU_THRESH
            best_di = -1
            for di, det in enumerate(detections):
                if di in matched_det:
                    continue
                cls_id, bbox, score = det
                if cls_id != track.cls_id:
                    continue
                v = self.iou(track.bbox, bbox)
                if v > best_iou:
                    best_iou = v
                    best_di = di
            if best_di >= 0:
                track.bbox = detections[best_di][1]
                track.score = detections[best_di][2]
                track.hits += 1
                track.age = 0
                if track.hits >= TRACK_MIN_HITS:
                    track.confirmed = True
                matched_det.add(best_di)
            else:
                track.age += 1
        for di, det in enumerate(detections):
            if di not in matched_det:
                self.tracks.append(Track(self.next_id, det[0], det[1], det[2]))
                self.next_id += 1
        self.tracks = [t for t in self.tracks if t.age <= TRACK_MAX_AGE]
        count = {}
        for t in self.tracks:
            if t.confirmed:
                lab = labels_global[t.cls_id]
                count[lab] = count.get(lab, 0) + 1
        return count

tracker = IOUTracker()

def extract_detections(res):
    """
    从DetectionApp.run()结果提取检测列表 [(cls_id, bbox_xyxy, score), ...]
    兼容 bbox/boxes/xyxy 多种键名；假设框格式为 [x1,y1,x2,y2]
    """
    if not isinstance(res, dict):
        return []
    idx_list = res.get("idx", []) or []
    bbox_list = []
    for key in ("bbox", "boxes", "xyxy", "box"):
        if key in res and res[key]:
            bbox_list = res[key]
            break
    score_list = []
    for key in ("score", "scores", "conf", "confidence"):
        if key in res and res[key]:
            score_list = res[key]
            break
    dets = []
    n = min(len(idx_list), len(bbox_list))
    for i in range(n):
        b = bbox_list[i]
        if b is None or len(b) < 4:
            continue
        x1, y1, x2, y2 = float(b[0]), float(b[1]), float(b[2]), float(b[3])
        # 若实际模型返回的是 [x,y,w,h]，取消下面两行注释转为xyxy
        # x2, y2 = x1 + x2, y1 + y2
        score = float(score_list[i]) if i < len(score_list) else 1.0
        dets.append((int(idx_list[i]), [x1, y1, x2, y2], score))
    return dets

def save_capture(prefix=IMG_PREFIX):
    global canvas
    img_name = f"{prefix}{state.img_index}.jpg"
    full_path = state.time_save_dir + img_name
    canvas.save(full_path)
    state.img_index += 1
    return full_path

def draw_detections_on_canvas(canvas, res):
    """
    在canvas视频区域绘制检测框+类别标签+置信度。
    每帧调用，使屏幕显示和自动截图都能看到具体识别的是哪样工具。
    框格式兼容 [x1,y1,x2,y2]，自动转为 OpenMV draw_rectangle 需要的 x,y,w,h。
    """
    if not isinstance(res, dict):
        return
    idx_list = res.get("idx", []) or []
    bbox_list = []
    for key in ("bbox", "boxes", "xyxy", "box"):
        if key in res and res[key]:
            bbox_list = res[key]
            break
    if not bbox_list:
        return
    score_list = res.get("score") or res.get("scores") or []
    n = min(len(idx_list), len(bbox_list))
    for i in range(n):
        b = bbox_list[i]
        if b is None or len(b) < 4:
            continue
        x1, y1, x2, y2 = int(b[0]), int(b[1]), int(b[2]), int(b[3])
        # 兼容 [x,y,w,h]：若x2<x1或y2<y1说明是宽高格式，自动转换
        if x2 < x1 or y2 < y1:
            x2, y2 = x1 + x2, y1 + y2
        w = max(1, x2 - x1)
        h = max(1, y2 - y1)
        cls_id = int(idx_list[i])
        if 0 <= cls_id < len(labels_global):
            lab = labels_global[cls_id]
        else:
            lab = "cls%d" % cls_id
        score = float(score_list[i]) if i < len(score_list) else 1.0
        # 画矩形框（OpenMV兼容：x, y, w, h, color, thickness）
        try:
            canvas.draw_rectangle(x1, y1, w, h, BOX_COLOR, BOX_THICKNESS)
        except TypeError:
            canvas.draw_rectangle(x1, y1, w, h, BOX_COLOR)
        # 标签文字：框上方，超出顶部则画在框内
        label = "%s %d%%" % (lab, int(score * 100))
        ty = y1 - BOX_LABEL_FONT - 2
        if ty < PANEL_TOP_H:
            ty = y1 + 2
        canvas.draw_string_advanced(x1, ty, BOX_LABEL_FONT, label, color=BOX_COLOR)

# ====================== 面板构建（低频，数据变化才调用） ======================
def rebuild_panels(stable_cnt, all_ok, missing_list, sorted_nums):
    global ui_top, ui_bot
    ui_top.clear()
    ui_top.draw_string_advanced(10, 8, 28, "分段锁定清点扫描【IOU跟踪】", color=COLOR_WHITE)
    if all_ok and len(state.standard_tool_count) > 0:
        ui_top.draw_string_advanced(10, 46, 24, "✅ 全部工具清点完成锁定", color=COLOR_GREEN)
    elif len(state.standard_tool_count) == 0:
        ui_top.draw_string_advanced(10, 46, 24, "⚠️ 未配置标准清单，发送PL/SETALL", color=COLOR_YELLOW)
    else:
        ui_top.draw_string_advanced(10, 46, 24, "❌ 存在未锁定缺失工具", color=COLOR_RED)
    ui_bot.clear()
    ui_bot.draw_string_advanced(10, 2, BOT_TITLE_FONT, "【清点清单 | 标准 | 当前】", color=COLOR_YELLOW)
    row = 0
    draw_line = 0
    n_items = len(sorted_nums)
    while draw_line < n_items and row < BOT_MAX_ROW:
        y = BOT_LIST_Y + row * BOT_ROW_H
        num_l = sorted_nums[draw_line]
        name_l = TOOL_DICT[num_l]
        std_l = state.standard_tool_count[num_l]
        if num_l in state.locked_count:
            now_l, tag_l, color_l = state.locked_count[num_l], "锁", COLOR_GREEN
        else:
            now_l = stable_cnt.get(name_l, 0)
            tag_l = ""
            color_l = COLOR_GREEN if now_l >= std_l else COLOR_RED
        ui_bot.draw_string_advanced(20, y, BOT_LIST_FONT,
                                    "%s %d|%d%s" % (name_l, std_l, now_l, tag_l), color=color_l)
        draw_line += 1
        if draw_line < n_items and row < BOT_MAX_ROW:
            num_r = sorted_nums[draw_line]
            name_r = TOOL_DICT[num_r]
            std_r = state.standard_tool_count[num_r]
            if num_r in state.locked_count:
                now_r, tag_r, color_r = state.locked_count[num_r], "锁", COLOR_GREEN
            else:
                now_r = stable_cnt.get(name_r, 0)
                tag_r = ""
                color_r = COLOR_GREEN if now_r >= std_r else COLOR_RED
            ui_bot.draw_string_advanced(340, y, BOT_LIST_FONT,
                                        "%s %d|%d%s" % (name_r, std_r, now_r, tag_r), color=color_r)
            draw_line += 1
        row += 1
    if not all_ok and len(missing_list) > 0:
        tip = "缺%d项:%s" % (len(missing_list), missing_list[0][:20])
        tip_color = COLOR_RED
    else:
        tip = "PL批量 L指令 CAP截图 RESET重置 | 短按截/长按重置"
        tip_color = COLOR_WHITE
    ui_bot.draw_string_advanced(10, BOT_HINT_Y, BOT_LIST_FONT, tip, color=tip_color)
# ====================== 上位机JSON联动指令处理 ======================
def handle_json_cmd(obj):
    """
    处理上位机下发的JSON联动指令：
      {"cmd":"sync_list","tools":[{"tool":"对讲机","count":3},...]}  → 同步整份清点清单，屏幕全量显示
      {"cmd":"set_target","tool":"对讲机","count":3}                → 仅切换当前识别目标，不清空清单
      {"cmd":"reset_all"}                                           → 清空锁定与清单，回传reset_done
    """
    cmd = obj.get("cmd")
    name2num = {v: k for k, v in TOOL_DICT.items()}
    if cmd == "sync_list":
        # 整份清单同步：tools = [{"tool":"对讲机","count":3}, ...]
        tools = obj.get("tools", [])
        state.standard_tool_count.clear()
        ok_cnt = 0
        for item in tools:
            if not isinstance(item, dict):
                continue
            t = item.get("tool")
            n = name2num.get(t)
            if n is None:
                continue
            try:
                c = int(item.get("count", 1))
            except Exception:
                c = 1
            if c < 1:
                c = 1
            state.standard_tool_count[n] = c
            ok_cnt += 1
        state.sync_selected()
        state.reset_all_lock()      # 清单变更，重置锁定与跟踪重新清点
        uart_json({"status": "list_ok", "total": ok_cnt})
        uart_print("联动: 清单同步%d种工具" % ok_cnt)
    elif cmd == "set_target":
        tool = obj.get("tool")
        num = name2num.get(tool)
        if num is None:
            uart_json({"status": "error", "msg": "未知工具: %s" % tool})
            return
        # 仅切换当前识别目标，不清空整份清单（屏幕仍显示全部已选工具）
        state.current_target = num
        try:
            count = int(obj.get("count", 1))
        except Exception:
            count = 1
        if num in state.standard_tool_count:
            state.standard_tool_count[num] = max(1, count)
        uart_json({"status": "ready", "target": tool})
        uart_print("联动: 当前目标 %s" % tool)
    elif cmd == "reset_all":
        state.reset_all_lock()
        # 联动重置：恢复默认全部工具（与开机状态一致），等待上位机重新选择
        state.standard_tool_count.clear()
        for num in ALL_TOOL_NUMS:
            state.standard_tool_count[num] = 1
        state.sync_selected()
        state.current_target = None
        uart_json({"status": "reset_done"})
        uart_print("联动: 已重置，恢复默认全部工具")
    else:
        uart_json({"status": "error", "msg": "未知cmd: %s" % cmd})

# ====================== 串口指令处理（PL批量+RESET重置+JSON联动） ======================
def handle_uart_cmd(cmd):
    global JSON_MODE
    cmd = cmd.strip()
    # 上位机JSON联动指令
    if cmd.startswith("{"):
        JSON_MODE = True
        try:
            obj = json.loads(cmd)
            handle_json_cmd(obj)
        except Exception as e:
            uart_json({"status": "error", "msg": "JSON解析失败"})
            uart_print("JSON解析失败: %s" % e)
        return
    # 收到文本指令 → 切回自主清点模式
    JSON_MODE = False
    if cmd == "L":
        uart_print("编号  工具名称")
        for num, name in TOOL_DICT.items():
            uart_print("%2d   %s" % (num, name))
        uart_print("====================")
        uart_print("【基础单条指令】")
        uart_print("S N X   设置单工具标准数量 例:S 2 3")
        uart_print("CLR     清空全部标准清单&选中")
        uart_print("CAP     保存当前渲染画布截图(含检测框)")
        uart_print("RESET   清空锁定状态+IOU跟踪，重新清点")
        uart_print("【批量配置指令】")
        uart_print("PL N1,X1;N2,X2;N3,X3 批量设置多工具标准数量")
        uart_print("SETALL X 全部工具统一设置标准数量X")
        uart_print("DEL N    从清单移除指定编号工具")
        uart_print("CLEARSEL 仅清空选中/清单")
        uart_print("【IOU跟踪+检测框】")
        uart_print("工具实例自动分配ID，连续4帧命中才计入，丢失8帧内补全")
        uart_print("屏幕与截图均绘制检测框+类别标签+置信度")
        uart_print("【锁定自动截图】")
        uart_print("工具达标自动锁定，同时自动保存lock开头截图(含框)")
        uart_print("【按键操作】")
        uart_print("短按按键：保存当前画面截图(含框)")
        uart_print("长按300ms：清空锁定+跟踪，重置清点")
    elif cmd.startswith("SETALL "):
        try:
            x_str = cmd.replace("SETALL ", "").strip()
            x = int(x_str)
            if x < 0:
                uart_print("数量不能为负数")
                return
            state.standard_tool_count.clear()
            for num in ALL_TOOL_NUMS:
                state.standard_tool_count[num] = x
            state.sync_selected()
            uart_print("已批量设置全部%d种工具标准数量=%d" % (len(ALL_TOOL_NUMS), x))
        except ValueError:
            uart_print("格式错误 示例: SETALL 2")
    elif cmd.startswith("PL"):
        batch_str = cmd.lstrip("PL").strip()
        group_list = batch_str.split(";")
        add_cnt = 0
        state.standard_tool_count.clear()
        err_group = []
        for group in group_list:
            group = group.strip()
            if not group:
                continue
            parts = group.split(",")
            if len(parts) != 2:
                err_group.append("%s 字段数量错误" % group)
                continue
            try:
                n = int(parts[0].strip())
                x = int(parts[1].strip())
                if n in TOOL_DICT and x >= 0:
                    state.standard_tool_count[n] = x
                    add_cnt += 1
                else:
                    err_group.append("%s 编号不存在/数量负数" % group)
            except ValueError:
                err_group.append("%s 数字解析失败" % group)
        state.sync_selected()
        uart_print("批量导入完成，共加载%d种工具清点配置" % add_cnt)
        if len(err_group) > 0:
            uart_print("⚠️ 错误分组：%s" % err_group)
    elif cmd.startswith("S "):
        parts = cmd.strip().split()
        if len(parts) != 3:
            uart_print("格式错误！示例:S 3 2 代表3号工具需要2个")
            return
        try:
            n = int(parts[1])
            cnt = int(parts[2])
            if n not in TOOL_DICT:
                uart_print("工具编号不存在")
                return
            if cnt < 0:
                uart_print("数量不能为负数")
                return
            state.standard_tool_count[n] = cnt
            state.sync_selected()
            uart_print("设置 %s 标准数量=%d" % (TOOL_DICT[n], cnt))
        except ValueError:
            uart_print("参数必须为数字，例:S 1 1")
    elif cmd.startswith("DEL "):
        try:
            n_str = cmd.replace("DEL ", "").strip()
            n = int(n_str)
            if n in state.standard_tool_count:
                del state.standard_tool_count[n]
                state.sync_selected()
                uart_print("已从清点清单移除 %d:%s" % (n, TOOL_DICT[n]))
            else:
                uart_print("编号%d不在清点清单内" % n)
        except ValueError:
            uart_print("格式错误 示例: DEL 5")
    elif cmd == "CLR" or cmd == "CLEARSEL":
        state.standard_tool_count.clear()
        state.sync_selected()
        uart_print("已清空全部清点清单与选中工具")
    elif cmd == "RESET":
        state.reset_all_lock()
        uart_print("✅ 已清空所有锁定计数+IOU跟踪，开启新一轮清点")
    elif cmd == "GET_STATUS":
        # 回传当前完整状态给上位机（亮屏/重连时全量同步，补回黑屏期间丢失的回传）
        uart_json({
            "status": "status_sync",
            "list": {str(k): v for k, v in state.standard_tool_count.items()},
            "locked": {str(k): v for k, v in state.locked_count.items()},
        })
        uart_print("联动: 已回传当前状态(清单%d种,锁定%d种)" % (
            len(state.standard_tool_count), len(state.locked_count)))
    elif cmd == "CAP":
        full_path = save_capture()
        uart_print("截图已保存:%s" % full_path)
    else:
        uart_print("未知指令！发送L查看全部指令")
    sel_list = sorted(state.selected_nums)
    if not sel_list:
        sel_tip = "已选工具：无"
    else:
        sel_str = ",".join(map(str, sel_list))
        sel_tip = "清点工具编号：%s" % sel_str
    std_tip = "批量标准配置：%s" % state.standard_tool_count
    lock_tip = "已锁定达标工具：%s" % list(state.locked_count.keys())
    uart_print("%s | %s | %s" % (sel_tip, std_tip, lock_tip))
    uart_print("")
# ====================== 主程序入口 ======================
if __name__ == "__main__":
    try:
        check_model_dir()
        create_time_dir()
        print("系统启动完成【IOU跟踪+检测框+分段锁定清点】")
        uart_print("==== 铁路工具分段清点系统(IOU跟踪+检测框版) ====")
        uart_print("L=查看工具列表与全部批量指令")
        uart_print("PL批量录入多种工具+数量，SETALL统一全量设置")
        uart_print("RESET重置锁定+跟踪，CAP=保存当前画面截图(含检测框)")
        uart_print("IOU跟踪自动分配工具ID，屏幕与截图均绘制检测框+标签")
        uart_print("工具达标自动锁定并自动截图(lock前缀，含框)")
        uart_print("截图存储目录：%s" % state.time_save_dir)
        uart_print("")
        sensor = Sensor()
        sensor.reset()
        sensor.set_framesize(width=SCREEN_W, height=SCREEN_H, chn=CAM_CHN_ID_1)
        sensor.set_pixformat(Sensor.RGB565, chn=CAM_CHN_ID_1)
        Display.init(Display.ST7701, width=SCREEN_W, height=SCREEN_H, to_ide=True)
        MediaManager.init()
        sensor.run()
        load_model()
        select_names_set = get_select_names()
        canvas = image.Image(SCREEN_W, SCREEN_H, image.RGB565)
        ui_top = image.Image(SCREEN_W, PANEL_TOP_H, image.RGB565)
        ui_bot = image.Image(SCREEN_W, PANEL_BOT_H, image.RGB565)
        sorted_nums_cache = sorted(state.standard_tool_count.keys())
        last_render_key = None
        uart_rx_buf = b""          # 串口行缓冲：防止长JSON被拆包
        key_press_start = 0
        key_is_down = False
        frame_cnt = 0
        while True:
            # 1、串口指令处理（行缓冲，按\n拼完整一行再解析）
            raw = uart.read()
            if raw:
                uart_rx_buf += raw
                while b"\n" in uart_rx_buf:
                    line, uart_rx_buf = uart_rx_buf.split(b"\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    cmd = line.decode("utf-8", "ignore")
                    print("收到指令:", cmd)
                    handle_uart_cmd(cmd)
                    select_names_set = get_select_names()
                    sorted_nums_cache = sorted(state.standard_tool_count.keys())
                    last_render_key = None
            # 2、按键检测
            press_state = key.is_pressed()
            now_ms = time.ticks_ms()
            if press_state == 1 and not key_is_down:
                key_is_down = True
                key_press_start = now_ms
            elif press_state == 0 and key_is_down:
                press_duration = now_ms - key_press_start
                key_is_down = False
                if press_duration < LONG_PRESS_MS:
                    full_path = save_capture()
                    uart_print("【按键短按】截图已保存:%s" % full_path)
                else:
                    state.reset_all_lock()
                    uart_print("【按键长按】已重置锁定+IOU跟踪，重新清点")
                    last_render_key = None
            # 3、取摄像头帧
            img = sensor.snapshot(chn=CAM_CHN_ID_1)
            # 4、内存转换推理
            tmp_path = "/tmp/tmp_frame.jpg"
            img.save(tmp_path)
            img_chw, rgb888 = read_image(tmp_path)
            uos.remove(tmp_path)
            res = det_app.run(img_chw)
            det_app.draw_result(rgb888, res)
            # 5、IOU跟踪：提取检测框并更新跟踪器，返回稳定计数
            detections = extract_detections(res)
            stable_cnt = tracker.update(detections)
            state.real_time_count = stable_cnt
            # 6、锁定逻辑
            all_ok, missing_list, newly_locked = check_and_lock_all_pass(stable_cnt)
            # 7、面板分层：数据变化才重绘面板
            rk = (
                tuple(sorted_nums_cache),
                tuple(sorted(state.standard_tool_count.items())),
                tuple(sorted(state.locked_count.items())),
                tuple(sorted(stable_cnt.items())),
            )
            if rk != last_render_key:
                rebuild_panels(stable_cnt, all_ok, missing_list, sorted_nums_cache)
                last_render_key = rk
            # 8、合成显示：摄像头帧 → 检测框 → 上下面板
            #    检测框画在面板之前，面板区域的框被覆盖，中间视频区域保留框
            canvas.copy_from(img)
            draw_detections_on_canvas(canvas, res)
            canvas.draw_image(ui_top, 0, 0)
            canvas.draw_image(ui_bot, 0, PANEL_BOT_Y)
            # 9、新锁定工具 → 自动截图（canvas已含检测框+面板+锁定状态）
            if newly_locked:
                names = "、".join(TOOL_DICT[n] for n in newly_locked)
                full_path = save_capture(prefix=LOCK_PREFIX)
                uart_print("【自动锁定截图】%s 已锁定，截图:%s" % (names, full_path))
                # 无论全量/点选，锁定后都回传结果给上位机（上位机据此更新界面/判断完成）
                for n in newly_locked:
                    uart_json({"status": "detect_success", "matched_tool": TOOL_DICT[n]})
            # 10、屏幕刷新
            Display.show_image(canvas, 0, 0)
            del img, res, detections
            frame_cnt += 1
            if frame_cnt % GC_INTERVAL_FRAME == 0:
                gc_clean()
            time.sleep_ms(10)
    except KeyboardInterrupt:
        print("程序手动终止")
    except OSError as e:
        print("文件/SD卡异常：", str(e))
        import sys
        sys.print_exception(e)
    except Exception as e:
        print("运行异常：", str(e))
        import sys
        sys.print_exception(e)
    finally:
        reset_det_model()
        if isinstance(sensor, Sensor):
            sensor.stop()
        Display.deinit()
        uos.exitpoint(uos.EXITPOINT_ENABLE_SLEEP)
        time.sleep_ms(100)
        MediaManager.deinit()
        gc_clean()
