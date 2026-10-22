# 《pcc 的设计与实现》风格契约 / Style Contract

本文件约束 `books/cn`(中文版)与 `books/en`(英文版)全部章节。任何章节不得偏离。

## 对标与声音

对标经典:《The Design and Implementation of the 4.4BSD Operating System》、
CS:APP、《The Garbage Collection Handbook》。这意味着:

1. **设计理由先于机制。** 每个小节先回答"为什么是这个设计、备选是什么、为什么放弃",
   再讲"怎么实现的"。机制描述没有理由支撑的,删。
2. **一切断言落在真实代码上。** 引用真实文件、真实函数名、真实结构体、真实 enum。
   引用格式:`pcc/runtime/src/py_gc_backend.c` 中的 `pcc_gc_store_ptr()`。
   **禁止引用行号**(行号会腐烂),用函数/类型/标识符名定位。
   **禁止发明不存在的 API、文件、数字。** 写之前必须读过对应源码。
3. **诚实是文体的一部分。** pcc 的声明卫生(mode-labeled claims)同样约束本书:
   - 不写"pcc 让 Python 达到 C 速度";写"对语义足够稳定可原生降低的部分"。
   - 区分 host pcc / pcc1;libpython / no-libpython;LLVM / self backend;
     stage1 / pcc1→pcc2→pcc3 不动点。
   - 实验性的东西明说实验性;已知缺陷如实写成开放问题,修复落地后补记
     修复日期、机制与回归归属(第 16 章的 typed-int 溢出档案是范例)。
4. **案例研究是一等内容。** 每章设"历史与教训"一节,体例对齐经典教科书的
   case study,取材 `docs/investigations/` 的真实调查(带日期),讲清:症状 →
   错误假设 → 证据链 → 真正根因 → 留下的不变式。案例必须服务于本章的设计论点,
   不是花絮。
5. **无营销腔。** 禁止"强大的""优雅的""令人兴奋的"。冷静、精确、对设计有立场。
6. **教科书书面语体。** 全书向教科书语域收敛(对标第 1 条列出的三本):
   - 栏目名只用标准词:"本章导读""历史与教训""案例研究""小结""练习"。
     禁止自造栏目名(旧稿的"读者地图""战例""战争故事"均已废止)。
   - 禁止讲席口吻:"只记一句话""先记住一句话""别急""咱们""想象一下"。
     要点句用"本节要点:""核心结论是:"引出。
   - 正文可用"我们"表示作者与读者的共同推理(教科书惯例);不对读者用"你"。
     练习题用祈使句("设计一个实验""证明""测量"),不用"你会"。
   - 章名与节名用陈述性短语,不用悬念式、比喻式标题;比喻可在正文中使用,
     但必须紧跟精确定义。

## 章节结构(每章统一)

```
# 第 N 章 标题                          # Chapter N: Title
本章导读(为什么有这一章、如何读,150-300字)
N.1 问题与设计空间(为什么)
N.2 ... N.x 机制(怎么做,落在源码上)
N.y 历史与教训(investigations 案例研究,≥2 个)
N.z 小结
练习(3-5 题,从"读源码验证"到"设计权衡论证"分层)
```

- **图是一等解释手段。** 每章至少 2 幅 ASCII 图(对象布局、阶段流水线、状态机、
  数据流),放在 ``` 围栏内;图必须与正文互相引用,不做装饰。
- **机制必须配真实源码摘录。** 关键机制小节引用当前仓库的真实代码片段
  (5-15 行,``` 围栏 + 语言标注,首行以注释标明文件路径;禁行号)。
  每章至少 3 处;摘录前必须读过源码,与仓库当前状态逐字一致。
- 代码块标注语言。
- 中文版每章目标 6000–10000 字;英文版 4500–8000 词。宁可深而窄,不可浅而宽。
- 英文版不是逐句翻译,是同一作者用地道技术英语重写;两版内容必须一致。

## 术语表(中文版固定译法;括号内为首次出现时标注的英文)

| English | 中文 |
|---|---|
| lowering | 降低(lowering) |
| codegen | 代码生成 |
| write/read barrier | 写/读屏障 |
| remembered set | 记忆集 |
| safepoint | 安全点 |
| bootstrap | 自举 |
| fixed point | 不动点 |
| self-hosting | 自托管/自举(按语境) |
| value class / value model | 值类 / 值模型 |
| projection | 投影 |
| owned / borrowed reference | 拥有引用 / 借用引用 |
| refcount | 引用计数 |
| tricolor invariant | 三色不变式 |
| tagged small-int lane | 标记小整数通道 |
| fallback | 回退 |
| gate | 闸门(gate) |
| claim hygiene | 声明卫生 |
| frontend / backend | 前端 / 后端 |
| object header | 对象头 |
| finalizer | 终结器 |
| resurrection | 复活 |
| relocation | 重定位 |
| generational | 分代 |
| concurrent | 并发 |
| incremental | 增量 |
| mode-labeled | 模式标注的 |
| no-libpython | no-libpython(不译) |
| translation unit (TU) | 翻译单元(TU) |

代码标识符、CLI 旗标、环境变量(`PCC_GC_BACKEND`、`--python-libpython=off`)一律不译。

## 文件命名

- 中文:`books/cn/chNN-slug.md`;英文:`books/en/chNN-slug.md`(slug 用英文,两版一致)。
- 首行标题:中文 `# 第 N 章 标题`;英文 `# Chapter N: Title`。

## 写作纪律(对 agent)

- 写前必读:本文件、`books/PLAN.md` 中本章蓝图、蓝图列出的全部源文件与调查文档。
- 只读探索(Read/rg),不跑编译、不跑测试、不写仓库其他位置。
- 所有 Bash 必须带 timeout。
- 拿不准的事实:去读源码确认;确认不了就不写,不允许编造。
