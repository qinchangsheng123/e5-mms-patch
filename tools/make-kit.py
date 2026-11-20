#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""make-kit v1.0.0 —— 把「最新补丁包 + 一键部署脚本」打成单个 zip

产物：e5-deploy-kit-<ver>.zip，解压后是

    e5-deploy-kit-<ver>/
      一键部署.bat              ← 双击这个
      e5-deploy.py
      e5-mms-patch-<ver>.zip
      使用说明.txt

要求补丁包已经在 e5-mms-patch/dist/ 里打好（package.sh 的产物）。

用法：
  python make-kit.py              取 VERSION 对应的版本
  python make-kit.py --ver 1.0.10 指定版本
"""

import os
import re
import shutil
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))


def find_repo():
    """脚本可能在两处运行：e5diag/ 根（仓库在旁边），或仓库内的 tools/。"""
    for cand in (os.path.join(HERE, "e5-mms-patch"), os.path.dirname(HERE)):
        if os.path.isfile(os.path.join(cand, "VERSION")) and \
           os.path.isdir(os.path.join(cand, "files")):
            return cand
    raise SystemExit("找不到仓库根（要有 VERSION 和 files/）：%s" % HERE)


REPO = find_repo()
DIST = os.path.join(REPO, "dist")
OUT_DIR = os.path.dirname(REPO)

README = """E5 短信转发补丁 · 一键部署包 v{ver}
================================================

这个压缩包里有两样东西：
  · e5-mms-patch-{ver}.zip   最新的补丁包本体（v{ver}）
  · 一键部署.bat             把它装到 E5 上，并自检

怎么用
------
1. 用 USB 线把 E5 连到电脑。连上后电脑会多出一块网卡，设备地址 192.168.9.1。
   如果设备已经深睡、连不上：把 USB 线拔掉再插一次，等 10 秒。

2. 双击「一键部署.bat」。
   它会自己找 Python、自己补依赖、自己挑这个文件夹里版本最高的补丁包。

3. 看窗口最后一行：
   [OK] exit code 0      → 装好了
   [FAIL] exit code N    → N=1 预检失败 / 2 传输失败 / 3 安装失败 / 4 验证失败
   窗口不会自动关，看完按任意键退出。

运行环境
--------
· Windows + Python 3.8 或更高。没装就装一个：https://www.python.org/downloads/
  安装时务必勾选 "Add python.exe to PATH"。
· 依赖 paramiko，第一次运行会自动 pip 安装，需要联网一次。
· 补丁包本身不联网，全部走 USB 局域网传到设备。

重复运行会怎样
--------------
如果设备上已经是 v{ver}，脚本会提示「设备已部署 {ver}」并跳过，不会白折腾。

要强制重装、或者想看详细选项，在本文件夹按住 Shift 右键 →「在此处打开终端」，然后：

    e5-deploy.bat --force             已有同版本也重装
    e5-deploy.bat --dry-run           只预检，不动设备
    e5-deploy.bat --list              列出所有可用补丁包
    e5-deploy.bat e5-mms-patch-1.0.10.zip   装指定包
    e5-deploy.bat --allow-downgrade   允许装比设备旧的版本

脚本会做什么
------------
  1 定位补丁包      挑版本最高的那个
  2 连接设备        192.168.9.1:22，失败重试 3 次
  3 预检            固件形态、核心文件、/tmp 空间、当前队列、版本比对
  4 备份            设备上 /root/e5-backup/pre-deploy-<时间>.tar.gz
  5 上传并校验      md5 双向核对
  6 安装 + 验证     跑 install.sh，再逐个核对文件 md5、两个守护进程、转发配置

装坏了怎么回滚
--------------
部署成功时窗口最后会打印一行真实可用的回滚命令，形如：

    tar -xzf /root/e5-backup/pre-deploy-20261120-181654.tar.gz -C / && reboot

用 SSH 登进设备执行它即可。（install.sh 自己打印的 /tmp/e5-deploy/... 路径在部署
结束后已被清理，别照抄那条。）
"""


def read_version():
    vf = os.path.join(REPO, "VERSION")
    if not os.path.isfile(vf):
        raise SystemExit("找不到 %s" % vf)
    return open(vf, encoding="utf-8").read().strip()


def main():
    ver = ""
    if "--ver" in sys.argv:
        i = sys.argv.index("--ver")
        if i + 1 < len(sys.argv):
            ver = sys.argv[i + 1]
    if not ver:
        ver = read_version()
    if not re.match(r"^\d+\.\d+\.\d+$", ver):
        raise SystemExit("版本号不像 x.y.z：%r" % ver)

    pkg = os.path.join(DIST, "e5-mms-patch-%s.zip" % ver)
    if not os.path.isfile(pkg):
        raise SystemExit("缺 %s\n先跑 e5-mms-patch/package.sh 打这个版本的包。" % pkg)

    for f in ("e5-deploy.py", "e5-deploy.bat"):
        p = os.path.join(HERE, f)
        if not os.path.isfile(p):
            raise SystemExit("缺 %s" % p)

    top = "e5-deploy-kit-%s" % ver
    out = os.path.join(OUT_DIR, "%s.zip" % top)
    if os.path.isfile(out):
        os.remove(out)

    tmp = tempfile.mkdtemp(prefix="e5-kit-")
    try:
        stage = os.path.join(tmp, top)
        os.makedirs(stage)
        shutil.copy2(os.path.join(HERE, "e5-deploy.py"), stage)
        shutil.copy2(os.path.join(HERE, "e5-deploy.bat"),
                     os.path.join(stage, "一键部署.bat"))
        shutil.copy2(pkg, stage)
        with open(os.path.join(stage, "使用说明.txt"), "w",
                  encoding="utf-8-sig", newline="\r\n") as f:
            f.write(README.format(ver=ver))

        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
            for name in sorted(os.listdir(stage)):
                z.write(os.path.join(stage, name), "%s/%s" % (top, name))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("已生成 %s" % out)
    print("  %d B" % os.path.getsize(out))
    with zipfile.ZipFile(out) as z:
        for i in z.infolist():
            print("  %-40s %8d B" % (i.filename, i.file_size))
    return 0


if __name__ == "__main__":
    sys.exit(main())
