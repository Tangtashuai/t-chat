# -*- coding: utf-8 -*-
"""
t-chat1.3 自动启动程序
- 检查 Python 版本和依赖
- 启动 server.py
- 等端口就绪后自动打开浏览器
- Ctrl+C 停止

用法：
    python start_tchat.py
    python start_tchat.py --no-browser
"""
import os
import socket
import subprocess
import sys
import time
import webbrowser

ROOT = os.path.dirname(os.path.abspath(__file__))
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "5000"))


def local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def port_ready(host, port, timeout=25):
    target = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            s = socket.create_connection((target, port), timeout=1)
            s.close()
            return True
        except OSError:
            time.sleep(0.4)
    return False


def main():
    if sys.version_info < (3, 6):
        print("[错误] 需要 Python 3.6 或更高版本，当前：%s" % sys.version.split()[0])
        return 1

    os.chdir(ROOT)
    check = subprocess.call([sys.executable, os.path.join(ROOT, "check_deps.py")])
    if check != 0:
        print("[错误] 依赖未安装或版本过低。")
        print("Windows 请双击 run.bat；Linux/macOS 请运行 ./run.sh。")
        print("也可以手动执行：")
        print("  %s -m pip install -r requirements.txt" % sys.executable)
        return 1

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    print("=" * 58)
    print("t-chat1.3 自动启动中 ...")
    print("本机访问: http://127.0.0.1:%d" % PORT)
    print("局域网:   http://%s:%d" % (local_ip(), PORT))
    print("管理后台: http://127.0.0.1:%d/admin" % PORT)
    print("按 Ctrl+C 停止")
    print("=" * 58)

    proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "server.py")], env=env)
    try:
        if port_ready(HOST, PORT):
            if "--no-browser" not in sys.argv:
                webbrowser.open("http://127.0.0.1:%d" % PORT)
        else:
            print("[警告] 端口等待超时，请检查上面的报错信息。")
        proc.wait()
    except KeyboardInterrupt:
        print("\n正在停止 ...")
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())
