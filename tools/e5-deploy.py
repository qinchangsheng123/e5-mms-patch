#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""e5-deploy v1.0.1 —— E5 补丁包一键部署

把本地 e5-mms-patch-*.zip / *.tar.gz / 解包目录 传到 E5，解包，跑 install.sh，然后验证。
传文件走 sftp（/tmp 可写；旧记录里 ENOENT 只出现在 /usr、/etc），失败自动退 base64 通道。

用法：
  python e5-deploy.py                     本地一键：自动挑版本号最大的源（含 dist/ 与源码树）
  python e5-deploy.py <包路径|目录>        指定包
  python e5-deploy.py --from-github       从 codeload 取 main 分支最新（设备侧 github.com 不通，本机取）
  python e5-deploy.py --dry-run           只预检，不传不装（设备上跑 install.sh --dry-run）
  python e5-deploy.py --force             设备已是最新也重装
  python e5-deploy.py --allow-downgrade   允许装比设备更旧的版本（默认拒绝）
  python e5-deploy.py --no-backup         跳过部署前备份
  python e5-deploy.py --list              列出可用源

退出码：0 成功 / 1 预检失败 / 2 传输失败 / 3 安装失败 / 4 验证失败

设计约束（来自项目铁律）：
  - 只用 root + 密码登录，深睡时 TCP 会超时，连接带重试
  - 不碰 /mnt/e5-data（f2fs 默认 ro），全部工作目录放 /tmp
  - 装完必须验 md5，不看 install.sh 的退出码就下结论
  - 部署前备份到 /root/e5-backup/pre-deploy-<时间>.tar.gz，失败可整目录回滚
"""

import base64
import hashlib
import json
import os
import re
import socket
import sys
import tarfile
import tempfile
import time
import zipfile
import shutil
import atexit

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HOST = os.environ.get("E5_HOST", "192.168.9.1")
USER = os.environ.get("E5_USER", "root")
PWD = os.environ.get("E5_PASS", "root")
BIND = os.environ.get("E5_BIND", "")
WORK = "/tmp/e5-deploy"
REMOTE_PKG = WORK + "/pkg.tar.gz"
BACKUP_DIR = "/root/e5-backup"
REPO = "qinchangsheng123/e5-mms-patch"
GH_URL = "https://codeload.github.com/%s/tar.gz/refs/heads/main" % REPO

HERE = os.path.dirname(os.path.abspath(__file__))
MARK = {
    "sms_pdu.py": "files/sms_pdu.py",
    "patch-forward.py": "files/patch-forward.py",
    "install.sh": "files/install-core.sh",
}


def say(msg=""):
    print(msg)
    sys.stdout.flush()


def step(n, total, msg):
    say("\n[%d/%d] %s" % (n, total, msg))


# ---------------- SSH ----------------

def connect():
    import paramiko
    errs = []
    for attempt in range(3):
        sock = None
        try:
            if BIND:
                sock = socket.socket()
                sock.settimeout(4)
                sock.bind((BIND, 0))
                sock.connect((HOST, 22))
                sock.settimeout(None)
            else:
                sock = socket.create_connection((HOST, 22), timeout=4)
            c = paramiko.SSHClient()
            c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            c.connect(HOST, 22, username=USER, password=PWD, sock=sock,
                      timeout=10, banner_timeout=15, auth_timeout=15,
                      allow_agent=False, look_for_keys=False)
            return c
        except Exception as e:
            errs.append(repr(e))
            if sock:
                try:
                    sock.close()
                except Exception:
                    pass
            if attempt < 2:
                time.sleep(2)
    raise RuntimeError("连接 %s 失败：%s\n"
                       "深睡中 SSH 会超时，插上 USB 线（物理拔插一次）再试；"
                       "WiFi 通道可设 E5_BIND=192.168.9.100" % (HOST, " | ".join(errs)))


def run(c, cmd, timeout=120):
    _, out, err = c.exec_command(cmd, timeout=timeout)
    o = out.read().decode("utf-8", "replace")
    e = err.read().decode("utf-8", "replace")
    return o, e


def run_ok(c, cmd, timeout=120):
    o, e = run(c, cmd, timeout)
    return o.strip()


# ---------------- 包 ----------------

PKG_FILE_RE = re.compile(r"e5-mms-patch-\d+\.\d+\.\d+\.(zip|tar\.gz)$")
PKG_DIR_RE = re.compile(r"e5-mms-patch-\d+\.\d+\.\d+$")


def ver_key(v):
    """'1.0.11' / 路径 / 任意字符串 → (1, 0, 11)，取不到就是 (0,0,0)。"""
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", str(v))
    return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)


def read_version(path):
    """目录取 VERSION；压缩包取文件名里的版本。"""
    if os.path.isdir(path):
        vf = os.path.join(path, "VERSION")
        if os.path.isfile(vf):
            return open(vf, encoding="utf-8", errors="replace").read().strip()
        return ""
    m = re.search(r"\d+\.\d+\.\d+", os.path.basename(path))
    return m.group(0) if m else ""


def is_patch_dir(p):
    return (os.path.isdir(p) and os.path.isfile(os.path.join(p, "install.sh"))
            and os.path.isdir(os.path.join(p, "files")))


def find_sources():
    """扫所有可能的包源：本目录、本目录/dist、cwd、cwd/dist、本目录下源码树。

    旧版只扫一层，漏掉 dist/ 里的新版，会把设备从 1.0.11 降回 1.0.10。
    """
    dirs = []
    for d in (HERE, os.path.join(HERE, "dist"),
              os.path.join(HERE, "e5-mms-patch", "dist"),
              os.getcwd(), os.path.join(os.getcwd(), "dist")):
        if os.path.isdir(d) and os.path.realpath(d) not in [os.path.realpath(x) for x in dirs]:
            dirs.append(d)

    out, seen = [], set()

    def add(path, is_dir):
        rp = os.path.realpath(path)
        if rp in seen:
            return
        v = read_version(path)
        if not v:
            return
        seen.add(rp)
        out.append({"ver": v, "path": path, "dir": is_dir})

    for d in dirs:
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for f in names:
            p = os.path.join(d, f)
            if os.path.isfile(p) and PKG_FILE_RE.match(f):
                add(p, False)
            elif os.path.isdir(p) and PKG_DIR_RE.match(f) and is_patch_dir(p):
                add(p, True)

    # 仓库工作树本身就是最新源（VERSION 里写着版本号，目录名没有）
    for tree in (os.path.join(HERE, "e5-mms-patch"), os.path.join(os.path.dirname(HERE), "e5-mms-patch")):
        if is_patch_dir(tree):
            add(tree, True)

    # 版本高的优先；同版本优先已打包文件（更接近发布态）
    out.sort(key=lambda x: (ver_key(x["ver"]), 0 if x["dir"] else 1), reverse=True)
    return out


def find_pkg():
    srcs = find_sources()
    return srcs[0]["path"] if srcs else None


def find_top(root):
    """含 install.sh 的顶层目录。"""
    for name in sorted(os.listdir(root)):
        p = os.path.join(root, name)
        if os.path.isdir(p) and os.path.isfile(os.path.join(p, "install.sh")):
            return p
    if os.path.isfile(os.path.join(root, "install.sh")):
        return root
    return None


def unpack_local(src):
    tmp = tempfile.mkdtemp(prefix="e5-pkg-")
    atexit.register(lambda: shutil.rmtree(tmp, ignore_errors=True))
    if os.path.isdir(src):
        return src
    if src.lower().endswith(".zip"):
        with zipfile.ZipFile(src) as z:
            z.extractall(tmp)
    elif src.lower().endswith((".tar.gz", ".tgz")):
        with tarfile.open(src, "r:gz") as t:
            try:
                t.extractall(tmp, filter="data")   # 3.12+；老版本没有这个参数
            except TypeError:
                t.extractall(tmp)
    else:
        raise RuntimeError("不认的包格式：%s（要 .zip / .tar.gz / 目录）" % src)
    top = find_top(tmp)
    if not top:
        raise RuntimeError("包里没有 install.sh，不像补丁包：%s" % src)
    return top


def mark(c, ver):
    """记下本脚本部署过的版本，供下次判断是否需要重装。"""
    if ver:
        run_ok(c, "printf '%s\\n' '%s' > /etc/e5/mms/.deployed-version" % (ver, ver))


SKIP_SEGS = {".git", "dist", "tools", "node_modules", "__pycache__", ".github"}


def pack(root):
    """压成设备可直接解包的 tar.gz。

    源码树里有 .git 和 dist（几百 KB 到几十 MB），设备上不需要，必须排掉，
    否则上传时间和 /tmp 占用都会白涨。
    """
    base = os.path.basename(root)
    out = os.path.join(tempfile.gettempdir(), "e5-deploy-%d.tar.gz" % int(time.time()))

    def flt(ti):
        segs = ti.name.replace("\\", "/").split("/")[1:-1]
        if any(s in SKIP_SEGS for s in segs):
            return None
        if ti.isfile() and ti.name.endswith((".zip", ".tar.gz", ".sha256", ".pyc")):
            return None
        return ti

    with tarfile.open(out, "w:gz") as t:
        t.add(root, arcname=base, filter=flt)
    atexit.register(lambda: os.path.exists(out) and os.remove(out))
    return out


def download_github():
    import urllib.request
    out = os.path.join(tempfile.gettempdir(), "e5-mms-patch-main.tar.gz")
    req = urllib.request.Request(GH_URL, headers={"User-Agent": "e5-deploy/1.0.1"})
    say("  下载 %s" % GH_URL)
    with urllib.request.urlopen(req, timeout=60) as r, open(out, "wb") as f:
        f.write(r.read())
    say("  已存 %s（%d B）" % (out, os.path.getsize(out)))
    return out


# ---------------- 传输 ----------------

def upload(c, local, remote):
    """先走 sftp（/tmp 可写；旧记录里 ENOENT 只出现在 /usr、/etc），失败再退 stdin 灌 base64。
    设备没有 base64 命令，解码用 openssl。"""
    data = open(local, "rb").read()
    md5 = hashlib.md5(data).hexdigest()
    run_ok(c, "mkdir -p %s && rm -f %s" % (os.path.dirname(remote), remote))
    mode = ""
    try:
        s = c.open_sftp()
        with s.open(remote, "wb") as f:
            f.write(data)
        s.close()
        mode = "sftp"
    except Exception:
        b64 = base64.b64encode(data)
        stdin, stdout, stderr = c.exec_command("cat > %s.b64" % remote, timeout=180)
        stdin.write(b64)
        stdin.flush()
        stdin.channel.shutdown_write()
        if stdout.channel.recv_exit_status() != 0:
            raise RuntimeError("base64 写入失败：%s" % stderr.read().decode("utf-8", "replace")[:200])
        run(c, "openssl base64 -d -in %s.b64 -out %s && rm -f %s.b64" % (remote, remote, remote), timeout=90)
        mode = "base64"
    got = run_ok(c, "md5sum %s 2>/dev/null | awk '{print $1}'" % remote)
    if got != md5:
        raise RuntimeError("md5 不符：本机 %s / 设备 %s（%s）" % (md5, got or "空", mode))
    return md5, len(data), mode


# ---------------- 主流程 ----------------

def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = set(a for a in sys.argv[1:] if a.startswith("--"))
    dry = "--dry-run" in flags
    do_backup = "--no-backup" not in flags
    from_gh = "--from-github" in flags

    if "--help" in flags or "-h" in flags:
        say(__doc__)
        return 0

    if "--list" in flags:
        srcs = find_sources()
        if not srcs:
            say("没找到任何补丁源（*.zip / *.tar.gz / 源码树）")
            return 1
        say("共 %d 个源（第一行是默认选择）：\n" % len(srcs))
        for s in srcs:
            if s["dir"]:
                size = sum(os.path.getsize(os.path.join(dp, f))
                           for dp, _, fs in os.walk(s["path"]) for f in fs)
                kind = "目录"
            else:
                size = os.path.getsize(s["path"])
                kind = "压缩包"
            say("  %-7s %s  %8d B  %s" % (s["ver"], kind, size, s["path"]))
        return 0

    t0 = time.time()
    total = 6

    # 0 —— 定位包
    step(0, total, "定位补丁包")
    if from_gh:
        src_pkg = download_github()
    elif args:
        src_pkg = args[0]
    else:
        srcs = find_sources()
        if not srcs:
            say("错误：本地没找到补丁源。放一个 e5-mms-patch-x.y.z.zip 到 %s（或其 dist/），" % HERE)
            say("      或用源码树 %s\\e5-mms-patch\\，或加 --from-github。" % HERE)
            return 1
        src_pkg = srcs[0]["path"]
        say("  本地共 %d 个源，选版本最高的：" % len(srcs))
        for s in srcs[:3]:
            say("    %-6s %s%s" % (s["ver"], s["path"], "  ←" if s["path"] == src_pkg else ""))
    if not os.path.exists(src_pkg):
        say("错误：包不存在 %s" % src_pkg)
        return 1
    root = unpack_local(src_pkg)
    ver = ""
    vf = os.path.join(root, "VERSION")
    if os.path.isfile(vf):
        ver = open(vf, encoding="utf-8", errors="replace").read().strip()
    files = os.path.join(root, "files")
    if not os.path.isdir(files):
        say("错误：包里缺 files/ 目录：%s" % root)
        return 1
    say("  包：%s" % src_pkg)
    say("  版本：%s" % (ver or "未知"))
    say("  顶层：%s（%d 项）" % (os.path.basename(root), len(os.listdir(files))))

    tarball = pack(root)
    say("  本地打包：%s（%d B）" % (os.path.basename(tarball), os.path.getsize(tarball)))

    # 1 —— 连接
    step(1, total, "连接设备 %s" % HOST)
    try:
        c = connect()
    except RuntimeError as e:
        say(str(e))
        return 1
    up = run_ok(c, "uptime")
    say("  %s" % up)

    # 2 —— 预检
    step(2, total, "预检")
    bad = []
    form = run_ok(c, "cat /etc/e5/image-form 2>/dev/null")
    img = run_ok(c, "cat /etc/e5/image-version 2>/dev/null")
    kern = run_ok(c, "uname -r")
    say("  固件：%s / %s / %s" % (form or "?", img or "?", kern))
    if not run_ok(c, "test -f /usr/lib/e5/sms_pdu.py && echo yes"):
        bad.append("缺 /usr/lib/e5/sms_pdu.py，不像 e5-sleep 固件")
    if not run_ok(c, "test -f /usr/libexec/unisoc-modem/sms-forward && echo yes"):
        bad.append("缺 sms-forward，转发补丁无处可打")
    free = run_ok(c, "df -k /tmp | awk 'NR==2{print $4}'")
    try:
        free_kb = int(free or 0)
    except ValueError:
        free_kb = 0
    need_kb = os.path.getsize(tarball) // 1024 * 8 + 10240
    say("  /tmp 可用 %d KB（需要约 %d KB）" % (free_kb, need_kb))
    if free_kb < need_kb:
        bad.append("/tmp 空间不足")
    cur_q = run_ok(c, "e5-forward queue 2>/dev/null | head -c 200")
    say("  当前队列：%s" % (cur_q.replace("\n", " ")[:80] or "?"))
    q = 0
    try:
        q = json.loads(cur_q).get("pending", 0)
    except Exception:
        pass
    if q:
        say("  注意：队列里有 %d 条待发，部署会重启转发服务" % q)
    # 与设备现状比对，避免重复安装（尤其别把老包刷回新设备）
    # 优先看本脚本上次部署写下的版本标记；没有标记文件的旧部署退回 md5 比对
    deployed = run_ok(c, "cat /etc/e5/mms/.deployed-version 2>/dev/null")
    want = hashlib.md5(open(os.path.join(files, "sms_pdu.py"), "rb").read()).hexdigest()
    have = run_ok(c, "md5sum /etc/e5/mms/sms_pdu.py 2>/dev/null | awk '{print $1}'")

    # 降级保护：本地包比设备旧就停手（旧版会把 1.0.11 刷回 1.0.10 而毫无察觉）
    if deployed and ver and ver_key(ver) < ver_key(deployed) and "--allow-downgrade" not in flags:
        say("  拒绝降级：设备已是 %s，本地这份是 %s。" % (deployed, ver))
        say("  确认要降级就加 --allow-downgrade；想装新版放个更高的包或用 --from-github。")
        c.close()
        return 1

    if "--force" not in flags:
        if deployed and ver and deployed == ver:
            say("  设备已部署 %s（版本标记一致），跳过安装。要重装加 --force" % ver)
            c.close()
            return 0
        if not deployed and have and have == want:
            mark(c, ver)
            say("  设备已是最新（sms_pdu.py md5 一致），跳过安装。要重装加 --force")
            c.close()
            return 0
    if deployed:
        say("  设备当前版本：%s → 本次 %s" % (deployed, ver or "?"))
    if have:
        say("  将覆盖 sms_pdu.py：%s → %s" % (have[:8], want[:8]))

    if bad:
        say("\n预检失败：")
        for b in bad:
            say("  - %s" % b)
        return 1

    if dry:
        step(3, total, "传送 + 试运行（--dry-run，不落盘）")
        try:
            md5, size, n = upload(c, tarball, REMOTE_PKG)
        except RuntimeError as e:
            say("  传输失败：%s" % e)
            return 2
        say("  上传完成：%d B（%s），md5 %s" % (size, n, md5))
        out = run_ok(c, "rm -rf %s/pkg && mkdir -p %s/pkg && tar -xzf %s -C %s/pkg && "
                        "D=$(ls -d %s/pkg/*/ | head -1) && cd \"$D\" && sh install.sh --dry-run 2>&1"
                     % (WORK, WORK, REMOTE_PKG, WORK, WORK), timeout=120)
        say(out)
        run_ok(c, "rm -rf %s" % WORK)
        say("\n试运行结束，未改动设备。")
        return 0

    # 3 —— 备份
    step(3, total, "备份现有部署")
    bpath = ""
    if do_backup:
        ts = time.strftime("%Y%m%d-%H%M%S")
        bpath = "%s/pre-deploy-%s.tar.gz" % (BACKUP_DIR, ts)
        run_ok(c, "mkdir -p %s" % BACKUP_DIR)
        o, e = run(c, "cd / && tar -czf %s "
                      "etc/e5/mms etc/e5/power etc/e5/usb etc/e5/sms-sync etc/e5/modem-guard "
                      "usr/libexec/unisoc-modem/sms-forward etc/init.d/e5-mms 2>/dev/null; "
                      "ls -l %s | awk '{print $5}'" % (bpath, bpath), timeout=120)
        sz = o.strip()
        if not sz.isdigit() or int(sz) < 1024:
            say("  备份异常（%s），中止。可用 --no-backup 跳过" % (sz or e.strip()[:100]))
            return 3
        say("  已备份：%s（%d B）" % (bpath, int(sz)))
        say("  回滚命令：tar -xzf %s -C /" % bpath)
    else:
        say("  已跳过（--no-backup）")

    # 4 —— 上传
    step(4, total, "上传并校验")
    try:
        md5, size, n = upload(c, tarball, REMOTE_PKG)
    except RuntimeError as e:
        say("  传输失败：%s" % e)
        return 2
    say("  %d B（%s）/ md5 %s" % (size, n, md5))

    # 5 —— 安装
    step(5, total, "解包并执行 install.sh")
    o, e = run(c, "rm -rf %s/pkg && mkdir -p %s/pkg && tar -xzf %s -C %s/pkg && ls -d %s/pkg/*/"
               % (WORK, WORK, REMOTE_PKG, WORK, WORK), timeout=60)
    top = o.strip().splitlines()[0].strip() if o.strip() else ""
    if not top:
        say("  解包失败：%s %s" % (o.strip()[:200], e.strip()[:200]))
        return 3
    say("  解到 %s" % top)
    t1 = time.time()
    o, e = run(c, "sh %sinstall.sh 2>&1; echo \"__RC__$?\"" % top, timeout=600)
    rc = "?"
    if "__RC__" in o:
        body, _, tail = o.rpartition("__RC__")
        rc = tail.strip()
    else:
        body = o
    for line in body.strip().splitlines()[-25:]:
        say("  | %s" % line)
    if e.strip():
        say("  ! %s" % e.strip()[:300])
    say("  耗时 %.1f s，install.sh 退出码 %s" % (time.time() - t1, rc))
    if rc not in ("0",):
        say("\n安装脚本非零退出，先看上面的输出；需要回滚就用 [3/6] 给的命令。")
        return 3

    # 6 —— 验证
    step(6, total, "验证")
    fails = []
    for dst, rel in MARK.items():
        rpath = "/etc/e5/mms/" + dst
        want = hashlib.md5(open(os.path.join(root, rel), "rb").read()).hexdigest()
        got = run_ok(c, "md5sum %s 2>/dev/null | awk '{print $1}'" % rpath)
        ok = (got == want)
        say("  %-16s %s  %s" % (dst, "OK" if ok else "不符", got or "缺失"))
        if not ok:
            fails.append(dst)

    svc = run_ok(c, "ps w | grep '[e]5-sms-receive serve' | head -1")
    say("  e5-sms-receive   %s" % ("运行中" if svc else "未运行"))
    if not svc:
        fails.append("e5-sms-receive")

    dom = run_ok(c, "ps w | grep '[d]ata-ondemand.sh run' | head -1")
    say("  data-ondemand    %s" % ("运行中" if dom else "未运行"))
    if not dom:
        fails.append("data-ondemand")

    st = run_ok(c, "e5-forward status 2>/dev/null")
    try:
        js = json.loads(st)
        meth = js.get("status", {}).get("method", "?")
        enabled = js.get("status", {}).get("enabled")
        say("  转发配置         method=%s enabled=%s" % (meth, enabled))
        if not enabled:
            fails.append("转发未启用")
    except Exception:
        say("  转发配置         无法解析（%d B）：%s" % (len(st), st[:120]))
        fails.append("e5-forward status")

    qq = run_ok(c, "e5-forward queue 2>/dev/null")
    try:
        pend = json.loads(qq).get("pending", "?")
        say("  队列             pending=%s" % pend)
    except Exception:
        say("  队列             无法解析")

    en = run_ok(c, "ls /etc/rc.d/ | grep -c e5-mms")
    say("  开机自愈 S19     %s" % ("已启用" if en.strip() not in ("", "0") else "未启用"))
    if en.strip() in ("", "0"):
        fails.append("e5-mms 未 enable")

    mark(c, ver)
    run_ok(c, "rm -rf %s" % WORK)
    c.close()

    say("\n" + "=" * 56)
    if fails:
        say("部署完成但验证有 %d 项不通过：%s" % (len(fails), "、".join(fails)))
        say("先看 install.sh 输出；回滚见 [3/6]。")
        return 4
    say("部署成功：%s → %s，用时 %.1f s" % (os.path.basename(src_pkg), HOST, time.time() - t0))
    say("建议抽查：发一条短信，确认 10-20 s 内收到推送。")
    if bpath:
        say("要回滚：tar -xzf %s -C / && reboot" % bpath)
        say("（install.sh 尾部提示的 /tmp/e5-deploy/... 部署完已清理，别照着跑）")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        say("\n已中断")
        sys.exit(130)
