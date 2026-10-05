# urlcheck

在 Markdown 文件里找出死链的小工具：丢一个文件或目录进去，吐出一张"好 / 警告 / 死"报告。

```bash
python -m urlcheck README.md
python -m urlcheck ./docs --jobs 8 --json
```

纯 Python 标准库，零依赖。

## 它会检查什么

从 Markdown 里提取三类链接（自动跳过代码块和行内代码）：

- `[文字](https://…)` 内联链接和裸 URL → 发 HEAD 请求（405 时降级 GET），10 秒超时
- `#锚点` → 检查本文标题里有没有这个锚点（GitHub 风格 slug）
- `./相对路径` → 检查文件在不在，带 `#锚点` 的还会检查目标文件的标题

## 输出

```
❌ ./docs/guide.md:12
   https://example.com/old-page
   BROKEN: HTTP 404 未找到
⚠️ ./docs/guide.md:30
   https://some-site.com/article
   WARN: HTTP 403（站点可能屏蔽爬虫，不一定是死链）

共检查 42 个链接：39 通过，1 警告，2 死链
```

退出码：`0` 全部通过，`1` 有死链，`2` 用法错误——可以直接进 CI。

| 参数 | 说明 |
|---|---|
| `--jobs N` | 并发数（默认 8） |
| `--timeout S` | 单个请求超时秒数（默认 10） |
| `--retry N` | 失败重试次数（默认 0） |
| `--exclude PAT` | 跳过匹配该 glob 的 URL，可多次使用 |
| `--json` | 输出机器可读的 JSON |
| `--version` | 显示版本 |

## 诚实说明

- **403 不等于死链**：很多站点会屏蔽无头请求，这类标为 WARN 而不是 BROKEN。
- 跳转超过 3 次会标 WARN 并显示最终 URL。
- 有些站点限流 aggressive，并发太高可能误报 429（标 WARN），可调小 `--jobs`。
- 锚点检查只认 `#`/`##` 标题生成的 slug，和 GitHub 渲染规则一致，自定义锚点（HTML `<a name>`）认不出。
- 只检查链接"能不能打开"，不检查内容对不对。
