# -*- coding: utf-8 -*-
# 安卓打包入口：p4a 要求主入口必须是 main.py
# 实际逻辑在 tool_host_kivy.py
from tool_host_kivy import ToolHostApp

if __name__ == "__main__":
    ToolHostApp().run()
