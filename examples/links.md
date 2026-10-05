# 链接测试页

## 真实存在的标题

两个好链接：[Example](https://example.com) 和 https://example.org 。

一个 404：[不存在的页面](https://httpstat.us/404)。

一个 DNS 坏域名：https://this-domain-definitely-does-not-exist-12345.invalid/ 。

锚点：[跳到本节](#真实存在的标题) 好，[跳到不存在](#这个标题不存在) 坏。

相对链接：[存在的文件](./exists.md) 好，[缺失的文件](./missing.md) 坏。

代码块里的链接应该被忽略：

```
[假链接](https://fake-in-code-block.invalid/)
```

行内代码 `https://fake-in-inline-code.invalid/` 也应该被忽略。
