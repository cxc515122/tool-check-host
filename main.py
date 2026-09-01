# -*- coding: utf-8 -*-
# 安卓打包入口：p4a 要求主入口必须是 main.py
# 实际逻辑在 tool_host_kivy.py
# 必须在任何 kivy.uix 导入之前设置 Config，确保窗口全屏
import os
os.environ.setdefault("KIVY_ORIENTATION", "Portrait")

from kivy.config import Config

Config.set("graphics", "width", "0")
Config.set("graphics", "height", "0")
Config.set("graphics", "fullscreen", "auto")
Config.set("graphics", "resizable", "0")

from kivy.core.window import Window
from kivy.utils import platform

if platform == "android":
    Window.fullscreen = "auto"

from tool_host_kivy import ToolHostApp

if __name__ == "__main__":
    ToolHostApp().run()
