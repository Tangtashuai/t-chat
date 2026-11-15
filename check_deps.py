# -*- coding: utf-8 -*-
"""
t-chat1.3 依赖检查器（供 run.bat / run.sh 调用，也可手动运行）

兼容 Python 3.6 ~ 3.13：
- Python 3.6 / 3.7 没有 importlib.metadata，会用模块 __version__ 或 pkg_resources 检测
- 不同 Python 版本对应不同的最低依赖版本

退出码 0 = 依赖齐全且版本兼容；1 = 需要安装/升级依赖。
检查过程不输出内容，方便脚本调用。

制作：汤at帅
B站主页：https://space.bilibili.com/3546392266017069?spm_id_from=333.1007.0.0
"""
import sys


def parse_version(raw):
    """把 2.1.3 / 2.1.3.dev0 之类解析成 (2, 1)，失败返回 None"""
    try:
        parts = []
        for piece in str(raw).split(".")[:2]:
            digits = ""
            for ch in piece:
                if ch.isdigit():
                    digits += ch
                else:
                    break
            parts.append(int(digits) if digits else 0)
        while len(parts) < 2:
            parts.append(0)
        return tuple(parts[:2])
    except Exception:
        return None


def module_version(module, dist_name):
    # Python 3.8+ 优先用 importlib.metadata（Flask 3.1 起 __version__ 已弃用）
    try:
        import importlib.metadata as metadata
        return parse_version(metadata.version(dist_name))
    except Exception:
        pass
    raw = getattr(module, "__version__", None)
    if raw:
        parsed = parse_version(raw)
        if parsed:
            return parsed
    try:
        import pkg_resources
        return parse_version(pkg_resources.get_distribution(dist_name).version)
    except Exception:
        return None


def minimum_versions():
    """按 Python 版本返回 (Flask 最低版本, Flask-SocketIO 最低版本)"""
    if sys.version_info >= (3, 8):
        return (2, 3), (5, 3)
    if sys.version_info >= (3, 7):
        return (2, 1), (5, 1)
    return (2, 0), (5, 1)


def main():
    if sys.version_info < (3, 6):
        return 1
    try:
        import flask
        import flask_socketio
        import socketio  # noqa: F401
        import engineio  # noqa: F401
    except Exception:
        return 1

    flask_min, socketio_min = minimum_versions()
    flask_ver = module_version(flask, "Flask")
    socketio_ver = module_version(flask_socketio, "Flask-SocketIO")
    if flask_ver is None or socketio_ver is None:
        # 极老的系统可能既没有 importlib.metadata 也没有 pkg_resources；
        # 此时导入成功即可认为依赖可用，避免误判。
        return 0 if sys.version_info < (3, 8) else 1
    if flask_ver < flask_min:
        return 1
    if socketio_ver < socketio_min:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
