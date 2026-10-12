"""
超星学习通 · 未完成作业查询脚本
---------------------------------
登录学习通后，遍历当前账号下所有课程，抓取作业列表并筛选出未完成的作业，
按剩余时间由近到远排序输出，便于优先处理临近截止的任务。

依赖：
    pip install requests pycryptodome
"""

import base64
import re
import time

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad


# ============ 配置区 ============
PHONE = "xxx"            # 学习通登录手机号
PASSWORD = "xxx"         # 学习通登录密码

KEY = b"u2oh6Vu^HWe4_AES"   # 登录表单 AES-CBC 加密所用的固定密钥
TIMEOUT = 20                # 单次请求超时时间（秒）
RETRY = 4                   # 请求失败最大重试次数

# 这些状态视为"已完成"，不再计入未完成列表
DONE_STATUS = ("已完成", "已提交", "待批阅", "已批阅", "已批改", "已互评")


def fetch_unfinished_homework_text() -> str:
    """抓取并返回未完成作业的汇总文本（可直接打印或发送）。"""

    s = requests.Session()
    s.headers["User-Agent"] = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/118 Safari/537.36"
    )

    def login():
        """使用 AES 加密的手机号/密码登录，登录态保留在 session 中。"""
        # 先访问登录页建立会话，拿到必要 Cookie
        s.get("https://passport2.chaoxing.com/login", timeout=TIMEOUT)

        def enc(text: str) -> str:
            """AES-CBC 加密 + Base64 编码，用于登录表单的 uname / password 字段。"""
            cipher = AES.new(KEY, AES.MODE_CBC, KEY)
            return base64.b64encode(cipher.encrypt(pad(text.encode(), 16))).decode()

        s.post(
            "https://passport2.chaoxing.com/fanyalogin",
            timeout=TIMEOUT,
            data={
                "fid": "-1",
                "uname": enc(PHONE),
                "password": enc(PASSWORD),
                "refer": "https%3A%2F%2Fi.chaoxing.com",
                "t": "true",
                "forbidotherlogin": "0",
                "validate": "",
                "doubleFactorLogin": "0",
                "independentId": "0",
            },
        )

    def get(url: str, **kw) -> requests.Response:
        """带重试的 GET：网络异常时退避重试；若被踢回登录页则重新登录后重放，学习通第一次登陆时常拒绝。"""
        kw.setdefault("timeout", TIMEOUT)
        last_exc = None

        for i in range(RETRY):
            try:
                r = s.get(url, **kw)
                break
            except Exception as e:          # 网络抖动，等待后重试
                last_exc = e
                time.sleep(1.2 * (i + 1))
        else:
            raise last_exc                  # 重试耗尽，抛出最后一次异常

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
            out.append(
                (
                    title.strip(),
                    status.strip(),
                    rm.group(1) if rm else None,
                )
            )
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
    fid = (
        re.search(r'"fid"\s*:\s*"?(\d+)"?', get("https://i.chaoxing.com/base").text)
        or [None, "427"]
    )[1]

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

    results: list[list[str | None]] = []    # [(课程名, 剩余时间原文, 作业标题), ...]
    seen: set[tuple[str, str, str]] = set() # 去重，避免同一课程被重复抓取

    for m in re.finditer(
        r"stucoursemiddle\?courseid=(\d+)&clazzid=(\d+)&cpi=(\d+)", course_list_html
    ):
        cid, clz, cpi = m.groups()
        if (cid, clz, cpi) in seen:
            continue
        seen.add((cid, clz, cpi))

        # 课程名在链接附近的 course-name 属性里
        name = (
            re.search(
                r'course-name[^>]*title="([^"]+)"',
                course_list_html[m.end(): m.end() + 1600],
            )
            or [None, "?"]
        )[1]

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
            continue    # 该课程没有作业模块，跳过

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
            if any(k in status for k in DONE_STATUS):
                continue
            results.append([name, remaining, title])

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

# 打印结果
print(fetch_unfinished_homework_text())