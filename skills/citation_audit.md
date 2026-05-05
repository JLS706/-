---
name: 引用溯源审计
description: 对综述论文中的引用进行忠实度审计——检查作者的引用是否准确反映了原文内容
trigger_keywords: [引用, 溯源, 审计, 验证, 忠实, 曲解, 过度引申, citation, verify, 原文对比, 引用检查, 交叉验证, 参考文献核查, 引用准确]
tools: [verify_citations, check_claim, auto_bind_literature, index_literature, search_literature, list_literature, read_document]
priority: 8
---

## 引用溯源审计（Citation Verification）

### 使用场景

用户需要验证综述/论文中引用的准确性：
- "帮我检查论文里的引用是否准确"
- "验证第3章的引用有没有过度引申"
- "对比一下我写的和原文是否一致"
- "审计一下引用"

### 触发方式与参考文献文件夹

**核心前提**：审计需要知道参考文献 PDF/Word 文件在哪里。获取方式按优先级：

1. **用户显式提供**：用户在消息中告知文件夹路径（如"参考文献在 C:/papers/refs/ 文件夹里"）
2. **自动检测（约定优于配置）**：工具会自动在论文同级目录寻找以下约定文件夹：
   - `{论文名}_refs/`、`{论文名}_参考文献/`
   - `参考文献/`、`refs/`、`references/`、`文献/`、`papers/`、`literature/`
3. **会话注入**：如果用户之前已在本轮对话中提供过 literature_folder，后续工具会自动复用

**关键行为规则**：
- 如果用户要求审计引用但未提供参考文献文件夹路径，**你必须主动询问**：
  "请告诉我参考文献 PDF/Word 文件所在的文件夹路径。例如：C:/papers/my_refs/"
- 如果自动检测到了文件夹，告知用户检测结果并确认是否正确
- 文档和参考文献文件夹是一一对应的关系

### 执行流程（推荐：一步到位）

1. 确认用户的论文文档路径（thesis_path）
2. 确认参考文献文件夹路径（literature_folder）——用户提供或自动检测
3. 直接调用 `verify_citations(thesis_path=..., literature_folder=...)` 执行审计：
   - 工具内部自动匹配论文参考文献列表与文件夹中的 PDF/Word 文件
   - 自动索引每篇原文文献（带缓存，不重复）
   - 提取综述中所有带 [N] 标记的主张句
   - 在原文中检索最相关的段落
   - LLM 以严苛审稿人视角判定忠实度
4. 输出审计报告

### 执行流程（高级：手动绑定）

如果自动匹配效果不好，可以手动绑定：
1. 用 `auto_bind_literature(thesis_path=..., literature_folder=...)` 查看匹配结果
2. 用 `index_literature(file_path=..., ref_key=...)` 手动修正未匹配的文献
3. 用 `verify_citations(thesis_path=..., ref_sources={...})` 指定精确映射

### 单句级校验

用户写了一句带引用的话，想快速验证：
- 调用 `check_claim(claim="MIMO 技术提升30%吞吐量[1]")`
- 需要先确保文献 [1] 已索引

### 输出格式

报告包含：
- 总体统计（忠实率、各类问题数量）
- 问题清单（逐条列出有偏差的引用，附原文证据）
- 每条问题的详细分析和改进建议

### Word 批注标注

`verify_citations` 默认开启 `annotate=True`，审计完成后会自动将有问题的引用以 Word 批注形式标注到原文档中：
- ⚠️ 轻微偏差（MINOR_ISSUE）
- ❌ 严重曲解（MAJOR_ISSUE）
- 🚫 原文未提及（UNSUPPORTED）

用户可以在 Word 的「审阅 → 批注」面板中查看所有批注。
如果用户不希望添加批注，可传入 `annotate=False`。

### 注意事项

- 审计质量取决于原文文献的完整性——如果提供的不是完整论文，可能会误判为 UNSUPPORTED
- `max_claims` 参数可控制审计范围，避免 API 调用过多
- 文件夹中的文件名最好与参考文献标题/作者有一定关联，以提高自动匹配准确率
