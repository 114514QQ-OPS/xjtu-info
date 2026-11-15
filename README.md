# 信息差 · 西安交通大学本科生通知聚合

把西交几个还在更新的官网通知，按**截止日期**排序，标出**受众**，每天自动更新一次。

## 这是什么

- 4 个来源：教务处、学生处、电信学部本科教学、南洋书院
- 只存标题 + 摘要 + 原文链接，不全文转载
- 每天北京时间 08:00 由 GitHub Actions 抓一次，结果提交为 `feed.json`

## 本地怎么跑

```bash
python tools/crawl_jwc.py --source all --out tools/raw.json
python tools/filter_rules.py --input tools/raw.json --output feed.json
python -m http.server 8777     # 然后打开 http://127.0.0.1:8777/
```

直接双击 `index.html` 打不开数据——浏览器在 `file://` 下禁止 `fetch()` 本地文件，必须起一个本地服务。

## 文件

| 文件 | 作用 |
|---|---|
| `index.html` | 页面本体，单文件、零依赖，`fetch('feed.json')` 渲染 |
| `feed.json` | 每天生成的数据 |
| `tools/crawl_jwc.py` | 抓取：4 个来源的列表页 → 详情页 → 抽标题/摘要/截止日/受众 |
| `tools/filter_rules.py` | 筛选：硬规则 → 打分 → 分档 → 去重 |
| `.github/workflows/update.yml` | 每天 08:00 自动跑上面两步并提交 |

## 筛选规则

在 `tools/filter_rules.py` 顶部：

```python
PUBLISH_WINDOW_DAYS = 60   # 发布超过 60 天的直接丢
DROP_WORDS = [...]         # 标题命中就丢（招标、中标…）
KEEP_WORDS = [...]         # 至少命中一个才留（报名、申请、讲座、停水…）
```

原则是**误杀比误放严重**：多推一条用户划过去就完了，漏掉一个奖学金截止是几千块的损失。

## 局限

- 公众号、班级群的通知抓不到——那才是大部分通知真正的来源
- 很多通知把时间写在附件（Word/PDF）里，正文只写「详见附件」，这类只能标「无截止」
- 书院官网大多已停更（南洋书院最后一条 2025-03）
