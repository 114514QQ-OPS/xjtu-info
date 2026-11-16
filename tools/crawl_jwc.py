# -*- coding: utf-8 -*-
"""
crawl_jwc.py —— 抓西安交通大学教务处的"教学通知"

它干四件事：
    1. 打开列表页，把每个通知的链接和发布日期抓下来
    2. 逐条打开详情页，取正文（用来判断有没有截止日期、给谁看的）
    3. 从正文里找截止日期和年级
    4. 存成 raw.json

跑法：
    python crawl_jwc.py            # 默认抓列表第一页 + 前 12 条详情
    python crawl_jwc.py --limit 5  # 少抓几条试试

只用标准库 urllib，不需要 pip install 任何东西。
"""
import argparse
import html as html_lib
import json
import re
import ssl
import sys
import time
import urllib.request
from datetime import date
from pathlib import Path
from urllib.parse import urljoin

BASE = "https://jwc.xjtu.edu.cn/"
LIST_URL = "https://jwc.xjtu.edu.cn/jxxx/jxtz2.htm"

# 有些学校的证书链不全，关掉校验才能连上（只影响本校站点，可以接受）
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE


def fetch(url: str) -> str:
    """打开一个网页，返回它的 HTML 文本。"""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    with urllib.request.urlopen(req, timeout=25, context=CTX) as resp:
        raw = resp.read()
    # 学校站点编码不统一，挨个试
    for enc in ("utf-8-sig", "utf-8", "gbk", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "ignore")


def fetch_json(url: str) -> str:
    """给华为招聘接口用：它挑 UA，而且必须带 Referer + X-Requested-With。

    用普通 fetch() 直接访问会 403 —— 这个坑值得记一笔。
    """
    req = urllib.request.Request(url, headers={
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"),
        "Referer": "https://career.huawei.com/reccampportal/portal5/campus-recruitment.html",
        "Accept": "application/json, text/plain, */*",
        "X-Requested-With": "XMLHttpRequest",
    })
    with urllib.request.urlopen(req, timeout=25, context=CTX) as resp:
        return resp.read().decode("utf-8", "ignore")


def to_text(h: str) -> str:
    """把 HTML 变成纯文字：去掉脚本、样式、标签，合并空白。"""
    h = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", h)
    h = re.sub(r"(?s)<[^>]+>", " ", h)
    h = html_lib.unescape(h)
    return re.sub(r"\s+", " ", h).strip()


# -------- 列表页 --------

def parse_list(page_url: str) -> list:
    """从列表页里抠出 (分类, 标题, 链接, 发布日期)。"""
    h = fetch(page_url)
    out = []
    for block in re.findall(r"(?is)<li[^>]*>(.*?)</li>", h):
        if "info/" not in block:
            continue
        m = re.search(r'href="([^"]+)"', block)
        if not m:
            continue
        href = m.group(1)
        text = to_text(block)
        # 列表里长这样：[考试安排] 关于xxx的通知 2026-11-11
        mm = re.match(r"\[(.+?)\]\s*(.*?)\s*(\d{4}-\d{2}-\d{2})\s*$", text)
        if not mm:
            continue
        cat, title, pub = mm.group(1), mm.group(2), mm.group(3)
        out.append({
            "category_raw": cat,
            "title": title.rstrip("…").rstrip(".").strip(),
            "url": urljoin(page_url, href),
            "published_at": pub,
        })
    # 同一链接在页面上可能出现两次（图片版+文字版），去重
    seen, uniq = set(), []
    for it in out:
        if it["url"] in seen:
            continue
        seen.add(it["url"])
        uniq.append(it)
    return uniq


def guess_cat_xsc(title: str) -> str:
    """学生处的列表没有栏目名，只能从标题里猜分类。"""
    if any(k in title for k in ("奖学金", "助学金", "助学贷款", "资助", "勤工助学", "困难补助", "补助")):
        return "奖助"
    if any(k in title for k in ("招聘", "就业", "见习", "实习", "双选会", "宣讲")):
        return "就业"
    if any(k in title for k in ("竞赛", "大赛", "比赛", "挑战杯")):
        return "竞赛"
    if any(k in title for k in ("讲座", "报告", "沙龙", "培训")):
        return "讲座"
    return "其他"


def parse_list_xsc(page_url: str) -> list:
    """学生处列表页。<li> 里长这样：

        04 2026/11 关于xxx的公示[零宽空格]各学院、书院：根据...

    日期是「日 年/月」，跟教务处的「年-月-日」正好反过来；
    标题和正文摘要之间夹着一个零宽空格（\\u200b），拿它当分隔符。
    """
    h = fetch(page_url)
    out = []
    for block in re.findall(r"(?is)<li[^>]*>(.*?)</li>", h):
        if "info/" not in block:
            continue
        m = re.search(r'href="([^"]+)"', block)
        if not m:
            continue
        text = to_text(block)
        mm = re.match(r"(\d{1,2})\s+(\d{4})/(\d{1,2})\s*(.+)$", text)
        if not mm:
            continue
        day, year, mon, rest = mm.group(1), mm.group(2), mm.group(3), mm.group(4)
        # 列表里标题后面直接粘着正文摘要，中间有时是零宽空格、有时是普通空格，
        # 甚至什么都没有。所以不靠分隔符，靠"标题一定以这些词收尾"来切。
        m2 = re.match(r"(.{6,90}?(?:通知|公告|公示|决定|名单|安排|方案|办法|意见|报告|说明|证明|函))"
                      r"(?=[\s\u200b\u3000]|$)", rest)
        title = m2.group(1) if m2 else rest.split("\u200b")[0]
        title = title.strip()
        if not title:
            continue
        out.append({
            "category_raw": guess_cat_xsc(title),
            "title": title.rstrip("…").strip(),
            "url": urljoin(page_url, m.group(1)),
            "published_at": "%s-%02d-%02d" % (year, int(mon), int(day)),
        })
    seen, uniq = set(), []
    for it in out:
        if it["url"] in seen:
            continue
        seen.add(it["url"])
        uniq.append(it)
    return uniq


def parse_list_eieug(page_url: str) -> list:
    """电信学部本科教学网。这一站的列表页有个坑：

        <a href="info/1030/3333.htm" title="标题">
            <span class="date fr">06-12</span> 标题</a>

    日期只有「月-日」，**没有年份**。所以这里先占个位，等进详情页
    读到「发布时间：2025-06-12」再补上真正的年份。
    """
    h = fetch(page_url)
    out = []
    for block in re.findall(r"(?is)<li[^>]*>(.*?)</li>", h):
        if "info/" not in block:
            continue
        m = re.search(r'<a[^>]+href="([^"]+)"[^>]*title="([^"]+)"', block)
        if not m:
            continue
        href, title = m.group(1), html_lib.unescape(m.group(2)).strip()
        out.append({
            "category_raw": guess_cat_xsc(title),
            "title": title.rstrip("…").strip(),
            "url": urljoin(page_url, href),
            "published_at": None,        # 详情页补
        })
    seen, uniq = set(), []
    for it in out:
        if it["url"] in seen:
            continue
        seen.add(it["url"])
        uniq.append(it)
    return uniq


# ============================================================================
# 书院：每个书院的网站是同一套建站系统，长得几乎一样，所以写一个通用解析器
#   彭康  <span class="time">2026-10-16</span> <a href="info/1070/2645.htm">标题</a>
#   启德  <span class="time fr">2024-09-19</span> <a href="../info/1016/2931.htm" title="标题">标题</a>
#   宗濂  <a class="clearfix" href="info/1036/6731.htm" title="标题"><p>标题…</p>
#           <span class="news_time fr">2026-10-16</span></a>
# 三种写法的共同点：都在 <li> 里、都有 info/ 链接、都带一个 2026-10-16 格式的日期。
# 所以不针对每一种写一遍，而是"每个 li 里找链接 + 找最后一个日期"。
# ============================================================================
_LI = re.compile(r"(?is)<li[^>]*>(.*?)</li>")
_A_ANY = re.compile(r'(?is)<a[^>]*href="([^"]*info/[^"]+)"([^>]*)>(.*?)</a>')
_A_TITLE = re.compile(r'(?is)title="([^"]+)"')
_DATE = re.compile(r"(20\d{2})-(\d{1,2})-(\d{1,2})")


def parse_list_shuyuan(page_url: str, name: str) -> list:
    """书院通知列表。name 是书院名，会直接拿去当 category。"""
    h = fetch(page_url)
    out = []
    for block in _LI.findall(h):
        m = _A_ANY.search(block)
        if not m:
            continue
        href, attrs, inner = m.group(1), m.group(2), m.group(3)
        if "weixin" in href or "javascript" in href:
            continue

        # 标题：优先用 title 属性（最完整）；没有就从 <a> 的内容里取纯文字。
        # 注意只能取 <a> 里面的，不能取整个 <li>——彭康的日期写在 <a> 外面，
        # 取整块会把 "2026-10-16" 粘到标题前面。
        t = _A_TITLE.search(attrs)
        if t:
            title = html_lib.unescape(t.group(1))
        else:
            title = html_lib.unescape(re.sub(r"<[^>]+>", " ", inner))
        title = re.sub(r"\s+", " ", title).strip().rstrip("…")

        # 日期：一个 li 里可能有多个日期（正文里提到别的日期），取最后一个
        ds = _DATE.findall(block)
        if not ds or len(title) < 6:
            continue
        y, mo, d = ds[-1]
        pub = "%s-%02d-%02d" % (y, int(mo), int(d))

        out.append({
            "category_raw": name,
            "topic": guess_cat_xsc(title),
            "title": title,
            "url": urljoin(page_url, href),
            "published_at": pub,
        })

    seen, uniq = set(), []
    for it in out:
        if it["url"] in seen:
            continue
        seen.add(it["url"])
        uniq.append(it)
    return uniq


# 书院站点清单。崇实书院的 tzgg.htm 是个导航页、没有列表，暂时接不了。
SHUYUAN_SITES = [
    ("彭康书院", "https://pksy.xjtu.edu.cn/tzgg.htm"),
    ("宗濂书院", "https://zlsy.xjtu.edu.cn/tzgg.htm"),
    ("启德书院", "https://qdsy.xjtu.edu.cn/qdzx/tzgg.htm"),
]


def parse_list_nanyang(page_url: str) -> list:
    """南洋书院（书院的通知对学生更贴身：宿舍、评优、活动、学代会）。

        <a href="../info/1010/3057.htm" title="标题">标题</a>
        <span class="date">2020-03-15</span>

    要注意有的条目直接链到公众号（mp.weixin.qq.com），那种没有日期，跳过。
    """
    h = fetch(page_url)
    out = []
    for m in re.finditer(
            r'(?is)<a[^>]+href="([^"]+)"[^>]*title="([^"]+)"[^>]*>.*?</a>\s*<span class="date">(\d{4}-\d{2}-\d{2})</span>',
            h):
        href, title, pub = m.group(1), html_lib.unescape(m.group(2)).strip(), m.group(3)
        if "weixin" in href:
            continue
        out.append({
            "category_raw": "南洋书院",
            "topic": guess_cat_xsc(title),
            "title": title.rstrip("…").strip(),
            "url": urljoin(page_url, href),
            "published_at": pub,
        })
    seen, uniq = set(), []
    for it in out:
        if it["url"] in seen:
            continue
        seen.add(it["url"])
        uniq.append(it)
    return uniq


# -------- 详情页 --------

DATE_RE = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日|(\d{1,2})\s*月\s*(\d{1,2})\s*日")

# 详情页正文前面都有一段元信息，各站点叫法不一样：
#   教务处 "发布日期: 2026-11-11 浏览次数: 123"
#   学生处 "2026-11-04 点击量: "
# 找到最后一个这样的标记，后面的就是正文。
# 数字只在后面跟空格时才吃掉，否则会把正文开头的"2026年下半年..."当成浏览量。
META_RE = re.compile(
    r"(?:发布日期|发布时间|点击量|点击数|浏览次数|访问量)[:：]?\s*(?:\d{1,6}(?=\s|$))?")


def find_deadline(text: str, today: date, pub: date | None = None):
    """在正文里找截止日期。找不到就返回 None（绝不猜）。

    分两轮找：先只认明确写了"截止"的句子，找不到才退回"XX日前""至X月X日"这种说法。
    这样能避免把"自发布之日起"里的日期当成截止日期。
    """
    # 关键一步：网页里的数字常被标签拆散，变成"202 6 年 1 1 月 30 日"，
    # 先把所有空白去掉，日期才连得起来。
    flat = re.sub(r"\s+", "", text)
    for keywords in (("截止",), ("前", "至")):
        for sent in re.split(r"[。；;！!\n]", flat):
            if not any(k in sent for k in keywords):
                continue
            found = list(DATE_RE.finditer(sent))
            if not found:
                continue
            # 取句子里最后一个日期："11月5日至11月20日"里 20 号才是截止
            m = found[-1]
            d = None
            try:
                if m.group(1):
                    d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                else:
                    mo, dd = int(m.group(4)), int(m.group(5))
                    for y in (today.year - 1, today.year, today.year + 1):
                        try:
                            cand = date(y, mo, dd)
                        except ValueError:
                            continue
                        if d is None or abs((cand - today).days) < abs((d - today).days):
                            d = cand
            except ValueError:
                continue
            # 截止日期不可能早于发布日期，早于的一定是抽错了
            if d and pub and d < pub:
                continue
            # 恰好等于发布日期 → 多半是把"发布日期"当成截止日期了，宁可留空
            if d and pub and d == pub:
                continue
            if d:
                return d
    return None


# 2026级=大一, 2025级=大二, 2024级=大三, 2023级=大四
GRADE_BY_YEAR = {str(date.today().year - i): g for i, g in
                 enumerate(["大一", "大二", "大三", "大四"])}


def find_audience(text: str) -> list:
    """看正文里提到了哪些年级。一个都没提到，就当"不限"。"""
    hits = []
    for token in ("大一", "大二", "大三", "大四"):
        if token in text:
            hits.append(token)
    for year, grade in GRADE_BY_YEAR.items():
        if year + "级" in text and grade not in hits:
            hits.append(grade)
    if not hits or "全体" in text or "所有本科生" in text:
        return ["不限"] if not hits else hits
    return hits


CAT_MAP = {
    "竞赛安排": "竞赛", "奖项申报": "奖助", "考试安排": "教务",
    "培养方案": "教务", "教改项目": "教务", "质量控制": "教务",
    "实践教学": "科研", "学籍管理": "教务", "选课": "教务",
    # 学生处那边是从标题猜出来的，已经是最终分类，直接放行
    "奖助": "奖助", "就业": "就业", "竞赛": "竞赛", "讲座": "讲座", "其他": "其他",
    # 电信学部/书院 也是这样
    "教务": "教务", "科研": "科研", "生活": "生活",
    # 书院：子类直接用书院名，所以分类就是它自己
    "彭康书院": "彭康书院", "宗濂书院": "宗濂书院", "启德书院": "启德书院",
    "崇实书院": "崇实书院", "南洋书院": "南洋书院",
}


def crawl_detail(item: dict, today: date, source_name: str = "教务处") -> dict:
    """打开一条通知的详情页，补上正文、截止日期、受众。"""
    h = fetch(item["url"])
    text = to_text(h)

    # 详情页的 <title> 一般是完整标题（列表里会被截断成"..."）
    m = re.search(r"(?is)<title>(.*?)</title>", h)
    if m:
        t = html_lib.unescape(m.group(1)).strip()
        t = re.sub(r"\s*[-—|]\s*(西安交通大学.*|知心网.*|[\u4e00-\u9fff]{2,8}书院)$", "", t).strip()
        if len(t) > len(item["title"]):
            item["title"] = t

    # 摘要：取正文中"标题之后"的那一段。
    # 详情页最前面是一堆导航（首页/部门简介/...），标题之后才是正文，
    # 所以用标题当锚点把它切掉。
    excerpt = ""
    # 元信息固定长这样："信息来源： 发布日期: 2026-11-11 浏览次数: 123"
    # 拿"发布日期 + 日期"当锚点最稳（有的页面浏览次数后面根本没数字，
    # 用 浏览次数 当锚点会把正文开头的年份一起吃掉）。
    marks = list(re.finditer(META_RE, text))
    if marks:
        tail = text[marks[-1].end():]
        tail = re.sub(r"^[\s:：\-—|、，,。]{0,6}", "", tail)
        # 网页把每个字塞进独立 span，抽出来全是"学 院（部 、 中心 ）"这种空格，去掉
        tail = re.sub(r"(?<=[\u4e00-\u9fff0-9%])[ \u3000]+(?=[\u4e00-\u9fff0-9%])", "", tail)
        excerpt = tail[:110].strip()
    item["summary"] = excerpt or None

    # 有的站（电信学部本科教学网）列表页只写"06-12"不写年份，
    # 真正的发布日期要到详情页的「发布时间：2025-06-12」里拿。
    if not item.get("published_at"):
        m2 = re.search(r"(?:发布时间|发布日期)[:：]?\s*(20\d{2}-\d{2}-\d{2})", text)
        if m2:
            item["published_at"] = m2.group(1)
        else:
            m2 = re.search(r"发布时间[:：]\s*(\d{2})-(\d{2})", text)
            if m2:
                item["published_at"] = "%d-%s-%s" % (today.year, m2.group(1), m2.group(2))

    pub = None
    try:
        if item.get("published_at"):
            pub = date.fromisoformat(item["published_at"])
    except (KeyError, ValueError, TypeError):
        pass
    dl = find_deadline(text, today, pub)
    item["deadline"] = dl.isoformat() if dl else None
    item["body_text"] = text[:2000]
    item["audience"] = find_audience(item["title"] + text[:1200])
    item["category"] = CAT_MAP.get(item["category_raw"], "其他")
    item["source"] = source_name
    item["source_type"] = source_name
    return item


# ---------------------------------------------------------------- 华为招聘
# 华为校招官网（career.huawei.com）是个 Vue 单页应用，页面上根本没有职位数据，
# 全靠 js 里的 jb.findJobList() 去打一个接口。接口路径是我从
# HwPortalReccamp.js 里翻出来的：
#   services/portal/portalpub/getJob/newHr/page/{pageSize}/{curPage}
# 踩过的四个坑：
#   ① jobType 是"招聘类型"：0=校园招聘、1=社会招聘。我们只要 0。
#      传 1 会返回社招热招职位（要求 3 年经验），那是另一个池子。
#   ② jobTypes 是"项目类型"：1=应届生、2=实习生、3=留学生，得分开问三遍。
#   ③ 必须带 reqTime（毫秒时间戳）且放在 query 里；用 POST 放 body 会 405。
#   ④ 这个源返回的就是结构化 JSON，不需要再开详情页（detail=False）。
HUAWEI_JOB_API = ("https://career.huawei.com/reccampportal/services/portal/"
                  "portalpub/getJob/newHr/page/20/1")

HUAWEI_JOB_TYPE_NAME = {"1": "应届生", "2": "实习生", "3": "留学生"}


# 岗位方向分类：只按标题（+职位族）打标签，规则可读、可改。
# 顺序很重要：先判芯片再判硬件（"芯片硬件工程师"归芯片），
# 先判硬件再判测试（"硬件测试"归硬件，"软件测试"归测试）。
JOB_CAT_RULES = [
    ("芯片", ["芯片", "ic ", "ic设计", "soc", "数字前端", "数字后端", "dft", "验证", "封装",
              "器件", "工艺", "版图", "eda", "模拟电路", "模拟ic", "硅"]),
    ("算法", ["算法", "ai", "机器学习", "深度学习", "大模型", "nlp", "自然语言",
              "计算机视觉", "cv", "数据挖掘", "推荐", "推理", "训练"]),
    ("嵌入式", ["嵌入式", "驱动", "bsp", "固件", "单片机", "rtos", "底层软件", "操作系统"]),
    ("硬件", ["硬件", "单板", "电路", "电源", "射频", "天线", "结构", "热设计", "pcb",
              "电磁", "可靠性设计", "光", "互连"]),
    ("测试", ["测试", "quality", "qa", "验证工程师", "可靠性"]),
    ("软件", ["软件", "后端", "前端", "java", "c++", "python", "云", "分布式",
              "大数据", "容器", "devops", "web"]),
    ("产品", ["产品", "解决方案", "售前", "fae", "技术支持", "市场", "运营", "销售"]),
]


def guess_job_cat(title: str, fam: str = "") -> str:
    """把岗位标题归到 8 个方向之一。命中不了就 '其他'。"""
    t = ((title or "") + " " + (fam or "")).lower()
    for name, words in JOB_CAT_RULES:
        for w in words:
            if w in t:
                return name
    return "其他"


def huawei_item(j: dict, jt: str) -> dict:
    """把华为接口返回的一条职位，翻译成我们自己的条目格式。"""
    jid = j.get("jobId")
    title = (j.get("jobname") or j.get("nameCn") or "").strip()
    area = (j.get("jobArea") or "").replace("/", "·")
    fam = j.get("jobFamilyName") or ""
    dept = j.get("deptName") or ""
    degree = {1: "大专", 2: "本科", 3: "硕士", 4: "博士"}.get(j.get("degree"), "")
    exp = j.get("workYear")
    biz = (j.get("mainBusiness") or "").strip()
    req = (j.get("jobRequire") or "").strip()

    bits = [x for x in (area, fam, degree, ("%s 年经验" % exp) if exp else "") if x]
    head = " · ".join(bits)
    summary = re.sub(r"\s+", " ", (head + "。" + biz) if (head and biz) else (head or biz))[:120]

    return {
        "title": title,
        "url": ("https://career.huawei.com/reccampportal/portal5/"
                "campus-recruitment-detail.html?jobId=%s&dataSource=1" % jid),
        "published_at": (j.get("releaseDate") or "")[:10] or None,
        "deadline": (j.get("expirationDate") or "")[:10] or None,
        "summary": summary or None,
        "body_text": (biz + " " + req)[:2000],
        "audience": ["不限"],
        # 就业大类下面一层是「公司」，所以 category 直接放公司名
        "category": "华为",
        "category_raw": "华为",
        "group": "就业",
        "firm": "华为",
        "jobcat": guess_job_cat(title, fam),
        "source": "华为招聘",
        "source_type": "华为招聘",
        "job_type": HUAWEI_JOB_TYPE_NAME.get(jt, ""),
        "job_area": area or None,
        "job_dept": dept or None,
    }


def huawei_jobs(_url=None, max_items: int = 40) -> list:
    """华为校招职位。三个项目类型分开问，按 jobId 去重。"""
    out, seen = [], set()
    for jt in ("1", "2", "3"):
        url = ("%s?jobType=0&jobTypes=%s&language=zh_CN&curPage=1&pageSize=20"
               "&reqTime=%d" % (HUAWEI_JOB_API, jt, int(time.time() * 1000)))
        try:
            data = json.loads(fetch_json(url))
        except Exception as e:
            print("  [华为招聘] %s 接口失败 %s: %s"
                  % (HUAWEI_JOB_TYPE_NAME[jt], type(e).__name__, e))
            continue
        rows = data.get("result") or []
        print("  [华为招聘] %s 当前开放 %d 条" % (HUAWEI_JOB_TYPE_NAME[jt], len(rows)))
        for j in rows:
            jid = j.get("jobId")
            if not jid or jid in seen:
                continue
            seen.add(jid)
            out.append(huawei_item(j, jt))
    return out[:max_items]


# 站点注册表：以后加一个新来源，就在这里加一行，别的都不用动
SOURCES = [
    {"name": "教务处", "url": LIST_URL, "parser": parse_list, "key": "jwc"},
    {"name": "学生处", "url": "https://xsc.xjtu.edu.cn/xgdt/tzgg.htm", "parser": parse_list_xsc, "key": "xsc"},
    {"name": "电信学部", "url": "http://eieug.xjtu.edu.cn/tzgg.htm", "parser": parse_list_eieug, "key": "eieug"},
    {"name": "南洋书院", "url": "http://nanyang.xjtu.edu.cn/xwtz/tzgg.htm", "parser": parse_list_nanyang, "key": "ny"},
    {"name": "华为招聘", "url": HUAWEI_JOB_API, "parser": huawei_jobs, "key": "huawei", "detail": False},
]

# 书院批量注册：每加一个书院，只在 SHUYUAN_SITES 里加一行
_SY_KEYS = {"彭康书院": "pk", "宗濂书院": "zl", "启德书院": "qd", "崇实书院": "cs", "南洋书院": "ny"}
for _name, _url in SHUYUAN_SITES:
    if any(s["name"] == _name for s in SOURCES):
        continue
    SOURCES.append({
        "name": _name, "url": _url, "key": _SY_KEYS.get(_name, _name),
        "parser": (lambda page_url, _n=_name: parse_list_shuyuan(page_url, _n)),
    })


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="all", choices=["all"] + [s["key"] for s in SOURCES])
    ap.add_argument("--limit", type=int, default=12, help="每个来源最多抓多少条详情")
    ap.add_argument("--out", default="raw.json")
    args = ap.parse_args()

    today = date.today()
    picked = [s for s in SOURCES if args.source in ("all", s["key"])]

    result, seen_url = [], set()
    for src in picked:
        try:
            listed = src["parser"](src["url"])
        except Exception as e:
            print("  [%s] 列表页失败 %s: %s" % (src["name"], type(e).__name__, e))
            continue
        print("%s 列表页 %d 条，取前 %d 条抓详情" % (src["name"], len(listed), min(args.limit, len(listed))))
        cap = len(listed) if src.get("detail") is False else args.limit
        for i, it in enumerate(listed[:cap], 1):
            if it["url"] in seen_url:
                continue
            try:
                if src.get("detail", True):
                    it = crawl_detail(it, today, src["name"])
                else:
                    # JSON 源自己在 parser 里就把字段填好了，不用再开详情页
                    it.setdefault("source", src["name"])
            except Exception as e:
                print("  [%s %d] 失败 %s: %s" % (src["name"], i, type(e).__name__, it["title"][:20]))
                continue
            seen_url.add(it["url"])
            result.append(it)
            dl = it["deadline"] or "无截止日期"
            print("  [%s %d] %s  %-12s %s" % (src["name"], i, it["published_at"], dl, it["title"][:34]))
            time.sleep(0.4)  # 别把人家服务器打疼
        print()

    Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    # 另外记一份"这次都抓了哪些源"——有的源今天 0 条（华为秋招收尾、书院没更新），
    # 它们不在 result 里，但页面上要显示"这个源我接了"。filter_rules 会读这个文件。
    Path(args.out).with_name("sources.json").write_text(
        json.dumps([s["name"] for s in SOURCES], ensure_ascii=False, indent=1), encoding="utf-8")
    print("已写入 %s（%d 条，来自 %d 个来源）" % (args.out, len(result), len({x["source"] for x in result})))
    return 0


if __name__ == "__main__":
    sys.exit(main())
