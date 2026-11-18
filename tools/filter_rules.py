# -*- coding: utf-8 -*-
"""
filter_rules.py — 校园通知筛选器（硬规则版 v0.1）

用法：
    python filter_rules.py --demo                         # 跑内置样例
    python filter_rules.py --input raw.json --output out.json --grade 大一
    python filter_rules.py --input raw.json --today 2026-11-13

设计原则：
    1. 只依赖标准库，任何机器上能跑
    2. 规则全部集中在下面的 CONFIG 区，改词表不用碰逻辑
    3. 宁可归档，不要删除（分四档：头条 / 列表 / 归档 / 丢弃）
    4. 每条都带 reason，方便你抽查它为什么被丢

输入：一个 JSON 数组，每个元素至少包含 title + url
输出：{"stats": {...}, "items": [...], "dropped": [...]}
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import unicodedata
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# ============================================================================
# CONFIG —— 你只需要改这一段
# ============================================================================

# 标题里出现这些词 → 直接丢弃（排除优先于保留）
DROP_WORDS = [
    "讣告", "任免", "招标", "采购", "中标", "询价", "磋商",
    "会议纪要", "巡视", "整改", "学术报告", "成果奖励", "表彰决定",
    "公开招聘", "人才招聘",
]

# 标题或正文命中任一 → 认为"有行动价值"（必须至少命中一个）
# 注意：停水/停电/讲座 这类"没 deadline 但必须知道"的，也要放进来，
# 否则就会误杀——误杀比误放严重得多。它们靠打分自然落到"列表/归档"。
KEEP_WORDS = [
    "报名", "申请", "申报", "提交", "截止", "评审", "选课", "退课",
    "补考", "缓考", "考试", "竞赛", "奖学金", "助学金", "立项",
    "结题", "中期检查", "采集", "预约", "领取", "填报", "推荐",
    "遴选", "认定", "注册", "缴费", "体测", "体检", "接种", "答辩",
    "讲座", "签到", "学分", "参加",
    "停水", "停电", "封路", "施工", "维修",
]

# 标题里出现这些 → 视为"强行动"（用于正文含排除词时救回）
STRONG_KEEP = ["报名", "申请", "申报", "截止", "选课", "退课", "评审", "竞赛"]

# 来源权重（近似"稀缺性"：越上游、越只有这里能看到的，分越高）
SOURCE_WEIGHT = {
    "教务处": 3,
    "学生处": 3,
    "资助中心": 3,
    "学院": 2,
    "就业中心": 2,
    "图书馆": 1,
    "书院": 1,
    "华为招聘": 2,
    "小米招聘": 2,
    "比亚迪招聘": 2,
    "腾讯招聘": 2,
    "美团招聘": 2,
    "字节招聘": 2,
    "中兴通讯招聘": 2,
    "大疆招聘": 2,
    "宁德时代招聘": 2,
    "其他": 1,
}

GRADE_ORDER = {"大一": 1, "大二": 2, "大三": 3, "大四": 4, "研究生": 5}

CATEGORIES = ["教务", "奖助", "竞赛", "科研", "就业", "讲座", "生活", "招聘", "华为", "小米", "比亚迪", "中兴通讯", "大疆", "宁德时代",
              "彭康书院", "宗濂书院", "启德书院", "崇实书院", "南洋书院", "其他"]

# 招聘类（企业岗位）走一套不同的规则：
#   学校通知看的是"有没有截止日期、要不要交材料"，
#   岗位看的是"这活儿是不是给学生的、什么时候过期"。
# 所以这类直接跳过关键词硬规则，靠 deadline + 来源权重打分。
#
# 「就业」大类下面的子类是**公司**：华为、以后的中兴/字节…
# JOB_FIRMS 里列的公司，即使这次抓到 0 个岗位，页面标签也会显示出来，
# 让你看得见"这个源我接了，只是它现在空着"。
JOB_FIRMS = ["华为", "小米", "比亚迪", "中兴通讯", "大疆", "宁德时代", "腾讯", "美团", "字节跳动"]
JOB_CATS = {"招聘"} | set(JOB_FIRMS)

# 书院：通知是给"住在这个书院的本科生"的（宿舍、评优、学代会、志愿活动），
# 标题里不一定有"报名/申请"，硬套关键词会全被误杀。
# 所以书院类也跳过关键词规则，只靠 60 天窗口 + 打分。
BOOK_HOUSE_CATS = {"彭康书院", "宗濂书院", "启德书院", "崇实书院", "南洋书院"}
SKIP_KEYWORD_CATS = JOB_CATS | BOOK_HOUSE_CATS

PUBLISH_WINDOW_DAYS = 60      # 发布超过这么多天的，不进主列表
DEDUP_THRESHOLD = 0.82        # 标题相似度超过这个值视为同一条

# 分档阈值（四维合计 0-12）
# 这几个数字是拿真实数据校准出来的：教务处通知本身权威分就高（3），
# 阈值定 8 会让一半的通知都进头条，等于没有头条。
HEADLINE_MIN = 10
LIST_MIN = 6

# ============================================================================
# 工具函数
# ============================================================================


def make_id(s: str) -> str:
    return hashlib.sha1((s or "").encode("utf-8")).hexdigest()[:16]


_TRACK_KEYS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "from", "spm", "share", "share_token", "timestamp", "_t", "t", "v", "isappinstalled",
}


def norm_url(u: str) -> str:
    """去掉追踪参数、统一大小写，用于去重。"""
    u = (u or "").strip()
    if not u:
        return ""
    p = urlsplit(u)
    q = sorted(
        (k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
        if k.lower() not in _TRACK_KEYS
    )
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/") or "/", urlencode(q), ""))


_PUNCT = re.compile(r"[\s\u3000，。；：、！？（）()《》〈〉“”\"'·—\-_/\\|\[\]【】<>]+")


def norm_title(s: str) -> str:
    """去掉公文套话和标点，只留下"实质字样"，用于相似度比较。"""
    s = unicodedata.normalize("NFKC", s or "")
    s = _PUNCT.sub("", s)
    for w in ("关于", "印发", "转发", "的通知", "通知", "开展", "工作", "有关", "如下", "的"):
        s = s.replace(w, "")
    return s


def bigrams(s: str) -> set:
    if len(s) < 2:
        return {s} if s else set()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def title_sim(a: str, b: str) -> float:
    """字符二元组 Jaccard 相似度，对中文比 SequenceMatcher 稳。"""
    A, B = bigrams(norm_title(a)), bigrams(norm_title(b))
    if not A or not B:
        return 0.0
    return len(A & B) / len(A | B)


_RE_YMD = re.compile(r"(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})")
_RE_MD = re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]")


def parse_date(s, today: date | None = None) -> date | None:
    """解析 2026-11-17 / 2026/11/17 / 2026年11月17日 / 11月17日。解析不了返回 None。"""
    if not s:
        return None
    if isinstance(s, datetime):
        return s.date()
    if isinstance(s, date):
        return s
    s = str(s).strip()
    if s.lower() in {"", "null", "none", "nan", "-"}:
        return None

    m = _RE_YMD.search(s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None

    m = _RE_MD.search(s)
    if m:
        base = today or date.today()
        cands = []
        for y in (base.year - 1, base.year, base.year + 1):
            try:
                cands.append(date(y, int(m.group(1)), int(m.group(2))))
            except ValueError:
                pass
        if cands:
            return min(cands, key=lambda d: abs((d - base).days))
    return None


# ============================================================================
# 四层筛选
# ============================================================================


def hard_filter(item: dict) -> tuple[bool, str]:
    """第一层：关键词硬规则（0 成本）。"""
    title = item.get("title") or ""
    body = (item.get("body_text") or "")[:800]
    text = title + body

    # 招聘岗位天然不含"报名/申请"这类词，用学校通知的规则筛会把它们全杀光。
    # 书院通知同理（宿舍评比、学代会提案、志愿活动）。
    if item.get("category") in SKIP_KEYWORD_CATS:
        return True, "招聘/书院类，跳过关键词规则"

    for w in DROP_WORDS:
        if w in title:
            return False, f"标题含排除词「{w}」"

    has_strong = any(w in title for w in STRONG_KEEP)
    if not has_strong:
        for w in DROP_WORDS:
            if w in body:
                return False, f"正文含排除词「{w}」且标题无强行动词"

    if not any(w in text for w in KEEP_WORDS):
        return False, "没有任何行动类关键词"

    return True, "通过硬规则"


def score_item(item: dict, grade: str, today: date) -> dict:
    """第二层：四维打分（0-3 各维，合计 0-12）。"""
    title = item.get("title") or ""
    body = (item.get("body_text") or "")[:800]
    text = title + body

    # 1) 相关性：受众匹配
    aud = item.get("audience") or []
    if not aud or "不限" in aud:
        rel = 2
    elif grade in aud:
        rel = 3
    else:
        g = GRADE_ORDER.get(grade, 1)
        near = any(abs(GRADE_ORDER.get(a, 9) - g) == 1 for a in aud)
        rel = 1 if near else 0

    # 2) 行动性：命中多少个行动词
    if item.get("category") in JOB_CATS:
        act = 2          # 岗位本身就是"可以去投"的东西，固定给 2 分
    else:
        n = sum(1 for w in KEEP_WORDS if w in text)
        act = 3 if n >= 3 else 2 if n == 2 else 1 if n == 1 else 0

    # 3) 时效性：距截止几天
    dl = parse_date(item.get("deadline"), today)
    if dl is None:
        urg = 0
    else:
        d = (dl - today).days
        urg = 3 if d <= 3 else 2 if d <= 7 else 1 if d <= 14 else 0

    # 4) 来源权威（近似稀缺性）
    auth = SOURCE_WEIGHT.get(item.get("source_type") or "其他", 1)

    total = rel + act + urg + auth
    return {
        "scores": {"relevance": rel, "action": act, "urgency": urg, "authority": auth},
        "score": total,
        "deadline_parsed": dl.isoformat() if dl else None,
        "days_left": (dl - today).days if dl else None,
    }


def decide(item: dict, grade: str, today: date) -> dict:
    """第三层：分档 + 时间窗口。"""
    s = score_item(item, grade, today)
    s["scores"], s["score"] = s["scores"], s["score"]

    pub = parse_date(item.get("published_at"), today)
    dl = s.get("deadline_parsed")
    dl_date = date.fromisoformat(dl) if dl else None

    # 时间窗口放最前面：一条 2019 年的通知，不管它截止没截止、受众对不对，
    # 都不该出现在 feed 里。这个判断必须在「已过截止」「受众不符」之前，
    # 否则它们会先被归到「归档」，永远绕开窗口检查。
    if pub and (today - pub).days > PUBLISH_WINDOW_DAYS and item.get("category") not in JOB_CATS:
        s["bucket"], s["reason"] = "丢弃", f"发布已 {(today - pub).days} 天，超出 {PUBLISH_WINDOW_DAYS} 天窗口"
        return s

    # 受众完全不符 → 归档（不是丢，搜得到）
    if s["scores"]["relevance"] == 0:
        s["bucket"], s["reason"] = "归档", f"受众不符（{grade} 不在 {'/'.join(item.get('audience') or [])}）"
        return s

    if dl_date and dl_date < today:
        s["bucket"], s["reason"] = "归档", "已过截止日期"
        return s

    if s["score"] >= HEADLINE_MIN:
        s["bucket"] = "头条"
        s["reason"] = f"{s['score']}/12 分，{s.get('days_left')} 天后截止"
    elif s["score"] >= LIST_MIN:
        s["bucket"] = "列表"
        s["reason"] = f"{s['score']}/12 分"
    else:
        s["bucket"] = "归档"
        s["reason"] = f"{s['score']}/12 分，低于列表阈值"

    # 书院通知绝大多数正文里不写截止日期（宿舍评比、学代会提案、作品征集），
    # 打分自然偏低。可它们是"就发生在你楼下"的事，沉到归档等于看不见，
    # 所以：书院类 + 没截止日期 → 至少留在列表。有截止日期且过了的，照样归档。
    if s["bucket"] == "归档" and item.get("category") in BOOK_HOUSE_CATS and not dl_date:
        s["bucket"] = "列表"
        s["reason"] = f"{s['score']}/12 分，书院通知无截止日期，保留在列表"

    # 企业岗位同理：小米、比亚迪的接口里根本没有截止日期字段，只有发布时间。
    # 没有截止日期不代表这个岗位没了，不该因为「不够紧急」沉到归档里。
    if s["bucket"] == "归档" and item.get("category") in JOB_CATS and not dl_date:
        s["bucket"] = "列表"
        s["reason"] = f"{s['score']}/12 分，岗位无公开截止日期，保留在列表"
    return s


# ============================================================================
# 主流程
# ============================================================================


# ---------------------------------------------------------------- 大类
# 页面是「大类 → 子类」两级筛选：
#   大类 = 学校通知（现在只有这一个）
#   子类 = 教务 / 奖助 / 竞赛 / 科研 / 就业 / 讲座（= category 字段）
#
# 以后要加「实习机会」「学习资料」这种新大类，两条路：
#   ① 爬虫抓的时候直接给 item["group"] 赋值（最灵活）
#   ② 在这里的 GROUP_BY_CATEGORY 加一行映射（比如 {"实习": "实习机会"}）
# 前端不用改——标签页是按数据里出现过哪些 group 自动长出来的。
GROUP_DEFAULT = "学校通知"
GROUP_BY_CATEGORY: dict[str, str] = {"招聘": "就业", "华为": "就业", "小米": "就业", "比亚迪": "就业", "中兴通讯": "就业", "大疆": "就业", "宁德时代": "就业", "腾讯": "就业", "美团": "就业", "字节跳动": "就业"}


def process(raw: list, grade: str = "大一", today: date | None = None, input_path=None) -> dict:
    today = today or date.today()
    dropped, survivors = [], []

    # --- 字段校验 + 硬规则 ---
    for it in raw:
        if not isinstance(it, dict):
            continue
        it = dict(it)
        title, url = (it.get("title") or "").strip(), (it.get("url") or "").strip()
        if not title or not url:
            dropped.append({**it, "reason": "缺 title 或 url"})
            continue

        # id 用「原始 url + 标题」：Moka 那类站点的岗位 id 在 #fragment 里，
        # 用 norm_url() 会把它们压成同一个 id，收藏和去重都会串台。
        it["id"] = it.get("id") or make_id((url or "") + "|" + (it.get("title") or "") or title)
        it.setdefault("audience", [])
        it.setdefault("category", "其他")
        it.setdefault("source_type", "其他")
        # 大类：数据里没写就按 category 查表，查不到就是「学校通知」
        it.setdefault("group", GROUP_BY_CATEGORY.get(it["category"], GROUP_DEFAULT))

        ok, why = hard_filter(it)
        if not ok:
            dropped.append({**it, "reason": why})
            continue
        survivors.append(it)

    # --- 去重（保留发布时间最新的那条）---
    # 注意：招聘类**不做标题相似度去重**。同一家公司常有「工艺工程师（深圳）」
    # 「工艺工程师（西安）」这种只差城市、甚至只差一个序号的两个岗位，
    # 按标题相似度一合并，20 个岗位只剩 1 个。它们只按 URL 去重。
    survivors.sort(key=lambda x: str(x.get("published_at") or ""), reverse=True)
    kept = []
    for it in survivors:
        is_job = it.get("category") in JOB_CATS
        dup = None
        for k in kept:
            same_url = (it["url"] == k["url"]) if is_job else (norm_url(it["url"]) == norm_url(k["url"]))
            sim = title_sim(it.get("title", ""), k.get("title", ""))
            if same_url or (not is_job and sim >= DEDUP_THRESHOLD):
                dup = (k, sim, same_url)
                break
        if dup:
            k, sim, same_url = dup
            why = "URL 相同" if same_url else f"标题相似度 {sim:.2f}"
            dropped.append({**it, "reason": f"重复（与「{k.get('title')}」{why}）"})
        else:
            kept.append(it)

    # --- 打分分档 ---
    # 分档后标成「丢弃」的（太旧、没行动类关键词）从 items 里拿出去，
    # 否则 feed.json 会越来越大，全是永远不会显示的东西。
    result, dropped_late = [], []
    for it in kept:
        it.update(decide(it, grade, today))
        if it["bucket"] == "丢弃":
            dropped_late.append(it)
        else:
            result.append(it)

    def sk(x):
        return ({"头条": 0, "列表": 1, "归档": 2}.get(x["bucket"], 3),
                x.get("days_left") if x.get("days_left") is not None else 999)

    result.sort(key=sk)

    # 「考试」大类不来自爬虫，是一份人工维护的目录（exams.json）：
    # 报名入口、官网、往年时间规律。它们不参与打分和筛选，直接进列表。
    exams = load_exams(input_path)
    result.extend(exams)

    stats = {
        "today": today.isoformat(),
        "grade": grade,
        "input": len(raw),
        "dropped": len(dropped) + len(dropped_late),
        "headline": sum(1 for x in result if x["bucket"] == "头条"),
        "list": sum(1 for x in result if x["bucket"] == "列表"),
        "archive": sum(1 for x in result if x["bucket"] == "归档"),
    }
    # 每个来源抓了多少、最后进了多少条。
    # 页面右栏要显示"抓 10 · 入选 0"——一个源对你没产出，本身就是信息。
    src_in, src_out = {}, {}
    for it in raw:
        if isinstance(it, dict):
            s = it.get("source") or "未知"
            src_in[s] = src_in.get(s, 0) + 1
    for it in result:
        s = it.get("source") or "未知"
        src_out[s] = src_out.get(s, 0) + 1
    stats["sources"] = [{"name": s, "in": n, "out": src_out.get(s, 0)} for s, n in src_in.items()]
    # 爬虫会在 raw.json 旁边放一份 sources.json（这次接了哪些源）。
    # 今天 0 条的源不在 raw 里，但页面上要看得到——"这个源我接了，只是它现在空着"。
    try:
        _sp = Path(input_path).with_name("sources.json") if input_path else None
        if _sp and _sp.exists():
            declared = json.loads(_sp.read_text(encoding="utf-8"))
            have = {s["name"] for s in stats["sources"]}
            for name in declared:
                if name not in have:
                    stats["sources"].append({"name": name, "in": 0, "out": 0})
    except Exception:
        pass

    # 每个大类入选多少条，页面顶层标签用。
    grp = {}
    for it in result:
        g = it.get("group") or GROUP_DEFAULT
        grp[g] = grp.get(g, 0) + 1
    stats["groups"] = grp

    # 就业大类下面的公司名单：注册过的 + 这次数据里真的出现的。
    # 页面用它画子类标签，所以公司这次 0 岗位标签也不会消失。
    firms = list(JOB_FIRMS)
    for it in result:
        c = it.get("category") or ""
        if (it.get("group") == "就业") and c and c not in firms:
            firms.append(c)
    stats["firms"] = firms

    return {"stats": stats, "items": result, "dropped": dropped + dropped_late}


# ============================================================================
# CLI
# ============================================================================

DEMO = [
    {"source": "教务处", "source_type": "教务处", "title": "关于 2026-2027 学年第一学期期中退课申请的补充通知",
     "url": "https://jwc.xjtu.edu.cn/info/1001/1.htm", "published_at": "2026-11-10", "deadline": "2026-11-17",
     "audience": ["大一", "大二"], "category": "教务", "body_text": "退课窗口 11 月 14 日至 17 日开放，逾期不再受理，需在系统提交申请。"},
    {"source": "学生处", "source_type": "学生处", "title": "关于开展 2026 年国家奖学金、国家励志奖学金评审工作的通知",
     "url": "https://xsc.xjtu.edu.cn/info/1002/2.htm", "published_at": "2026-11-09", "deadline": "2026-11-18",
     "audience": ["大二", "大三"], "category": "奖助", "body_text": "需提交成绩单原件、获奖证书复印件、个人陈述，材料交至书院，逾期视为放弃。"},
    {"source": "教务处", "source_type": "教务处", "title": "2026 年下半年全国大学英语四、六级口语考试报名通知",
     "url": "https://jwc.xjtu.edu.cn/info/1003/3.htm", "published_at": "2026-11-11", "deadline": "2026-11-25",
     "audience": ["大一", "大二"], "category": "教务", "body_text": "笔试报名成功方可报考对应级别口试，机位有限，按报名先后顺序。"},
    {"source": "教务处", "source_type": "教务处", "title": "关于 2027 届毕业生图像信息采集工作安排的通知",
     "url": "https://jwc.xjtu.edu.cn/info/1004/4.htm", "published_at": "2026-11-12", "deadline": "2026-12-01",
     "audience": ["大四"], "category": "教务", "body_text": "图像用于学历学位证书及学信网注册，不可由他人代办。"},
    {"source": "图书馆", "source_type": "图书馆", "title": "钱学森图书馆冬季信息素养系列讲座（第 3 讲）",
     "url": "https://lib.xjtu.edu.cn/info/1005/5.htm", "published_at": "2026-11-12", "deadline": "2026-11-22",
     "audience": ["不限"], "category": "讲座", "body_text": "11 月 22 日 19:00，图书馆三楼报告厅，现场签到可计入第二课堂学分。"},
    {"source": "后勤保障部", "source_type": "其他", "title": "关于兴庆校区部分区域临时停水的通知",
     "url": "https://hq.xjtu.edu.cn/info/1006/6.htm", "published_at": "2026-11-12", "deadline": None,
     "audience": ["不限"], "category": "生活", "body_text": "因管网维修，11 月 15 日 8:00-17:00 停水。"},
    {"source": "采购办", "source_type": "其他", "title": "西安交通大学 XX 实验设备采购招标公告",
     "url": "https://zbcg.xjtu.edu.cn/info/1007/7.htm", "published_at": "2026-11-11", "deadline": "2026-11-28",
     "audience": ["不限"], "category": "其他", "body_text": "现对该项目进行公开招标，欢迎符合条件的供应商报名。"},
    {"source": "教务处", "source_type": "教务处", "title": "关于 2026-2027 学年第一学期期中退课申请的补充通知（转载）",
     "url": "https://jwc.xjtu.edu.cn/info/1001/1.htm?from=timeline", "published_at": "2026-11-10", "deadline": "2026-11-17",
     "audience": ["大一", "大二"], "category": "教务", "body_text": "同前。"},
    {"source": "教务处", "source_type": "教务处", "title": "关于 2025-2026 学年第二学期期末考试安排的通知",
     "url": "https://jwc.xjtu.edu.cn/info/1008/8.htm", "published_at": "2025-06-01", "deadline": "2025-06-20",
     "audience": ["大一"], "category": "教务", "body_text": "考试安排见附件。"},
    {"source": "数学学院", "source_type": "学院", "title": "第十六届全国大学生数学竞赛（陕西赛区）报名及校内选拔安排",
     "url": "https://math.xjtu.edu.cn/info/1009/9.htm", "published_at": "2026-11-08", "deadline": "2026-11-25",
     "audience": ["大一", "大二"], "category": "竞赛", "body_text": "非数学类专业组，初赛 11 月 28 日，校内选拔前 60 名统一报名。"},
]


def load(path: str) -> list:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return data if isinstance(data, list) else data.get("items", [])


def load_exams(near: str | None = None) -> list:
    """读人工维护的考试目录。找不到文件就返回空列表，不影响主流程。"""
    cands = []
    if near:
        cands.append(Path(near).resolve().parent / "exams.json")
    cands.append(Path(__file__).resolve().parent.parent / "exams.json")
    cands.append(Path.cwd() / "exams.json")
    path = next((p for p in cands if p.exists()), None)
    if path is None:
        return []

    out = []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:                      # noqa: BLE001 —— 目录坏了就当没有，不让整条流水线挂
        return []
    for e in (data.get("items") if isinstance(data, dict) else data) or []:
        if not isinstance(e, dict) or not e.get("title"):
            continue
        out.append({
            "id": e.get("id") or e["title"],
            "title": e["title"],
            "url": e.get("register_url") or e.get("site_url") or "",
            "register_url": e.get("register_url"),
            "site_url": e.get("site_url"),
            "published_at": None,
            "deadline": None,
            "summary": e.get("summary"),
            "window": e.get("window"),
            "tips": e.get("tips"),
            "audience": e.get("audience") or ["不限"],
            "category": e.get("category") or "其他",
            "category_raw": e.get("category") or "其他",
            "group": "考试",
            "kind": "exam",
            "professional": bool(e.get("professional")),
            "source": "考试入口",
            "source_type": "考试入口",
            "bucket": "列表",
            "score": 10,
            "days_left": None,
            "scores": {"relevance": 3, "action": 3, "urgency": 1, "authority": 3},
            "reason": "常设考试入口（人工维护，不参与筛选）",
        })
    return out


# ---------------------------------------------------------------------------
# 前端产物：拆包 + 瘦身 + 日历
# ---------------------------------------------------------------------------
# 页面上真正会用到的字段。整页正文（body_text）、打分细节（scores）、丢弃原因（reason）
# 只在筛选阶段有用，发到手机上是纯流量，全部不进前端文件。
SLIM_KEYS = (
    "id", "title", "url", "summary", "require", "salary",
    "deadline", "days_left", "published_at", "source", "source_type",
    "category", "audience", "bucket", "score", "group", "firm",
    "jobcat", "subcat", "job_type", "kind", "window", "tips",
    "register_url", "site_url", "professional",
)


def slim(item: dict) -> dict:
    out = {}
    for k in SLIM_KEYS:
        v = item.get(k)
        if v is None or v == "" or v == [] or v == {}:
            continue
        out[k] = v
    return out


def write_split(out: dict, output_path: str) -> dict:
    """把结果拆成几个文件，首屏只下载「学校通知」。

    feed.json          统计 + 学校通知（首屏，几十 KB）
    feed-jobs.json     就业（点「就业」时才拉）
    feed-exam.json     考试
    feed-archive.json  归档（默认不加载）
    """
    base = Path(output_path).resolve().parent
    items = out.get("items") or []
    groups = {
        "feed.json": [x for x in items
                      if (x.get("group") or "学校通知") == "学校通知" and x.get("bucket") != "归档"],
        "feed-jobs.json": [x for x in items if x.get("group") == "就业" and x.get("bucket") != "归档"],
        "feed-exam.json": [x for x in items if x.get("group") == "考试"],
        "feed-archive.json": [x for x in items if x.get("bucket") == "归档"],
    }
    sizes = {}
    for name, rows in groups.items():
        payload = {"items": [slim(x) for x in rows]}
        if name == "feed.json":
            payload["stats"] = out["stats"]
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        (base / name).write_text(text, encoding="utf-8")
        sizes[name] = round(len(text.encode()) / 1024, 1)
    return sizes


def write_ics(out: dict, output_path: str) -> int:
    """把有截止日期的条目导出成日历文件：手机上订阅一次，到期前一天自动提醒。"""
    base = Path(output_path).resolve().parent
    stamp_now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    def esc(s: str) -> str:
        return (s or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//infocha//xjtu-info//CN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:大学生信息网 · 截止提醒",
        "X-WR-TIMEZONE:Asia/Shanghai",
    ]
    n = 0
    for it in out.get("items") or []:
        if it.get("bucket") == "归档":
            continue
        d = parse_date(it.get("deadline") or "")
        if not d:
            continue
        n += 1
        title = (it.get("title") or "")[:70]
        desc = ((it.get("summary") or "") + "\n" + (it.get("url") or "")).strip()
        lines += [
            "BEGIN:VEVENT",
            f"UID:{(it.get('id') or str(n))}@xjtu-info",
            f"DTSTAMP:{stamp_now}",
            f"DTSTART;VALUE=DATE:{d.strftime('%Y%m%d')}",
            f"SUMMARY:{esc(title)}",
            f"DESCRIPTION:{esc(desc)}",
            f"URL:{it.get('url') or ''}",
            "TRANSP:TRANSPARENT",
            "BEGIN:VALARM",
            "TRIGGER:-P1D",
            "ACTION:DISPLAY",
            f"DESCRIPTION:{esc('明天截止：' + title)}",
            "END:VALARM",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    (base / "deadlines.ics").write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
    return n


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="校园通知筛选器")
    ap.add_argument("--input", help="原始 JSON 数组文件")
    ap.add_argument("--output", help="输出 JSON 文件（不填则打印摘要）")
    ap.add_argument("--grade", default="大一", help="你的年级，默认 大一")
    ap.add_argument("--today", help="指定今天，如 2026-11-13（便于复现）")
    ap.add_argument("--demo", action="store_true", help="跑内置样例")
    args = ap.parse_args()

    if args.demo:
        raw = DEMO
    elif args.input:
        raw = load(args.input)
    else:
        ap.print_help()
        return 1

    today = parse_date(args.today) or date.today()
    out = process(raw, grade=args.grade, today=today, input_path=args.input)

    if args.output:
        sizes = write_split(out, args.output)
        n_ics = write_ics(out, args.output)
        total = sum(sizes.values())
        print("已写入 " + " / ".join(f"{k} {v}KB" for k, v in sizes.items())
              + f"（合计 {total:.0f}KB）")
        print(f"日历 deadlines.ics：{n_ics} 个截止日期")

    st = out["stats"]
    print(f"\n[{st['today']}] 学段={st['grade']}  输入 {st['input']} 条 → "
          f"头条 {st['headline']} / 列表 {st['list']} / 归档 {st['archive']} / 丢弃 {st['dropped']}\n")
    for it in out["items"]:
        mark = {"头条": "HEAD", "列表": "LIST", "归档": "ARCH"}.get(it["bucket"], "????")
        d = f"{it.get('days_left')}d" if it.get("days_left") is not None else "--"
        print(f"  [{mark}] {it['score']:>2}/12 {d:>4}  {it['title'][:44]}")
        print(f"         └ {it['reason']}")
    if out["dropped"]:
        print("\n  丢弃：")
        for it in out["dropped"]:
            print(f"  [DROP] {it.get('title','')[:44]}")
            print(f"         └ {it['reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

