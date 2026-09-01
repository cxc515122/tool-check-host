[app]
# 应用显示名称
title = 铁路工机具清点

# 包名（应用唯一标识，首次确定后不要再改）
package.name = toolhost
package.domain = org.railway

# 源码目录与包含的文件类型
source.dir = .
source.include_exts = py,png,jpg,jpeg,kv,atlas,ttf

# 版本号
version = 0.1

# 依赖库（kivy + 安卓USB串口 usbserial4a/usb4a）
requirements = python3,kivy==2.3.1,pyserial,pyjnius,usb4a,usbserial4a

# 屏幕方向与全屏
orientation = portrait
fullscreen = 0

# Android 目标/最低版本与 CPU 架构
android.api = 33
android.minapi = 21
android.archs = arm64-v8a

# 权限：联网（USB host 能力由 intent-filter/device_filter 提供，勿用 android.features）
android.permissions = INTERNET

# usbserial4a 必需：termios 白名单 + USB 插拔 intent 过滤
android.p4a_whitelist = lib-dynload/termios.so
android.manifest.intent_filters = intent-filter.xml

[buildozer]
log_level = 2
warn_on_root = 1
