"""
超星学习通 · 未完成作业查询脚本
---------------------------------
登录学习通后，遍历当前账号下所有课程，抓取作业列表并筛选出未完成的作业，
按剩余时间由近到远排序输出，便于优先处理临近截止的任务。

运行结束后，结果会保存到桌面的 unfinished_homework.txt 并自动打开。

账号与密码以登录表单所需的 AES 密文形式保存在 ~/.xuexitong_homeworkcheck/config.json，
后续运行直接复用密文，无需重复输入、重复加密。

依赖：
    pip install requests pycryptodome

说明：
    本脚本仅支持 Windows
"""

import base64
import json
import os
import re
import time
import winreg
from pathlib import Path

import msvcrt
import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad

# ============ 配置区 ============
CONFIG_NAME = "xuexitong_homeworkcheck"
CONFIG_DIR = Path.home() / f".{CONFIG_NAME}"
CONFIG_FILE = CONFIG_DIR / "config.json"

KEY = b"u2oh6Vu^HWe4_AES"  # 登录表单 AES-CBC 加密所用的固定密钥
TIMEOUT = 20  # 单次请求超时时间（秒）
RETRY = 4  # 请求失败最大重试次数


# ============ 账号读写 ============
def aes_enc(text: str) -> str:
    """AES-CBC 加密 + Base64 编码，产出登录表单 uname / password 字段的取值。"""
    cipher = AES.new(KEY, AES.MODE_CBC, KEY)
    return base64.b64encode(cipher.encrypt(pad(text.encode(), 16))).decode()


def aes_dec(blob: str) -> str:
    """aes_enc 的逆运算，仅用于显示已保存的账号。"""
    cipher = AES.new(KEY, AES.MODE_CBC, KEY)
    return unpad(cipher.decrypt(base64.b64decode(blob)), 16).decode()


def masked_account(uname_enc: str) -> str:
    """从密文解出账号并打码显示（138****0000），解不出时返回「已保存的账号」。"""
    try:
        uname = aes_dec(uname_enc)
    except Exception:
        return "已保存的账号"
    return f"{uname[:3]}****{uname[-4:]}" if len(uname) >= 8 else uname


def save_config(uname_enc: str, password_enc: str) -> None:
    """把登录表单所需的密文写入配置文件，明文不落盘。"""
    CONFIG_FILE.write_text(
        json.dumps(
            {"uname": uname_enc, "password": password_enc}, ensure_ascii=False, indent=2
        ),
        encoding="utf-8",
    )


def load_login() -> tuple[str, str]:
    """检测/创建配置文件，返回登录表单所需的 (uname 密文, password 密文)。"""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if cfg.get("phone") and cfg.get("password"):
                # 旧版本以明文保存，这里自动迁移为密文，之后不再落明文
                uname_enc, password_enc = aes_enc(cfg["phone"]), aes_enc(cfg["password"])
                save_config(uname_enc, password_enc)
            else:
                uname_enc = cfg.get("uname", "")
                password_enc = cfg.get("password", "")
            if uname_enc and password_enc:
                print(f"检测到已保存的学习通账号：{masked_account(uname_enc)}")
                print("接下来将登录该账号，抓取各课程中未完成的作业，")
                print("**如需更换账号，请在 3 秒内按任意键**；否则自动继续……")

                deadline = time.time() + 3
                pressed = False
                while time.time() < deadline:
                    remain = int(deadline - time.time()) + 1
                    print(
                        f"\r  倒计时 {remain} 秒，按任意键修改账号…", end="", flush=True
                    )
                    if msvcrt.kbhit():
                        msvcrt.getch()
                        pressed = True
                        break
                    time.sleep(0.1)
                print()

                if not pressed:
                    return uname_enc, password_enc

                print("请输入新的账号密码，输入完成后进入查询。")
            else:
                print("配置文件内容不完整，请重新输入账号密码。")
        except Exception as e:
            print(f"读取配置失败（{e}），请重新输入账号密码。")
    else:
        print("首次运行，需要先登录学习通账号。")
        print("登录后脚本会查询账号下的未完成作业。")
        print()
    phone = input("手机号: ").strip()
    password = input("密码:   ").strip()

    uname_enc, password_enc = aes_enc(phone), aes_enc(password)
    save_config(uname_enc, password_enc)
    return uname_enc, password_enc


# ============ 作业抓取 ============
def fetch_unfinished_homework_text(uname_enc: str, password_enc: str) -> str:
    """用已保存的登录密文抓取并返回未完成作业的汇总文本。"""

    s = requests.Session()
    s.headers["User-Agent"] = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/118 Safari/537.36"
    )

    def login() -> None:
        """用已保存的登录密文登录，登录态保留在 session 中。"""
        # 先访问登录页建立会话，拿到必要 Cookie
        s.get("https://passport2.chaoxing.com/login", timeout=TIMEOUT)

        s.post(
            "https://passport2.chaoxing.com/fanyalogin",
            timeout=TIMEOUT,
            data={
                "fid": "-1",
                "uname": uname_enc,
                "password": password_enc,
                "refer": "https%3A%2F%2Fi.chaoxing.com",
                "t": "true",
                "forbidotherlogin": "0",
                "validate": "",
                "doubleFactorLogin": "0",
                "independentId": "0",
            },
        )

    def get(url: str, **kw) -> requests.Response:
        """带重试的 GET：网络异常时退避重试；若被踢回登录页则重新登录后重放。"""
        kw.setdefault("timeout", TIMEOUT)
        last_exc = None

        for i in range(RETRY):
            try:
                r = s.get(url, **kw)
                break
            except Exception as e:  # 网络抖动，等待后重试
                last_exc = e
                time.sleep(1.2 * (i + 1))
        else:
            raise last_exc  # 重试耗尽，抛出最后一次异常

        # 登录态失效：重新登录并重放一次请求
        if "passport2.chaoxing.com/login" in r.url or "用户登录" in r.text:
            login()
            r = s.get(url, **kw)
        return r

    def hidden(page: str, element_id: str) -> str:
        """提取页面中指定 id 的隐藏 input 的 value（兼容属性顺序颠倒），找不到返回空串。"""
        m = re.search(rf'id="{element_id}"[^>]*value="([^"]*)"', page) or re.search(
            rf'value="([^"]*)"[^>]*id="{element_id}"', page
        )
        return m.group(1) if m else ""

    def parse_hw_list(html: str) -> list[tuple[str, str, str | None]]:
        """解析作业列表页，返回 [(标题, 状态, 剩余时间原文), ...]。"""
        out = []
        for m in re.finditer(
            r'<li[^>]*data="[^"]+"[^>]*aria-label="([^"]*)"[^>]*>([\s\S]*?)</li>',
            html,
        ):
            label, body = m.groups()
            # aria-label 形如 "作业标题;状态"，用分号切分
            title, _, status = label.partition(";")
            rm = re.search(r"(剩余\d+小时\d+分钟)", body)
            out.append((title.strip(), status.strip(), rm.group(1) if rm else None))
        return out

    def to_minutes(remaining: str | None) -> int | None:
        """把"剩余X小时Y分钟"换算为总分钟数；无截止时间返回 None。"""
        if not remaining:
            return None
        m = re.match(r"剩余(\d+)小时(\d+)分钟", remaining)
        return int(m.group(1)) * 60 + int(m.group(2)) if m else None

    def fmt_remaining(remaining: str | None) -> str:
        """格式化为：剩余X小时Y分钟（Z天A小时B分钟后）。"""
        mins = to_minutes(remaining)
        if mins is None:
            return "无截止时间"

        days, rem = divmod(mins, 24 * 60)
        hours, minutes = divmod(rem, 60)

        if days > 0:
            suffix = f"{days}天{hours}小时{minutes}分钟后"
        elif hours > 0:
            suffix = f"{hours}小时{minutes}分钟后"
        else:
            suffix = f"{minutes}分钟后"
        return f"{remaining}（{suffix}）"

    # ---------- 主流程 ----------
    login()

    # 从个人主页提取当前账号的机构 fid，失败时回落到默认值
    try:
        base_html = get("https://i.chaoxing.com/base").text
        m = re.search(r'"fid"\s*:\s*"?(\d+)"?', base_html)
        fid = m.group(1) if m else "427"
    except Exception:
        fid = "427"

    # 拉取课程列表页 HTML
    course_list_html = get(
        "https://mooc2-ans.chaoxing.com/mooc2-ans/visit/courselistdata",
        params={
            "courseType": 1,
            "courseFid": fid,
            "courseTeacherid": 0,
            "courseOrderBy": 0,
        },
    ).text

    results: list[list[str | None]] = []  # [(课程名, 剩余时间原文, 作业标题), ...]
    seen: set[tuple[str, str, str]] = set()  # 去重，避免同一课程被重复抓取

    for m in re.finditer(
        r"stucoursemiddle\?courseid=(\d+)&clazzid=(\d+)&cpi=(\d+)", course_list_html
    ):
        cid, clz, cpi = m.groups()
        if (cid, clz, cpi) in seen:
            continue
        seen.add((cid, clz, cpi))

        # 课程名在链接附近的 course-name 属性里
        name_m = re.search(
            r'course-name[^>]*title="([^"]+)"',
            course_list_html[m.end() : m.end() + 1600],
        )
        name = name_m.group(1) if name_m else "?"

        # 进入课程主页，取出访问作业列表所需的隐藏参数
        page = get(
            "https://mooc1.chaoxing.com/visit/stucoursemiddle",
            params={
                "courseid": cid,
                "clazzid": clz,
                "cpi": cpi,
                "ismooc2": 1,
                "v": 2,
            },
        ).text

        if not (work_enc := hidden(page, "workEnc")):
            continue  # 该课程没有作业模块，跳过

        hw_html = get(
            "https://mooc1.chaoxing.com/mooc2/work/list",
            params={
                "courseId": cid,
                "classId": clz,
                "cpi": cpi,
                "ut": "s",
                "openc": hidden(page, "openc"),
                "enc": work_enc,
                "t": int(time.time() * 1000),
                "stuenc": hidden(page, "enc"),
                "isdisplaytable": 2,
            },
        ).text

        for title, status, remaining in parse_hw_list(hw_html):
            if status == "未交":
                results.append([name, remaining, title])
            elif status == "待互评":
                results.append([name, remaining, title + "（待互评）"])

    if not results:
        return "暂无未完成作业。"

    # 按剩余时间升序排序，无截止时间的排到最后
    results.sort(key=lambda x: (to_minutes(x[1]) is None, to_minutes(x[1]) or 0))

    lines = [
        "=" * 30,
        f"未完成作业（共 {len(results)} 条）",
        "=" * 30,
        "",
    ]

    # 最临近提交的一条作业
    top_course, top_remaining, top_title = results[0]
    if top_remaining is None:
        lines.append("最临近提交：暂无（均无截止时间）")
    else:
        lines.append(f"最临近提交：【{top_course}】{top_title}")
        lines.append(f"           剩余：{fmt_remaining(top_remaining)}")
    lines.append("")

    # 按课程分组（分组顺序 = 各课程最早截止时间的顺序）
    grouped: dict[str, list[tuple[str, str | None]]] = {}
    for course, remaining, title in results:
        grouped.setdefault(course, []).append((title, remaining))

    for course, items in grouped.items():
        lines.append(f"【课程】{course}（{len(items)} 项作业）")
        for title, remaining in items:
            lines.append(f"  作业：{title}")
            lines.append(f"  剩余：{fmt_remaining(remaining)}")
        lines.append("")

    return "\n".join(lines).rstrip().replace("&nbsp;", "")


# ============ 结果输出 ============
def get_desktop_path() -> Path:
    """通过注册表获取当前用户真实的桌面路径，兼容 OneDrive 重定向与中文系统。"""
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
        )
        value, _ = winreg.QueryValueEx(key, "Desktop")
        winreg.CloseKey(key)
        return Path(os.path.expandvars(value))  # 展开 %USERPROFILE% 等环境变量
    except OSError:
        return Path.home() / "Desktop"


def save_and_open(text: str) -> None:
    """把结果写入桌面文件并自动打开。"""
    path = get_desktop_path() / "unfinished_homework.txt"
    path.write_text(text, encoding="utf-8")
    print("查询完成")
    os.startfile(path)


# ============ 入口 ============
def main() -> None:
    print("=" * 46)
    print(" 超星学习通 · 未完成作业查询")
    print("=" * 46)

    uname_enc, password_enc = load_login()
    text = fetch_unfinished_homework_text(uname_enc, password_enc)
    save_and_open(text)


if __name__ == "__main__":
    main()
